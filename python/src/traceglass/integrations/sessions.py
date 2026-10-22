"""Shared plumbing for coding-agent sessions (Claude Code, Kiro): paths, hook sidecars, heartbeat.

This module sits on the hook hot path, so it imports nothing beyond the standard library.

Hook sidecars live at ``<home>/hooks/<source>/<session_id>.jsonl``. Each line is a small timing
record written by ``traceglass hook <source>`` — never prompt or tool *content*, which always
comes from the agent's own transcript.
"""

from __future__ import annotations

import json
import os
import time

# Annotations only (``from __future__ import annotations`` keeps them unevaluated): importing
# typing/pathlib eagerly would cost the hook several milliseconds on every agent event.
TYPE_CHECKING = False
if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path
    from typing import Any, Dict, List, Optional

SOURCES = ("claude-code", "kiro")
HEARTBEAT_FRESH_S = 10.0
_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def _P(p: str) -> "Path":
    from pathlib import Path

    return Path(p)


def _home_str() -> str:
    env = os.environ.get("TRACEGLASS_HOME")
    return os.path.expanduser(env) if env else os.path.join(os.path.expanduser("~"), ".traceglass")


def _safe(session_id: str) -> str:
    return "".join(c if c in _SAFE_CHARS else "_" for c in session_id)[:128]


def traceglass_home() -> Path:
    return _P(_home_str())


def hooks_dir(source: str) -> Path:
    return _P(os.path.join(_home_str(), "hooks", source))


def claude_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return _P(os.path.expanduser(env) if env else os.path.join(os.path.expanduser("~"), ".claude"))


def claude_projects_dir() -> Path:
    return claude_config_dir() / "projects"


def _kiro_str() -> str:
    env = os.environ.get("KIRO_HOME")
    return os.path.join(os.path.expanduser(env) if env else os.path.expanduser("~"), ".kiro")


def kiro_dir() -> Path:
    """``$KIRO_HOME/.kiro`` (Kiro's own convention) or ``~/.kiro``."""
    return _P(_kiro_str())


def kiro_sessions_str() -> str:
    return os.path.join(_kiro_str(), "sessions", "cli")


def kiro_sessions_dir() -> Path:
    return kiro_dir() / "sessions" / "cli"


def input_digest(tool_input: Any) -> Optional[str]:
    """Stable fingerprint of a tool call's input, used to pair hook events with transcript calls.

    Top-level keys starting with ``__`` (e.g. Kiro's ``__tool_use_purpose``) are ignored, because
    agents do not always pass them to hooks. Importers must use this same function.
    """
    if tool_input is None:
        return None
    import hashlib

    if isinstance(tool_input, dict):
        tool_input = {k: v for k, v in tool_input.items() if not str(k).startswith("__")}
    try:
        blob = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return None
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _sidecar_str(source: str, session_id: str) -> str:
    return os.path.join(_home_str(), "hooks", source, _safe(session_id) + ".jsonl")


def _sidecar(source: str, session_id: str) -> Path:
    return _P(_sidecar_str(source, session_id))


def read_hook_events(source: str, session_id: Optional[str]) -> List[Dict[str, Any]]:
    """All sidecar events for a session, oldest first. Tolerates a partially written last line."""
    if not session_id:
        return []
    path = _sidecar_str(source, session_id)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    out.sort(key=lambda e: e.get("ts") or 0)
    return out


def append_hook_event(source: str, session_id: Optional[str], event: Dict[str, Any]) -> None:
    """Append one event with a single O_APPEND write (atomic for small lines). Never raises."""
    if not session_id:
        return
    try:
        path = _sidecar_str(source, session_id)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:
        pass


def sidecar_path(source: str, session_id: str) -> Path:
    return _sidecar(source, session_id)


def heartbeat_path() -> Path:
    return _P(os.path.join(_home_str(), "watch.heartbeat"))


def write_heartbeat(db: Optional[str] = None) -> None:
    try:
        p = heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "ts": time.time(), "db": db}))
        os.replace(tmp, p)
    except Exception:
        pass


def clear_heartbeat() -> None:
    try:
        p = heartbeat_path()
        data = json.loads(p.read_text())
        if data.get("pid") == os.getpid():
            p.unlink()
    except Exception:
        pass


def watcher_alive() -> bool:
    """True when a ``traceglass watch`` process refreshed its heartbeat in the last few seconds."""
    try:
        with open(os.path.join(_home_str(), "watch.heartbeat"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return time.time() - float(data.get("ts", 0)) < HEARTBEAT_FRESH_S
    except Exception:
        return False
