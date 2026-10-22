"""Shareable, server-free exports.

* ``export_html(bundles)`` — one self-contained HTML file (UI + data inlined). Attach it to a
  pull request or an incident ticket; it opens from disk with no server.
* ``export_site(store, out_dir)`` — a static site (UI + JSON files) for GitHub Pages or any
  static host.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .server import UI_DIR
from .store import Store


def _ui_index() -> str:
    index = UI_DIR / "index.html"
    if not index.exists():
        raise RuntimeError("UI bundle not built: run `npm install && npm run build` in ui/ first")
    return index.read_text(encoding="utf-8")


def _json_for_script(obj: Any) -> str:
    # Inside <script type="application/json"> the only dangerous sequence is "</": escape "<".
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _summary(bundle: Dict[str, Any]) -> Dict[str, Any]:
    run = dict(bundle["run"])
    run.pop("input", None)
    run.pop("output", None)
    summary = dict(run.get("summary") or {})
    summary["artifacts"] = len(bundle.get("artifacts") or [])
    run["summary"] = summary
    return run


def export_html(bundles: List[Dict[str, Any]], title: Optional[str] = None) -> str:
    html = _ui_index()

    def inline_script(m: "re.Match[str]") -> str:
        js = (UI_DIR / m.group(1).lstrip("./")).read_text(encoding="utf-8")
        return "<script type=\"module\">" + js.replace("</script", "<\\/script") + "</script>"

    def inline_style(m: "re.Match[str]") -> str:
        css = (UI_DIR / m.group(1).lstrip("./")).read_text(encoding="utf-8")
        return "<style>" + css.replace("</style", "<\\/style") + "</style>"

    html = re.sub(r'<script type="module"[^>]*src="([^"]+)"[^>]*></script>', inline_script, html)
    html = re.sub(r'<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>', inline_style, html)
    html = re.sub(r'<link rel="modulepreload"[^>]*>', "", html)
    data = {"mode": "embedded", "runs": [_summary(b) for b in bundles], "bundles": {b["run"]["id"]: b for b in bundles}}
    payload = f'<script id="traceglass-data" type="application/json">{_json_for_script(data)}</script>'
    html = html.replace("</head>", payload + "</head>", 1)
    if title:
        html = re.sub(r"<title>.*?</title>", f"<title>{_esc(title)}</title>", html, count=1)
    return html


def export_site(store: Store, out_dir: Path, run_ids: Optional[Iterable[str]] = None) -> int:
    out_dir = Path(out_dir)
    if not (UI_DIR / "index.html").exists():
        raise RuntimeError("UI bundle not built: run `npm install && npm run build` in ui/ first")
    if out_dir.exists():
        shutil.rmtree(out_dir / "assets", ignore_errors=True)
    shutil.copytree(UI_DIR, out_dir, dirs_exist_ok=True)
    ids = list(run_ids) if run_ids else [r["id"] for r in store.list_runs(limit=5000)]
    (out_dir / "data" / "runs").mkdir(parents=True, exist_ok=True)
    summaries = []
    for rid in ids:
        b = store.get_bundle(rid)
        if not b:
            continue
        (out_dir / "data" / "runs" / f"{rid}.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        summaries.append(_summary(b))
    (out_dir / "data" / "index.json").write_text(json.dumps({"runs": summaries}, ensure_ascii=False), encoding="utf-8")
    index = (out_dir / "index.html").read_text(encoding="utf-8")
    index = index.replace("</head>", '<meta name="traceglass-static" content="data/"></head>', 1)
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return len(summaries)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
