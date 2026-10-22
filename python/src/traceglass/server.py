"""The local Traceglass server: REST API, live stream (SSE), ingestion, and the UI.

Security posture (local-first by design):

* binds to loopback only — remote access is done with an SSH tunnel, never a wider bind;
* rejects requests whose ``Host`` header is not a loopback name (defeats DNS rebinding);
* state-changing requests must be ``application/json`` and, when a browser sends an
  ``Origin``, same-origin — so arbitrary web pages cannot write to or delete your traces;
* no CORS headers are ever emitted, and HTML is served with a strict CSP.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import queue
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._version import __version__
from .store import Store

UI_DIR = Path(__file__).parent / "_ui"
DEFAULT_PORT = 7357
MAX_BODY = 64 * 1024 * 1024
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def is_loopback_host(host: str) -> bool:
    h = host.strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class Broadcaster:
    """Tails the store's change log and fans changes out to SSE subscribers."""

    def __init__(self, store: Store, interval: float = 0.2) -> None:
        self.store = store
        self.interval = interval
        self._subs: List["queue.Queue[Optional[Dict[str, Any]]]"] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.seq = store.latest_seq()
        self._thread = threading.Thread(target=self._loop, name="traceglass-broadcast", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for q in self._subs:
                q.put(None)

    def subscribe(self) -> "queue.Queue[Optional[Dict[str, Any]]]":
        q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=10_000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _loop(self) -> None:
        last_prune = time.time()
        while not self._stop.is_set():
            changes: List[Dict[str, Any]] = []
            try:
                changes = self.store.changes_since(self.seq)
                if changes:
                    self.seq = changes[-1]["seq"]
                    with self._lock:
                        subs = list(self._subs)
                    for ch in changes:
                        for q in subs:
                            try:
                                q.put_nowait(ch)
                            except queue.Full:
                                pass
                if time.time() - last_prune > 600:
                    self.store.prune_changes()
                    last_prune = time.time()
            except Exception:
                pass
            if not changes:
                self._stop.wait(self.interval)


class TraceglassServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], store: Store) -> None:
        if ":" in addr[0]:
            import socket

            self.address_family = socket.AF_INET6
        self.store = store
        self.broadcaster = Broadcaster(store)
        super().__init__(addr, Handler)
        self.broadcaster.start()

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), which can block for many seconds
        # on a reverse-DNS lookup. We only ever bind loopback, so skip it.
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def server_close(self) -> None:
        self.broadcaster.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: TraceglassServer
    protocol_version = "HTTP/1.1"
    server_version = f"traceglass/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    # -- plumbing ---------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return is_loopback_host(name)

    def _mutation_ok(self) -> Optional[str]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return "requests that change state must be application/json"
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            parsed = urllib.parse.urlparse(origin)
            if parsed.netloc != self.headers.get("Host"):
                return "cross-origin requests are not allowed"
        elif origin == "null":
            return "cross-origin requests are not allowed"
        return None

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8") or "null")

    def _guard(self) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Host header must be a loopback address")
            return False
        return True

    # -- verbs ------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        q1 = lambda k: (qs.get(k) or [None])[0]  # noqa: E731
        store = self.server.store
        try:
            if path == "/api/health":
                return self._json({"ok": True, "version": __version__, "db": str(store.path), "seq": self.server.broadcaster.seq})
            if path == "/api/runs":
                limit = min(int(q1("limit") or 500), 5000)
                return self._json({"runs": store.list_runs(project=q1("project"), q=q1("q"), status=q1("status"), limit=limit)})
            if path.startswith("/api/runs/"):
                rest = path[len("/api/runs/"):]
                if rest.endswith("/export.html"):
                    run_id = urllib.parse.unquote(rest[: -len("/export.html")])
                    bundle = store.get_bundle(run_id)
                    if not bundle:
                        return self._error(404, "run not found")
                    from .export import export_html

                    html = export_html([bundle]).encode()
                    fname = f"traceglass-{run_id}.html"
                    return self._send(200, html, "text/html; charset=utf-8", {"Content-Disposition": f'attachment; filename="{fname}"'})
                bundle = store.get_bundle(urllib.parse.unquote(rest))
                return self._json(bundle) if bundle else self._error(404, "run not found")
            if path == "/api/status":
                return self._json(agent_status(store))
            if path == "/api/search":
                return self._json({"hits": store.search(q1("q") or "", limit=min(int(q1("limit") or 50), 500))})
            if path == "/api/stream":
                return self._stream()
            if path.startswith("/api/"):
                return self._error(404, "no such endpoint")
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        if not self._guard():
            return
        problem = self._mutation_ok()
        if problem:
            return self._error(HTTPStatus.FORBIDDEN if "origin" in problem else HTTPStatus.UNSUPPORTED_MEDIA_TYPE, problem)
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(400, f"invalid JSON body: {exc}")
        try:
            if path == "/api/ingest":
                records = body.get("records") if isinstance(body, dict) else body
                if not isinstance(records, list):
                    return self._error(400, 'expected {"records": [...]}')
                n = self.server.store.write(records)
                return self._json({"accepted": n})
            if path == "/v1/traces":
                from .otlp import otlp_to_records

                records = otlp_to_records(body)
                n = self.server.store.write(records)
                return self._json({"partialSuccess": {}, "accepted": n})
            return self._error(404, "no such endpoint")
        except Exception as exc:
            self._error(400, f"{type(exc).__name__}: {exc}")

    def do_DELETE(self) -> None:
        if not self._guard():
            return
        problem = self._mutation_ok()
        if problem:
            return self._error(HTTPStatus.FORBIDDEN if "origin" in problem else HTTPStatus.UNSUPPORTED_MEDIA_TYPE, problem)
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/runs/"):
            ok = self.server.store.delete_run(urllib.parse.unquote(path[len("/api/runs/"):]))
            return self._json({"deleted": ok}, 200 if ok else 404)
        self._error(404, "no such endpoint")

    # -- SSE --------------------------------------------------------------------------
    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.server.broadcaster.subscribe()
        try:
            self.wfile.write(f"event: hello\ndata: {json.dumps({'seq': self.server.broadcaster.seq})}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    return
                batch = [item]
                while len(batch) < 500:
                    try:
                        nxt = q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        break
                    batch.append(nxt)
                payload = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"event: changes\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.broadcaster.unsubscribe(q)
            self.close_connection = True

    # -- static UI --------------------------------------------------------------------
    def _static(self, path: str) -> None:
        root = UI_DIR.resolve()
        if not (root / "index.html").exists():
            return self._send(200, _NOT_BUILT.encode(), "text/html; charset=utf-8")
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            target = root / "index.html"  # SPA fallback (and path-traversal guard)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if "/assets/" in target.as_posix() else "no-cache"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": cache})


def agent_status(store: Store) -> Dict[str, Any]:
    """What the "Connect your agents" panel needs: watcher, hooks and session sources. Never raises."""

    def safe(fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    from .integrations import sessions

    def watcher() -> Dict[str, Any]:
        from . import watch

        beat = safe(lambda: json.loads(sessions.heartbeat_path().read_text()), {}) or {}
        active = watch.ACTIVE
        in_process = bool(active and active.thread and active.thread.is_alive())
        return {
            "alive": sessions.watcher_alive() or in_process,
            "pid": beat.get("pid"),
            "last_beat": beat["ts"] * 1000 if isinstance(beat.get("ts"), (int, float)) else None,
            "in_process": in_process,
        }

    def hook_info() -> Dict[str, Any]:
        from . import hooks

        last = safe(hooks.last_events, {}) or {}
        claude = [{"scope": sc, "path": str(p)} for sc, p, found in (safe(hooks.claude_status, []) or []) if found]
        kiro = [{"agent": p.stem, "path": str(p)} for p, found in (safe(hooks.kiro_status, []) or []) if found]
        ts = lambda src: (last.get(src) or {}).get("ts")  # noqa: E731
        return {
            "claude-code": {"installed": claude, "last_event": ts("claude-code")},
            "kiro": {"installed": kiro, "last_event": ts("kiro")},
        }

    def runs_by_source() -> Dict[str, int]:
        rows = store._conn().execute(
            "SELECT json_extract(data, '$.source') AS src, COUNT(*) AS n FROM runs GROUP BY src"
        ).fetchall()
        return {r["src"]: r["n"] for r in rows if r["src"]}

    counts = safe(runs_by_source, {}) or {}

    def source(name: str, d: Any) -> Dict[str, Any]:
        path = safe(d)
        return {"available": bool(path and path.is_dir()), "dir": str(path) if path else None, "runs": counts.get(name, 0)}

    return {
        "version": __version__,
        "db": str(store.path),
        "watcher": safe(watcher, {"alive": False, "pid": None, "last_beat": None, "in_process": False}),
        "hooks": safe(hook_info, {"claude-code": {"installed": [], "last_event": None}, "kiro": {"installed": [], "last_event": None}}),
        "sources": {
            "claude-code": source("claude-code", sessions.claude_projects_dir),
            "kiro": source("kiro", sessions.kiro_sessions_dir),
        },
    }


_NOT_BUILT = """<!doctype html><meta charset=utf-8><title>Traceglass</title>
<body style="background:#111214;color:#d6d7dc;font:15px system-ui;padding:48px">
<h2>Traceglass API is running — the UI bundle is not built.</h2>
<p>Build it once with <code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code>, then reload.</p>
<p>API: <a style="color:#8b93ff" href="/api/runs">/api/runs</a></p></body>"""


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    db: Optional[str] = None,
    open_browser: bool = False,
    quiet: bool = False,
) -> None:
    if not is_loopback_host(host):
        raise SystemExit(
            f"traceglass: refusing to bind to {host!r}. The server only listens on loopback because "
            "traces can contain prompts, documents and tool payloads.\n"
            f"To view it from another machine, use an SSH tunnel: ssh -L {port}:127.0.0.1:{port} <this-host>"
        )
    store = Store(db)
    httpd = TraceglassServer((host, port), store)
    url = f"http://{'[' + host + ']' if ':' in host else host}:{httpd.server_address[1]}"
    if not quiet:
        print(f"\n  ◆ Traceglass {__version__}\n\n  ➜  {url}\n  ➜  db: {store.path}\n\n  Ctrl-C to stop.\n", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def start_background(host: str = "127.0.0.1", port: int = 0, db: Optional[str] = None) -> Tuple[TraceglassServer, str]:
    """Start a server in a daemon thread (tests, notebooks). Returns (server, base_url)."""
    if not is_loopback_host(host):
        raise ValueError("traceglass only binds to loopback addresses")
    httpd = TraceglassServer((host, port), Store(db))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"
