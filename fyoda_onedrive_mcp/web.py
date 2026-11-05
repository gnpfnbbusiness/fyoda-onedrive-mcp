"""Hosted (HTTPS) MCP server for claude.ai custom connectors.

Claude <-> this server: standard MCP OAuth 2.1 (dynamic client registration + PKCE), implemented by the MCP SDK.
This server <-> Microsoft: ordinary OAuth code flow for a PERSONAL account using your own Entra app registration.

The server keeps NO database. Client ids, auth codes and tokens are sealed (Fernet, SERVER_SECRET) and carry the
Microsoft refresh token, so a restart does not log you out. Consequences: rotating SERVER_SECRET revokes every
connection at once; individual token revocation is not supported. Only ALLOWED_ACCOUNT can sign in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, OAuthToken,
                                      RefreshToken, TokenError)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from . import core
from .graph import GraphClient
from .tools import register

MS = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
OWN_SCOPE = "onedrive"
ACCESS_TTL, REFRESH_TTL, CODE_TTL, PENDING_TTL = 3600, 30 * 86400, 300, 600


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def readonly() -> bool:
    return _env("ONEDRIVE_READONLY") == "1"


def ms_scope() -> str:
    return "openid email profile offline_access " + ("Files.Read" if readonly() else "Files.ReadWrite")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Vault:
    def __init__(self, secret: str):
        if len(secret) < 24:
            raise RuntimeError("SERVER_SECRET must be a long random string (24+ characters).")
        self._key = hashlib.sha256(secret.encode()).digest()  # any long random string works
        self._f = Fernet(__import__("base64").urlsafe_b64encode(self._key))

    def seal(self, kind: str, data: dict, ttl: int) -> str:
        return self._f.encrypt(json.dumps({**data, "_k": kind, "_x": int(time.time()) + ttl}).encode()).decode()

    def open(self, kind: str, token: str) -> dict | None:
        try:
            d = json.loads(self._f.decrypt(token.encode()))
        except (InvalidToken, ValueError):
            return None
        return d if d.get("_k") == kind and d.get("_x", 0) > time.time() else None

    def client_secret(self, client_id: str) -> str:
        return hmac.new(self._key, b"cs:" + client_id.encode(), "sha256").hexdigest()


async def _ms_post(data: dict) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(MS + "/token", data=data)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}


def _claims(id_token: str) -> dict:
    try:  # received directly from Microsoft's token endpoint over TLS, so not re-verified here
        part = id_token.split(".")[1]
        return json.loads(__import__("base64").urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


class Provider:
    """Stateless OAuthAuthorizationServerProvider for a single allowed Microsoft account."""

    def __init__(self, vault: Vault, base_url: str, ms_client_id: str, ms_client_secret: str, allowed: str, ms_post=_ms_post):
        self.v, self.base, self.cid, self.secret, self.allowed, self.ms_post = vault, base_url.rstrip("/"), ms_client_id, ms_client_secret, allowed.casefold(), ms_post
        self._latest: dict[str, str] = {}
        self._cache: dict[str, tuple[str, float]] = {}
        self._used: dict[str, float] = {}

    @property
    def callback_url(self) -> str:
        return self.base + "/oauth/microsoft/callback"

    # --- dynamic client registration, no storage: the client id IS the sealed registration
    async def register_client(self, info: OAuthClientInformationFull) -> None:
        cid = self.v.seal("client", {"ru": [str(u) for u in info.redirect_uris], "n": info.client_name, "am": info.token_endpoint_auth_method,
                                     "gt": info.grant_types, "rt": info.response_types, "sc": info.scope}, 10 * 365 * 86400)
        info.client_id = cid
        info.client_secret = None if info.token_endpoint_auth_method == "none" else self.v.client_secret(cid)
        info.client_secret_expires_at = None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        d = self.v.open("client", client_id)
        if not d:
            return None
        return OAuthClientInformationFull(client_id=client_id, redirect_uris=[AnyUrl(u) for u in d["ru"]], client_name=d.get("n"),
                                          token_endpoint_auth_method=d["am"], grant_types=d["gt"], response_types=d["rt"], scope=d.get("sc"),
                                          client_secret=None if d["am"] == "none" else self.v.client_secret(client_id))

    # --- step 1: send the user to Microsoft
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.v.seal("pending", {"c": client.client_id, "ru": str(params.redirect_uri), "ex": params.redirect_uri_provided_explicitly,
                                        "cc": params.code_challenge, "s": params.state, "sc": params.scopes or [OWN_SCOPE]}, PENDING_TTL)
        q = {"client_id": self.cid, "response_type": "code", "redirect_uri": self.callback_url, "scope": ms_scope(),
             "state": state, "response_mode": "query", "prompt": "select_account"}
        return MS + "/authorize?" + urlencode(q)

    # --- step 2: Microsoft returns here
    async def callback(self, request: Request):
        p = self.v.open("pending", request.query_params.get("state", ""))
        if not p:
            return PlainTextResponse("Sign-in link expired or invalid. Start the connection again from Claude.", status_code=400)

        def back(**kw):
            sep = "&" if "?" in p["ru"] else "?"
            return RedirectResponse(p["ru"] + sep + urlencode({k: v for k, v in {**kw, "state": p["s"]}.items() if v}), status_code=302)

        if request.query_params.get("error") or not request.query_params.get("code"):
            return back(error="access_denied")
        status, body = await self.ms_post({"client_id": self.cid, "client_secret": self.secret, "grant_type": "authorization_code",
                                            "code": request.query_params["code"], "redirect_uri": self.callback_url, "scope": ms_scope()})
        if status != 200 or "refresh_token" not in body:
            return back(error="server_error")
        who = _claims(body.get("id_token", ""))
        names = {str(who.get(k, "")).casefold() for k in ("email", "preferred_username")}
        if self.allowed not in names:
            return back(error="access_denied")
        code = self.v.seal("code", {**p, "mr": body["refresh_token"], "sub": who.get("sub", "")}, CODE_TTL)
        return back(code=code)

    # --- step 3: Claude exchanges our code for our tokens
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        d = self.v.open("code", authorization_code)
        if not d or d["c"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, scopes=d["sc"], expires_at=time.time() + 60, client_id=d["c"],
                                 code_challenge=d["cc"], redirect_uri=AnyUrl(d["ru"]), redirect_uri_provided_explicitly=d["ex"], subject=d["sub"])

    def _mint(self, client_id: str, scopes: list[str], ms_refresh: str, sub: str) -> OAuthToken:
        base = {"c": client_id, "sc": scopes, "mr": ms_refresh, "sub": sub}
        return OAuthToken(access_token=self.v.seal("access", base, ACCESS_TTL), token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=self.v.seal("refresh", base, REFRESH_TTL))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, ac: AuthorizationCode) -> OAuthToken:
        now = time.time()
        self._used = {k: t for k, t in self._used.items() if t > now}
        if ac.code in self._used:  # one-time use (best effort within this process)
            raise TokenError(error="invalid_grant", error_description="code already used")
        self._used[ac.code] = now + CODE_TTL
        d = self.v.open("code", ac.code)
        return self._mint(client.client_id, ac.scopes, d["mr"], d["sub"])

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        d = self.v.open("refresh", refresh_token)
        if not d or d["c"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=d["c"], scopes=d["sc"], expires_at=int(time.time()) + REFRESH_TTL, subject=d["sub"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, rt: RefreshToken, scopes: list[str]) -> OAuthToken:
        d = self.v.open("refresh", rt.token)
        return self._mint(client.client_id, scopes or rt.scopes, self._resolve(d["mr"]), d["sub"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.v.open("access", token)
        if not d:
            return None
        return AccessToken(token=token, client_id=d["c"], scopes=d["sc"], expires_at=int(time.time()) + 60, subject=d["sub"])

    async def revoke_token(self, token) -> None:  # stateless: rotate SERVER_SECRET to revoke everything
        return None

    # --- Microsoft access tokens for Graph calls
    def _resolve(self, refresh: str) -> str:
        for _ in range(20):
            nxt = self._latest.get(_sha(refresh))
            if not nxt or nxt == refresh:
                break
            refresh = nxt
        return refresh

    async def ms_access(self, ms_refresh: str) -> str:
        cur = self._resolve(ms_refresh)
        hit = self._cache.get(_sha(cur))
        if hit and hit[1] > time.time() + 60:
            return hit[0]
        status, body = await self.ms_post({"client_id": self.cid, "client_secret": self.secret, "grant_type": "refresh_token",
                                            "refresh_token": cur, "scope": ms_scope()})
        if status != 200 or "access_token" not in body:
            raise PermissionError("Microsoft sign-in expired or was revoked. Reconnect the connector in Claude.")
        new = body.get("refresh_token", cur)
        self._latest[_sha(cur)] = new
        self._cache[_sha(new)] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return body["access_token"]


def build_app(provider: Provider | None = None, vault: Vault | None = None):
    base = _env("PUBLIC_BASE_URL").rstrip("/")
    if provider is None:
        missing = [k for k in ("PUBLIC_BASE_URL", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "SERVER_SECRET", "ALLOWED_ACCOUNT") if not _env(k)]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        vault = Vault(_env("SERVER_SECRET"))
        provider = Provider(vault, base, _env("MS_CLIENT_ID"), _env("MS_CLIENT_SECRET"), _env("ALLOWED_ACCOUNT"))
    mcp = FastMCP("onedrive-personal", auth_server_provider=provider, stateless_http=True, host="0.0.0.0",
                  auth=AuthSettings(issuer_url=AnyUrl(provider.base), resource_server_url=AnyUrl(provider.base + "/mcp"),
                                    required_scopes=[OWN_SCOPE], validate_token_resource=False,  # tokens are sealed by this server for this server only
                                    
                                    client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[OWN_SCOPE], default_scopes=[OWN_SCOPE])))

    @mcp.custom_route("/oauth/microsoft/callback", methods=["GET"])
    async def _cb(request: Request):
        return await provider.callback(request)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def _health(request: Request):
        return JSONResponse({"ok": True, "readonly": readonly()})

    async def runner(fn, *a, **k):
        tok = get_access_token()
        d = provider.v.open("access", tok.token) if tok else None
        if not d:
            raise PermissionError("Not authenticated.")
        core.request_token.set(await provider.ms_access(d["mr"]))
        return await anyio.to_thread.run_sync(lambda: fn(*a, **k))

    core._client = GraphClient(lambda: core.request_token.get() or "")
    register(mcp, runner=runner, readonly=readonly(), hosted=True)
    return mcp.streamable_http_app()


def app():  # uvicorn --factory fyoda_onedrive_mcp.web:app
    return build_app()
