from pathlib import Path

import pytest

from traceglass.integrations import claude_code as cc
from traceglass.store import Store

FIX = Path(__file__).parent / "fixtures" / "claude_code"
SESSION = FIX / "proj" / "sess-1111.jsonl"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read the developer's real ~/.traceglass hook sidecars."""
    monkeypatch.setenv("TRACEGLASS_HOME", str(tmp_path / "tg-home"))


@pytest.fixture
def recs():
    return cc.transcript_to_records(SESSION)


def by(recs, type_):
    return [r for r in recs if r["type"] == type_]


def test_run_record(recs):
    run = recs[0]
    assert run["type"] == "run" and run["id"] == "cc_sess-1111" and run["name"] == "Fix failing calc test"
    assert run["project"] == "demo-app" and run["session"] == "sess-1111" and run["source"] == "claude-code"
    assert run["input"] == "Fix the failing test in calc.py" and run["metadata"]["git_branch"] == "main"
    assert run["started_at"] < run["ended_at"]


def test_turns_llm_and_tools(recs):
    spans = by(recs, "span")
    turns = [s for s in spans if s["kind"] == "step"]
    assert [t["name"] for t in turns] == ["Turn 1: Fix the failing test in calc.py", "Turn 2: Thanks! Now add a docstring"]
    assert all(t["parent_id"] is None for t in turns)
    llms = [s for s in spans if s["kind"] == "llm" and s.get("agent") is None]
    first = llms[0]
    assert first["model"] == "claude-opus-5-5" and first["usage"]["output_tokens"] == 40 and first["usage"]["total_tokens"] == 140
    assert first["input"]["$tg"] == "not_captured"
    assert first["output"]["content"][0] == {"type": "thinking", "$tg": "not_captured", "reason": "thinking is not exported"}
    assert "secret chain of thought" not in str(recs)
    tools = {s["name"]: s for s in spans if s["kind"] == "tool" and s.get("agent") is None}
    assert set(tools) == {"Read", "Edit", "Bash", "Write"}
    assert tools["Read"]["output"].startswith("def add") and tools["Read"]["parent_id"] == turns[0]["id"]
    assert tools["Read"]["depends_on"] == [first["id"]]
    assert tools["Bash"]["status"] == "error" and tools["Bash"]["error"]["message"] == "1 failed"
    assert tools["Bash"]["input"]["env"]["API_KEY"]["$tg"] == "redacted"
    assert tools["Read"]["ended_at"] - tools["Read"]["started_at"] == 1000


def test_subagent_delegation(recs):
    spans = by(recs, "span")
    deleg = next(s for s in spans if s["kind"] == "delegation")
    assert deleg["name"] == "Agent: Investigate test failure" and deleg["status"] == "ok"
    agent = next(s for s in spans if s["kind"] == "agent")
    assert agent["parent_id"] == deleg["id"] and agent["agent"] == "general-purpose"
    assert agent["output"] == "The test expects add(2,2)==4."
    kids = [s for s in spans if s.get("parent_id") == agent["id"]]
    assert {k["kind"] for k in kids} == {"llm", "tool"} and any(k["name"] == "Grep" for k in kids)
    assert all(k.get("agent") == "general-purpose" for k in kids)
    assert agent["started_at"] < agent["ended_at"]


def test_edit_artifacts_and_final(recs):
    arts = {a["name"]: a for a in by(recs, "artifact")}
    diff = arts["edit calc.py"]
    assert diff["kind"] == "diff" and "-    return a - b" not in diff["content"]
    assert "-return a - b" in diff["content"] and "+return a + b" in diff["content"] and diff["content"].startswith("--- a/")
    assert arts["write NOTES.md"]["kind"] == "code" and arts["write NOTES.md"]["content"] == "# Notes\n"
    final = arts["Final response"]
    assert final["final"] is True and final["content"] == "Done — added NOTES.md."
    assert recs[0]["final_artifact_id"] == final["id"]
    span_ids = {s["id"] for s in by(recs, "span")}
    assert all(a["span_id"] in span_ids for a in arts.values())


def test_ids_unique_and_parents_resolve(recs):
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))
    span_ids = {s["id"] for s in by(recs, "span")}
    assert all(s["parent_id"] in span_ids for s in by(recs, "span") if s.get("parent_id"))


