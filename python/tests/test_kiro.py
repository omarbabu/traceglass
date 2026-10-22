import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from traceglass.integrations import kiro, sessions
from traceglass.store import Store

FIX = Path(__file__).parent / "fixtures" / "kiro"
CLOSED = FIX / "k-closed-0001.jsonl"
FAR_FUTURE = 4_000_000_000_000.0  # "now" long after any fixture write → nothing is recent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read the developer's real ~/.traceglass hook sidecars."""
    monkeypatch.setenv("TRACEGLASS_HOME", str(tmp_path / "tg-home"))


def by(recs, type_):
    return [r for r in recs if r["type"] == type_]


def spans(recs, kind=None):
    return [r for r in by(recs, "span") if kind is None or r["kind"] == kind]


@pytest.fixture
def closed():
    return kiro.session_to_records(CLOSED, now=FAR_FUTURE, hook_events=[])


# ---------------------------------------------------------------------------- closed session
def test_run_record(closed):
    run = closed[0]
    assert run["id"] == "kiro_k-closed-0001" and run["source"] == "kiro" and run["name"] == "Fix flaky date test"
    assert run["project"] == "demo-app" and run["session"] == "k-closed-0001" and run["status"] == "ok"
    assert run["input"] == "Fix the flaky date test in billing"
    m = run["metadata"]
    assert m["agent_tool"] == "kiro" and m["activity"] == "idle" and m["timing"] == "estimated"
    assert m["model"] == "Claude Sonnet 5.5" and m["agent_name"] == "default" and m["kiro_format_version"] == "v1"
    assert m["warnings"] == 2  # one unknown record kind + one partially written last line
    assert run["started_at"] < run["ended_at"]


def test_turns_carry_usage_and_metadata(closed):
    t1, t2 = spans(closed, "step")
    assert t1["name"] == "Turn 1: Fix the flaky date test in billing" and t2["name"].startswith("Turn 2: Now write NOTES.md")
    assert t1["usage"] == {"input_tokens": 5200, "output_tokens": 320, "cache_read_tokens": 900, "cache_write_tokens": 100, "total_tokens": 5520}
    assert t1["attributes"]["requests"] == 4 and t1["attributes"]["end_reason"] == "UserTurnEnd"
    assert t1["attributes"]["credits"] == [{"value": 0.42, "unit": "credits"}]
    assert t1["attributes"]["timing"] == "exact" and t1["ended_at"] - t1["started_at"] == pytest.approx(40500)
    assert all("usage" not in s for s in spans(closed, "llm"))  # Kiro does not record per-request usage


def test_llm_tools_and_honesty(closed):
    llms = spans(closed, "llm")
    assert len(llms) == 6 and llms[0]["model"] == "claude-sonnet-5-5"
    assert llms[0]["input"]["$tg"] == "not_captured"
    assert llms[0]["output"]["content"][0] == {"type": "thinking", "$tg": "not_captured", "reason": "thinking is not exported"}
    assert "private chain of thought" not in json.dumps(closed)
    tools = {s["attributes"]["tool_use_id"]: s for s in spans(closed, "tool")}
    read = tools["tu_read"]
    assert read["name"] == "read" and read["attributes"]["purpose"] == "Read the date helper"
    assert "__tool_use_purpose" not in read["input"] and read["output"] == {"content": "def parse(v):\n    return v\n"}
    assert read["depends_on"] == [llms[0]["id"]] and read["attributes"]["timing"] == "estimated"
    sh = tools["tu_sh"]
    assert sh["status"] == "error" and sh["error"]["message"] == "1 failed, 12 passed"
    assert "sk-ant-" not in json.dumps(sh["input"]) and "‹redacted:anthropic-key›" in sh["input"]["command"]
    assert tools["tu_mcp"]["attributes"]["mcp_server"] == "search-mcp"


def test_write_artifacts_and_final(closed):
    arts = {a["name"]: a for a in by(closed, "artifact")}
    diff = arts["edit dates.py"]
    assert diff["kind"] == "diff" and diff["content"].startswith("--- a//work/demo-app/billing/dates.py")
    assert "-    return v\n" in diff["content"] and "+    return v.astimezone(UTC)\n" in diff["content"]
    assert arts["create NOTES.md"]["kind"] == "code" and arts["create NOTES.md"]["content"] == "# Notes\nFixed dates.\n"
    final = arts["Final response"]
    assert final["final"] and final["content"] == "Done — created NOTES.md." and closed[0]["final_artifact_id"] == final["id"]
    span_ids = {s["id"] for s in spans(closed)}
    assert all(a["span_id"] in span_ids and a["created_at"] for a in arts.values())


