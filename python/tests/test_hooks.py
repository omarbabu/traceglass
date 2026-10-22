"""Agent hooks: the silent recorder, installers (idempotent, reversible), and /api/status.

Every test runs against a temporary HOME / TRACEGLASS_HOME / CLAUDE_CONFIG_DIR / KIRO_HOME; the real
~/.claude, ~/.kiro and ~/.traceglass are never touched.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from traceglass import hooks
from traceglass.integrations import sessions


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TRACEGLASS_HOME", str(home / ".traceglass"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.delenv("KIRO_SESSION_ID", raising=False)
    monkeypatch.delenv("TRACEGLASS_HOOKS_DISABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a[0]))
    return {"home": home, "tmp": tmp_path, "spawned": spawned}


CC_PRE = {
    "session_id": "sess-1",
    "transcript_path": "/x/sess-1.jsonl",
    "cwd": "/work/app",
    "hook_event_name": "PreToolUse",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q", "description": "run tests"},
    "tool_use_id": "toolu_01",
}


# -- normalisation ---------------------------------------------------------------------
def test_claude_events_normalise_without_content(env):
    ev = hooks.normalize("claude-code", CC_PRE, now=1000.0)
    assert ev["event"] == "pre_tool" and ev["raw_event"] == "PreToolUse"
    assert ev["session_id"] == "sess-1" and ev["tool_use_id"] == "toolu_01" and ev["tool_name"] == "Bash"
    assert ev["tool_input_digest"] == sessions.input_digest(CC_PRE["tool_input"])
    assert "pytest" not in json.dumps(ev)  # no tool content ever reaches the sidecar
    post = hooks.normalize("claude-code", {**CC_PRE, "hook_event_name": "PostToolUseFailure", "tool_response": {"error": "boom"}})
    assert post["event"] == "post_tool" and post["ok"] is False
    prompt = hooks.normalize("claude-code", {"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": "my secret plan"})
    assert prompt["event"] == "prompt" and "secret" not in json.dumps(prompt)
    end = hooks.normalize("claude-code", {"session_id": "s", "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"})
    assert end["event"] == "session_end" and end["reason"] == "prompt_input_exit"
    sub = hooks.normalize("claude-code", {"session_id": "s", "hook_event_name": "SubagentStop", "agent_id": "a1", "agent_transcript_path": "/x/a.jsonl"})
    assert sub["event"] == "subagent_stop" and sub["agent_id"] == "a1"
    assert hooks.normalize("claude-code", {"hook_event_name": "Notification"}) is None


def test_digest_ignores_dunder_keys():
    a = sessions.input_digest({"__tool_use_purpose": "why", "command": "ls"})
    assert a == sessions.input_digest({"command": "ls"}) and a is not None


def test_kiro_event_from_flag_and_session_from_env(env):
    ev = hooks.normalize("kiro", {"tool_name": "shell", "tool_input": {"command": "ls"}}, event="preToolUse", env={"KIRO_SESSION_ID": "k-1"})
    assert ev["event"] == "pre_tool" and ev["session_id"] == "k-1" and ev["tool_name"] == "shell"
    post = hooks.normalize("kiro", {"tool_name": "shell", "tool_response": {"success": False}}, event="postToolUse", env={"KIRO_SESSION_ID": "k-1"})
    assert post["ok"] is False


def test_kiro_session_falls_back_to_cwd(env):
    d = sessions.kiro_sessions_dir()
    d.mkdir(parents=True)
    (d / "old.json").write_text(json.dumps({"session_id": "old", "cwd": str(env["tmp"] / "other")}))
    (d / "mine.json").write_text(json.dumps({"session_id": "mine", "cwd": str(env["tmp"])}))
    os.utime(d / "old.json", (time.time() + 5, time.time() + 5))  # newest, but a different cwd
    ev = hooks.normalize("kiro", {}, event="stop", env={})
    assert ev["session_id"] == "mine"


# -- the command ---------------------------------------------------------------------
_ORIG_POPEN = subprocess.Popen  # captured before the fixture stubs Popen for _maybe_import


def _real_run(cmd, data):
    proc = _ORIG_POPEN(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
    out, err = proc.communicate(data, timeout=20)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def test_hook_command_is_silent_and_records(env):
    out = _real_run([sys.executable, "-m", "traceglass", "hook", "claude-code"], json.dumps(CC_PRE).encode())
    assert out.returncode == 0 and out.stdout == b"" and out.stderr == b""
    evs = sessions.read_hook_events("claude-code", "sess-1")
    assert len(evs) == 1 and evs[0]["event"] == "pre_tool"


@pytest.mark.parametrize("payload", [b"", b"not json", b"[1,2,3]", json.dumps({"hook_event_name": "Mystery"}).encode()])
def test_hook_never_fails_or_prints(env, payload):
    out = _real_run([sys.executable, "-m", "traceglass", "hook", "claude-code"], payload)
    assert out.returncode == 0 and out.stdout == b"" and out.stderr == b""
    out = _real_run([sys.executable, "-m", "traceglass", "hook", "nonsense-source"], payload)
    assert out.returncode == 0 and out.stdout == b"" and out.stderr == b""


def test_hooks_can_be_disabled(env, monkeypatch):
    monkeypatch.setenv("TRACEGLASS_HOOKS_DISABLED", "1")
    out = _real_run([sys.executable, "-m", "traceglass", "hook", "claude-code"], json.dumps(CC_PRE).encode())
    assert out.returncode == 0
    assert sessions.read_hook_events("claude-code", "sess-1") == []


def test_turn_end_imports_when_no_watcher(env, tmp_path):
    transcript = tmp_path / "sess-9.jsonl"
    transcript.write_text("")
    hooks.handle("claude-code", {"session_id": "sess-9", "hook_event_name": "Stop", "transcript_path": str(transcript)})
    assert env["spawned"] and env["spawned"][-1][-3:] == ["import", "claude-code", str(transcript)]
    hooks.handle("claude-code", {"session_id": "sess-9", "hook_event_name": "Stop", "transcript_path": str(transcript)})
    assert len(env["spawned"]) == 1  # debounced


def test_turn_end_does_not_import_when_watcher_alive(env, tmp_path):
    sessions.write_heartbeat("db")
    assert sessions.watcher_alive()
    hooks.handle("claude-code", {"session_id": "s", "hook_event_name": "Stop", "transcript_path": str(tmp_path / "s.jsonl")})
    assert env["spawned"] == []
    sessions.clear_heartbeat()
    assert not sessions.watcher_alive()


# -- Claude Code installer -----------------------------------------------------------
def test_claude_install_is_idempotent_and_uninstall_restores_bytes(env):
    settings = sessions.claude_config_dir() / "settings.json"
    settings.parent.mkdir(parents=True)
    original = '{\n    "model": "opus",\n    "hooks": {\n        "PreToolUse": [\n            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}\n        ]\n    }\n}\n'
    settings.write_text(original)

    dry = hooks.install_claude(dry_run=True)
    assert dry.action == "would install" and "+" in dry.diff and settings.read_text() == original

    ch = hooks.install_claude()
    assert ch.action == "installed"
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "echo mine"  # the user's own hook is preserved
    ours = pre[1]
    assert ours["matcher"] == "*" and ours["hooks"][0]["async"] is True and ours["hooks"][0]["command"].endswith("-m traceglass hook claude-code")
    assert "matcher" not in data["hooks"]["UserPromptSubmit"][0] and "matcher" not in data["hooks"]["Stop"][0]
    assert set(hooks.CLAUDE_EVENTS) <= set(data["hooks"])
    assert '    "model"' in settings.read_text()  # original 4-space indent kept

    assert hooks.install_claude().action == "unchanged"
    assert [(sc, found) for sc, _, found in hooks.claude_status()] == [("user", True)]

    un = hooks.uninstall_claude()
    assert un.action == "uninstalled" and settings.read_text() == original
    assert hooks.uninstall_claude().action == "unchanged"


def test_claude_uninstall_removes_file_it_created(env):
    settings = sessions.claude_config_dir() / "settings.json"
    hooks.install_claude()
    assert settings.exists()
    hooks.uninstall_claude()
    assert not settings.exists()


def test_claude_uninstall_keeps_later_user_edits(env):
    settings = sessions.claude_config_dir() / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "opus"}\n')
    hooks.install_claude()
    data = json.loads(settings.read_text())
    data["theme"] = "dark"  # the user changed something else meanwhile
    settings.write_text(json.dumps(data, indent=2))
    hooks.uninstall_claude()
    assert json.loads(settings.read_text()) == {"model": "opus", "theme": "dark"}


def test_claude_project_scope_and_quoting(env, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/my python/bin/python3")
    ch = hooks.install_claude(scope="local")
    assert ch.path == env["tmp"] / ".claude" / "settings.local.json"
    cmd = json.loads(ch.path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.startswith("'/opt/my python/bin/python3' -m traceglass hook claude-code")


def test_claude_refuses_invalid_json(env):
    settings = sessions.claude_config_dir() / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    with pytest.raises(ValueError):
        hooks.install_claude()
    assert settings.read_text() == "{not json"


# -- Kiro installer -------------------------------------------------------------------
def test_kiro_install_uninstall_and_managed_files(env):
    agents = hooks.kiro_agents_dir()
    agents.mkdir(parents=True)
    default = agents / "default.json"
    original = json.dumps({"name": "default", "tools": ["*"], "hooks": {}}, indent=2) + "\n"
    default.write_text(original)
    managed = agents / "team.json"
    managed_text = json.dumps({"name": "team", "description": "⚠️ managed by AIM. DO NOT EDIT MANUALLY."}, indent=2)
    managed.write_text(managed_text)

    [ch] = hooks.install_kiro()
    assert ch.action == "installed"
    h = json.loads(default.read_text())["hooks"]
    assert set(h) == set(hooks.KIRO_EVENTS)
    assert h["preToolUse"][0]["matcher"] == "*" and h["preToolUse"][0]["command"].endswith("hook kiro --event preToolUse")
    assert "matcher" not in h["agentSpawn"][0]

    changes = {c.path.name: c for c in hooks.install_kiro(all_agents=True)}
    assert changes["default.json"].action == "unchanged"
    assert changes["team.json"].action == "skipped" and managed.read_text() == managed_text
    forced = {c.path.name: c for c in hooks.install_kiro(agents=["team"], force=True)}
    assert forced["team.json"].action == "installed"

    status = {p.stem: found for p, found in hooks.kiro_status()}
    assert status == {"default": True, "team": True}

    hooks.uninstall_kiro(all_agents=True)
    assert default.read_text() == original and managed.read_text() == managed_text
    assert hooks.install_kiro(agents=["missing"])[0].action == "skipped"


# -- /api/status ----------------------------------------------------------------------
def test_api_status(env, tmp_path):
    from traceglass.server import start_background

    (sessions.claude_projects_dir()).mkdir(parents=True)
    hooks.install_claude()
    hooks.handle("claude-code", {**CC_PRE, "session_id": "sess-2"})
    srv, url = start_background(db=str(tmp_path / "s.db"))
    try:
        srv.store.write([{"type": "run", "id": "cc_x", "name": "x", "source": "claude-code"}, {"type": "run", "id": "r2", "name": "y", "source": "sdk"}])
        t = time.perf_counter()
        with urllib.request.urlopen(url + "/api/status") as resp:
            st = json.loads(resp.read())
        assert (time.perf_counter() - t) < 1.0
    finally:
        srv.shutdown()
        srv.server_close()
    assert st["watcher"] == {"alive": False, "pid": None, "last_beat": None, "in_process": False}
    assert st["hooks"]["claude-code"]["installed"][0]["scope"] == "user"
    assert isinstance(st["hooks"]["claude-code"]["last_event"], float)
    assert st["hooks"]["kiro"] == {"installed": [], "last_event": None}
    assert st["sources"]["claude-code"] == {"available": True, "dir": str(sessions.claude_projects_dir()), "runs": 1}
    assert st["sources"]["kiro"]["available"] is False and st["sources"]["kiro"]["runs"] == 0
