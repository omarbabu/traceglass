"""Import Kiro CLI sessions as Traceglass runs — closed or still running.

Kiro CLI keeps each session as two files in ``~/.kiro/sessions/cli`` (``$KIRO_HOME/.kiro`` if set):

* ``<session_id>.jsonl`` — the event log: ``Prompt``, ``AssistantMessage`` (text, thinking,
  ``toolUse``), ``ToolResults``, ``Clear`` and ``Compaction`` records, appended as they happen;
* ``<session_id>.json`` — session metadata, including one entry per completed user turn with
  its end time, duration, model, token counts, request count and end reason.

Mapping:

* session → run ``kiro_<session_id>``; user turn → ``step`` span "Turn N: …" carrying the
  turn's token usage (Kiro records usage per turn, not per model request, so none is invented);
* assistant message → ``llm`` span (thinking is never imported); ``toolUse`` → ``tool`` span
  (``use_subagent``/``delegate`` → ``delegation``) with its stated purpose and result;
* ``write`` → ``diff`` (strReplace / insert) or ``code`` (create) artifacts; last reply → final
  ``markdown`` artifact; ``Clear`` / ``Compaction`` → state events.

Timing honesty: only prompts and turn ends are timestamped, so steps inside a turn are spread
evenly across it and marked ``attributes.timing = "estimated"``. When ``traceglass hook kiro``
is installed, its sidecar supplies exact tool start/end times and those spans become ``exact``.

Subagent sessions carry no link to the session that spawned them. A parent is inferred only
when exactly one other session in the same directory was mid-turn when the subagent session
was created; such runs get ``metadata.parent_inferred = True``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..capture import CaptureConfig, capture, not_captured
from . import sessions

SOURCE = "kiro"
SESSIONS_DIR: Optional[Path] = None  # override (tests); defaults to sessions.kiro_sessions_dir()
WORKING_WINDOW_MS = 10 * 60 * 1000
HOOK_ACTIVE_WINDOW_MS = 30 * 60 * 1000
FORMAT_VERSIONS = {"v1"}
_KNOWN_KINDS = {"Prompt", "AssistantMessage", "ToolResults", "Clear", "Compaction"}
_DELEGATION_TOOLS = {"use_subagent", "delegate", "subagent"}
_CFG = CaptureConfig()


def _dir() -> Path:
    return Path(SESSIONS_DIR) if SESSIONS_DIR is not None else sessions.kiro_sessions_dir()


def _cap(v: Any) -> Any:
    return capture(v, _CFG)


def _preview(text: str, n: int = 72) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _iso_ms(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def _mtime_ms(*paths: Path) -> Optional[float]:
    best = None
    for p in paths:
        try:
            m = p.stat().st_mtime * 1000.0
        except OSError:
            continue
        best = m if best is None else max(best, m)
    return best


def _read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    out: List[Dict[str, Any]] = []
    bad = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1  # e.g. a partially written last line while the session is live
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out, bad


def _read_meta(jsonl_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(jsonl_path.with_suffix(".json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _turn_metas(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        turns = meta["session_state"]["conversation_metadata"]["user_turn_metadatas"]
    except (KeyError, TypeError):
        return []
    return [t for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []


def _turn_window(t: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    end = _iso_ms(t.get("end_timestamp"))
    d = t.get("turn_duration") or {}
    try:
        dur = float(d.get("secs", 0)) * 1000.0 + float(d.get("nanos", 0)) / 1e6
    except (TypeError, ValueError, AttributeError):
        dur = None
    start = end - dur if end is not None and dur is not None else None
    return start, end


def _digest(tool_input: Any) -> Optional[str]:
    return sessions.input_digest(tool_input)


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    return "".join(l if l.endswith("\n") else l + "\n" for l in lines)


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(c.get("data")) for c in content if isinstance(c, dict) and c.get("kind") == "text" and isinstance(c.get("data"), str))
    return content if isinstance(content, str) else ""


# --------------------------------------------------------------------------------------- discovery
def _session_info(jsonl: Path) -> Dict[str, Any]:
    meta = _read_meta(jsonl)
    windows = [w for w in (_turn_window(t) for t in _turn_metas(meta)) if w[0] is not None and w[1] is not None]
    return {
        "session_id": meta.get("session_id") or jsonl.stem,
        "cwd": meta.get("cwd"),
        "created_at": _iso_ms(meta.get("created_at")),
        "reason": meta.get("session_created_reason"),
        "windows": windows,
        "path": str(jsonl),
    }


_INDEX_CACHE: Dict[str, Dict[str, Tuple[float, Dict[str, Any]]]] = {}


def build_index(sessions_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """``{session_id: {cwd, created_at, reason, windows, path}}`` for every session in a folder.

    Incremental: a session's metadata is re-read only when its ``.json`` changed, so the
    watcher can call this on every sync cheaply.
    """
    d = Path(sessions_dir) if sessions_dir else _dir()
    cache = _INDEX_CACHE.setdefault(str(d), {})
    seen = set()
    try:
        entries = list(os.scandir(d))
    except OSError:
        return {}
    for e in entries:
        if not e.name.endswith(".jsonl"):
            continue
        jsonl = Path(e.path)
        try:
            m = jsonl.with_suffix(".json").stat().st_mtime
        except OSError:
            m = 0.0
        seen.add(e.path)
        hit = cache.get(e.path)
        if hit is None or hit[0] != m:
            cache[e.path] = (m, _session_info(jsonl))
    for k in list(cache):
        if k not in seen:
            del cache[k]
    return {info["session_id"]: info for _, info in cache.values()}


def find_sessions(cwd: Optional[str] = None, latest: bool = False, limit: int = 20, include_subagents: bool = True) -> List[Path]:
    """Kiro session logs (``.jsonl``) with content, newest first; optionally only those for ``cwd``."""
    d = _dir()
    if not d.is_dir():
        return []
    want = str(Path(cwd).expanduser().resolve()) if cwd else None
    out: List[Tuple[float, Path]] = []
    for jsonl in d.glob("*.jsonl"):
        meta = _read_meta(jsonl)
        try:
            empty = jsonl.stat().st_size == 0
        except OSError:
            continue
        if empty and not _turn_metas(meta):
            continue
        if not include_subagents and meta.get("session_created_reason") == "subagent":
            continue
        if want:
            mc = meta.get("cwd")
            if not mc or str(Path(mc).expanduser().resolve()) != want:
                continue
        out.append((_mtime_ms(jsonl, jsonl.with_suffix(".json")) or 0.0, jsonl))
    out.sort(key=lambda x: x[0], reverse=True)
    paths = [p for _, p in out]
    return paths[:1] if latest else paths[:limit]


def infer_parent(info: Dict[str, Any], index: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The one session (same cwd) that was mid-turn when ``info``'s session was created, if unique."""
    created = info.get("created_at")
    if created is None or not info.get("cwd"):
        return None
    hits = [
        sid
        for sid, other in index.items()
        if sid != info["session_id"]
        and other.get("cwd") == info["cwd"]
        and any(s <= created <= e for s, e in other.get("windows") or [])
    ]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------------------- import