def test_compaction_event(closed):
    (ev,) = by(closed, "event")
    assert ev["name"] == "context compacted" and ev["kind"] == "state"
    assert ev["data"]["summary_chars"] > 0 and ev["data"]["messages_summarised"] == 3
    assert "fixed dates.py" not in json.dumps(ev)  # the summary text itself is not copied
    assert ev["span_id"] == spans(closed, "step")[0]["id"] and ev["ts"]


def test_estimated_steps_tile_the_turn(closed):
    t1 = spans(closed, "step")[0]
    kids = sorted((s for s in spans(closed) if s.get("parent_id") == t1["id"]), key=lambda s: s["started_at"])
    assert kids[0]["started_at"] == pytest.approx(t1["started_at"]) and kids[-1]["ended_at"] == pytest.approx(t1["ended_at"])
    assert all(a["ended_at"] == pytest.approx(b["started_at"]) for a, b in zip(kids, kids[1:]))


def test_ids_unique_parents_resolve_and_deterministic(closed):
    ids = [r["id"] for r in closed]
    assert len(ids) == len(set(ids))
    span_ids = {s["id"] for s in spans(closed)}
    assert all(s["parent_id"] in span_ids for s in spans(closed) if s.get("parent_id"))
    again = kiro.session_to_records(CLOSED, now=FAR_FUTURE, hook_events=[])
    assert json.dumps(again, sort_keys=True) == json.dumps(closed, sort_keys=True)
    json.dumps(closed)  # JSON-safe


# --------------------------------------------------------------------------- live sessions
def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def write_session(d: Path, sid: str, recs, turns=(), *, cwd="/work/app", reason=None, created_ms=1_790_000_000_000.0, mtime_ms=None):
    d.mkdir(parents=True, exist_ok=True)
    jl = d / f"{sid}.jsonl"
    jl.write_text("".join(json.dumps(r) + "\n" for r in recs))
    meta = {
        "session_id": sid, "cwd": cwd, "created_at": _iso(created_ms), "updated_at": _iso(created_ms), "title": None,
        "session_created_reason": reason,
        "session_state": {"version": "v1", "agent_name": "default",
                          "conversation_metadata": {"user_turn_metadatas": list(turns)},
                          "rts_model_state": {"model_info": {"model_id": "claude-sonnet-5-5", "model_name": "Claude Sonnet 5.5"}}},
    }
    (d / f"{sid}.json").write_text(json.dumps(meta))
    if mtime_ms is not None:
        for p in (jl, d / f"{sid}.json"):
            os.utime(p, (mtime_ms / 1000, mtime_ms / 1000))
    return jl


def prompt(mid, text, ts_s):
    return {"version": "v1", "kind": "Prompt", "data": {"message_id": mid, "content": [{"kind": "text", "data": text}], "meta": {"timestamp": ts_s}}}


def assistant(mid, *tool_ids, text=None, name="shell"):
    content = [{"kind": "text", "data": text}] if text else []
    content += [{"kind": "toolUse", "data": {"toolUseId": t, "name": name, "input": {"command": "make test", "__tool_use_purpose": "run tests"}}} for t in tool_ids]
    return {"version": "v1", "kind": "AssistantMessage", "data": {"message_id": mid, "content": content}}


def result(mid, tid, status="success"):
    return {"version": "v1", "kind": "ToolResults", "data": {"message_id": mid, "content": [{"kind": "toolResult", "data": {"toolUseId": tid, "content": [{"kind": "text", "data": "ok"}], "status": status}}], "results": {}}}


def turn_meta(ids, start_ms, secs):
    return {"message_ids": ids, "turn_duration": {"secs": secs, "nanos": 0}, "end_timestamp": _iso(start_ms + secs * 1000), "end_reason": "UserTurnEnd",
            "input_token_count": 10, "output_token_count": 5, "model": "claude-sonnet-5-5"}


T0 = 1_790_000_000_000.0


def live_session(tmp_path, mtime_ms):
    recs = [prompt("p1", "first", T0 / 1000), assistant("a1", text="hi"), prompt("p2", "run the tests", T0 / 1000 + 60), assistant("a2", "tu_1")]
    return write_session(tmp_path / "s", "live-1", recs, [turn_meta(["p1", "a1"], T0, 5)], mtime_ms=mtime_ms)


def test_in_progress_turn_is_working_when_recent(tmp_path):
    p = live_session(tmp_path, mtime_ms=T0 + 70_000)
    recs = kiro.session_to_records(p, now=T0 + 75_000, hook_events=[])
    run = recs[0]
    assert run["metadata"]["activity"] == "working" and run["status"] == "running" and run["ended_at"] is None
    t2 = spans(recs, "step")[1]
    assert t2["status"] == "running" and t2["ended_at"] is None and t2["input"] == "run the tests"
    (tool,) = spans(recs, "tool")
    assert tool["status"] == "running" and tool["ended_at"] is None and tool["started_at"] >= t2["started_at"]
    assert spans(recs, "step")[0]["status"] == "ok"


