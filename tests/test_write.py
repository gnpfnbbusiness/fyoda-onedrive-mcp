import httpx, pytest
from fyoda_onedrive_mcp import core
from fyoda_onedrive_mcp.graph import GraphClient

FILE = {"id": "F1", "name": "a.md", "size": 1, "file": {}, "eTag": "E1", "parentReference": {"driveId": "D"}}
log = []

def make(existing):
    def h(req):
        log.append((req.method, req.url.raw_path.decode(), dict(req.headers)))
        if req.method == "GET": return httpx.Response(200, json=FILE) if existing else httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "x"}})
        if req.method == "PUT": return httpx.Response(201, json=dict(FILE, eTag="E2"))
        return httpx.Response(201, json={"id": "N", "name": "sub", "folder": {}})
    return GraphClient(lambda: "t", httpx.MockTransport(h))

@pytest.fixture(autouse=True)
def pre(monkeypatch): log.clear(); monkeypatch.setenv("ONEDRIVE_WRITE_PREFIXES", "/FYODA, /Notes/Claude")

def test_writes_disabled_without_prefixes(monkeypatch):
    monkeypatch.delenv("ONEDRIVE_WRITE_PREFIXES"); core._client = make(False)
    with pytest.raises(PermissionError): core.write_file("/FYODA/a.md", "x")

@pytest.mark.parametrize("p", ["/Other/a.md", "/FYODAX/a.md", "/FYODA/../Other/a.md", "FYODA/a.md", "/"])
def test_paths_outside_allowlist_or_traversal_refused(p):
    core._client = make(False)
    with pytest.raises((PermissionError, ValueError)): core.write_file(p, "x")
    assert not [m for m, *_ in log if m == "PUT"]

def test_create_new_uses_fail_on_conflict():
    core._client = make(False); r = core.write_file("/fyoda/Sub/a.md", "hello")
    put = [x for x in log if x[0] == "PUT"][0]
    assert "conflictBehavior=fail" in put[1] and r["sha256"].startswith("2cf24d")

def test_replace_requires_overwrite_and_matching_etag():
    core._client = make(True)
    with pytest.raises(ValueError, match="overwrite=true"): core.write_file("/FYODA/a.md", "x")
    with pytest.raises(ValueError, match="overwrite=true"): core.write_file("/FYODA/a.md", "x", overwrite=True)
    with pytest.raises(ValueError, match="changed"): core.write_file("/FYODA/a.md", "x", overwrite=True, if_match="OLD")
    assert not [m for m, *_ in log if m == "PUT"]
    core.write_file("/FYODA/a.md", "x", overwrite=True, if_match="E1")
    assert [x for x in log if x[0] == "PUT"][0][2]["if-match"] == "E1"

def test_size_limit_and_base64():
    core._client = make(False)
    with pytest.raises(ValueError): core.write_file("/FYODA/b.bin", "A" * (5 * 1024 * 1024))
    assert core.write_file("/FYODA/b.bin", "aGk=", encoding="base64")["sha256"].startswith("8f434346")

def test_create_folder():
    core._client = make(False); core.create_folder("/FYODA/new"); assert log[-1][0] == "POST"