def test_import_is_idempotent(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    ids = cc.import_transcripts([SESSION], st)
    assert ids == ["cc_sess-1111"]
    n = len(st.get_bundle(ids[0])["spans"])
    cc.import_transcripts([SESSION], st)
    assert len(st.list_runs()) == 1 and len(st.get_bundle(ids[0])["spans"]) == n
    assert st.list_runs()[0]["summary"]["tokens"] > 0


def test_find_transcripts(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "PROJECTS_DIR", FIX)
    assert cc.find_transcripts() == [SESSION]
    assert cc.find_transcripts(latest=True) == [SESSION]
    monkeypatch.setattr(cc, "PROJECTS_DIR", tmp_path / "none")
    assert cc.find_transcripts() == []


def test_find_transcripts_by_project_dir(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    d = root / "-work-my-app"
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text("{}\n")
    monkeypatch.setattr(cc, "PROJECTS_DIR", root)
    assert [p.name for p in cc.find_transcripts("/work/my-app")] == ["abc.jsonl"]
    assert cc.find_transcripts("/work/other") == []


def test_malformed_and_empty(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n[1,2]\n{\"type\": \"weird\"}\n")
    recs = cc.transcript_to_records(p)
    assert recs[0]["type"] == "run" and len(recs) == 1
    st = Store(tmp_path / "db.sqlite")
    assert cc.import_transcripts([p], st) == []


# ------------------------------------------------------------------------------ liveness
import json as _json
import os as _os
from datetime import datetime as _dt, timezone as _tz

T0 = 1_790_000_000_000.0
FAR_FUTURE = 4_000_000_000_000.0


def _iso(ms):
    return _dt.fromtimestamp(ms / 1000, tz=_tz.utc).isoformat().replace("+00:00", "Z")


def _user(text, ms, uid):
    return {"type": "user", "uuid": uid, "sessionId": "live-1", "cwd": "/work/app", "timestamp": _iso(ms), "message": {"role": "user", "content": text}}


def _assistant(ms, uid, *, text=None, tool=None, stop="end_turn"):
    content = ([{"type": "text", "text": text}] if text else []) + ([{"type": "tool_use", "id": tool, "name": "Bash", "input": {"command": "make"}}] if tool else [])
    return {"type": "assistant", "uuid": uid, "sessionId": "live-1", "timestamp": _iso(ms),
            "message": {"id": "msg_" + uid, "model": "claude-opus-5-5", "role": "assistant", "content": content, "stop_reason": stop,
                        "usage": {"input_tokens": 10, "output_tokens": 5}}}


def _result(ms, uid, tool):
    return {"type": "user", "uuid": uid, "sessionId": "live-1", "timestamp": _iso(ms),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool, "content": "ok"}]}}


def _write(tmp_path, recs, mtime_ms=T0 + 10_000):
    p = tmp_path / "live-1.jsonl"
    p.write_text("".join(_json.dumps(r) + "\n" for r in recs))
    _os.utime(p, (mtime_ms / 1000, mtime_ms / 1000))
    return p


OPEN_TOOL = [_user("run make", T0, "u1"), _assistant(T0 + 2000, "a1", tool="tu_1", stop="tool_use")]


def _turn(recs):
    return [r for r in recs if r["type"] == "span" and r["kind"] == "step"][-1]


def test_working_while_a_tool_runs(tmp_path):
    recs = cc.transcript_to_records(_write(tmp_path, OPEN_TOOL), now=T0 + 20_000, hook_events=[])
    run = recs[0]
    assert run["metadata"]["activity"] == "working" and run["status"] == "running" and run["ended_at"] is None
    assert run["metadata"]["agent_tool"] == "claude-code" and run["metadata"]["timing"] == "exact"
    tool = next(r for r in recs if r["type"] == "span" and r["kind"] == "tool")
    assert tool["status"] == "running" and tool["ended_at"] is None
    assert _turn(recs)["status"] == "running" and _turn(recs)["ended_at"] is None


def test_working_while_the_model_digests_a_tool_result(tmp_path):
    p = _write(tmp_path, OPEN_TOOL + [_result(T0 + 5000, "r1", "tu_1")])
    recs = cc.transcript_to_records(p, now=T0 + 20_000, hook_events=[])
    assert recs[0]["metadata"]["activity"] == "working"
    assert next(r for r in recs if r["type"] == "span" and r["kind"] == "tool")["status"] == "ok"