def test_in_progress_turn_is_interrupted_when_stale(tmp_path):
    p = live_session(tmp_path, mtime_ms=T0 + 70_000)
    recs = kiro.session_to_records(p, now=T0 + 70_000 + 11 * 60_000, hook_events=[])
    run = recs[0]
    assert run["metadata"]["activity"] == "interrupted" and run["status"] == "ok" and run["ended_at"] is not None
    t2 = spans(recs, "step")[1]
    assert t2["status"] == "cancelled" and t2["ended_at"] is not None
    (tool,) = spans(recs, "tool")
    assert tool["status"] == "cancelled" and tool["attributes"]["note"]


def test_hook_activity_keeps_a_stale_file_working_and_stop_ends_the_turn(tmp_path):
    p = live_session(tmp_path, mtime_ms=T0 + 70_000)
    now = T0 + 70_000 + 15 * 60_000
    pre = {"event": "pre_tool", "ts": now - 60_000, "tool_name": "shell", "tool_use_id": None}
    assert kiro.session_to_records(p, now=now, hook_events=[pre])[0]["metadata"]["activity"] == "working"
    stop = {"event": "stop", "ts": now - 30_000}
    recs = kiro.session_to_records(p, now=now, hook_events=[pre, stop])
    assert recs[0]["metadata"]["activity"] == "idle"
    t2 = spans(recs, "step")[1]
    assert t2["ended_at"] == stop["ts"] and t2["status"] == "ok"


def test_partial_and_missing_metadata(tmp_path):
    d = tmp_path / "s"
    p = write_session(d, "nometa", [prompt("p1", "hello", T0 / 1000), assistant("a1", text="hey")])
    (d / "nometa.json").unlink()
    with open(p, "a") as fh:
        fh.write('{"version": "v1", "kind": "Assi')
    recs = kiro.session_to_records(p, now=FAR_FUTURE, hook_events=[])
    assert recs[0]["id"] == "kiro_nometa" and recs[0]["metadata"]["warnings"] == 1
    assert [s["kind"] for s in spans(recs)] == ["step", "llm"]


def test_empty_session_yields_only_a_run(tmp_path):
    p = write_session(tmp_path / "s", "empty", [])
    assert len(kiro.session_to_records(p, now=FAR_FUTURE, hook_events=[])) == 1


# ------------------------------------------------------------------------------ hook timing
def test_hook_events_give_exact_tool_times(tmp_path):
    recs_in = [prompt("p1", "go", T0 / 1000), assistant("a1", "tu_a", "tu_b"), result("r1", "tu_a"), result("r2", "tu_b"), assistant("a2", text="done")]
    p = write_session(tmp_path / "s", "hooked", recs_in, [turn_meta(["p1", "a1", "r1", "r2", "a2"], T0, 30)])
    digest = sessions.input_digest({"command": "make test", "__tool_use_purpose": "run tests"})
    hooks = [
        {"event": "pre_tool", "ts": T0 + 5000, "tool_name": "shell", "tool_use_id": "tu_a"},
        {"event": "post_tool", "ts": T0 + 9000, "tool_name": "shell", "tool_use_id": "tu_a"},
        {"event": "pre_tool", "ts": T0 + 12000, "tool_name": "shell", "tool_use_id": None, "tool_input_digest": digest},
        {"event": "post_tool", "ts": T0 + 20000, "tool_name": "shell", "tool_use_id": None, "tool_input_digest": digest},
    ]
    recs = kiro.session_to_records(p, now=FAR_FUTURE, hook_events=hooks)
    tools = {s["attributes"]["tool_use_id"]: s for s in spans(recs, "tool")}
    assert (tools["tu_a"]["started_at"], tools["tu_a"]["ended_at"]) == (T0 + 5000, T0 + 9000)
    assert (tools["tu_b"]["started_at"], tools["tu_b"]["ended_at"]) == (T0 + 12000, T0 + 20000)
    assert tools["tu_a"]["attributes"]["timing"] == "exact" and tools["tu_b"]["attributes"]["timing"] == "exact"
    llm = spans(recs, "llm")
    assert llm[0]["attributes"]["timing"] == "estimated" and llm[0]["ended_at"] <= T0 + 5000
    assert llm[1]["started_at"] >= T0 + 20000 and llm[1]["ended_at"] == pytest.approx(T0 + 30000)
    assert recs[0]["metadata"]["timing"] == "mixed"
    without = kiro.session_to_records(p, now=FAR_FUTURE, hook_events=[])
    assert without[0]["metadata"]["timing"] == "estimated"


