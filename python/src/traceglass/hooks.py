"""Agent hooks: ``traceglass hook <source>`` and ``traceglass hooks install|uninstall|status``.

Claude Code and Kiro can run a command on lifecycle events (prompt submitted, tool about to run,
tool finished, turn stopped, session ended…). Traceglass uses that to learn *exactly when* things
happen, which transcripts do not always record (Kiro only timestamps prompts), and to know when a
session has really ended.

The hook is on the agent's hot path, so it is deliberately tiny: read the event from stdin, append
one timing line to a sidecar file (``~/.traceglass/hooks/<source>/<session>.jsonl``), exit 0, print
nothing. It never stores prompt or tool *content* — that always comes from the transcript — and it
can never block or alter the agent: every failure is swallowed.
"""

from __future__ import annotations

import json
import os
import sys
import time

from .integrations import sessions

TYPE_CHECKING = False
if TYPE_CHECKING:  # pragma: no cover - annotations only; keeps the hook's import cost minimal
    from pathlib import Path
    from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_STDIN = 32 * 1024 * 1024

CLAUDE_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "PostToolUseFailure": "post_tool",
    "Stop": "stop",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "PreCompact": "compact",
    "SessionEnd": "session_end",
}
CLAUDE_TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}

KIRO_EVENTS = {
    "agentSpawn": "agent_spawn",
    "userPromptSubmit": "prompt",
    "preToolUse": "pre_tool",
    "postToolUse": "post_tool",
    "stop": "stop",
}
KIRO_TOOL_EVENTS = {"preToolUse", "postToolUse"}

IMPORT_TRIGGERS = {"stop", "subagent_stop", "session_end"}
MANAGED_MARKERS = ("do not edit", "managed by")


