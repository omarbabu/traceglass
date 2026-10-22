"""Import Claude Code session transcripts as Traceglass runs.

Claude Code writes one JSON-lines transcript per session under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``; subagents live next to it in
``<session-id>/subagents/agent-<id>.jsonl`` with an ``agent-<id>.meta.json`` naming the
``toolUseId`` of the Agent call that launched them.

Mapping:

* session → run (``cc_<sessionId>``, so re-importing upserts instead of duplicating)
* user prompt → ``step`` span "Turn N: …"
* assistant API message → ``llm`` span (model, usage; thinking text is never exported)
* tool call → ``tool`` span (input, matched ``tool_result`` output, error status)
* Agent/Task call with a subagent transcript → ``delegation`` span containing an ``agent``
  span with the subagent's own turns, model calls and tools
* Edit / MultiEdit → ``diff`` artifact; Write → ``code`` artifact; last assistant text →
  final ``markdown`` artifact

Liveness: a session whose last turn is unfinished (unanswered tool calls, or a prompt / tool
result the model has not answered) and whose files changed in the last 10 minutes is
``working`` — the run is ``running`` and the open turn and tools stay open. Stale unfinished
sessions are ``interrupted``; finished ones ``idle``; a ``SessionEnd`` hook event makes them
``ended``. Hook sidecar events (``traceglass hook claude-code``) refine all of this.

All payloads pass through the default redactor before they are stored.
"""

from __future__ import annotations

import difflib
import re
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..capture import CaptureConfig, capture, not_captured
from ..store import Store
from . import sessions

SOURCE = "claude-code"
PROJECTS_DIR = sessions.claude_projects_dir()
WORKING_WINDOW_MS = 10 * 60 * 1000
HOOK_ACTIVE_WINDOW_MS = 30 * 60 * 1000
_DONE_STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
_INTERRUPT_PREFIX = "[Request interrupted"
_CFG = CaptureConfig()
_NOT_PROMPT_PREFIXES = ("<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>", "<system-reminder>")
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_AGENT_TOOLS = {"Agent", "Task"}


def _encode_project(path: str) -> str:
    return str(Path(path).expanduser().resolve()).replace("/", "-").replace(".", "-").replace("_", "-") if path else ""


def find_transcripts(project_dir: Optional[str] = None, latest: bool = False, limit: int = 20) -> List[Path]:
    """Session transcripts, newest first. ``project_dir`` is a working directory path."""
    root = PROJECTS_DIR
    if not root.exists():
        return []
    if project_dir:
        candidates = {str(Path(project_dir).expanduser()).replace("/", "-"), _encode_project(project_dir)}
        dirs = [root / c for c in candidates if (root / c).is_dir()]
    else:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    files = [f for d in dirs for f in d.glob("*.jsonl") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:1] if latest else files[:limit]