def test_interrupted_when_stale(tmp_path):
    recs = cc.transcript_to_records(_write(tmp_path, OPEN_TOOL), now=FAR_FUTURE, hook_events=[])
    run = recs[0]
    assert run["metadata"]["activity"] == "interrupted" and run["status"] == "ok" and run["ended_at"]
    tool = next(r for r in recs if r["type"] == "span" and r["kind"] == "tool")
    assert tool["status"] == "cancelled" and _turn(recs)["status"] == "cancelled"


def test_idle_after_end_turn_even_if_recent(tmp_path):
    p = _write(tmp_path, OPEN_TOOL + [_result(T0 + 5000, "r1", "tu_1"), _assistant(T0 + 8000, "a2", text="All green.")])
    recs = cc.transcript_to_records(p, now=T0 + 9000, hook_events=[])
    assert recs[0]["metadata"]["activity"] == "idle" and recs[0]["status"] == "ok"
    assert _turn(recs)["status"] == "ok" and _turn(recs)["ended_at"] == T0 + 8000


def test_ended_by_session_end_hook(tmp_path):
    p = _write(tmp_path, OPEN_TOOL)
    recs = cc.transcript_to_records(p, now=T0 + 20_000, hook_events=[{"event": "session_end", "ts": T0 + 15_000}])
    assert recs[0]["metadata"]["activity"] == "ended" and recs[0]["status"] == "ok"


def test_hooks_refine_liveness(tmp_path):
    p = _write(tmp_path, OPEN_TOOL, mtime_ms=T0 + 2000)
    stale_now = T0 + 20 * 60_000
    pre = {"event": "pre_tool", "ts": stale_now - 60_000, "tool_name": "Bash", "tool_use_id": "tu_1"}
    assert cc.transcript_to_records(p, now=stale_now, hook_events=[pre])[0]["metadata"]["activity"] == "working"
    stop = {"event": "stop", "ts": stale_now - 1000}
    assert cc.transcript_to_records(p, now=stale_now, hook_events=[pre, stop])[0]["metadata"]["activity"] == "idle"


def test_user_interrupt_marks_the_turn_instead_of_starting_one(tmp_path):
    p = _write(tmp_path, OPEN_TOOL + [_user("[Request interrupted by user for tool use]", T0 + 3000, "u2")])
    recs = cc.transcript_to_records(p, now=T0 + 4000, hook_events=[])
    turns = [r for r in recs if r["type"] == "span" and r["kind"] == "step"]
    assert len(turns) == 1 and turns[0]["attributes"]["interrupted"] is True
    assert recs[0]["metadata"]["activity"] == "idle"
    assert next(r for r in recs if r["type"] == "span" and r["kind"] == "tool")["status"] == "cancelled"


def test_slash_command_prompt_is_not_awaiting_the_model(tmp_path):
    p = _write(tmp_path, [_user("<command-name>/model</command-name><command-args>opus</command-args>", T0, "u1")])
    recs = cc.transcript_to_records(p, now=T0 + 1000, hook_events=[])
    assert recs[0]["metadata"]["activity"] == "idle"


def test_live_import_is_deterministic(tmp_path):
    p = _write(tmp_path, OPEN_TOOL)
    a = cc.transcript_to_records(p, now=T0 + 20_000, hook_events=[])
    b = cc.transcript_to_records(p, now=T0 + 20_000, hook_events=[])
    assert _json.dumps(a, sort_keys=True) == _json.dumps(b, sort_keys=True)


def test_working_session_does_not_promote_a_progress_note_to_final(tmp_path):
    recs = [_user("first", T0, "u0"), _assistant(T0 + 1000, "a0", text="First answer.", stop="end_turn"),
            _user("run make", T0 + 3000, "u1"), _assistant(T0 + 4000, "a1", text="Running make…", tool="tu_1", stop="tool_use")]
    out = cc.transcript_to_records(_write(tmp_path, recs), now=T0 + 20_000, hook_events=[])
    assert out[0]["metadata"]["activity"] == "working"
    fin = [r for r in out if r["type"] == "artifact" and r.get("final")]
    assert [a["content"] for a in fin] == ["First answer."]
