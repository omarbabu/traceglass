"""``traceglass watch`` — keep Traceglass in sync with Claude Code and Kiro sessions as they happen.

The watcher polls the agents' own session folders (stdlib only, no file-system-event dependency),
fingerprints each session (transcript + subagent transcripts + hook sidecar), and when a
fingerprint changes re-derives the whole run with the importer and hands it to
``Store.sync_run``, which writes only the records that actually changed. Running sessions keep
being re-evaluated every few seconds so they flip from *working* to *idle* or *interrupted*
even when nothing new is written.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .integrations import sessions
from .store import Store

THROTTLE_S = 0.75
LIVE_RECHECK_S = 5.0
KIRO_INDEX_TTL_S = 15.0

Fingerprint = Tuple[Any, ...]


@dataclass
class _State:
    fingerprint: Fingerprint = ()
    last_sync: float = 0.0
    working: bool = False
    spans: int = 0
    run_id: Optional[str] = None
    failures: int = 0


@dataclass
class _Session:
    source: str
    path: Path
    session_id: str
    mtime: float
    extra: Dict[str, Any] = field(default_factory=dict)


def _stat(p: Path) -> Tuple[int, int]:
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return (0, 0)


def encode_claude_project(path: str) -> str:
    """Claude Code's folder name for a working directory (non-alphanumerics become '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


