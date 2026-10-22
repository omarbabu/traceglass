"""Local-first SQLite event store.

Records are upserted by id into per-type tables, and every write appends to a ``changes``
log. The server tails that log, which is what makes live view work across processes with
zero configuration: an agent writes to the database, and any open UI sees it within a
fraction of a second — no network hop, no daemon the agent has to know about.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project TEXT, session TEXT, name TEXT, status TEXT,
  started_at REAL, ended_at REAL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project, session);
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_id TEXT, kind TEXT, name TEXT, status TEXT,
  started_at REAL, ended_at REAL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, span_id TEXT, ts REAL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, span_id TEXT, kind TEXT, name TEXT, created_at REAL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id);
CREATE TABLE IF NOT EXISTS links (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_run ON links(run_id);
CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, type TEXT, id TEXT, ts REAL
);
"""

_TABLES = {"run": "runs", "span": "spans", "event": "events", "artifact": "artifacts", "link": "links"}
_COLUMNS = {
    "runs": ("project", "session", "name", "status", "started_at", "ended_at"),
    "spans": ("run_id", "parent_id", "kind", "name", "status", "started_at", "ended_at"),
    "events": ("run_id", "span_id", "ts"),
    "artifacts": ("run_id", "span_id", "kind", "name", "created_at"),
    "links": ("run_id",),
}


