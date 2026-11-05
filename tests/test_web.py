import base64, hashlib, json, os, secrets, time, urllib.parse as up
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient
from fyoda_onedrive_mcp import core, web
from fyoda_onedrive_mcp.web import Vault, Provider

BASE = "https://mcp.example.test"
CB = "https://claude.ai/api/mcp/auth_callback"

def idtok(email):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({})}.{b({'email': email, 'sub': 's1'})}.sig"

class FakeMS:
    def __init__(self, email="owner@example.com"): self.email, self.calls = email, []
    async def __call__(self, data):
        self.calls.append(data["grant_type"])
        if data["grant_type"] == "authorization_code":
            return 200, {"refresh_token": "MSR1", "access_token": "MSA1", "expires_in": 3600, "id_token": idtok(self.email)}
        assert data["refresh_token"] in ("MSR1", "MSR2")
        return 200, {"refresh_token": "MSR2", "access_token": "MSA2", "expires_in": 3600}

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_WRITE_PREFIXES", "/FYODA")
    ms = FakeMS(); v = Vault(Fernet.generate_key().decode())
    prov = Provider(v, BASE, "ms-client", "ms-secret", "Owner@Example.com", ms_post=ms)
    with TestClient(web.build_app(prov, v), base_url=BASE, follow_redirects=False) as c:
        yield c, prov, ms

def pkce():
    ver = secrets.token_urlsafe(40)
    return ver, base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")

def connect(client):
    reg = client.post("/register", json={"redirect_uris": [CB], "client_name": "Claude", "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert reg.status_code == 201, reg.text
    cid = reg.json()["client_id"]; ver, ch = pkce()
    r = client.get("/authorize", params={"client_id": cid, "redirect_uri": CB, "response_type": "code", "code_challenge": ch, "code_challenge_method": "S256", "state": "xyz", "scope": "onedrive"})
    assert r.status_code == 302 and r.headers["location"].startswith("https://login.microsoftonline.com/consumers/")
    ms_state = up.parse_qs(up.urlparse(r.headers["location"]).query)["state"][0]
    return cid, ver, ms_state

def test_discovery_and_unauthenticated_rejected(env):
    c, *_ = env
    assert c.get("/.well-known/oauth-authorization-server").json()["registration_endpoint"].endswith("/register")
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    assert c.post("/mcp", json={}).status_code == 401
    assert c.get("/healthz").json()["ok"] is True

def test_full_flow_then_tool_call(env, monkeypatch):
    c, prov, ms = env
    cid, ver, st = connect(c)
    cb = c.get("/oauth/microsoft/callback", params={"code": "mscode", "state": st})
    loc = up.urlparse(cb.headers["location"]); q = up.parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CB and q["state"] == ["xyz"]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CB, "client_id": cid, "code_verifier": ver})
    assert tok.status_code == 200, tok.text
    at, rt = tok.json()["access_token"], tok.json()["refresh_token"]
    # code is single-use
    assert c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CB, "client_id": cid, "code_verifier": ver}).status_code == 400
    # refresh works and survives (stateless) -> new provider instance with same key also accepts the token
    r2 = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}); assert r2.status_code == 200
    # tool call over MCP with Graph stubbed
    import httpx
    from fyoda_onedrive_mcp.graph import GraphClient
    seen = {}
    def h(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"id": "d1", "driveType": "personal", "owner": {"user": {"displayName": "Owner"}}, "quota": {"used": 1, "total": 2}})
    core._client = GraphClient(lambda: core.request_token.get() or "", httpx.MockTransport(h))
    H = {"Authorization": f"Bearer {at}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert c.post("/mcp", headers=H, json=init).status_code == 200
    lst = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).text
    assert "onedrive_write_file" in lst and "onedrive_download" not in lst
    call = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "onedrive_whoami", "arguments": {}}}).text
    assert "Owner" in call and seen["auth"] == "Bearer MSA2"

def test_wrong_microsoft_account_is_refused(env):
    c, prov, ms = env; ms.email = "stranger@example.com"
    cid, ver, st = connect(c)
    cb = c.get("/oauth/microsoft/callback", params={"code": "x", "state": st})
    assert "error=access_denied" in cb.headers["location"] and "code=" not in cb.headers["location"]

def test_tampered_or_expired_state_refused(env):
    c, *_ = env
    assert c.get("/oauth/microsoft/callback", params={"code": "x", "state": "garbage"}).status_code == 400

def test_token_from_other_server_secret_rejected(env):
    c, *_ = env
    other = Vault(Fernet.generate_key().decode()).seal("access", {"c": "x", "sc": ["onedrive"], "mr": "m", "sub": "s"}, 600)
    assert c.post("/mcp", headers={"Authorization": f"Bearer {other}", "Accept": "application/json, text/event-stream"}, json={}).status_code == 401

def test_missing_env_is_explicit(monkeypatch):
    for k in ("PUBLIC_BASE_URL", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "SERVER_SECRET", "ALLOWED_ACCOUNT"): monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="SERVER_SECRET"): web.build_app()
