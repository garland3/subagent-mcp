"""Phase 1 tests: RESULT.md contract, Stop-hook sentinel, STATUS.json
wrapper, resume_subagent, and check_subagent result-when-idle.

These exercise the new behaviour added by Phase 1 of the agent-fleet plan
(see DESIGN-AGENT-FLEET-CONTROL-PLANE.md §12, items 1.1–1.5). They use the
same isolated tmux + fake CLI fixture as the other test phases, plus
synthetic RESULT.md / sentinel files written into the run dir.
"""

import json
import stat
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from subagent_mcp.config import set_config
from subagent_mcp.runners import build_runner
from subagent_mcp.runs import Run, discover_runs
from subagent_mcp.server import (
    _check_stop_sentinel,
    _install_stop_hook,
    _result_payload,
    _stop_hook_script_path,
    _stop_sentinel_path,
    check_subagent,
    launch_subagent,
    resume_subagent,
    stop_subagent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_for(cfg, run_id: str) -> Run:
    reg = discover_runs(cfg.runs_root)
    return reg[run_id]


def _write_result_md(run: Run, content: str) -> Path:
    path = run.run_dir / "RESULT.md"
    path.write_text(content, encoding="utf-8")
    return path


def _write_status_json(run: Run, data: dict) -> Path:
    path = run.run_dir / "STATUS.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _touch_sentinel(run: Run) -> Path:
    path = _stop_sentinel_path(run)
    path.touch()
    return path


# ---------------------------------------------------------------------------
# 1.1 — RESULT.md contract: SUBAGENT_RESULT_FILE in run.sh + prompt augmentation
# ---------------------------------------------------------------------------

def test_run_sh_exports_subagent_result_file(tmp_path):
    """The generated run.sh must export SUBAGENT_RESULT_FILE."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="do something",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    run_sh = runner.write_wrapper("rid", tmp_path / "cwd")
    text = run_sh.read_text()
    assert "SUBAGENT_RESULT_FILE=" in text
    assert "RESULT.md" in text
    # The env var points at the run dir's RESULT.md.
    assert str(run_dir / "RESULT.md") in text


def test_prompt_md_contains_standing_instruction(tmp_path):
    """The persisted prompt.md must include the RESULT.md standing instruction."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="fix the bug",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
    )
    runner.write_wrapper("rid", tmp_path / "cwd")
    content = (run_dir / "prompt.md").read_text(encoding="utf-8")
    assert content.startswith("fix the bug")
    assert "RESULT.md" in content
    assert "What you did" in content
    assert "open questions" in content.lower() or "Open questions" in content


def test_pointer_mode_prompt_also_has_instruction(tmp_path):
    """Pointer mode must also append the instruction to prompt.md."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="review the PR",
        prompt_mode="pointer",
        run_dir=run_dir,
        dangerous=True,
    )
    runner.write_wrapper("rid", tmp_path / "cwd")
    content = (run_dir / "prompt.md").read_text(encoding="utf-8")
    assert content.startswith("review the PR")
    assert "RESULT.md" in content


def test_launch_payload_includes_result_file(env):
    """launch_subagent return must include the result_file path."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="test result file",
            cwd=str(env["tmp"]),
            cli="claude",
            session="result-file",
            settle_seconds=1,
        )
    )
    assert out["result_file"].endswith("RESULT.md")
    stop_subagent(handle=out["pane_id"], kill_window=True)


# ---------------------------------------------------------------------------
# 1.2 — Stop-hook confirmation: sentinel + hook installation
# ---------------------------------------------------------------------------