def default_db_path() -> Path:
    env = os.environ.get("TRACEGLASS_DB")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("TRACEGLASS_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".traceglass"
    return base / "traceglass.db"


def _merge(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(existing)
    for k, v in incoming.items():
        if v is None and k in out:
            continue
        if k in ("attributes", "metadata") and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    if incoming.get("type") == "run":
        # Runs may be assembled from many partial batches (e.g. OTLP exports): widen the window.
        for k, pick in (("started_at", min), ("ended_at", max)):
            a, b = existing.get(k), incoming.get(k)
            if a is not None and b is not None:
                out[k] = pick(a, b)
    return out


class Store:
    """Thread-safe SQLite store. One connection per thread; WAL mode for concurrent readers."""

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_DDL)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- connections -----------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            if str(self.path) != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- writes ----------------------------------------------------------------------
    def write(self, records: Iterable[Dict[str, Any]]) -> int:
        records = [r for r in records if isinstance(r, dict) and r.get("type") in _TABLES and r.get("id")]
        if not records:
            return 0
        conn = self._conn()
        now = time.time()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for rec in records:
                    table = _TABLES[rec["type"]]
                    row = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (rec["id"],)).fetchone()
                    data = _merge(json.loads(row["data"]), rec) if row else rec
                    cols = _COLUMNS[table]
                    values = [data.get(c) for c in cols]
                    conn.execute(
                        f"INSERT INTO {table}(id, {', '.join(cols)}, data) VALUES (?, {', '.join('?' for _ in cols)}, ?) "
                        f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, data=excluded.data",
                        [rec["id"], *values, json.dumps(data, separators=(",", ":"), ensure_ascii=False)],
                    )
                    run_id = data["id"] if rec["type"] == "run" else data.get("run_id")
                    conn.execute(
                        "INSERT INTO changes(run_id, type, id, ts) VALUES (?, ?, ?, ?)",
                        (run_id, rec["type"], rec["id"], now),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return len(records)

    def sync_run(self, run_id: str, records: Iterable[Dict[str, Any]]) -> Tuple[int, int]:
        """Make the stored run match ``records`` exactly, touching only what changed.

        Used by importers that re-derive a whole run from files on disk (Claude Code, Kiro) every
        time those files change. Unchanged records are not rewritten, so the change log — and the
        live UI — only sees real differences. Records of the run that are no longer produced are
        removed (``remove`` changes). Returns ``(written, removed)``.
        """
        recs = [r for r in records if isinstance(r, dict) and r.get("type") in _TABLES and r.get("id")]
        wanted: Dict[str, set] = {t: set() for t in _TABLES.values()}
        for r in recs:
            wanted[_TABLES[r["type"]]].add(r["id"])
        conn = self._conn()
        now = time.time()
        written = removed = 0
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for rec in recs:
                    table = _TABLES[rec["type"]]
                    canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    row = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (rec["id"],)).fetchone()
                    if row is not None:
                        old = json.dumps(json.loads(row["data"]), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                        if old == canonical:
                            continue
                    cols = _COLUMNS[table]
                    conn.execute(
                        f"INSERT INTO {table}(id, {', '.join(cols)}, data) VALUES (?, {', '.join('?' for _ in cols)}, ?) "
                        f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, data=excluded.data",
                        [rec["id"], *[rec.get(c) for c in cols], json.dumps(rec, separators=(",", ":"), ensure_ascii=False)],
                    )
                    conn.execute(
                        "INSERT INTO changes(run_id, type, id, ts) VALUES (?, ?, ?, ?)",
                        (run_id, rec["type"], rec["id"], now),
                    )
                    written += 1
                for rtype, table in _TABLES.items():
                    if table == "runs":
                        continue
                    stale = [
                        r["id"]
                        for r in conn.execute(f"SELECT id FROM {table} WHERE run_id = ?", (run_id,))
                        if r["id"] not in wanted[table]
                    ]
                    for sid in stale:
                        conn.execute(f"DELETE FROM {table} WHERE id = ?", (sid,))
                        conn.execute(
                            "INSERT INTO changes(run_id, type, id, ts) VALUES (?, ?, ?, ?)",
                            (run_id, f"remove:{rtype}", sid, now),
                        )
                    removed += len(stale)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return written, removed

    def delete_run(self, run_id: str) -> bool:
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount
                for t in ("spans", "events", "artifacts", "links"):
                    conn.execute(f"DELETE FROM {t} WHERE run_id = ?", (run_id,))
                conn.execute("INSERT INTO changes(run_id, type, id, ts) VALUES (?, 'delete', ?, ?)", (run_id, run_id, time.time()))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return n > 0

    def prune_changes(self, older_than_s: float = 3600) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM changes WHERE ts < ?", (time.time() - older_than_s,))

    # -- reads -----------------------------------------------------------------------
    def latest_seq(self) -> int:
        row = self._conn().execute("SELECT MAX(seq) AS s FROM changes").fetchone()
        return row["s"] or 0

    def changes_since(self, seq: int, limit: int = 2000) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT seq, run_id, type, id FROM changes WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
        ).fetchall()
        out = []
        for r in rows:
            if r["type"] == "delete":
                out.append({"seq": r["seq"], "type": "delete", "run_id": r["run_id"], "record": None})
                continue
            if r["type"].startswith("remove:"):
                out.append({"seq": r["seq"], "type": "remove", "run_id": r["run_id"], "record": {"id": r["id"], "type": r["type"][7:]}})
                continue
            if r["type"] not in _TABLES:
                continue
            row = conn.execute(f"SELECT data FROM {_TABLES[r['type']]} WHERE id = ?", (r["id"],)).fetchone()
            if row:
                out.append({"seq": r["seq"], "type": r["type"], "run_id": r["run_id"], "record": json.loads(row["data"])})
        return out

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_bundle(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Everything needed to render one run: the run plus its spans, events, artifacts, links."""
        conn = self._conn()
        run = self.get_run(run_id)
        spans = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM spans WHERE run_id = ? ORDER BY started_at, rowid", (run_id,))]
        if run is None and not spans:
            return None
        if run is None:  # spans arrived (e.g. via OTLP) before / without a run record
            run = {"type": "run", "id": run_id, "name": spans[0].get("name", run_id)}
        bundle = {
            "schema": f"traceglass/v{SCHEMA_VERSION}",
            "run": self._with_summary(run, spans),
            "spans": spans,
            "events": [json.loads(r["data"]) for r in conn.execute("SELECT data FROM events WHERE run_id = ? ORDER BY ts, rowid", (run_id,))],
            "artifacts": [json.loads(r["data"]) for r in conn.execute("SELECT data FROM artifacts WHERE run_id = ? ORDER BY created_at, rowid", (run_id,))],
            "links": [json.loads(r["data"]) for r in conn.execute("SELECT data FROM links WHERE run_id = ? ORDER BY rowid", (run_id,))],
        }
        return bundle

    def list_runs(
        self,
        project: Optional[str] = None,
        q: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        conn = self._conn()
        where, args = [], []
        if project:
            where.append("r.project = ?")
            args.append(project)
        if status:
            where.append("r.status = ?")
            args.append(status)
        if q:
            where.append("(r.name LIKE ? OR r.id LIKE ? OR r.project LIKE ? OR r.session LIKE ?)")
            args += [f"%{q}%"] * 4
        sql = (
            "SELECT r.data AS data, "
            " (SELECT COUNT(*) FROM spans s WHERE s.run_id = r.id) AS span_count,"
            " (SELECT COUNT(*) FROM spans s WHERE s.run_id = r.id AND s.status = 'error') AS error_count,"
            " (SELECT COUNT(*) FROM spans s WHERE s.run_id = r.id AND s.status = 'running') AS running_count,"
            " (SELECT COUNT(*) FROM artifacts a WHERE a.run_id = r.id) AS artifact_count,"
            " (SELECT SUM(COALESCE(json_extract(s.data, '$.usage.input_tokens'), 0) + COALESCE(json_extract(s.data, '$.usage.output_tokens'), 0))"
            "    FROM spans s WHERE s.run_id = r.id) AS tokens,"
            " (SELECT SUM(json_extract(s.data, '$.usage.cost_usd')) FROM spans s WHERE s.run_id = r.id) AS cost,"
            " (SELECT MAX(s.ended_at) FROM spans s WHERE s.run_id = r.id) AS last_end,"
            " (SELECT COUNT(*) FROM spans s WHERE s.run_id = r.id AND s.parent_id IS NULL AND s.status = 'error') AS root_errors"
            " FROM runs r"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.started_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for row in conn.execute(sql, args):
            run = json.loads(row["data"])
            run.pop("input", None)
            run.pop("output", None)
            run["summary"] = {
                "spans": row["span_count"],
                "errors": row["error_count"],
                "artifacts": row["artifact_count"],
                "tokens": row["tokens"] or 0,
                "cost_usd": row["cost"],
            }
            if not run.get("status"):
                run["status"] = "running" if row["running_count"] else ("error" if row["root_errors"] else "ok")
            if run.get("ended_at") is None and run["status"] != "running" and row["last_end"]:
                run["ended_at"] = row["last_end"]
            out.append(run)
        return out

    def search(self, q: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Search names across runs, spans and artifacts (and artifact content)."""
        conn = self._conn()
        like = f"%{q}%"
        hits: List[Dict[str, Any]] = []
        for r in conn.execute("SELECT id, name, project, status FROM runs WHERE name LIKE ? OR id LIKE ? LIMIT ?", (like, like, limit)):
            hits.append({"kind": "run", "id": r["id"], "run_id": r["id"], "name": r["name"], "detail": r["project"], "status": r["status"]})
        for r in conn.execute(
            "SELECT s.id, s.run_id, s.name, s.kind, s.status, r.name AS run_name FROM spans s LEFT JOIN runs r ON r.id = s.run_id "
            "WHERE s.name LIKE ? OR s.id LIKE ? LIMIT ?",
            (like, like, limit),
        ):
            hits.append({"kind": "span", "id": r["id"], "run_id": r["run_id"], "name": r["name"], "detail": f"{r['kind']} · {r['run_name'] or r['run_id']}", "status": r["status"]})
        for r in conn.execute(
            "SELECT a.id, a.run_id, a.name, a.kind, r.name AS run_name FROM artifacts a LEFT JOIN runs r ON r.id = a.run_id "
            "WHERE a.name LIKE ? OR a.id LIKE ? OR json_extract(a.data, '$.content') LIKE ? LIMIT ?",
            (like, like, like, limit),
        ):
            hits.append({"kind": "artifact", "id": r["id"], "run_id": r["run_id"], "name": r["name"], "detail": f"{r['kind']} · {r['run_name'] or r['run_id']}"})
        return hits[:limit]

    # -- helpers ---------------------------------------------------------------------
    @staticmethod
    def _with_summary(run: Dict[str, Any], spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        run = dict(run)
        tokens = 0
        cost = None
        for s in spans:
            u = s.get("usage") or {}
            tokens += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
            if u.get("cost_usd") is not None:
                cost = (cost or 0) + u["cost_usd"]
        run["summary"] = {
            "spans": len(spans),
            "errors": sum(1 for s in spans if s.get("status") == "error"),
            "tokens": tokens,
            "cost_usd": cost,
        }
        if not run.get("status"):
            roots = [s for s in spans if not s.get("parent_id")]
            if any(s.get("status") == "running" for s in spans):
                run["status"] = "running"
            else:
                run["status"] = "error" if any(s.get("status") == "error" for s in roots) else "ok"
        if spans:
            starts = [s["started_at"] for s in spans if s.get("started_at") is not None]
            ends = [s["ended_at"] for s in spans if s.get("ended_at") is not None]
            if run.get("started_at") is None and starts:
                run["started_at"] = min(starts)
            if run.get("ended_at") is None and ends and run["status"] != "running":
                run["ended_at"] = max(ends)
        return run
