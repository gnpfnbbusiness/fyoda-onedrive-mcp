import io, json, pathlib
import httpx, openpyxl, pytest
from fyoda_onedrive_mcp import core
from fyoda_onedrive_mcp.graph import GraphClient, GraphError

def xlsx_bytes():
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Gates"; ws.append(["G01", "Passed"]); ws.append(["G02", None])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

ITEM = {"id": "ABC!1", "name": "FYODA Program Control.xlsx", "size": 10, "file": {"mimeType": "x"},
        "eTag": "e1", "lastModifiedDateTime": "2026-10-17T00:00:00Z", "parentReference": {"driveId": "D1", "path": "/drive/root:"}}
seen = []

def handler(req: httpx.Request) -> httpx.Response:
    seen.append(str(req.url)); assert req.headers["authorization"] == "Bearer tok"
    p = req.url.raw_path.decode()
    if "/content" in p: return httpx.Response(200, content=xlsx_bytes())
    if "search(q=" in p: return httpx.Response(200, json={"value": [ITEM]})
    if p.endswith("/children") or "/children?" in p: return httpx.Response(200, json={"value": [ITEM]})
    if "missing" in p: return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "nope"}})
    if "/drive" in p: return httpx.Response(200, json=ITEM)
    return httpx.Response(500)

@pytest.fixture(autouse=True)
def fake(monkeypatch):
    seen.clear(); monkeypatch.setattr(core, "_client", GraphClient(lambda: "tok", httpx.MockTransport(handler)))

def test_share_encoding():
    assert core.encode_share("https://1drv.ms/x/s!abc") == "u!aHR0cHM6Ly8xZHJ2Lm1zL3gvcyFhYmM"

def test_search_escapes_quotes():
    core.search("o'brien"); assert "o%27%27brien" in seen[0] or "o''brien" in seen[0]

def test_list_and_meta_paths():
    assert core.list_folder("/")[0]["name"].startswith("FYODA")
    core.metadata("/Docs/My File.xlsx"); assert "root:/Docs/My%20File.xlsx" in seen[-1]
    core.metadata("https://1drv.ms/x/s!abc"); assert "/shares/u!" in seen[-1]

def test_read_xlsx_text():
    r = core.read_text("ABC!1"); assert "## Sheet: Gates" in r["content"] and "G01 | Passed" in r["content"]
    assert "untrusted" in r["note"]

def test_read_paging():
    r = core.read_text("ABC!1", offset=0, max_chars=10); assert r["truncated"] and r["returnedChars"] == 10

def test_download_never_overwrites_and_hashes(tmp_path):
    a = core.download("ABC!1", str(tmp_path)); b = core.download("ABC!1", str(tmp_path))
    assert a["savedTo"] != b["savedTo"] and len(a["sha256"]) == 64 and pathlib.Path(a["savedTo"]).exists()

def test_download_name_sanitised(tmp_path, monkeypatch):
    bad = dict(ITEM, name="../../etc/passwd")
    monkeypatch.setattr(core, "metadata", lambda ref: core._norm(bad))
    assert pathlib.Path(core.download("x", str(tmp_path))["savedTo"]).parent == tmp_path

def test_graph_error_and_size_limit(monkeypatch):
    with pytest.raises(GraphError) as e: core.metadata("/missing")
    assert e.value.status == 404 and e.value.code == "itemNotFound"
    monkeypatch.setattr(core, "MAX_READ_BYTES", 5)
    with pytest.raises(GraphError) as e: core.read_text("ABC!1")
    assert e.value.status == 413

def test_unsupported_type_refused(monkeypatch):
    monkeypatch.setattr(core, "_content", lambda r, m: ({"name": "x.zip", "eTag": "", "modified": ""}, b""))
    with pytest.raises(ValueError): core.read_text("x")