class _Turn:
    def __init__(self) -> None:
        self.prompt: Optional[Dict[str, Any]] = None
        self.items: List[Tuple[str, Dict[str, Any]]] = []  # ("assistant"|"results"|"state", record)
        self.meta: Optional[Dict[str, Any]] = None
        self.ids: List[str] = []


def _segment(recs: List[Dict[str, Any]]) -> Tuple[List[_Turn], List[Dict[str, Any]], int]:
    """Split the log into turns at each Prompt. Returns (turns, pre-turn state records, warnings)."""
    turns: List[_Turn] = []
    pre: List[Dict[str, Any]] = []
    warnings = 0
    for r in recs:
        kind = r.get("kind")
        if kind not in _KNOWN_KINDS:
            warnings += 1
            continue
        if r.get("version") not in FORMAT_VERSIONS:
            warnings += 1  # newer format: still read what we recognise
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        if kind == "Prompt":
            t = _Turn()
            t.prompt = data
            turns.append(t)
        elif not turns:
            if kind in ("Clear", "Compaction"):
                pre.append(r)
            continue
        else:
            turns[-1].items.append(({"AssistantMessage": "assistant", "ToolResults": "results"}.get(kind, "state"), r))
        if data.get("message_id") and turns:
            turns[-1].ids.append(data["message_id"])
    return turns, pre, warnings


