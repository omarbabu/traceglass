import http.client
import json
import threading
import time
import urllib.parse

import pytest

from traceglass import server as srv


@pytest.fixture
def live(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html><head></head><body>INDEX</body></html>")
    (ui / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    monkeypatch.setattr(srv, "UI_DIR", ui)
    httpd, url = srv.start_background(db=str(tmp_path / "db.sqlite"))
    port = httpd.server_address[1]
    yield port, httpd
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw), resp
    except ValueError:
        return resp.status, raw.decode(errors="replace"), resp


RECORDS = [
    {"type": "run", "id": "run_a", "name": "Alpha run", "project": "p", "status": "ok", "started_at": 1, "ended_at": 9},
    {"type": "span", "id": "sp_1", "run_id": "run_a", "name": "lookup", "kind": "tool", "status": "ok", "started_at": 2, "ended_at": 3},
    {"type": "artifact", "id": "art_1", "run_id": "run_a", "name": "Answer", "kind": "answer", "content": "42", "created_at": 4},
]


def test_api_roundtrip(live):
    port, _ = live
    status, body, _ = req(port, "GET", "/api/health")
    assert status == 200 and body["ok"] is True
    status, body, _ = req(port, "POST", "/api/ingest", {"records": RECORDS})
    assert status == 200 and body["accepted"] == 3
    status, body, _ = req(port, "GET", "/api/runs")
    assert [r["id"] for r in body["runs"]] == ["run_a"] and body["runs"][0]["summary"]["spans"] == 1
    status, body, _ = req(port, "GET", "/api/runs/run_a")
    assert body["run"]["name"] == "Alpha run" and body["spans"][0]["id"] == "sp_1" and body["artifacts"][0]["content"] == "42"
    status, body, _ = req(port, "GET", "/api/runs/nope")
    assert status == 404
    status, body, _ = req(port, "GET", "/api/search?q=lookup")
    assert body["hits"][0]["id"] == "sp_1"
    status, body, _ = req(port, "GET", "/api/nothing")
    assert status == 404
    status, body, _ = req(port, "POST", "/api/ingest", [RECORDS[1]])  # bare list accepted too
    assert status == 200 and body["accepted"] == 1
    status, body, _ = req(port, "POST", "/api/ingest", {"records": "x"})
    assert status == 400
    status, body, _ = req(port, "POST", "/api/ingest", b"{not json")
    assert status == 400
    status, body, _ = req(port, "DELETE", "/api/runs/run_a", headers={"Content-Type": "application/json"})
    assert status == 200 and body["deleted"] is True
    status, body, _ = req(port, "GET", "/api/runs")
    assert body["runs"] == []


def test_otlp_endpoint(live):
    port, _ = live
    payload = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [
        {"traceId": "ab", "spanId": "cd", "name": "root", "startTimeUnixNano": "1000000", "endTimeUnixNano": "2000000"}]}]}]}
    status, body, _ = req(port, "POST", "/v1/traces", payload)
    assert status == 200 and body["accepted"] == 2
    status, body, _ = req(port, "GET", "/api/runs/otel_ab")
    assert body["spans"][0]["name"] == "root"


def test_sse_stream_delivers_changes(live):
    port, _ = live
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    assert resp.fp.readline().startswith(b"event: hello")
    threading.Timer(0.2, lambda: req(port, "POST", "/api/ingest", {"records": RECORDS[:2]})).start()
    deadline = time.time() + 5
    got = None
    while time.time() < deadline:
        line = resp.fp.readline()
        if line.startswith(b"data: [") :
            got = json.loads(line[6:])
            break
    conn.close()
    assert got and {c["type"] for c in got} >= {"run"} and got[0]["record"]["id"] in ("run_a", "sp_1")


def test_static_ui_and_headers(live):
    port, _ = live
    status, body, resp = req(port, "GET", "/")
    assert status == 200 and "INDEX" in body
    csp = resp.getheader("Content-Security-Policy")
    assert "default-src 'self'" in csp and resp.getheader("X-Frame-Options") == "DENY"
    status, body, resp = req(port, "GET", "/assets/app.js")
    assert status == 200 and "console.log" in body and "immutable" in resp.getheader("Cache-Control")
    status, body, _ = req(port, "GET", "/run/abc/deep/link")  # SPA fallback
    assert "INDEX" in body
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_export_endpoint_404(live):
    port, _ = live
    status, _, _ = req(port, "GET", "/api/runs/missing/export.html")
    assert status == 404


# -- security -------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["evil.example", "192.168.1.10:7357", "attacker.com:80", "127.0.0.1.evil.com"])
def test_rejects_non_loopback_host_header(live, host):
    port, _ = live
    status, body, _ = req(port, "GET", "/api/runs", headers={"Host": host})
    assert status == 403


@pytest.mark.parametrize("host", ["localhost:1234", "127.0.0.1", "[::1]:99"])
def test_accepts_loopback_host_header(live, host):
    port, _ = live
    status, _, _ = req(port, "GET", "/api/health", headers={"Host": host})
    assert status == 200


def test_rejects_non_json_mutations(live):
    port, _ = live
    status, _, _ = req(port, "POST", "/api/ingest", b'{"records": []}', headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = req(port, "POST", "/api/ingest", b'records=1', headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415
    status, _, _ = req(port, "DELETE", "/api/runs/x")
    assert status == 415


def test_rejects_cross_origin_mutations(live):
    port, _ = live
    for origin in ("https://evil.example", "null", f"http://localhost:{port + 1}"):
        status, body, _ = req(port, "POST", "/api/ingest", {"records": RECORDS}, headers={"Origin": origin})
        assert status == 403, origin
    status, _, _ = req(port, "DELETE", "/api/runs/run_a", headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403
    # same-origin browser requests are fine
    status, body, _ = req(port, "POST", "/api/ingest", {"records": RECORDS}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200


@pytest.mark.parametrize("path", ["/../secret.txt", "/../../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt", "/..%5csecret.txt"])
def test_path_traversal_does_not_escape(live, path):
    port, _ = live
    status, body, _ = req(port, "GET", path)
    assert "TOP SECRET" not in str(body)


def test_serve_refuses_non_loopback(tmp_path):
    for host in ("0.0.0.0", "::", "192.168.0.5", "example.com"):
        with pytest.raises(SystemExit):
            srv.serve(host=host, port=0, db=str(tmp_path / "x.db"), quiet=True)
    with pytest.raises(ValueError):
        srv.start_background(host="0.0.0.0")


def test_is_loopback_host():
    assert srv.is_loopback_host("127.0.0.1") and srv.is_loopback_host("localhost") and srv.is_loopback_host("[::1]") and srv.is_loopback_host("127.5.5.5")
    assert not srv.is_loopback_host("0.0.0.0") and not srv.is_loopback_host("10.0.0.1") and not srv.is_loopback_host("localhost.evil.com")