def test_stop_hook_installed_for_claude(env):
    """launch_subagent installs a Stop hook in .claude/settings.local.json for claude."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="hook me up",
            cwd=str(env["tmp"]),
            cli="claude",
            session="hook-claude",
            settle_seconds=1,
        )
    )
    settings_path = env["tmp"] / ".claude" / "settings.local.json"
    assert settings_path.is_file(), "settings.local.json was not created"

    settings = json.loads(settings_path.read_text())
    stop_hooks = settings.get("hooks", {}).get("Stop", [])
    assert stop_hooks, "no Stop hook entry"

    run = _run_for(cfg, out["run_id"])
    script_path = str(_stop_hook_script_path(run))
    commands = [
        h.get("command")
        for entry in stop_hooks
        for h in entry.get("hooks", [])
    ]
    assert script_path in commands, "this run's hook script is not registered"

    # The hook script exists and is executable.
    assert _stop_hook_script_path(run).is_file()
    assert _stop_hook_script_path(run).stat().st_mode & stat.S_IEXEC

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_stop_hook_not_installed_for_opencode(env):
    """opencode does not get a Stop hook (relies on transcript parsing)."""
    cfg = env["cfg"]
    set_config(cfg)
    json.loads(
        launch_subagent(
            prompt="no hook for opencode",
            cwd=str(env["tmp"]),
            cli="opencode",
            session="hook-opencode",
            settle_seconds=1,
        )
    )
    settings_path = env["tmp"] / ".claude" / "settings.local.json"
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text())
        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        assert not stop_hooks, "opencode should not install a Stop hook"


def test_stop_hook_idempotent(env):
    """Launching twice in the same cwd does not duplicate the hook entry."""
    cfg = env["cfg"]
    set_config(cfg)
    out1 = json.loads(
        launch_subagent(
            prompt="first hook",
            cwd=str(env["tmp"]),
            cli="claude",
            session="hook-dedup",
            window="one",
            settle_seconds=1,
        )
    )
    # Manually re-install for the same run — should be a no-op.
    run1 = _run_for(cfg, out1["run_id"])
    _install_stop_hook(run1, env["tmp"], cfg)
    _install_stop_hook(run1, env["tmp"], cfg)

    settings = json.loads(
        (env["tmp"] / ".claude" / "settings.local.json").read_text()
    )
    script_path = str(_stop_hook_script_path(run1))
    commands = [
        h.get("command")
        for entry in settings.get("hooks", {}).get("Stop", [])
        for h in entry.get("hooks", [])
    ]
    assert commands.count(script_path) == 1, "hook was duplicated"

    stop_subagent(handle=out1["pane_id"], kill_window=True)


def test_stop_hook_merge_preserves_existing(env):
    """Installing a hook must not clobber existing settings.local.json content."""
    cfg = env["cfg"]
    set_config(cfg)

    # Pre-existing user settings with a custom hook.
    settings_dir = env["tmp"] / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    existing = {
        "permissions": {"allow": ["Bash(git:*)"]},
        "hooks": {"Stop": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": "echo user-hook"},
            ]},
        ]},
    }
    (settings_dir / "settings.local.json").write_text(json.dumps(existing))

    out = json.loads(
        launch_subagent(
            prompt="merge my hook",
            cwd=str(env["tmp"]),
            cli="claude",
            session="hook-merge",
            settle_seconds=1,
        )
    )
    settings = json.loads(
        (env["tmp"] / ".claude" / "settings.local.json").read_text()
    )
    # User's permission config is preserved.
    assert settings.get("permissions", {}).get("allow") == ["Bash(git:*)"]
    # User's hook is preserved.
    all_commands = [
        h.get("command")
        for entry in settings.get("hooks", {}).get("Stop", [])
        for h in entry.get("hooks", [])
    ]
    assert "echo user-hook" in all_commands

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_stop_hook_removed_on_stop(env):
    """stop_subagent removes the run's hook entry from settings.local.json."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="remove my hook",
            cwd=str(env["tmp"]),
            cli="claude",
            session="hook-remove",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    script_path = str(_stop_hook_script_path(run))

    stop_subagent(handle=out["pane_id"], kill_window=True)

    settings_path = env["tmp"] / ".claude" / "settings.local.json"
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text())
        all_commands = [
            h.get("command")
            for entry in settings.get("hooks", {}).get("Stop", [])
            for h in entry.get("hooks", [])
        ]
        assert script_path not in all_commands, "stale hook entry not cleaned up"


