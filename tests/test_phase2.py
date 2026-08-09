import json
import os
import stat
import time
from pathlib import Path

import pytest
import shlex

from subagent_mcp.config import set_config
from subagent_mcp.runs import Run
from subagent_mcp.server import (
    _detect_blocked_output,
    _is_alive,
    launch_subagent,
    send_to_subagent,
    stop_subagent,
)
from subagent_mcp.tmuxio import Tmux


def _fake_cli_script(output: str) -> str:
    """Return a shell script that emits output, then sleeps."""
    return (
        "#!/usr/bin/env bash\n"
        f"{output}\n"
        "sleep 120\n"
    )


def test_stop_kill_window_other_pane_survives(env):
    """Direct version of the above."""
    from subagent_mcp.server import check_subagent

    cfg = env["cfg"]
    set_config(cfg)
    out1 = json.loads(
        launch_subagent(
            prompt="first",
            cwd=str(env["tmp"]),
            cli="claude",
            session="killpane2",
            window="fixit",
            settle_seconds=1,
        )
    )
    out2 = json.loads(
        launch_subagent(
            prompt="second",
            cwd=str(env["tmp"]),
            cli="claude",
            session="killpane2",
            window="fixit-2",
            settle_seconds=1,
        )
    )

    stop1 = json.loads(stop_subagent(handle=out1["pane_id"], kill_window=True))
    assert stop1["alive"] is False

    check2 = json.loads(check_subagent(handle=out2["pane_id"], lines=10))
    assert check2["alive"] is True

    stop_subagent(handle=out2["pane_id"], kill_window=True)


def test_exact_session_targeting(env):
    """Sessions that share a prefix should not be conflated."""
    cfg = env["cfg"]
    set_config(cfg)
    tmux = Tmux(socket=cfg.tmux_socket)
    tmux.ensure_session("atlas", env["tmp"])
    tmux.ensure_session("atlas-group", env["tmp"])

    out = json.loads(
        launch_subagent(
            prompt="task",
            cwd=str(env["tmp"]),
            cli="claude",
            session="atlas",
            window="fixit",
            settle_seconds=1,
        )
    )
    assert out["session"] == "atlas"
    assert out["window"] == "fixit"

    atlas_group_windows = {name for _, name in tmux.list_windows("atlas-group")}
    assert "fixit" not in atlas_group_windows
    assert "fixit" in {name for _, name in tmux.list_windows("atlas")}
    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_trust_dialog_detection():
    """Dialogs that wait for human input should be flagged."""
    sample = (
        "Some banner text\n"
        "Quick safety check\n"
        "Is this a project you created or one you trust? (y/N)"
    )
    blocked, reason = _detect_blocked_output(sample)
    assert blocked is True
    assert reason and "Quick safety check" in reason

    blocked, _ = _detect_blocked_output("normal subagent output here")
    assert blocked is False


def test_blocked_detection_no_false_positives():
    """Ordinary agent output must never be reported as blocked."""
    benign = [
        "❯ Fix the authentication bug in the login flow",
        "❯ Add a Sign in button to the header",
        "● Welcome to Claude Code v2.1.226",
        "❯ Review PR #738 which touches authentication middleware",
        "● Reading src/auth.py ... Authentication helper updated",
        "❯ refactor the sign in page",
    ]
    for line in benign:
        blocked, reason = _detect_blocked_output(line)
        assert blocked is False, f"false positive on {line!r}: {reason}"


def test_blocked_detection_ignores_echoed_prompt():
    """The TUI echoes the prompt back; that echo is not a dialog."""
    prompt = "Investigate why the Quick safety check dialog appears on new worktrees."
    pane = f"❯ {prompt}\n● Thinking...\n"
    blocked, _ = _detect_blocked_output(pane, prompt)
    assert blocked is False

    # The same phrase from the CLI itself (not the prompt) still trips it.
    real = "  Quick safety check: Is this a project you created or one you trust?"
    blocked, reason = _detect_blocked_output(pane + real, prompt)
    assert blocked is True and reason


def test_trust_dialog_launches_blocked(env, monkeypatch, tmp_path):
    """A fake CLI that prints a trust dialog should make launch_subagent report blocked."""
    cfg = env["cfg"]
    set_config(cfg)

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        _fake_cli_script(
            'echo "Quick safety check"; echo "Is this a project you created or one you trust?";',
        )
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    out = json.loads(
        launch_subagent(
            prompt="do work",
            cwd=str(env["tmp"]),
            cli="claude",
            session="trust-test",
            settle_seconds=1,
        )
    )
    assert out["blocked"] is True
    assert "interactive prompt" in (out.get("blocked_reason") or "").lower()
    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_pane_pid_recycle_detection():
    """A reused pane id after a tmux restart must not be trusted without matching pid."""
    run = Run(
        run_id="r1",
        run_dir=Path("/tmp"),
        cwd=Path("/tmp"),
        cli="claude",
        session="s",
        window="w",
        pane_id="%42",
        pane_pid=12345,
    )
    assert _is_alive(run, {("%42", 12345)}) is True
    assert _is_alive(run, {("%42", 99999)}) is False

    legacy = Run(
        run_id="r2",
        run_dir=Path("/tmp"),
        cwd=Path("/tmp"),
        cli="claude",
        session="s",
        window="w2",
        pane_id="%42",
        pane_pid=None,
    )
    assert _is_alive(legacy, {("%42", 1)}) is True


def test_send_to_subagent_uses_named_buffer(env):
    """send_to_subagent should paste text into the target pane."""
    from subagent_mcp.server import check_subagent

    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="echo start",
            cwd=str(env["tmp"]),
            cli="claude",
            session="sendtest",
            settle_seconds=1,
        )
    )
    pane_id = out["pane_id"]

    marker = "__subagent_marker_789__"
    json.loads(send_to_subagent(handle=pane_id, text=marker, submit=False))
    time.sleep(0.5)

    # tmux should have the pasted text in the pane's scrollback.
    snapshot = json.loads(check_subagent(handle=pane_id, lines=20))
    assert marker in snapshot["output"]
    stop_subagent(handle=pane_id, kill_window=True)


def test_runner_prompt_sentinel(env, tmp_path):
    """The prompt token in wrapper_command should not be confused with extra_args."""
    from subagent_mcp.runners import build_runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="real prompt",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=False,
        extra_args=["--add-dir", "$(cat /tmp/stuff)"],
    )
    cmd = runner.wrapper_command()
    # The prompt command substitution is left as an unquoted shell expansion
    # (wrapped in double quotes to keep it a single token). An extra argument
    # that happens to contain the same text must be quoted as a literal string.
    assert cmd.count('"$(cat') == 1
    assert "real prompt" not in cmd  # prompt content should expand at shell runtime only
    assert "'$(cat /tmp/stuff)'" in cmd  # extra arg quoted literally


def test_empty_prompt_rejected():
    """Launching without a real prompt should fail early."""
    from subagent_mcp.server import _read_prompt
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _read_prompt(prompt="", prompt_file=None)
    with pytest.raises(ToolError):
        _read_prompt(prompt=None, prompt_file=None)
