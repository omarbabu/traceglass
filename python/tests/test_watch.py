"""End-to-end: a Watcher following live (synthetic) Claude Code and Kiro sessions into a store."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from traceglass.integrations import sessions
from traceglass.store import Store
from traceglass.watch import THROTTLE_S, Watcher, encode_claude_project, parse_since


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TRACEGLASS_HOME", str(home / ".traceglass"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.delenv("KIRO_SESSION_ID", raising=False)
    return home


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _append(path: Path, *recs):
    with open(path, "a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _wait_throttle():
    time.sleep(THROTTLE_S + 0.05)


def _bundle_by_name(store, run_id):
    b = store.get_bundle(run_id)
    return b, {s["name"]: s for s in b["spans"]}


# -- Claude Code ------------------------------------------------------------------------
def _cc_base(sid, cwd, uuid, kind, t, **extra):
    return {"sessionId": sid, "cwd": cwd, "version": "2.1.0", "isSidechain": False, "type": kind, "uuid": uuid, "timestamp": _iso(t), **extra}


def test_live_claude_code_session(env, tmp_path):
    cwd = "/work/live-app"
    proj = sessions.claude_projects_dir() / encode_claude_project(cwd)
    proj.mkdir(parents=True)
    path = proj / "sess-live.jsonl"
    t0 = time.time() - 20
    _append(
        path,
        _cc_base("sess-live", cwd, "u1", "user", t0, message={"role": "user", "content": "Run the tests"}),
        _cc_base(
            "sess-live", cwd, "a1", "assistant", t0 + 2,
            message={"id": "m1", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "id": "toolu_t", "name": "Bash", "input": {"command": "pytest -q"}}],
                     "usage": {"input_tokens": 50, "output_tokens": 10}},
        ),
    )
    store = Store(tmp_path / "w.db")
    logs = []
    w = Watcher(store, kiro=False, since_days=1, log=logs.append)

    assert w.poll_once() == ["cc_sess-live"]
    run = store.get_run("cc_sess-live")
    assert run["status"] == "running" and run["metadata"]["activity"] == "working"
    _, spans = _bundle_by_name(store, "cc_sess-live")
    assert spans["Bash"]["status"] == "running"
    assert logs and "working" in logs[-1]

    # Nothing changed on disk: an immediate poll does no work at all.
    seq = store.latest_seq()
    assert w.poll_once() == []
    assert store.latest_seq() == seq

    # The tool finishes and the model answers.
    _wait_throttle()
    _append(
        path,
        _cc_base("sess-live", cwd, "u2", "user", t0 + 9,
                 message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_t", "content": "3 passed"}]}),
        _cc_base("sess-live", cwd, "a2", "assistant", t0 + 11,
                 message={"id": "m2", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                          "content": [{"type": "text", "text": "All 3 tests pass."}], "usage": {"input_tokens": 60, "output_tokens": 8}}),
    )
    before = {s["id"]: s for s in store.get_bundle("cc_sess-live")["spans"]}
    seq = store.latest_seq()
    assert w.poll_once() == ["cc_sess-live"]
    run = store.get_run("cc_sess-live")
    assert run["status"] == "ok" and run["metadata"]["activity"] == "idle"
    _, spans = _bundle_by_name(store, "cc_sess-live")
    assert spans["Bash"]["status"] == "ok"

    # Minimal change set: records that did not change were not rewritten.
    changed = {c["record"]["id"] for c in store.changes_since(seq) if c["type"] == "span"}
    unchanged = {sid for sid, s in before.items() if s == next(x for x in store.get_bundle("cc_sess-live")["spans"] if x["id"] == sid)}
    assert unchanged and not (changed & unchanged)

    # A hook-reported SessionEnd marks the session ended.
    _wait_throttle()
    sessions.append_hook_event("claude-code", "sess-live", {"ts": time.time() * 1000, "event": "session_end", "session_id": "sess-live"})
    assert w.poll_once() == ["cc_sess-live"]
    assert store.get_run("cc_sess-live")["metadata"]["activity"] == "ended"


def test_backfill_window_and_project_filter(env, tmp_path):
    root = sessions.claude_projects_dir()
    for cwd, sid, age in (("/a", "new", 0), ("/a", "old", 10 * 86400), ("/b", "other", 0)):
        d = root / encode_claude_project(cwd)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        _append(p, _cc_base(sid, cwd, "u1", "user", time.time() - age, message={"role": "user", "content": "hi"}),
                _cc_base(sid, cwd, "a1", "assistant", time.time() - age + 1,
                         message={"id": "m", "role": "assistant", "model": "m", "stop_reason": "end_turn", "content": [{"type": "text", "text": "hello"}]}))
        t = time.time() - age
        import os

        os.utime(p, (t, t))
    store = Store(tmp_path / "w.db")
    assert sorted(Watcher(store, kiro=False, since_days=3).poll_once()) == ["cc_new", "cc_other"]
    store2 = Store(tmp_path / "w2.db")
    assert Watcher(store2, kiro=False, since_days=None, project_dir="/a").poll_once() == ["cc_new", "cc_old"]


def test_watcher_survives_bad_files_and_disappearing_sessions(env, tmp_path):
    d = sessions.claude_projects_dir() / "-x"
    d.mkdir(parents=True)
    (d / "garbage.jsonl").write_text('{"half": \n\x00\x01 not json at all')
    good = d / "good.jsonl"
    _append(good, _cc_base("good", "/x", "u1", "user", time.time(), message={"role": "user", "content": "hi"}))
    store = Store(tmp_path / "w.db")
    w = Watcher(store, kiro=False)
    w.poll_once()
    assert w.errors == 0
    good.unlink()
    w.poll_once()  # a vanished session is simply no longer polled
    assert w.errors == 0


# -- Kiro -------------------------------------------------------------------------------
def _kiro_meta(sid, cwd, turns):
    return {
        "session_id": sid, "cwd": cwd, "created_at": _iso(time.time() - 60), "updated_at": _iso(time.time()),
        "title": "Live Kiro session", "session_created_reason": None,
        "session_state": {"version": "v1", "conversation_metadata": {"user_turn_metadatas": turns},
                          "rts_model_state": {"model_info": {"model_name": "claude-sonnet-5-5", "model_id": "claude-sonnet-5-5"}},
                          "agent_name": "default"},
    }


def test_live_kiro_session_with_exact_hook_timing(env, tmp_path):
    d = sessions.kiro_sessions_dir()
    d.mkdir(parents=True)
    sid, cwd = "k-live", "/work/kiro-app"
    jl, js = d / f"{sid}.jsonl", d / f"{sid}.json"
    t0 = int(time.time()) - 30
    js.write_text(json.dumps(_kiro_meta(sid, cwd, [])))
    _append(
        jl,
        {"version": "v1", "kind": "Prompt", "data": {"message_id": "p1", "content": [{"kind": "text", "data": "List the files"}], "meta": {"timestamp": t0}}},
        {"version": "v1", "kind": "AssistantMessage", "data": {"message_id": "a1", "content": [
            {"kind": "text", "data": "Listing."},
            {"kind": "toolUse", "data": {"toolUseId": "tu_1", "name": "shell", "input": {"__tool_use_purpose": "see files", "command": "ls"}}}]}},
    )
    pre_ts = (t0 + 4) * 1000.0
    sessions.append_hook_event("kiro", sid, {"ts": pre_ts, "event": "pre_tool", "session_id": sid, "tool_name": "shell",
                                             "tool_use_id": None, "tool_input_digest": sessions.input_digest({"command": "ls"})})
    store = Store(tmp_path / "w.db")
    w = Watcher(store, claude_code=False, since_days=1)
    assert w.poll_once() == [f"kiro_{sid}"]
    run = store.get_run(f"kiro_{sid}")
    assert run["status"] == "running" and run["metadata"]["activity"] == "working"
    _, spans = _bundle_by_name(store, f"kiro_{sid}")
    assert spans["shell"]["status"] == "running" and spans["shell"]["started_at"] == pre_ts

    _wait_throttle()
    post_ts = (t0 + 7) * 1000.0
    _append(
        jl,
        {"version": "v1", "kind": "ToolResults", "data": {"message_id": "r1", "content": [
            {"kind": "toolResult", "data": {"toolUseId": "tu_1", "content": [{"kind": "text", "data": "a.py\nb.py"}], "status": "success"}}]}},
        {"version": "v1", "kind": "AssistantMessage", "data": {"message_id": "a2", "content": [{"kind": "text", "data": "Two files: a.py and b.py."}]}},
    )
    sessions.append_hook_event("kiro", sid, {"ts": post_ts, "event": "post_tool", "session_id": sid, "tool_name": "shell", "ok": True,
                                             "tool_input_digest": sessions.input_digest({"command": "ls"})})
    sessions.append_hook_event("kiro", sid, {"ts": (t0 + 9) * 1000.0, "event": "stop", "session_id": sid})
    js.write_text(json.dumps(_kiro_meta(sid, cwd, [{
        "message_ids": ["p1", "a1", "r1", "a2"], "turn_duration": {"secs": 9, "nanos": 0}, "end_timestamp": _iso(t0 + 9),
        "end_reason": "UserTurnEnd", "input_token_count": 900, "output_token_count": 40, "cache_read_input_token_count": 0,
        "cache_write_input_token_count": 0, "model": "claude-sonnet-5-5", "total_request_count": 2, "number_of_cycles": 2}])))
    assert w.poll_once() == [f"kiro_{sid}"]
    run = store.get_run(f"kiro_{sid}")
    assert run["status"] == "ok" and run["metadata"]["activity"] == "idle"
    _, spans = _bundle_by_name(store, f"kiro_{sid}")
    shell = spans["shell"]
    assert shell["status"] == "ok" and shell["started_at"] == pre_ts and shell["ended_at"] == post_ts
    assert shell["attributes"].get("timing") == "exact" and shell["attributes"].get("purpose") == "see files"
    assert run["metadata"]["timing"] in ("mixed", "exact")


def test_working_sessions_are_rechecked_without_new_writes(env, tmp_path, monkeypatch):
    """A session that stops being written flips to interrupted once it goes stale."""
    cwd = "/work/stale"
    proj = sessions.claude_projects_dir() / encode_claude_project(cwd)
    proj.mkdir(parents=True)
    path = proj / "s-stale.jsonl"
    t0 = time.time() - 5
    _append(path, _cc_base("s-stale", cwd, "u1", "user", t0, message={"role": "user", "content": "go"}),
            _cc_base("s-stale", cwd, "a1", "assistant", t0 + 1, message={"id": "m", "role": "assistant", "model": "m", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 999"}}]}))
    clock = [time.time()]
    store = Store(tmp_path / "w.db")
    w = Watcher(store, kiro=False, clock=lambda: clock[0])
    w.poll_once()
    assert store.get_run("cc_s-stale")["metadata"]["activity"] == "working"
    clock[0] += 3600  # an hour later, nothing written
    assert w.poll_once() == ["cc_s-stale"]
    assert store.get_run("cc_s-stale")["metadata"]["activity"] == "interrupted"


def test_heartbeat_and_parse_since(env, tmp_path):
    import threading

    store = Store(tmp_path / "w.db")
    w = Watcher(store, interval=0.05)
    stop = w.start()
    deadline = time.time() + 5
    while not sessions.watcher_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert sessions.watcher_alive()
    stop.set()
    w.thread.join(5)
    assert not sessions.watcher_alive()
    assert parse_since("3d") == 3 and parse_since("12h") == 0.5 and parse_since("all") is None and parse_since(None) == 3.0
    assert parse_since("2w") == 14
    with pytest.raises(ValueError):
        parse_since("soon")
    assert isinstance(threading.active_count(), int)
