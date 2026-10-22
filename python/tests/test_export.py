import json
import re

import pytest

from traceglass import export
from traceglass.store import Store


@pytest.fixture
def fake_ui(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text(
        '<!doctype html><html><head><title>Traceglass</title>'
        '<script type="module" crossorigin src="./assets/index-abc.js"></script>'
        '<link rel="modulepreload" crossorigin href="./assets/vendor.js">'
        '<link rel="stylesheet" crossorigin href="./assets/index-abc.css">'
        "</head><body><div id=root></div></body></html>"
    )
    (ui / "assets" / "index-abc.js").write_text('const s = "</script>"; console.log(s);')
    (ui / "assets" / "index-abc.css").write_text("body{color:red}")
    monkeypatch.setattr(export, "UI_DIR", ui)
    return ui


def bundle(content="hello"):
    return {
        "schema": "traceglass/v1",
        "run": {"id": "run_x", "name": "X", "input": "big", "summary": {"spans": 1}},
        "spans": [{"id": "s", "run_id": "run_x", "name": "a"}],
        "events": [],
        "artifacts": [{"id": "art", "run_id": "run_x", "content": content}],
        "links": [],
    }


def test_export_html_inlines_assets_and_data(fake_ui):
    html = export.export_html([bundle("evil </script><script>alert(1)</script>")], title="My <run>")
    assert 'src="./assets' not in html and "stylesheet" not in html and "modulepreload" not in html
    assert "<style>body{color:red}</style>" in html
    assert '<script type="module">const s = "<\\/script>"' in html
    assert "<title>My &lt;run&gt;</title>" in html
    m = re.search(r'<script id="traceglass-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    data_text = m.group(1)
    assert "</script" not in data_text and "<" not in data_text
    data = json.loads(data_text)
    assert data["mode"] == "embedded" and data["runs"][0]["id"] == "run_x" and "input" not in data["runs"][0]
    assert data["runs"][0]["summary"]["artifacts"] == 1
    assert data["bundles"]["run_x"]["artifacts"][0]["content"].startswith("evil </script>")


def test_export_requires_built_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "UI_DIR", tmp_path / "nothing")
    with pytest.raises(RuntimeError):
        export.export_html([bundle()])


def test_export_site(fake_ui, tmp_path):
    st = Store(tmp_path / "db.sqlite")
    st.write([{"type": "run", "id": "run_1", "name": "One", "started_at": 1},
              {"type": "span", "id": "sp", "run_id": "run_1", "name": "s", "status": "ok", "started_at": 1, "ended_at": 2}])
    out = tmp_path / "site"
    assert export.export_site(st, out) == 1
    assert (out / "assets" / "index-abc.js").exists() and (out / ".nojekyll").exists()
    assert json.loads((out / "data" / "index.json").read_text())["runs"][0]["id"] == "run_1"
    assert json.loads((out / "data" / "runs" / "run_1.json").read_text())["spans"][0]["id"] == "sp"
    assert '<meta name="traceglass-static" content="data/">' in (out / "index.html").read_text()