def test_sentinel_check_returns_idle():
    """_check_stop_sentinel returns 'idle' when the sentinel file exists."""
    run = Run(
        run_id="r1", run_dir=Path("/tmp"), cwd=Path("/tmp"), cli="claude",
        session="s", window="w",
    )
    # No sentinel — should fall back.
    state, activity = _check_stop_sentinel(run)
    assert state is None

    # Touch the sentinel.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        run2 = Run(
            run_id="r2", run_dir=Path(d), cwd=Path(d), cli="claude",
            session="s", window="w",
        )
        (Path(d) / ".stop-sentinel").touch()
        state, activity = _check_stop_sentinel(run2)
        assert state == "idle"
        assert activity  # ISO timestamp


def test_sentinel_overrides_transcript_state(env):
    """When the sentinel exists, state_for returns idle even mid-transcript."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="sentinel test",
            cwd=str(env["tmp"]),
            cli="claude",
            session="sentinel",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])

    # Write a transcript that says "working" (has tool_use).
    transcript_dir = cfg.claude_projects_root / str(run.cwd).replace("/", "-")
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / f"{run.session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        ]}}) + "\n"
    )

    # Without sentinel, state is "working".
    snap = json.loads(check_subagent(handle=out["pane_id"], lines=10))
    assert snap["state"] == "working"

    # Touch the sentinel — state should become "idle".
    _touch_sentinel(run)
    snap = json.loads(check_subagent(handle=out["pane_id"], lines=10))
    assert snap["state"] == "idle"

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_stop_hook_disabled_by_config(env):
    """When install_stop_hooks=False, no hook is installed."""
    cfg = env["cfg"]
    object.__setattr__(cfg, "install_stop_hooks", False)
    set_config(cfg)
    json.loads(
        launch_subagent(
            prompt="no hook please",
            cwd=str(env["tmp"]),
            cli="claude",
            session="no-hook",
            settle_seconds=1,
        )
    )
    settings_path = env["tmp"] / ".claude" / "settings.local.json"
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text())
        assert not settings.get("hooks", {}).get("Stop")


# ---------------------------------------------------------------------------
# 1.3 — STATUS.json: wrapper writes exit code, git SHA, changed files, etc.
# ---------------------------------------------------------------------------

def test_run_sh_contains_status_json_writing(tmp_path):
    """The generated run.sh must contain the STATUS.json writing logic."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="write status",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    run_sh = runner.write_wrapper("rid", tmp_path / "cwd")
    text = run_sh.read_text()
    assert "STATUS.json" in text
    assert "SUBAGENT_STATUS_FILE" in text
    assert "git_sha_before" in text
    assert "git_sha_after" in text
    assert "changed_files" in text
    assert "git_branch" in text
    assert "pr_url" in text
    assert "exit_code" in text
    assert "started_at" in text
    assert "ended_at" in text