class Watcher:
    def __init__(
        self,
        store: Store,
        *,
        claude_code: bool = True,
        kiro: bool = True,
        since_days: Optional[float] = 3.0,
        interval: float = 1.0,
        project_dir: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.claude_code = claude_code
        self.kiro = kiro
        self.since_days = since_days
        self.interval = interval
        self.project_dir = project_dir
        self.log = log or (lambda _msg: None)
        self.clock = clock
        self._states: Dict[Tuple[str, str], _State] = {}
        self._kiro_cwd: Dict[str, Optional[str]] = {}
        self._kiro_index: Any = None
        self._kiro_index_at = 0.0
        self.syncs = 0
        self.errors = 0
        self.thread: Optional[threading.Thread] = None
        self._db_dir = Path(store.path).resolve().parent if str(store.path) != ":memory:" else None

    # -- discovery ---------------------------------------------------------------------
    def _cutoff(self) -> float:
        return 0.0 if self.since_days is None else self.clock() - self.since_days * 86400

    def _tracked(self, source: str, path: Path) -> bool:
        return (source, str(path)) in self._states

    def _discover_claude(self) -> List[_Session]:
        root = sessions.claude_projects_dir()
        if not root.is_dir():
            return []
        if self.project_dir:
            dirs = [root / encode_claude_project(self.project_dir)]
        else:
            try:
                dirs = [Path(e.path) for e in os.scandir(root) if e.is_dir()]
            except OSError:
                return []
        out, cutoff = [], self._cutoff()
        for d in dirs:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".jsonl"):
                    continue
                p = Path(e.path)
                if self._db_dir is not None and p.parent == self._db_dir:
                    continue  # never treat Traceglass's own files as a session
                try:
                    mtime = e.stat().st_mtime
                except OSError:
                    continue
                sub = p.with_suffix("") / "subagents"
                if mtime < cutoff and not self._tracked("claude-code", p):
                    # A long-quiet main transcript can still have live subagents.
                    if not sub.is_dir() or max((_stat(s)[0] for s in sub.glob("*.jsonl")), default=0) / 1e9 < cutoff:
                        continue
                out.append(_Session("claude-code", p, p.stem, mtime))
        return out

    def _kiro_session_cwd(self, json_path: Path) -> Optional[str]:
        key = str(json_path)
        if key not in self._kiro_cwd:
            cwd = None
            try:
                import json

                with open(json_path, "r", encoding="utf-8") as fh:
                    cwd = json.load(fh).get("cwd")
            except Exception:
                cwd = None
            self._kiro_cwd[key] = cwd
        return self._kiro_cwd[key]

    def _discover_kiro(self) -> List[_Session]:
        root = sessions.kiro_sessions_dir()
        if not root.is_dir():
            return []
        try:
            entries = list(os.scandir(root))
        except OSError:
            return []
        out, cutoff = [], self._cutoff()
        want = os.path.realpath(os.path.expanduser(self.project_dir)) if self.project_dir else None
        for e in entries:
            if not e.name.endswith(".jsonl"):
                continue
            p = Path(e.path)
            try:
                mtime = max(e.stat().st_mtime, _stat(p.with_suffix(".json"))[0] / 1e9)
            except OSError:
                continue
            if mtime < cutoff and not self._tracked("kiro", p):
                continue
            if want:
                cwd = self._kiro_session_cwd(p.with_suffix(".json"))
                if not cwd or os.path.realpath(cwd) != want:
                    continue
            out.append(_Session("kiro", p, p.stem, mtime))
        return out

    def discover(self) -> List[_Session]:
        found: List[_Session] = []
        if self.claude_code:
            found += self._discover_claude()
        if self.kiro:
            found += self._discover_kiro()
        found.sort(key=lambda s: s.mtime, reverse=True)
        return found

    # -- fingerprints & sync -----------------------------------------------------------
    def fingerprint(self, s: _Session) -> Fingerprint:
        parts: List[Any] = [_stat(s.path)]
        if s.source == "claude-code":
            sub = s.path.with_suffix("") / "subagents"
            if sub.is_dir():
                try:
                    parts.append(tuple(sorted((e.name, *_stat(Path(e.path))) for e in os.scandir(sub))))
                except OSError:
                    pass
        else:
            parts.append(_stat(s.path.with_suffix(".json")))
        parts.append(_stat(sessions.sidecar_path(s.source, s.session_id)))
        return tuple(parts)

    def _kiro_idx(self) -> Any:
        from .integrations import kiro

        now = self.clock()
        build = getattr(kiro, "build_index", None)
        if build is None:
            return None
        if self._kiro_index is None or now - self._kiro_index_at > KIRO_INDEX_TTL_S:
            try:
                self._kiro_index = build(sessions.kiro_sessions_dir())
            except Exception:
                self._kiro_index = None
            self._kiro_index_at = now
        return self._kiro_index

    def records_for(self, s: _Session) -> List[Dict[str, Any]]:
        now_ms = self.clock() * 1000.0
        if s.source == "claude-code":
            from .integrations import claude_code

            return claude_code.transcript_to_records(s.path, now=now_ms)
        from .integrations import kiro

        idx = self._kiro_idx()
        if idx is not None:
            try:
                return kiro.session_to_records(s.path, now=now_ms, index=idx)
            except TypeError:
                pass
        return kiro.session_to_records(s.path, now=now_ms)

    def sync(self, s: _Session, state: _State) -> Optional[str]:
        records = self.records_for(s)
        run = next((r for r in records if r.get("type") == "run"), None)
        if run is None or len(records) <= 1:
            return None
        written, removed = self.store.sync_run(run["id"], records)
        spans = sum(1 for r in records if r.get("type") == "span")
        activity = (run.get("metadata") or {}).get("activity") or ("working" if run.get("status") == "running" else "idle")
        first = state.run_id is None
        delta = spans - state.spans
        state.run_id = run["id"]
        state.working = activity == "working"
        state.spans = spans
        if written or removed:
            label = "claude" if s.source == "claude-code" else "kiro"
            title = str(run.get("name") or s.session_id)
            title = title if len(title) <= 60 else title[:59] + "…"
            change = f"{spans} spans" if first else (f"+{delta} spans" if delta > 0 else f"{delta} spans" if delta < 0 else "updated")
            self.log(f"  ◆ {label:<6} {title:<60}  {change:<12} {activity}")
        return run["id"] if (written or removed) else None

    def poll_once(self) -> List[str]:
        synced: List[str] = []
        now = self.clock()
        last_beat = time.time()
        for s in self.discover():
            key = (s.source, str(s.path))
            state = self._states.get(key)
            fp = self.fingerprint(s)
            if state is None:
                state = self._states[key] = _State()
                due = True
            else:
                changed = fp != state.fingerprint
                due = changed or (state.working and now - state.last_sync >= LIVE_RECHECK_S)
                if due and now - state.last_sync < THROTTLE_S:
                    continue  # picked up on the next poll
            if not due:
                continue
            state.fingerprint = fp
            state.last_sync = now
            try:
                rid = self.sync(s, state)
                state.failures = 0
                self.syncs += 1
                if rid:
                    synced.append(rid)
            except Exception as exc:  # one bad session never stops the watcher
                self.errors += 1
                state.failures += 1
                if state.failures == 1:
                    self.log(f"  ! {s.source} {s.session_id}: {type(exc).__name__}: {exc}")
            if time.time() - last_beat > 2.0:
                sessions.write_heartbeat(str(self.store.path))
                last_beat = time.time()
        return synced

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        try:
            while not stop_event.is_set():
                sessions.write_heartbeat(str(self.store.path))
                self.poll_once()
                sessions.write_heartbeat(str(self.store.path))
                stop_event.wait(self.interval)
        except KeyboardInterrupt:
            pass
        finally:
            sessions.clear_heartbeat()

    def start(self) -> threading.Event:
        """Run in a daemon thread (``traceglass serve --watch``). Returns the stop event."""
        global ACTIVE
        stop = threading.Event()
        self.thread = threading.Thread(target=self.run_forever, args=(stop,), name="traceglass-watch", daemon=True)
        self.thread.start()
        ACTIVE = self
        return stop


ACTIVE: Optional[Watcher] = None


def parse_since(value: Optional[str]) -> Optional[float]:
    """'3d', '12h', '30m', '2w', a bare number of days, or 'all' → days (None = everything)."""
    if value is None:
        return 3.0
    v = value.strip().lower()
    if v in ("all", "ever", "0"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw]?)", v)
    if not m:
        raise ValueError(f"cannot parse --since {value!r} (try 3d, 12h, 30m, all)")
    n, unit = float(m.group(1)), m.group(2) or "d"
    return n * {"m": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7}[unit]


def stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