# ---------------------------------------------------------------------------------------
# The hook itself
# ---------------------------------------------------------------------------------------
def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _kiro_session_for_cwd(cwd: Optional[str]) -> Optional[str]:
    """Kiro fallback: the most recently updated session whose cwd matches."""
    d = sessions.kiro_sessions_str()
    try:
        entries = [e for e in os.scandir(d) if e.name.endswith(".json")]
    except OSError:
        return None
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    want = os.path.realpath(cwd or os.getcwd())
    for e in entries[:20]:
        try:
            with open(e.path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            continue
        c = meta.get("cwd")
        if c and os.path.realpath(c) == want:
            return meta.get("session_id") or e.name[:-5]
    return None


def normalize(source: str, payload: Dict[str, Any], event: Optional[str] = None, env: Optional[Dict[str, str]] = None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Turn an agent's hook payload into a sidecar event (timing + identifiers only)."""
    env = os.environ if env is None else env
    raw = event or _first(payload, "hook_event_name", "hookEventName", "event", "hook")
    table = CLAUDE_EVENTS if source == "claude-code" else KIRO_EVENTS
    norm = table.get(str(raw)) if raw else None
    if norm is None and raw:
        # Accept either naming style for robustness (e.g. "PreToolUse" for Kiro).
        lowered = {k.lower(): v for k, v in {**CLAUDE_EVENTS, **KIRO_EVENTS}.items()}
        norm = lowered.get(str(raw).lower())
    if norm is None:
        return None
    sid = _first(payload, "session_id", "sessionId", "conversation_id", "conversationId")
    if not sid and source == "kiro":
        sid = env.get("KIRO_SESSION_ID") or _kiro_session_for_cwd(_first(payload, "cwd"))
    tool_input = _first(payload, "tool_input", "toolInput", "input") if norm in ("pre_tool", "post_tool") else None
    ok: Optional[bool] = None
    if norm == "post_tool":
        if raw == "PostToolUseFailure":
            ok = False
        elif source == "claude-code":
            ok = True
        else:
            resp = _first(payload, "tool_response", "toolResponse", "response", "result")
            if isinstance(resp, dict):
                if isinstance(resp.get("success"), bool):
                    ok = resp["success"]
                elif str(resp.get("status", "")).lower() in ("error", "failed", "failure"):
                    ok = False
                elif str(resp.get("status", "")).lower() in ("success", "ok"):
                    ok = True
    ev: Dict[str, Any] = {
        "ts": now if now is not None else time.time() * 1000.0,
        "event": norm,
        "raw_event": str(raw),
        "session_id": sid,
        "tool_name": _first(payload, "tool_name", "toolName") if norm in ("pre_tool", "post_tool") else None,
        "tool_use_id": _first(payload, "tool_use_id", "toolUseId"),
        "tool_input_digest": sessions.input_digest(tool_input) if tool_input is not None else None,
        "ok": ok,
        "cwd": _first(payload, "cwd") or os.getcwd(),
        "transcript_path": _first(payload, "transcript_path", "transcriptPath"),
    }
    for k in ("agent_id", "agent_type", "agent_transcript_path", "duration_ms", "source", "reason", "trigger"):
        if payload.get(k) not in (None, ""):
            ev[k] = payload[k]
    return ev


def _session_file(source: str, ev: Dict[str, Any]) -> Optional[str]:
    if source == "claude-code":
        return ev.get("transcript_path")
    sid = ev.get("session_id")
    if not sid:
        return None
    p = os.path.join(sessions.kiro_sessions_str(), f"{sid}.jsonl")
    return p if os.path.exists(p) else None


def _maybe_import(source: str, ev: Dict[str, Any]) -> None:
    """Without a running watcher, record the session ourselves when a turn or session ends."""
    if ev["event"] not in IMPORT_TRIGGERS or sessions.watcher_alive():
        return
    path = _session_file(source, ev)
    if not path:
        return
    stamp = os.path.join(str(sessions.hooks_dir(source)), f".import-{sessions._safe(ev.get('session_id') or 'x')}")
    try:
        if time.time() - os.stat(stamp).st_mtime < 2.0:
            return
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "a"):
            os.utime(stamp)
        import subprocess

        subprocess.Popen(
            [sys.executable, "-m", "traceglass", "import", source, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        pass


def handle(source: str, payload: Dict[str, Any], event: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    ev = normalize(source, payload, event=event, env=env)
    if ev is None:
        return None
    sessions.append_hook_event(source, ev.get("session_id"), ev)
    _maybe_import(source, ev)
    return ev


def main_hook(argv: List[str]) -> None:
    """Entry point for ``traceglass hook <source> [--event NAME]``. Always exits 0, silently."""
    try:
        if os.environ.get("TRACEGLASS_HOOKS_DISABLED", "").lower() in ("1", "true", "yes"):
            return
        args = list(argv)
        source = args.pop(0) if args else ""
        event = None
        if "--event" in args:
            i = args.index("--event")
            event = args[i + 1] if i + 1 < len(args) else None
        if source not in sessions.SOURCES:
            return
        raw = b""
        try:
            if not sys.stdin.isatty():
                raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        except Exception:
            raw = b""
        payload: Dict[str, Any] = {}
        if raw and len(raw) <= MAX_STDIN:
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    payload = data
            except ValueError:
                payload = {}
        handle(source, payload, event=event)
    except BaseException:
        pass


# ---------------------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------------------
def hook_command(source: str, event: Optional[str] = None) -> str:
    import shlex

    cmd = f"{shlex.quote(sys.executable)} -m traceglass hook {source}"
    return cmd + (f" --event {event}" if event else "")


def _is_ours(cmd: Any, source: str) -> bool:
    return isinstance(cmd, str) and "traceglass" in cmd and f" hook {source}" in cmd


def _indent_of(text: str) -> Any:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) != len(line):
            ws = line[: len(line) - len(stripped)]
            return "\t" if ws.startswith("\t") else len(ws)
    return 2


def _dump(obj: Any, indent: Any) -> str:
    return json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"


def _read_json(path: Path) -> Tuple[Optional[str], Dict[str, Any]]:
    if not path.exists():
        return None, {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return text, {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return text, data


def _diff(path: Path, old: Optional[str], new: Optional[str]) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
        )
    )


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".traceglass-backup")


def _write(path: Path, old_text: Optional[str], new_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_path(path)
    if not backup.exists():
        # An empty backup records that the file did not exist before Traceglass touched it.
        backup.write_text(old_text or "", encoding="utf-8")
    tmp = path.with_name(path.name + ".traceglass-tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def _restore_or_write(path: Path, stripped: Dict[str, Any], indent: Any) -> str:
    """Uninstall: if what's left equals the pre-install file, restore its exact bytes."""
    backup = _backup_path(path)
    if backup.exists():
        original = backup.read_text(encoding="utf-8")
        try:
            orig_data = json.loads(original) if original.strip() else {}
        except ValueError:
            orig_data = None
        if orig_data == stripped:
            if original == "":
                path.unlink()
                backup.unlink()
                return "removed (Traceglass had created it)"
            path.write_text(original, encoding="utf-8")
            backup.unlink()
            return "restored to its original contents"
    path.write_text(_dump(stripped, indent), encoding="utf-8")
    return "updated"


class Change:
    def __init__(self, path: Path, action: str, diff: str = "", note: str = "") -> None:
        self.path, self.action, self.diff, self.note = path, action, diff, note

    def __repr__(self) -> str:  # pragma: no cover
        return f"Change({self.path}, {self.action})"


# -- Claude Code -----------------------------------------------------------------------
def claude_settings_path(scope: str = "user", cwd: Optional[str] = None) -> Path:
    from pathlib import Path

    base = Path(cwd or os.getcwd())
    if scope == "user":
        return sessions.claude_config_dir() / "settings.json"
    if scope == "project":
        return base / ".claude" / "settings.json"
    if scope == "local":
        return base / ".claude" / "settings.local.json"
    raise ValueError(f"unknown scope {scope!r} (user, project or local)")


def _claude_entry(event: str) -> Dict[str, Any]:
    hook = {"type": "command", "command": hook_command("claude-code"), "async": True, "timeout": 10}
    entry: Dict[str, Any] = {"hooks": [hook]}
    if event in CLAUDE_TOOL_EVENTS:
        entry = {"matcher": "*", **entry}
    return entry


def _strip_claude(data: Dict[str, Any], original: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = json.loads(json.dumps(data))
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    orig_hooks = (original or {}).get("hooks") if isinstance(original, dict) else None
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                inner = [h for h in entry["hooks"] if not (isinstance(h, dict) and _is_ours(h.get("command"), "claude-code"))]
                if not inner and entry["hooks"]:
                    continue
                entry = {**entry, "hooks": inner}
            kept.append(entry)
        if kept:
            hooks[event] = kept
        elif not (isinstance(orig_hooks, dict) and event in orig_hooks):
            del hooks[event]
        else:
            hooks[event] = []
    if not hooks and not (isinstance(original, dict) and "hooks" in original):
        del data["hooks"]
    return data


def install_claude(scope: str = "user", dry_run: bool = False, cwd: Optional[str] = None) -> Change:
    path = claude_settings_path(scope, cwd)
    old_text, data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' is not an object")
    added = 0
    for event in CLAUDE_EVENTS:
        entries = hooks.setdefault(event, [])
        if any(
            isinstance(e, dict) and any(isinstance(h, dict) and _is_ours(h.get("command"), "claude-code") for h in e.get("hooks") or [])
            for e in entries
        ):
            continue
        entries.append(_claude_entry(event))
        added += 1
    if not added:
        return Change(path, "unchanged", note="Traceglass hooks are already installed")
    new_text = _dump(data, _indent_of(old_text or ""))
    diff = _diff(path, old_text, new_text)
    if not dry_run:
        _write(path, old_text, new_text)
    return Change(path, "would install" if dry_run else "installed", diff, f"{added} hook events")


def uninstall_claude(scope: str = "user", dry_run: bool = False, cwd: Optional[str] = None) -> Change:
    path = claude_settings_path(scope, cwd)
    old_text, data = _read_json(path)
    if old_text is None:
        return Change(path, "unchanged", note="no settings file")
    backup = _backup_path(path)
    original = None
    if backup.exists():
        bt = backup.read_text(encoding="utf-8")
        try:
            original = json.loads(bt) if bt.strip() else {}
        except ValueError:
            original = None
    stripped = _strip_claude(data, original)
    if stripped == data:
        return Change(path, "unchanged", note="no Traceglass hooks found")
    if dry_run:
        return Change(path, "would uninstall", _diff(path, old_text, _dump(stripped, _indent_of(old_text))))
    note = _restore_or_write(path, stripped, _indent_of(old_text))
    return Change(path, "uninstalled", _diff(path, old_text, path.read_text(encoding="utf-8") if path.exists() else ""), note)


def claude_status(cwd: Optional[str] = None) -> List[Tuple[str, Path, bool]]:
    """(scope, settings path, Traceglass hooks present) for each settings file that exists."""
    out = []
    for scope in ("user", "project", "local"):
        p = claude_settings_path(scope, cwd)
        try:
            _, data = _read_json(p)
        except Exception:
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        found = isinstance(hooks, dict) and any(
            isinstance(e, dict) and any(isinstance(h, dict) and _is_ours(h.get("command"), "claude-code") for h in e.get("hooks") or [])
            for entries in hooks.values()
            if isinstance(entries, list)
            for e in entries
        )
        if p.exists():
            out.append((scope, p, found))
    return out


# -- Kiro ------------------------------------------------------------------------------
def kiro_agents_dir() -> Path:
    return sessions.kiro_dir() / "agents"


def kiro_agent_paths(agents: Iterable[str] = (), all_agents: bool = False) -> List[Path]:
    d = kiro_agents_dir()
    if all_agents:
        return sorted(p for p in d.glob("*.json") if p.is_file())
    names = list(agents) or ["default"]
    return [d / (n if n.endswith(".json") else f"{n}.json") for n in names]


def _kiro_entry(event: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"command": hook_command("kiro", event)}
    if event in KIRO_TOOL_EVENTS:
        entry = {"matcher": "*", **entry}
    return entry


def _is_managed(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(m in low for m in MANAGED_MARKERS)


def install_kiro(agents: Iterable[str] = (), all_agents: bool = False, dry_run: bool = False, force: bool = False) -> List[Change]:
    changes = []
    for path in kiro_agent_paths(agents, all_agents):
        if not path.exists():
            changes.append(Change(path, "skipped", note="agent file not found"))
            continue
        old_text, data = _read_json(path)
        if _is_managed(old_text) and not force:
            changes.append(Change(path, "skipped", note="generated by another tool (marked 'do not edit'/'managed by'); use --force to edit anyway"))
            continue
        hooks = data.get("hooks")
        if hooks is None:
            hooks = data["hooks"] = {}
        if not isinstance(hooks, dict):
            changes.append(Change(path, "skipped", note="'hooks' is not an object"))
            continue
        added = 0
        for event in KIRO_EVENTS:
            entries = hooks.setdefault(event, [])
            if any(isinstance(e, dict) and _is_ours(e.get("command"), "kiro") for e in entries):
                continue
            entries.append(_kiro_entry(event))
            added += 1
        if not added:
            changes.append(Change(path, "unchanged", note="Traceglass hooks are already installed"))
            continue
        new_text = _dump(data, _indent_of(old_text or ""))
        diff = _diff(path, old_text, new_text)
        if not dry_run:
            _write(path, old_text, new_text)
        changes.append(Change(path, "would install" if dry_run else "installed", diff, f"{added} hook events"))
    return changes


def _strip_kiro(data: Dict[str, Any], original: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = json.loads(json.dumps(data))
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    orig_hooks = original.get("hooks") if isinstance(original, dict) else None
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e.get("command"), "kiro"))]
        if kept or (isinstance(orig_hooks, dict) and event in orig_hooks):
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks and not (isinstance(original, dict) and "hooks" in original):
        del data["hooks"]
    return data


def uninstall_kiro(agents: Iterable[str] = (), all_agents: bool = False, dry_run: bool = False) -> List[Change]:
    changes = []
    for path in kiro_agent_paths(agents, all_agents):
        if not path.exists():
            continue
        old_text, data = _read_json(path)
        backup = _backup_path(path)
        original = None
        if backup.exists():
            bt = backup.read_text(encoding="utf-8")
            try:
                original = json.loads(bt) if bt.strip() else {}
            except ValueError:
                original = None
        stripped = _strip_kiro(data, original)
        if stripped == data:
            if all_agents:
                continue
            changes.append(Change(path, "unchanged", note="no Traceglass hooks found"))
            continue
        if dry_run:
            changes.append(Change(path, "would uninstall", _diff(path, old_text, _dump(stripped, _indent_of(old_text or "")))))
            continue
        note = _restore_or_write(path, stripped, _indent_of(old_text or ""))
        changes.append(Change(path, "uninstalled", note=note))
    return changes


def kiro_status() -> List[Tuple[Path, bool]]:
    out = []
    for p in kiro_agent_paths(all_agents=True):
        try:
            _, data = _read_json(p)
        except Exception:
            continue
        hooks = data.get("hooks")
        found = isinstance(hooks, dict) and any(
            isinstance(e, dict) and _is_ours(e.get("command"), "kiro") for entries in hooks.values() if isinstance(entries, list) for e in entries
        )
        out.append((p, found))
    return out


def last_events() -> Dict[str, Optional[Dict[str, Any]]]:
    """The most recent sidecar event per source (for ``traceglass hooks status``)."""
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for source in sessions.SOURCES:
        d = sessions.hooks_dir(source)
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        ev = None
        if files:
            evs = sessions.read_hook_events(source, files[0].stem)
            ev = evs[-1] if evs else None
        out[source] = ev
    return out