def test_run_sh_bash_syntax_valid(tmp_path):
    """The generated run.sh must pass bash -n (syntax check)."""
    import subprocess

    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="syntax check",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    run_sh = runner.write_wrapper("rid", tmp_path / "cwd")
    result = subprocess.run(
        ["bash", "-n", str(run_sh)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_resume_sh_bash_syntax_valid(tmp_path):
    """The generated resume.sh must also pass bash -n."""
    import subprocess

    # We can't easily call resume_subagent without a tmux fixture, but we can
    # check the run.sh for an opencode run also passes syntax check.
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    runner = build_runner(
        "opencode",
        prompt="syntax check",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
    )
    run_sh = runner.write_wrapper("rid", tmp_path / "cwd")
    result = subprocess.run(
        ["bash", "-n", str(run_sh)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# 1.4 — resume_subagent: claude --resume / opencode run -s
# ---------------------------------------------------------------------------

def test_resume_fails_on_capture_failed(env):
    """resume_subagent must fail with a clear message when session_id_status is capture_failed."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="unresumable",
            cwd=str(env["tmp"]),
            cli="opencode",
            session="resume-fail",
            settle_seconds=1,
        )
    )
    assert out["session_id_status"] == "capture_failed"

    with pytest.raises(ToolError) as exc:
        resume_subagent(handle=out["run_id"], message="try to resume me")
    msg = str(exc.value)
    assert "not resumable" in msg.lower() or "capture_failed" in msg

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_resume_fails_on_unknown_run(env):
    """resume_subagent must fail for a run that doesn't exist."""
    cfg = env["cfg"]
    set_config(cfg)
    with pytest.raises(ToolError):
        resume_subagent(handle="nonexistent-run-id", message="hello")


def test_resume_claude_creates_new_pane_with_resume_command(env):
    """resume_subagent creates a new pane with claude --resume <session_id>."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="original task",
            cwd=str(env["tmp"]),
            cli="claude",
            session="resume-claude",
            settle_seconds=1,
        )
    )
    sid = out["session_id"]
    assert sid

    result = json.loads(
        resume_subagent(handle=out["run_id"], message="also fix the tests")
    )
    assert result["resumed"] is True
    assert result["alive"] is True
    assert result["session_id"] == sid

    # The fake claude logs argv; the --resume flag and session_id should be present.
    time.sleep(0.5)
    log_lines = env["log"].read_text().splitlines()
    entries = [json.loads(line) for line in log_lines if line.strip()]
    resume_entry = entries[-1]
    assert "--resume" in resume_entry["argv"]
    assert sid in resume_entry["argv"]
    assert "also fix the tests" in resume_entry["prompt"]

    stop_subagent(handle=result["pane_id"], kill_window=True)


def test_resume_kills_old_pane(env):
    """resume_subagent kills the old pane before creating the new one."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="original",
            cwd=str(env["tmp"]),
            cli="claude",
            session="resume-kill",
            settle_seconds=1,
        )
    )
    old_pane = out["pane_id"]
    assert old_pane

    result = json.loads(
        resume_subagent(handle=out["run_id"], message="follow up")
    )
    new_pane = result["pane_id"]
    assert new_pane != old_pane

    # Old pane should be gone.
    live = env["tmux"].list_panes()
    live_ids = {p["pane_id"] for p in live}
    assert old_pane not in live_ids
    assert new_pane in live_ids

    stop_subagent(handle=new_pane, kill_window=True)


def test_resume_works_after_pane_closed(env):
    """resume_subagent works when the original pane is already gone (§3.2)."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="i will close",
            cwd=str(env["tmp"]),
            cli="claude",
            session="resume-dead",
            settle_seconds=1,
        )
    )
    sid = out["session_id"]

    # Kill the pane manually.
    env["tmux"].kill_window(out["pane_id"])
    time.sleep(0.3)

    result = json.loads(
        resume_subagent(handle=out["run_id"], message="come back")
    )
    assert result["resumed"] is True
    assert result["alive"] is True
    assert result["session_id"] == sid

    stop_subagent(handle=result["pane_id"], kill_window=True)


def test_resume_clears_old_sentinel(env):
    """resume_subagent clears the old Stop-hook sentinel (fresh start)."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="sentinel before resume",
            cwd=str(env["tmp"]),
            cli="claude",
            session="resume-sentinel",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _touch_sentinel(run)
    assert _stop_sentinel_path(run).exists()

    result = json.loads(
        resume_subagent(handle=out["run_id"], message="follow up")
    )
    # Sentinel should be cleared.
    assert not _stop_sentinel_path(run).exists()

    stop_subagent(handle=result["pane_id"], kill_window=True)


def test_resume_subagent_docstring_is_informative():
    """The docstring is the only text the model sees — it must be informative."""
    doc = resume_subagent.__doc__ or ""
    assert len(doc) > 100, "resume_subagent docstring too terse"
    assert "session_id" in doc
    assert "resume" in doc.lower()
    assert "capture_failed" in doc
    assert "run_id" in doc
    assert "session:window" in doc or "sess:win" in doc
    assert "bare window" in doc


# ---------------------------------------------------------------------------
# 1.5 — check_subagent returns RESULT.md when idle, tail when working
# ---------------------------------------------------------------------------

def test_check_returns_result_md_when_idle(env):
    """When state is idle and RESULT.md exists, check_subagent returns its content."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="i will finish",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-idle-result",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])

    # Write a transcript that says "idle" (assistant with no tool_use).
    transcript_dir = cfg.claude_projects_root / str(run.cwd).replace("/", "-")
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / f"{run.session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "all done"},
        ]}}) + "\n"
    )

    # Write RESULT.md.
    _write_result_md(run, "## Result\n\nFixed the bug.\n- Verified: tests pass\n- Could not: update docs")

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["result"] == "present"
    assert "Fixed the bug" in snap["result_md"]
    assert "tests pass" in snap["result_md"]

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_check_notes_result_absence_when_idle(env):
    """When state is idle and RESULT.md is absent, result must be 'none'."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="i will finish without writing result",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-idle-none",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])

    # Write a transcript that says "idle".
    transcript_dir = cfg.claude_projects_root / str(run.cwd).replace("/", "-")
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / f"{run.session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}}) + "\n"
    )

    # Do NOT write RESULT.md.
    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["result"] == "none"
    assert snap["result_md"] == ""

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_check_returns_status_json_when_present(env):
    """When idle and STATUS.json exists, check_subagent includes it."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="with status",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-status",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])

    # Make the state idle.
    _touch_sentinel(run)
    _write_status_json(run, {
        "exit_code": 0,
        "git_sha_before": "abc123",
        "git_sha_after": "def456",
        "changed_files": ["src/app.py"],
    })

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["status"] is not None
    assert snap["status"]["exit_code"] == 0
    assert "src/app.py" in snap["status"]["changed_files"]

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_check_status_null_when_absent(env):
    """When STATUS.json doesn't exist, status should be null."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="no status file",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-no-status",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _touch_sentinel(run)

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["status"] is None

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_check_returns_tail_when_working(env):
    """When state is working, check_subagent returns the pane tail, not result."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="still working",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-working",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])

    # Write a transcript that says "working" (has tool_use).
    transcript_dir = cfg.claude_projects_root / str(run.cwd).replace("/", "-")
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / f"{run.session_id}.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        ]}}) + "\n"
    )

    # Write RESULT.md (should NOT be returned while working).
    _write_result_md(run, "should not appear in working output")

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "working"
    assert "result" not in snap or snap.get("result") is None or snap.get("result") == ""
    assert "result_md" not in snap or snap.get("result_md") == ""

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_check_result_md_capped():
    """_result_payload caps RESULT.md to keep the payload small."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        run = Run(
            run_id="r1", run_dir=Path(d), cwd=Path(d), cli="claude",
            session="s", window="w",
        )
        long_content = "A" * 10000
        _write_result_md(run, long_content)
        payload = _result_payload(run)
        assert len(payload["result_md"]) < 10000
        assert "truncated" in payload["result_md"].lower() or "…" in payload["result_md"]


def test_check_idle_output_empty(env):
    """When idle, the output field should be empty (keep payload small)."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="idle output test",
            cwd=str(env["tmp"]),
            cli="claude",
            session="check-idle-output",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _touch_sentinel(run)

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["output"] == ""

    stop_subagent(handle=out["pane_id"], kill_window=True)