def _match_metas(turns: List[_Turn], metas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach turn metadata to log turns by message id, then by order. Returns unmatched metas."""
    unmatched = list(range(len(metas)))
    for t in turns:
        ids = set(t.ids)
        for i in unmatched:
            if ids & set(metas[i].get("message_ids") or []):
                t.meta = metas[i]
                unmatched.remove(i)
                break
    # Order fallback for turns whose ids were not recorded in the metadata.
    rest = [t for t in turns if t.meta is None]
    for t, i in zip(rest, list(unmatched)):
        mids = metas[i].get("message_ids") or []
        if mids and set(mids) & {mid for tt in turns for mid in tt.ids}:
            continue  # belongs to another (already matched) turn
        t.meta = metas[i]
        unmatched.remove(i)
    return [metas[i] for i in unmatched]


def _place(steps: List[Dict[str, Any]], t0: float, t1: float) -> None:
    """Fill ``started_at``/``ended_at`` for steps lacking exact times by spreading them evenly
    between the surrounding known times."""
    n = len(steps)
    i = 0
    cursor = t0
    while i < n:
        if steps[i].get("_exact"):
            cursor = max(cursor, steps[i]["ended_at"] if steps[i].get("ended_at") is not None else steps[i]["started_at"])
            i += 1
            continue
        j = i
        while j < n and not steps[j].get("_exact"):
            j += 1
        right = steps[j]["started_at"] if j < n else t1
        right = max(right, cursor)
        width = (right - cursor) / (j - i) if j > i else 0.0
        for k in range(i, j):
            s = cursor + width * (k - i)
            steps[k]["started_at"] = s
            if steps[k].get("_open"):
                steps[k]["ended_at"] = None
            else:
                steps[k]["ended_at"] = s + width
        cursor = right
        i = j


class _Builder:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.run_id = f"kiro_{session_id}"
        self.short = hashlib.sha1(session_id.encode()).hexdigest()[:10]
        self.records: List[Dict[str, Any]] = []
        self._seq = 0

    def sid(self, *parts: str) -> str:
        return "kiro_" + self.short + "_" + "_".join(str(p).replace("-", "")[:48] for p in parts)

    def seq(self) -> int:
        self._seq += 1
        return self._seq


def session_to_records(
    jsonl_path: Path,
    *,
    now: Optional[float] = None,
    hook_events: Optional[List[Dict[str, Any]]] = None,
    index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    path = Path(jsonl_path)
    if path.suffix == ".json":
        path = path.with_suffix(".jsonl")
    now = time.time() * 1000.0 if now is None else now
    recs, bad_lines = _read_jsonl(path)
    meta = _read_meta(path)
    session_id = meta.get("session_id") or path.stem
    if hook_events is None:
        hook_events = sessions.read_hook_events(SOURCE, session_id)
    state = meta.get("session_state") if isinstance(meta.get("session_state"), dict) else {}
    rts = state.get("rts_model_state") if isinstance(state.get("rts_model_state"), dict) else {}
    model_info = rts.get("model_info") if isinstance(rts.get("model_info"), dict) else {}
    session_model = model_info.get("model_id") or model_info.get("model_name")
    agent_name = state.get("agent_name")
    cwd = meta.get("cwd")
    mtime = _mtime_ms(path, path.with_suffix(".json"))

    turns, pre_state, warnings = _segment(recs)
    metas = _turn_metas(meta)
    meta_only = _match_metas(turns, metas)
    b = _Builder(session_id)

    # ---- hook timing ------------------------------------------------------------------
    hk_prompts = [e for e in hook_events if e.get("event") == "prompt" and isinstance(e.get("ts"), (int, float))]
    hk_pre = [e for e in hook_events if e.get("event") == "pre_tool" and isinstance(e.get("ts"), (int, float))]
    hk_post = [e for e in hook_events if e.get("event") == "post_tool" and isinstance(e.get("ts"), (int, float))]
    hk_stops = [e["ts"] for e in hook_events if e.get("event") in ("stop", "session_end") and isinstance(e.get("ts"), (int, float))]
    used_pre: set = set()
    used_post: set = set()

    def hook_tool(tool_use_id: str, name: str, digest: Optional[str], lo: float, hi: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
        def pick(evs: List[Dict[str, Any]], used: set) -> Optional[Dict[str, Any]]:
            for i, e in enumerate(evs):
                if i not in used and e.get("tool_use_id") and e["tool_use_id"] == tool_use_id:
                    used.add(i)
                    return e
            cands = [
                (i, e) for i, e in enumerate(evs)
                if i not in used and not e.get("tool_use_id") and e.get("tool_name") == name
                and lo - 2000 <= e["ts"] <= (hi if hi is not None else float("inf")) + 2000
            ]
            if digest:
                exact = [c for c in cands if c[1].get("tool_input_digest") == digest]
                cands = exact or cands
            if not cands:
                return None
            i, e = cands[0]
            used.add(i)
            return e

        pre = pick(hk_pre, used_pre)
        post = pick(hk_post, used_post)
        return (pre["ts"] if pre else None), (post["ts"] if post else None)

    # ---- build turns --------------------------------------------------------------------
    all_turns: List[Tuple[float, Optional[_Turn], Optional[Dict[str, Any]]]] = []
    for t in turns:
        p_ts = (t.prompt or {}).get("meta", {}).get("timestamp") if isinstance((t.prompt or {}).get("meta"), dict) else None
        start = float(p_ts) * 1000.0 if isinstance(p_ts, (int, float)) else None
        if t.meta:
            ms, _ = _turn_window(t.meta)
            start = ms if ms is not None else start
        all_turns.append((start if start is not None else (mtime or now), t, t.meta))
    for m in meta_only:
        ms, _ = _turn_window(m)
        all_turns.append((ms if ms is not None else 0.0, None, m))
    all_turns.sort(key=lambda x: x[0])

    final: Optional[Tuple[str, str, Optional[float]]] = None
    finals: List[Tuple[str, str]] = []  # every non-empty reply, for picking the last *completed* answer
    exact_spans = 0
    estimated_spans = 0
    open_turn: Optional[Dict[str, Any]] = None
    open_tools: List[Dict[str, Any]] = []
    first_prompt: Optional[str] = None
    last_turn_complete = True
    events_out: List[Dict[str, Any]] = []

    for n, (start, t, tm) in enumerate(all_turns, 1):
        is_last = n == len(all_turns)
        next_start = all_turns[n][0] if not is_last else None
        prompt_text = _text_of((t.prompt or {}).get("content")) if t else ""
        if prompt_text and first_prompt is None:
            first_prompt = prompt_text
        m_start, m_end = _turn_window(tm) if tm else (None, None)
        # A hook-reported prompt near the logged one gives sub-second precision.
        hk = next((e["ts"] for e in hk_prompts if abs(e["ts"] - start) <= 3000), None) if m_start is None else None
        if hk is not None:
            start = hk
        end = m_end
        end_estimated = False
        if end is None:
            later_stop = [x for x in hk_stops if x >= start and (next_start is None or x <= next_start)]
            end = min(later_stop) if later_stop else None
        if end is None and next_start is not None:
            # A later prompt exists, so this turn finished; its end time just wasn't recorded.
            end, end_estimated = next_start, True
        is_open = end is None
        turn = {
            "type": "span", "v": 1, "run_id": b.run_id, "id": b.sid("t", n), "parent_id": None, "kind": "step",
            "name": f"Turn {n}: {_preview(prompt_text)}" if prompt_text else f"Turn {n}", "status": "ok",
            "started_at": start, "ended_at": end, "seq": b.seq(), "agent": agent_name,
            "attributes": {"timing": "estimated" if end_estimated else "exact"},
        }
        turn["input"] = _cap(prompt_text) if prompt_text else not_captured("the prompt is not in the session log")
        if tm:
            usage = {
                "input_tokens": tm.get("input_token_count"),
                "output_tokens": tm.get("output_token_count"),
                "cache_read_tokens": tm.get("cache_read_input_token_count"),
                "cache_write_tokens": tm.get("cache_write_input_token_count"),
            }
            usage = {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)}
            if "input_tokens" in usage or "output_tokens" in usage:
                usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            if usage:
                turn["usage"] = usage
            if tm.get("model"):
                turn["model"] = tm["model"]
            attrs = turn["attributes"]
            for src, dst in (("total_request_count", "requests"), ("number_of_cycles", "cycles"), ("end_reason", "end_reason"),
                             ("request_attempts", "request_attempts"), ("final_context_usage_percentage", "context_usage_percentage")):
                if tm.get(src) is not None:
                    attrs[dst] = tm[src]
            credits = [u for u in tm.get("metering_usage") or [] if isinstance(u, dict)]
            if credits:
                attrs["credits"] = [{"value": u.get("value"), "unit": u.get("unitPlural") or u.get("unit")} for u in credits]
            reason = tm.get("end_reason")
            if reason == "Error":
                turn["status"] = "error"
                err = ((tm.get("result") or {}).get("Err") or {})
                stream = err.get("Stream") if isinstance(err, dict) else None
                kind = (stream or {}).get("kind") if isinstance(stream, dict) else None
                data = (kind or {}).get("data") if isinstance(kind, dict) else None
                turn["error"] = {
                    "type": str((kind or {}).get("kind") or "TurnError") if isinstance(kind, dict) else "TurnError",
                    "message": _cap(str((data or {}).get("message") or (data or {}).get("reason_code") or "the turn ended with an error")) if isinstance(data, dict) else "the turn ended with an error",
                }
            elif reason in ("Cancelled", "ToolUseRejected"):
                turn["status"] = "cancelled"
                attrs["note"] = "tool use rejected by the user" if reason == "ToolUseRejected" else "turn cancelled"
        b.records.append(turn)

        # Steps inside the turn.
        steps: List[Dict[str, Any]] = []
        tools: Dict[str, Dict[str, Any]] = {}
        model = (tm or {}).get("model") or session_model
        for kind, rec in (t.items if t else []):
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            if kind == "assistant":
                mid = data.get("message_id") or f"m{len(steps)}"
                llm = {
                    "type": "span", "v": 1, "run_id": b.run_id, "id": b.sid("m", mid), "parent_id": turn["id"], "kind": "llm",
                    "name": model or "model", "status": "ok", "started_at": None, "ended_at": None, "seq": b.seq(),
                    "agent": agent_name, "attributes": {"timing": "estimated"},
                    "input": not_captured("Kiro session logs do not include the full request context"),
                    "output": {"role": "assistant", "content": []},
                }
                if model:
                    llm["model"] = model
                steps.append(llm)
                for blk in data.get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    bk, bd = blk.get("kind"), blk.get("data")
                    if bk == "text" and isinstance(bd, str):
                        llm["output"]["content"].append({"type": "text", "text": _cap(bd)})
                        if bd.strip():
                            final = (bd, llm["id"], None)
                            finals.append((bd, llm["id"]))
                    elif bk == "thinking":
                        llm["attributes"]["thinking"] = True
                        llm["output"]["content"].append({"type": "thinking", **not_captured("thinking is not exported")})
                    elif bk == "toolUse" and isinstance(bd, dict):
                        tid = str(bd.get("toolUseId") or f"u{len(steps)}")
                        name = str(bd.get("name") or "tool")
                        raw = bd.get("input") if isinstance(bd.get("input"), dict) else {"input": bd.get("input")}
                        purpose = raw.get("__tool_use_purpose")
                        inp = {k: v for k, v in raw.items() if k != "__tool_use_purpose"}
                        llm["output"]["content"].append({"type": "tool_use", "name": name, "id": tid})
                        tool = {
                            "type": "span", "v": 1, "run_id": b.run_id, "id": b.sid("u", tid), "parent_id": turn["id"],
                            "kind": "delegation" if name in _DELEGATION_TOOLS else "tool", "name": name, "status": "running",
                            "started_at": None, "ended_at": None, "seq": b.seq(), "agent": agent_name,
                            "input": _cap(inp), "depends_on": [llm["id"]],
                            "attributes": {"tool": name, "tool_use_id": tid, "timing": "estimated"},
                            "_digest": _digest(raw),
                        }
                        if purpose:
                            tool["attributes"]["purpose"] = _cap(str(purpose))
                        steps.append(tool)
                        tools[tid] = tool
                        _write_artifact(b, name, inp, tool)
            elif kind == "results":
                extra = data.get("results") if isinstance(data.get("results"), dict) else {}
                for blk in data.get("content") or []:
                    if not isinstance(blk, dict) or blk.get("kind") != "toolResult" or not isinstance(blk.get("data"), dict):
                        continue
                    rd = blk["data"]
                    tool = tools.get(str(rd.get("toolUseId")))
                    if tool is None:
                        continue
                    tool["output"] = _cap(_result_output(rd.get("content")))
                    st = rd.get("status")
                    x = extra.get(str(rd.get("toolUseId"))) if extra else None
                    xres = (x or {}).get("result") if isinstance(x, dict) else None
                    if isinstance(xres, str) and xres.lower() == "cancelled":
                        tool["status"] = "cancelled"
                    elif st == "error":
                        tool["status"] = "error"
                        out = _result_output(rd.get("content"))
                        text = out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)
                        tool["error"] = {"type": "ToolError", "message": _cap(text[:2000]) or "tool returned an error"}
                    else:
                        tool["status"] = "ok"
                    tk = ((x or {}).get("tool") or {}).get("kind") if isinstance(x, dict) and isinstance(x.get("tool"), dict) else None
                    if isinstance(tk, dict) and isinstance(tk.get("Mcp"), dict):
                        if tk["Mcp"].get("serverName"):
                            tool["attributes"]["mcp_server"] = tk["Mcp"]["serverName"]
            else:  # Clear / Compaction
                ev = _state_event(b, rec, turn)
                ev["_after"] = len(steps)
                events_out.append(ev)

        unanswered = [s for s in steps if s["kind"] in ("tool", "delegation") and s["status"] == "running"]
        # The last turn is still open while Kiro has not written its metadata (and no stop hook fired).
        if is_last:
            last_turn_complete = not is_open
        if unanswered and not is_open:
            for s in unanswered:  # the turn finished without a result for this call
                s["status"] = "cancelled"
                s["attributes"]["note"] = "no tool result recorded in the session log"
        if is_open:
            open_turn = turn
            open_tools = unanswered
            for s in unanswered:
                s["_open"] = True

        # Exact times from hooks, then even spreading for the rest.
        t_end = end if end is not None else max(start, mtime or start)
        for s in steps:
            if s["kind"] in ("tool", "delegation"):
                hs, he = hook_tool(s["attributes"]["tool_use_id"], s["name"], s.pop("_digest", None), start, end)
                if hs is not None:
                    s["started_at"] = hs
                    s["ended_at"] = he if he is not None else (None if s.get("_open") else hs)
                    s["_exact"] = True
                    s["attributes"]["timing"] = "exact"
            else:
                s.pop("_digest", None)
        _place(steps, start, t_end)
        for s in steps:
            if s.pop("_exact", False):
                exact_spans += 1
            else:
                estimated_spans += 1
            s.pop("_open", None)
            if s["kind"] == "llm" and final and final[1] == s["id"]:
                final = (final[0], final[1], s["ended_at"])
        for e in events_out:
            if e.get("span_id") == turn["id"] and "_after" in e:
                k = e.pop("_after")
                prev = steps[k - 1] if k > 0 else None
                e["ts"] = (prev.get("ended_at") or prev.get("started_at")) if prev else start
        by_span = {s["id"]: s for s in steps}
        for a in b.records:
            if a["type"] == "artifact" and a.get("created_at") is None and a.get("span_id") in by_span:
                sp = by_span[a["span_id"]]
                a["created_at"] = sp.get("ended_at") or sp.get("started_at")
        b.records.extend(steps)
        if not t and tm:
            # Turn known only from metadata (its log lines were cleared or compacted away).
            ok = (tm.get("result") or {}).get("Ok") if isinstance(tm.get("result"), dict) else None
            txt = _text_of((ok or {}).get("content") if isinstance(ok, dict) else None)
            if not txt and isinstance(ok, dict):
                txt = "\n".join(str((c.get("data") or {}).get("text") or "") for c in ok.get("content") or [] if isinstance(c, dict) and c.get("kind") == "text" and isinstance(c.get("data"), dict))
            if txt:
                turn["output"] = _cap(txt)
            turn["attributes"]["note"] = "turn known from session metadata only"

    # Pre-turn state records (e.g. a Clear before the first prompt) belong to the run.
    for rec in pre_state:
        ev = _state_event(b, rec, None)
        ev["ts"] = all_turns[0][0] if all_turns else mtime
        events_out.append(ev)

    # ---- liveness -----------------------------------------------------------------------
    last_hook = hook_events[-1] if hook_events else None
    hook_active = bool(
        last_hook and last_hook.get("event") in ("prompt", "pre_tool", "post_tool", "agent_spawn")
        and isinstance(last_hook.get("ts"), (int, float)) and now - last_hook["ts"] <= HOOK_ACTIVE_WINDOW_MS
    )
    ended = any(e.get("event") == "session_end" for e in hook_events)
    if ended:
        activity = "ended"
    elif not last_turn_complete and all_turns:
        recent = mtime is not None and now - mtime <= WORKING_WINDOW_MS
        activity = "working" if (recent or hook_active) else "interrupted"
    else:
        activity = "idle"
    working = activity == "working"
    if open_turn is not None:
        if working:
            open_turn["status"] = "running"
            open_turn["ended_at"] = None
        else:
            open_turn["status"] = "cancelled"
            open_turn["attributes"]["note"] = "the turn did not complete"
            kids_end = [r.get("ended_at") for r in b.records if r.get("parent_id") == open_turn["id"] and r.get("ended_at")]
            open_turn["ended_at"] = max(kids_end) if kids_end else open_turn["started_at"]
            for s in open_tools:
                s["status"] = "cancelled"
                s["ended_at"] = s.get("ended_at") or open_turn["ended_at"]
                s["attributes"]["note"] = "no tool result recorded in the session log"

    # ---- run ----------------------------------------------------------------------------
    span_recs = [r for r in b.records if r["type"] == "span"]
    times = [r["started_at"] for r in span_recs if r.get("started_at") is not None] + [r["ended_at"] for r in span_recs if r.get("ended_at") is not None]
    title = meta.get("title")
    timing = "exact" if estimated_spans == 0 else ("mixed" if exact_spans else "estimated")
    tags = ["kiro"]
    run_meta: Dict[str, Any] = {
        "agent_tool": "kiro", "activity": activity, "timing": timing, "cwd": cwd, "agent_name": agent_name,
        "model": model_info.get("model_name") or session_model, "session_created_reason": meta.get("session_created_reason"),
        "context_usage_percentage": rts.get("context_usage_percentage"), "kiro_format_version": state.get("version") or "v1",
        "transcript": str(path),
    }
    if warnings or bad_lines:
        run_meta["warnings"] = warnings + bad_lines
    session_group = session_id
    if meta.get("session_created_reason") == "subagent":
        tags.append("subagent")
        idx = index if index is not None else build_index(path.parent)
        info = idx.get(session_id) or _session_info(path)
        parent = infer_parent(info, idx)
        if parent:
            run_meta["parent_run_id"] = f"kiro_{parent}"
            run_meta["parent_inferred"] = True
            session_group = parent
    run: Dict[str, Any] = {
        "type": "run", "v": 1, "id": b.run_id,
        "name": _preview(title or first_prompt or f"Kiro session {session_id[:8]}", 96),
        "project": Path(cwd).name if cwd else "kiro", "session": session_group,
        "status": "running" if working else "ok",
        "started_at": min(times) if times else _iso_ms(meta.get("created_at")),
        "ended_at": None if working else (max(times) if times else _iso_ms(meta.get("updated_at"))),
        "source": "kiro", "tags": tags, "metadata": {k: v for k, v in run_meta.items() if v is not None},
    }
    if first_prompt:
        run["input"] = _cap(first_prompt)
    out: List[Dict[str, Any]] = [run]
    if working and final:
        # Mid-turn, the latest reply is a progress note, not the answer: use the last finished turn's reply.
        final = _completed_final(b.records, finals)
    if final:
        text, span_id, ts = final
        aid = f"kiro_art_{b.short}_final"
        b.records.append({
            "type": "artifact", "v": 1, "id": aid, "run_id": b.run_id, "span_id": span_id, "kind": "markdown",
            "name": "Final response", "content": _cap(text), "size": len(text), "created_at": ts, "final": True, "metadata": {},
        })
        run["final_artifact_id"] = aid
        run["output"] = _cap(_preview(text, 400))
    return out + b.records + events_out


def _result_output(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    parts: List[Any] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("kind") == "text":
            parts.append(c.get("data") if isinstance(c.get("data"), str) else json.dumps(c.get("data"), default=str))
        elif c.get("kind") == "json":
            parts.append(c.get("data"))
        else:
            parts.append(c)
    if len(parts) == 1:
        return parts[0]
    if parts and all(isinstance(p, str) for p in parts):
        return "\n".join(parts)
    return parts


def _write_artifact(b: _Builder, name: str, inp: Dict[str, Any], tool: Dict[str, Any]) -> None:
    if name != "write":
        return
    cmd = inp.get("command")
    path = str(inp.get("path") or "file")
    short = path.rstrip("/").split("/")[-1]
    if cmd == "create":
        content, kind, title = str(inp.get("content") or ""), "code", f"create {short}"
    elif cmd == "strReplace":
        content, kind, title = _diff(path, str(inp.get("oldStr") or ""), str(inp.get("newStr") or "")), "diff", f"edit {short}"
    elif cmd == "insert":
        content, kind, title = _diff(path, "", str(inp.get("newStr") or inp.get("content") or "")), "diff", f"insert into {short}"
    else:
        return
    b.records.append({
        "type": "artifact", "v": 1, "id": "kiro_art_" + tool["id"][5:], "run_id": b.run_id, "span_id": tool["id"],
        "kind": kind, "name": title, "content": _cap(content), "size": len(content),
        "uri": f"file://{path}" if path.startswith("/") else path, "created_at": None,
        "metadata": {"path": path, "tool": name, "command": cmd},
    })


def _state_event(b: _Builder, rec: Dict[str, Any], turn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    kind = rec.get("kind")
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    ev: Dict[str, Any] = {
        "type": "event", "v": 1, "id": b.sid("ev", b.seq()), "run_id": b.run_id, "span_id": turn["id"] if turn else None,
        "kind": "state", "level": "info", "ts": None,
    }
    if kind == "Clear":
        ev["name"] = "conversation cleared"
        ev["message"] = "The conversation history was cleared; earlier context is no longer visible to the model."
    else:
        strategy = data.get("strategy") if isinstance(data.get("strategy"), dict) else None
        ev["name"] = "context compacted"
        ev["message"] = "Earlier messages were summarised to free context."
        ev["data"] = {"summary_chars": len(data.get("summary") or "") if isinstance(data.get("summary"), str) else None,
                      "messages_summarised": len(data.get("messages_snapshot") or []) if isinstance(data.get("messages_snapshot"), list) else None,
                      "strategy": strategy}
    return ev


def import_sessions(paths: Iterable[Path], store: Any, *, now: Optional[float] = None) -> List[str]:
    """Import (or re-sync) sessions; returns run ids. Unchanged records are not rewritten."""
    ids: List[str] = []
    indexes: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for p in paths:
        p = Path(p)
        key = str(p.parent)
        if key not in indexes:
            indexes[key] = build_index(p.parent)
        records = session_to_records(p, now=now, index=indexes[key])
        if len(records) <= 1:
            continue
        store.sync_run(records[0]["id"], records)
        ids.append(records[0]["id"])
    return ids


def _completed_final(records: List[Dict[str, Any]], finals: List[Tuple[str, str]]) -> Optional[Tuple[str, str, Optional[float]]]:
    spans = {r["id"]: r for r in records if r.get("type") == "span"}
    turns = [r for r in records if r.get("type") == "span" and r.get("kind") == "step" and not r.get("parent_id")]
    current = max(turns, key=lambda t: (t.get("started_at") or 0, t.get("seq") or 0))["id"] if turns else None
    for text, sid in reversed(finals):
        if spans.get(sid, {}).get("parent_id") != current:
            return (text, sid, spans.get(sid, {}).get("ended_at"))
    return None