def test_hook_events_are_loaded_from_the_sidecar_by_default(tmp_path):
    recs_in = [prompt("p1", "go", T0 / 1000), assistant("a1", "tu_a"), result("r1", "tu_a")]
    p = write_session(tmp_path / "s", "side", recs_in, [turn_meta(["p1", "a1", "r1"], T0, 10)])
    sessions.append_hook_event("kiro", "side", {"event": "pre_tool", "ts": T0 + 2000, "tool_name": "shell", "tool_use_id": "tu_a"})
    recs = kiro.session_to_records(p, now=FAR_FUTURE)
    assert spans(recs, "tool")[0]["started_at"] == T0 + 2000


# ------------------------------------------------------------------------- subagent parents
def test_subagent_parent_inferred_only_when_unambiguous(tmp_path):
    d = tmp_path / "s"
    write_session(d, "parent", [prompt("p1", "spawn helpers", T0 / 1000)], [turn_meta(["p1"], T0, 120)], cwd="/work/x")
    write_session(d, "elsewhere", [prompt("p1", "other dir", T0 / 1000)], [turn_meta(["p1"], T0, 120)], cwd="/work/y")
    child = write_session(d, "child", [prompt("c1", "do part A", T0 / 1000 + 30)], [turn_meta(["c1"], T0 + 30_000, 10)],
                          cwd="/work/x", reason="subagent", created_ms=T0 + 30_000)
    run = kiro.session_to_records(child, now=FAR_FUTURE, hook_events=[])[0]
    assert run["metadata"]["parent_run_id"] == "kiro_parent" and run["metadata"]["parent_inferred"] is True
    assert run["session"] == "parent" and "subagent" in run["tags"]

    write_session(d, "parent2", [prompt("p1", "also spawning", T0 / 1000)], [turn_meta(["p1"], T0, 120)], cwd="/work/x")
    kiro._INDEX_CACHE.clear()
    run = kiro.session_to_records(child, now=FAR_FUTURE, hook_events=[])[0]
    assert "parent_run_id" not in run["metadata"] and run["session"] == "child" and "subagent" in run["tags"]


def test_find_sessions_filters(tmp_path, monkeypatch):
    d = tmp_path / "s"
    write_session(d, "a", [prompt("p", "x", 1)], cwd=str(tmp_path / "proj"), mtime_ms=T0)
    write_session(d, "b", [prompt("p", "y", 1)], cwd="/elsewhere", reason="subagent", mtime_ms=T0 + 1000)
    write_session(d, "empty", [], mtime_ms=T0 + 2000)
    monkeypatch.setattr(kiro, "SESSIONS_DIR", d)
    assert [p.stem for p in kiro.find_sessions()] == ["b", "a"]
    assert [p.stem for p in kiro.find_sessions(include_subagents=False)] == ["a"]
    assert [p.stem for p in kiro.find_sessions(cwd=str(tmp_path / "proj"))] == ["a"]
    assert [p.stem for p in kiro.find_sessions(latest=True)] == ["b"]
    monkeypatch.setattr(kiro, "SESSIONS_DIR", tmp_path / "missing")
    assert kiro.find_sessions() == []


def test_import_sessions_syncs_incrementally(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    assert kiro.import_sessions([CLOSED], st) == ["kiro_k-closed-0001"]
    seq = st.latest_seq()
    n = len(st.get_bundle("kiro_k-closed-0001")["spans"])
    kiro.import_sessions([CLOSED], st)
    assert len(st.list_runs()) == 1 and len(st.get_bundle("kiro_k-closed-0001")["spans"]) == n
    assert st.latest_seq() == seq  # nothing changed, nothing rewritten
    assert st.list_runs()[0]["summary"]["tokens"] == 5520 + 2220


def test_working_session_keeps_the_last_completed_answer_as_final(tmp_path):
    recs = [prompt("p1", "first", T0 / 1000), assistant("a1", text="Done: first answer"),
            prompt("p2", "run the tests", T0 / 1000 + 60), assistant("a2", "tu_1", text="Running the tests now.")]
    p = write_session(tmp_path / "s", "live-2", recs, [turn_meta(["p1", "a1"], T0, 5)], mtime_ms=T0 + 70_000)
    working = kiro.session_to_records(p, now=T0 + 75_000, hook_events=[])
    fin = [r for r in working if r["type"] == "artifact" and r.get("final")]
    assert [a["content"] for a in fin] == ["Done: first answer"]  # not the mid-turn progress note
    stale = kiro.session_to_records(p, now=T0 + 70_000 + 11 * 60_000, hook_events=[])
    assert [a["content"] for a in stale if a["type"] == "artifact" and a.get("final")] == ["Running the tests now."]