def _ts(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def _read(path: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _cap(v: Any) -> Any:
    return capture(v, _CFG)


def _preview(text: str, n: int = 72) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _prompt_text(rec: Dict[str, Any]) -> Optional[str]:
    """The user's typed prompt for a ``user`` record, or None if it is a tool result / meta."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_NOT_PROMPT_PREFIXES) or text.startswith(_INTERRUPT_PREFIX):
        return None
    if text.startswith("<command-name>"):
        # A slash command such as /model: show it the way the user typed it.
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        cmd = (name.group(1).strip() if name else "/command") + (" " + args.group(1).strip() if args and args.group(1).strip() else "")
        return cmd if cmd.startswith("/") else "/" + cmd
    return text


def _result_output(block: Dict[str, Any]) -> Any:
    content = block.get("content")
    if isinstance(content, list):
        parts: List[Any] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append({"$tg": "not_captured", "reason": "image tool output"})
            else:
                parts.append(b)
        if all(isinstance(p, str) for p in parts):
            return "\n".join(parts)
        return parts
    return content


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}", n=3
    )
    return "".join(l if l.endswith("\n") else l + "\n" for l in lines)


class _Builder:
    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.short = hashlib.sha1(session_id.encode()).hexdigest()[:10]
        self.records: List[Dict[str, Any]] = []
        self.final_text: Optional[Tuple[str, Optional[str], Optional[float]]] = None
        self.finals: List[Tuple[str, Optional[str], Optional[float]]] = []
        self._seq = 0

    def sid(self, *parts: str) -> str:
        return "cc_" + self.short + "_" + "_".join(p.replace("-", "")[:40] for p in parts)

    def seq(self) -> int:
        self._seq += 1
        return self._seq

    def span(self, **fields: Any) -> Dict[str, Any]:
        rec = {"type": "span", "v": 1, "run_id": self.run_id, "attributes": {}, "seq": self.seq(), **fields}
        self.records.append(rec)
        return rec

    def build_conversation(
        self,
        recs: List[Dict[str, Any]],
        parent_id: Optional[str],
        agent: Optional[str],
        subagents: Dict[str, Path],
        scope: str,
        depth: int = 0,
        container: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Emit turn/llm/tool spans for one conversation. Returns its (start, end) times."""
        tool_spans: Dict[str, Dict[str, Any]] = {}
        llm_spans: Dict[str, Dict[str, Any]] = {}
        turn: Optional[Dict[str, Any]] = None
        turn_n = 0
        last_ts: Optional[float] = None
        first_ts: Optional[float] = None
        is_sub = depth > 0
        if container is not None:
            parent_id = container["id"]

        def ensure_turn(ts: Optional[float]) -> Dict[str, Any]:
            nonlocal turn, turn_n
            if container is not None:
                # Subagents have a single task: their steps hang directly off the agent span.
                return container
            if turn is None:
                turn_n += 1
                turn = self.span(
                    id=self.sid(scope, f"turn{turn_n}"), parent_id=parent_id, kind="step",
                    name=f"Turn {turn_n}", status="ok", started_at=ts, ended_at=ts, agent=agent,
                )
            return turn

        for rec in recs:
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            ts = _ts(rec.get("timestamp"))
            if ts is not None:
                first_ts = ts if first_ts is None else min(first_ts, ts)
            prompt = _prompt_text(rec)
            if prompt is not None:
                if container is None:
                    turn = None
                    t = ensure_turn(ts)
                    t["input"] = _cap(prompt)
                    t["name"] = f"Turn {turn_n}: {_preview(prompt)}"
                last_ts = ts or last_ts
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if rtype == "user":
                if not (isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                    if turn is not None and _is_interrupt(rec):
                        turn.setdefault("attributes", {})["interrupted"] = True
                        if ts is not None:
                            turn["ended_at"] = ts
                        for sp in tool_spans.values():  # calls cut short by the interrupt
                            if sp.get("status") == "running":
                                sp["status"] = "cancelled"
                                sp["ended_at"] = ts
                                sp.setdefault("attributes", {})["note"] = "interrupted by the user"
                    continue  # meta / caveat records carry no tool results and are not turns
                t = ensure_turn(ts)
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_result":
                            if isinstance(b, dict) and b.get("type") == "text" and "interrupted" in (b.get("text") or "").lower():
                                t.setdefault("attributes", {})["interrupted"] = True
                            continue
                        sp = tool_spans.get(b.get("tool_use_id", ""))
                        if sp is None:
                            continue
                        sp["ended_at"] = ts
                        if sp.get("kind") != "delegation" or "output" not in sp:
                            sp["output"] = _cap(_result_output(b))
                        if b.get("is_error"):
                            sp["status"] = "error"
                            out = _result_output(b)
                            text = out if isinstance(out, str) else json.dumps(out, default=str)
                            sp["error"] = {"type": "ToolError", "message": _cap(text[:2000]) or "tool returned an error"}
                        else:
                            sp["status"] = "ok"
                if ts is not None:
                    t["ended_at"] = ts
                last_ts = ts or last_ts
                continue
            # assistant
            t = ensure_turn(ts)
            mid = msg.get("id") or rec.get("uuid") or f"m{self._seq}"
            sp = llm_spans.get(mid)
            if sp is None:
                sp = self.span(
                    id=self.sid(scope, "llm", mid), parent_id=t["id"], kind="llm",
                    name=f"{msg.get('model') or 'model'}", status="ok",
                    started_at=last_ts if last_ts is not None else ts, ended_at=ts, agent=agent,
                )
                if msg.get("model"):
                    sp["model"] = msg["model"]
                sp["output"] = {"role": "assistant", "content": []}
                sp["input"] = not_captured("Claude Code transcripts do not include the full request context")
                llm_spans[mid] = sp
            sp["ended_at"] = ts or sp["ended_at"]
            usage = msg.get("usage") or {}
            if usage:
                u = {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_tokens": usage.get("cache_read_input_tokens"),
                    "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                }
                u = {k: v for k, v in u.items() if isinstance(v, int)}
                if "input_tokens" in u or "output_tokens" in u:
                    u["total_tokens"] = u.get("input_tokens", 0) + u.get("output_tokens", 0)
                sp["usage"] = u
            if msg.get("stop_reason"):
                sp["attributes"]["stop_reason"] = msg["stop_reason"]
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt in ("thinking", "redacted_thinking"):
                    sp["attributes"]["thinking"] = True
                    sp["output"]["content"].append({"type": "thinking", **not_captured("thinking is not exported")})
                elif bt == "text":
                    text = b.get("text") or ""
                    sp["output"]["content"].append({"type": "text", "text": _cap(text)})
                    if text.strip() and not is_sub:
                        self.final_text = (text, sp["id"], ts)
                        self.finals.append(self.final_text)
                elif bt == "tool_use":
                    self._tool(b, sp, t, ts, agent, subagents, scope, depth, tool_spans)
            if ts is not None:
                t["ended_at"] = ts
            last_ts = ts or last_ts
        return first_ts, last_ts

    def _tool(self, b: Dict[str, Any], llm: Dict[str, Any], turn: Dict[str, Any], ts: Optional[float], agent: Optional[str], subagents: Dict[str, Path], scope: str, depth: int, tool_spans: Dict[str, Dict[str, Any]]) -> None:
        tid = b.get("id") or f"t{self._seq}"
        name = b.get("name") or "tool"
        inp = b.get("input") if isinstance(b.get("input"), dict) else {"input": b.get("input")}
        llm["output"]["content"].append({"type": "tool_use", "name": name, "id": tid})
        sub_path = subagents.get(tid)
        kind = "delegation" if name in _AGENT_TOOLS else "tool"
        label = name
        if name in _AGENT_TOOLS and inp.get("description"):
            label = f"{name}: {_preview(str(inp['description']), 48)}"
        elif name.startswith("mcp__"):
            label = name.split("__")[-1]
        sp = self.span(
            id=self.sid(scope, "tool", tid), parent_id=turn["id"], kind=kind, name=label, status="running",
            started_at=ts, ended_at=None, agent=agent, input=_cap(inp), depends_on=[llm["id"]],
        )
        sp["attributes"]["tool"] = name
        sp["attributes"]["tool_use_id"] = tid
        tool_spans[tid] = sp
        self._edit_artifact(name, inp, sp, ts)
        if sub_path is not None and depth < 4:
            meta = _read_meta(sub_path)
            sub_recs = _read(sub_path)
            agent_name = meta.get("agentType") or "subagent"
            ag = self.span(
                id=self.sid(scope, "agent", tid), parent_id=sp["id"], kind="agent",
                name=meta.get("description") or agent_name, status="ok", started_at=ts, ended_at=ts,
                agent=agent_name, input=_cap(inp.get("prompt")) if inp.get("prompt") else None,
            )
            ag["attributes"]["agent_type"] = agent_name
            start, end = self.build_conversation(sub_recs, ag["id"], agent_name, {}, scope + "_" + tid[-8:], depth + 1, container=ag)
            ag["started_at"] = start or ts
            ag["ended_at"] = end or ts
            last_text = _last_text(sub_recs)
            if last_text:
                ag["output"] = _cap(last_text)
                sp["output"] = _cap(last_text)

    def _edit_artifact(self, name: str, inp: Dict[str, Any], sp: Dict[str, Any], ts: Optional[float]) -> None:
        if name not in _EDIT_TOOLS:
            return
        path = str(inp.get("file_path") or inp.get("notebook_path") or "file")
        short = path.split("/")[-1]
        if name == "Write":
            kind, content, title = "code", inp.get("content") or "", f"write {short}"
        elif name == "MultiEdit":
            edits = inp.get("edits") or []
            content = "".join(_diff(path, e.get("old_string") or "", e.get("new_string") or "") for e in edits if isinstance(e, dict))
            kind, title = "diff", f"edit {short}"
        elif name == "Edit":
            kind, content, title = "diff", _diff(path, inp.get("old_string") or "", inp.get("new_string") or ""), f"edit {short}"
        else:
            kind, content, title = "code", inp.get("new_source") or "", f"edit {short}"
        aid = "cc_art_" + sp["id"][3:]
        self.records.append({
            "type": "artifact", "v": 1, "id": aid, "run_id": self.run_id, "span_id": sp["id"], "kind": kind,
            "name": title, "content": _cap(content), "size": len(content), "uri": f"file://{path}" if path.startswith("/") else path,
            "created_at": ts, "metadata": {"path": path, "tool": name},
        })


def _read_meta(sub_path: Path) -> Dict[str, Any]:
    meta_path = sub_path.with_name(sub_path.name[: -len(".jsonl")] + ".meta.json")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _last_text(recs: List[Dict[str, Any]]) -> Optional[str]:
    for rec in reversed(recs):
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()]
            if texts:
                return texts[-1]
    return None


def _is_interrupt(rec: Dict[str, Any]) -> bool:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip().startswith(_INTERRUPT_PREFIX)
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip().startswith(_INTERRUPT_PREFIX) for b in content)
    return False


def _turn_state(recs: List[Dict[str, Any]]) -> Tuple[bool, Optional[float]]:
    """(last turn complete?, timestamp of the last conversational record) for a main transcript."""
    complete = True
    last_ts: Optional[float] = None
    for rec in recs:
        rtype = rec.get("type")
        if rtype not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        ts = _ts(rec.get("timestamp"))
        if rtype == "assistant":
            msg = rec.get("message") or {}
            reason = msg.get("stop_reason")
            if reason in _DONE_STOP_REASONS:
                complete = True
            elif reason == "tool_use" or any(isinstance(b, dict) and b.get("type") == "tool_use" for b in msg.get("content") or [] if isinstance(msg.get("content"), list)):
                complete = False
            else:
                complete = False  # still streaming, or no stop reason recorded yet
            last_ts = ts or last_ts
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            complete = False  # the model has a tool result to respond to
            last_ts = ts or last_ts
        elif _is_interrupt(rec):
            complete = True
            last_ts = ts or last_ts
        else:
            prompt = _prompt_text(rec)
            if prompt is None:
                continue
            # Built-in slash commands (/model, /clear…) run locally and get no model reply.
            complete = prompt.startswith("/")
            last_ts = ts or last_ts
    return complete, last_ts


def _subagents(path: Path) -> Dict[str, Path]:
    """Map Agent tool_use ids → subagent transcript paths for one session."""
    d = path.with_suffix("") / "subagents"
    out: Dict[str, Path] = {}
    if not d.is_dir():
        return out
    for sub in d.glob("agent-*.jsonl"):
        meta = _read_meta(sub)
        if meta.get("toolUseId"):
            out[meta["toolUseId"]] = sub
    return out


def transcript_to_records(
    path: Path,
    *,
    now: Optional[float] = None,
    hook_events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Turn one session transcript into records. Pure: same files + hooks + ``now`` ⇒ same output."""
    path = Path(path)
    now = time.time() * 1000.0 if now is None else now
    recs = _read(path)
    session_id = next((r.get("sessionId") for r in recs if r.get("sessionId")), None) or path.stem
    run_id = f"cc_{session_id}"
    title = next((r.get("aiTitle") for r in reversed(recs) if r.get("type") == "ai-title" and r.get("aiTitle")), None)
    prompts = [p for p in (_prompt_text(r) for r in recs) if p]
    first_prompt = next((p for p in prompts if not p.startswith("/")), prompts[0] if prompts else None)
    cwd = next((r.get("cwd") for r in recs if r.get("cwd")), None)
    branch = next((r.get("gitBranch") for r in recs if r.get("gitBranch")), None)
    version = next((r.get("version") for r in recs if r.get("version")), None)
    if hook_events is None:
        hook_events = sessions.read_hook_events(SOURCE, session_id)
    subs = _subagents(path)
    b = _Builder(session_id, run_id)
    start, end = b.build_conversation(recs, None, None, subs, "s")
    activity = _activity(recs, b.records, hook_events, now, [path, *subs.values()])
    working = activity == "working"
    turns = [r for r in b.records if r["type"] == "span" and r["kind"] == "step" and r.get("parent_id") is None]
    last_turn = turns[-1] if turns else None
    running = [r for r in b.records if r["type"] == "span" and r.get("status") == "running"]
    if working:
        # Live: unanswered calls stay running and the open turn (and any running subagent) stays open.
        for r in running:
            r["ended_at"] = None
            for ag in b.records:
                if ag["type"] == "span" and ag.get("parent_id") == r["id"] and ag["kind"] == "agent":
                    ag["status"], ag["ended_at"] = "running", None
        if last_turn is not None:
            last_turn["status"], last_turn["ended_at"] = "running", None
    else:
        # Tool calls that never got a result (session ended / interrupted) are not "running" forever.
        for r in running:
            r["status"] = "cancelled"
            r["ended_at"] = r.get("ended_at") or end
            r.setdefault("attributes", {})["note"] = "no tool_result recorded in the transcript"
        if activity == "interrupted" and last_turn is not None:
            last_turn["status"] = "cancelled"
            last_turn.setdefault("attributes", {})["note"] = "the turn did not complete"
    times = [r.get("started_at") for r in b.records if r.get("started_at")] + [r.get("ended_at") for r in b.records if r.get("ended_at")]
    run: Dict[str, Any] = {
        "type": "run", "v": 1, "id": run_id,
        "name": _preview(title or first_prompt or f"Claude Code session {session_id[:8]}", 96),
        "project": Path(cwd).name if cwd else path.parent.name, "session": session_id,
        "status": "running" if working else "ok",
        "started_at": min(times) if times else start, "ended_at": None if working else (max(times) if times else end),
        "source": "claude-code", "tags": ["claude-code"],
        "metadata": {
            "agent_tool": "claude-code", "activity": activity, "timing": "exact", "cwd": cwd,
            "git_branch": branch, "claude_code_version": version, "transcript": str(path),
        },
    }
    if first_prompt:
        run["input"] = _cap(first_prompt)
    out: List[Dict[str, Any]] = [run]
    if working and b.final_text:
        # Mid-turn, the latest reply is a progress note, not the answer: use the last finished turn's reply.
        spans = {r["id"]: r for r in b.records if r.get("type") == "span"}
        roots = [r for r in b.records if r.get("type") == "span" and not r.get("parent_id")]
        current = max(roots, key=lambda t: (t.get("started_at") or 0, t.get("seq") or 0))["id"] if roots else None

        def turn_of(sid: Optional[str]) -> Optional[str]:
            seen = set()
            while sid and sid in spans and spans[sid].get("parent_id") and sid not in seen:
                seen.add(sid)
                sid = spans[sid]["parent_id"]
            return sid

        b.final_text = next((f for f in reversed(b.finals) if turn_of(f[1]) != current), None)
    if b.final_text:
        text, span_id, ts = b.final_text
        aid = f"cc_art_{b.short}_final"
        out_art = {
            "type": "artifact", "v": 1, "id": aid, "run_id": run_id, "span_id": span_id, "kind": "markdown",
            "name": "Final response", "content": _cap(text), "size": len(text), "created_at": ts, "final": True, "metadata": {},
        }
        run["final_artifact_id"] = aid
        run["output"] = _cap(_preview(text, 400))
        b.records.append(out_art)
    return out + b.records


def _activity(
    recs: List[Dict[str, Any]],
    built: List[Dict[str, Any]],
    hook_events: List[Dict[str, Any]],
    now: float,
    files: List[Path],
) -> str:
    """working · idle · interrupted · ended — see the liveness contract in the module docs."""
    complete, last_ts = _turn_state(recs)
    if any(r["type"] == "span" and r.get("status") == "running" and r.get("parent_id") for r in built):
        complete = False
    evs = [e for e in hook_events if isinstance(e.get("ts"), (int, float))]
    ref = last_ts or 0.0
    if any(e.get("event") == "session_end" and e["ts"] >= ref - 1000 for e in evs):
        return "ended"
    last = evs[-1] if evs else None
    if last and last.get("event") == "stop" and last["ts"] >= ref - 1000:
        complete = True
    hook_active = bool(
        last and last.get("event") in ("prompt", "pre_tool", "post_tool", "subagent_stop")
        and last["ts"] >= ref - 1000 and now - last["ts"] <= HOOK_ACTIVE_WINDOW_MS
    )
    if complete and not hook_active:
        return "idle"
    mtimes = []
    for f in files:
        try:
            mtimes.append(f.stat().st_mtime * 1000.0)
        except OSError:
            pass
    recent = bool(mtimes) and now - max(mtimes) <= WORKING_WINDOW_MS
    return "working" if (recent or hook_active) else "interrupted"


def import_transcripts(paths: Iterable[Path], store: Store) -> List[str]:
    """Import (or re-sync) transcripts; returns run ids. Unchanged records are not rewritten."""
    ids = []
    for p in paths:
        records = transcript_to_records(Path(p))
        if len(records) <= 1:
            continue
        # sync_run rewrites only what changed and drops records a newer import no longer produces.
        store.sync_run(records[0]["id"], records)
        ids.append(records[0]["id"])
    return ids
