"""Phase 0 tests: session_id, transcript-based state, stop reconciliation,
box-drawing strip, and docstring quality.

These exercise the new behaviour added by Phase 0 of the agent-fleet plan
(see DESIGN-AGENT-FLEET-CONTROL-PLANE.md §12). They use the same isolated
tmux + fake CLI fixture as the other test phases, plus synthetic claude
transcript files written under cfg.claude_projects_root.
"""

import json
import os
import re
import stat
import time
from pathlib import Path

from subagent_mcp.config import set_config
from subagent_mcp.runs import Run, derive_state, discover_runs
from subagent_mcp.server import (
    _find_uuid,
    _strip_box_drawing,
    _tail,
    check_subagent,
    launch_subagent,
    list_subagents,
    stop_subagent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claude_transcript_path(cfg, run: Run) -> Path:
    cwd_slug = str(run.cwd).replace("/", "-")
    return cfg.claude_projects_root / cwd_slug / f"{run.session_id}.jsonl"


def _write_claude_transcript(cfg, run: Run, records: list) -> Path:
    transcript = _claude_transcript_path(cfg, run)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return transcript


def _assistant_record(content) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _run_for(cfg, run_id: str) -> Run:
    reg = discover_runs(cfg.runs_root)
    return reg[run_id]


# ---------------------------------------------------------------------------
# 0.1 — session_id minted (claude) and captured (opencode)
# ---------------------------------------------------------------------------

def test_claude_session_id_minted_and_in_argv(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="mint me a session",
            cwd=str(env["tmp"]),
            cli="claude",
            session="sid-claude",
            settle_seconds=1,
        )
    )
    assert out["session_id_status"] == "minted"
    sid = out["session_id"]
    assert sid and re.match(r"^[0-9a-f-]{36}$", sid), sid

    # The fake claude logs argv + session_id; the --session-id flag is present.
    time.sleep(0.3)
    entry = json.loads(env["log"].read_text().splitlines()[-1])
    assert "--session-id" in entry["argv"]
    assert entry["session_id"] == sid

    # meta.json on disk records the same id.
    run = _run_for(cfg, out["run_id"])
    assert run.session_id == sid
    assert run.session_id_status == "minted"

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_opencode_capture_failed_is_visible(env):
    """opencode has no pre-assign flag; a failed capture must be VISIBLE, not silent."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="capture me if you can",
            cwd=str(env["tmp"]),
            cli="opencode",
            session="sid-opencode",
            settle_seconds=1,
        )
    )
    # The fake opencode returns [] from `session list`, so capture fails.
    assert out["session_id_status"] == "capture_failed"
    assert out["session_id"] == ""

    run = _run_for(cfg, out["run_id"])
    assert run.session_id_status == "capture_failed"
    assert run.session_id == ""

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_claude_session_id_passed_to_runner_argv(env):
    """build_runner must inject --session-id <uuid> for claude only."""
    from subagent_mcp.runners import build_runner

    run_dir = env["tmp"] / "runner-test"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="hi",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    argv = runner.argv()
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "11111111-2222-3333-4444-555555555555"

    # opencode has no pre-assign flag; build_runner must not add --session-id.
    run_dir2 = env["tmp"] / "runner-test2"
    run_dir2.mkdir()
    op_runner = build_runner(
        "opencode",
        prompt="hi",
        prompt_mode="inline",
        run_dir=run_dir2,
        dangerous=True,
        session_id="11111111-2222-3333-4444-555555555555",
    )
    assert "--session-id" not in op_runner.argv()


# ---------------------------------------------------------------------------
# 0.2 — transcript-based state
# ---------------------------------------------------------------------------

def test_state_idle_when_last_record_is_final_assistant(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="go idle",
            cwd=str(env["tmp"]),
            cli="claude",
            session="state-idle",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _write_claude_transcript(cfg, run, [
        {"type": "user", "message": {"role": "user", "content": "go idle"}},
        _assistant_record([{"type": "text", "text": "all done"}]),
    ])

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "idle"
    assert snap["last_activity"]

    listing = json.loads(list_subagents())
    item = next(e for e in listing["subagents"] if e["run_id"] == out["run_id"])
    assert item["state"] == "idle"

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_state_working_when_last_record_has_tool_use(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="keep working",
            cwd=str(env["tmp"]),
            cli="claude",
            session="state-working",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _write_claude_transcript(cfg, run, [
        {"type": "user", "message": {"role": "user", "content": "keep working"}},
        _assistant_record([
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "x"}},
        ]),
    ])

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == "working"

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_state_unknown_when_no_session_id(env):
    """A run with no session_id (capture_failed) returns empty state, not a crash."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="no sid",
            cwd=str(env["tmp"]),
            cli="opencode",
            session="state-nosid",
            settle_seconds=1,
        )
    )
    assert out["session_id_status"] == "capture_failed"

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["state"] == ""
    assert snap["last_activity"] == ""

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_state_survives_dead_pane(env):
    """State must be derivable whether or not the pane is alive — the containerisation signal."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="survive my pane",
            cwd=str(env["tmp"]),
            cli="claude",
            session="state-dead",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    _write_claude_transcript(cfg, run, [
        _assistant_record([{"type": "text", "text": "done before pane closed"}]),
    ])

    # Kill the pane out from under the run.
    env["tmux"].kill_window(out["pane_id"])
    time.sleep(0.3)

    snap = json.loads(check_subagent(handle=out["pane_id"], lines=20))
    assert snap["alive"] is False
    assert snap["state"] == "idle"

    # Clean up the run dir since the pane is gone.
    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_state_idle_after_quiescence_without_final_record(env):
    """A stale transcript mtime is idle even if the last record is not a clean assistant turn."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="go quiet",
            cwd=str(env["tmp"]),
            cli="claude",
            session="state-quiet",
            settle_seconds=1,
        )
    )
    run = _run_for(cfg, out["run_id"])
    transcript = _write_claude_transcript(cfg, run, [
        {"type": "user", "message": {"role": "user", "content": "go quiet"}},
    ])
    # Backdate the file so the quiescence window considers it idle.
    old = time.time() - 60
    os.utime(transcript, (old, old))

    state, _ = derive_state(
        run,
        claude_projects_root=cfg.claude_projects_root,
        opencode_state_root=cfg.opencode_state_root,
    )
    assert state == "idle"

    stop_subagent(handle=out["pane_id"], kill_window=True)


# ---------------------------------------------------------------------------
# 0.3 — stop_subagent dead-pane reconciliation
# ---------------------------------------------------------------------------

def test_stop_already_dead_reconciles(env):
    """A pane that is already gone is SUCCESS, not an error — the 33% failure mode."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="i will die on my own",
            cwd=str(env["tmp"]),
            cli="claude",
            session="stop-dead",
            settle_seconds=1,
        )
    )
    pane_id = out["pane_id"]
    run_id = out["run_id"]

    # Simulate the pane dying before the operator calls stop.
    env["tmux"].kill_window(pane_id)
    time.sleep(0.3)

    result = json.loads(stop_subagent(handle=pane_id))
    assert result["alive"] is False
    assert result["reconciled"] is True

    # The run directory is dropped from the registry.
    assert run_id not in discover_runs(cfg.runs_root)


def test_stop_alive_sends_signal_and_does_not_reconcile(env):
    """A live pane gets the signal; the run dir stays for sweep_stale_subagents."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="i am alive",
            cwd=str(env["tmp"]),
            cli="claude",
            session="stop-alive",
            settle_seconds=1,
        )
    )
    result = json.loads(stop_subagent(handle=out["pane_id"], kill_window=True))
    assert result["alive"] is False
    # A live-then-killed pane is NOT reconciled: the run dir stays so the
    # transcript/session_id are retained and sweep can reap it later.
    assert result.get("reconciled") is not True or result["reconciled"] is False
    assert out["run_id"] in discover_runs(cfg.runs_root)

    # Clean up.
    json.loads(stop_subagent(handle=out["pane_id"], kill_window=True))


# ---------------------------------------------------------------------------
# 0.5 — _tail strips box-drawing and caps initial_output
# ---------------------------------------------------------------------------

def test_tail_strips_box_drawing():
    text = "┌────────┐\n│ hello  │\n└────────┘\nreal content\n"
    result = _tail(text, lines=10)
    for box in "┌┐└┘│─":
        assert box not in result
    assert "hello" in result
    assert "real content" in result
    # Lines that were only box-drawing are dropped entirely.
    assert result.strip().count("\n") < text.strip().count("\n")


def test_tail_drops_box_only_lines():
    text = "╭──────╮\n│ done │\n╰──────╯\n"
    result = _tail(text, lines=10)
    assert "done" in result
    # Only the "done" line survives — the borders were box-only.
    assert len(result.strip().splitlines()) == 1


def test_tail_caps_to_n_lines():
    text = "\n".join(f"line {i}" for i in range(50))
    result = _tail(text, lines=20)
    assert len(result.splitlines()) <= 20
    assert "line 49" in result
    assert "line 0" not in result


def test_tail_preserves_content_without_box_chars():
    text = "ordinary output\nwith no borders\n"
    result = _tail(text, lines=10)
    assert "ordinary output" in result
    assert "with no borders" in result


def test_initial_output_capped_at_20_lines(env):
    """launch_subagent's initial_output must be capped to 20 lines (down from 40)."""
    cfg = env["cfg"]
    set_config(cfg)
    # A fake claude that prints 60 lines of plain content.
    fake_bin = env["tmp"] / "fakebin-long"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        + "\n".join(f"echo line {i}" for i in range(60))
        + "\nsleep 120\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"

    out = json.loads(
        launch_subagent(
            prompt="long output",
            cwd=str(env["tmp"]),
            cli="claude",
            session="cap-test",
            settle_seconds=1.5,
        )
    )
    initial = out["initial_output"]
    assert len(initial.splitlines()) <= 20
    assert "line 59" in initial  # the last line is kept
    assert "line 0" not in initial  # the first line is well past the cap

    stop_subagent(handle=out["pane_id"], kill_window=True)


# ---------------------------------------------------------------------------
# 0.4 — docstrings reach the model and mention the handle format
# ---------------------------------------------------------------------------

def test_subagent_docstrings_describe_purpose_and_handle_format():
    """The tool's own docstring is the ONLY text that reaches the model."""
    from subagent_mcp import server

    for name in (
        "launch_subagent",
        "check_subagent",
        "watch_subagent",
        "list_subagents",
        "send_to_subagent",
        "stop_subagent",
        "sweep_stale_subagents",
    ):
        fn = getattr(server, name)
        doc = fn.__doc__ or ""
        assert doc, f"{name} has no docstring"
        # Each should say what it is for (not just a verb phrase).
        assert len(doc) > 60, f"{name} docstring too terse: {doc!r}"

    # The handle-taking tools must document the handle format explicitly.
    for name in (
        "check_subagent",
        "watch_subagent",
        "send_to_subagent",
        "stop_subagent",
    ):
        doc = getattr(server, name).__doc__ or ""
        assert "run_id" in doc, f"{name} docstring must mention run_id"
        assert "session:window" in doc or "sess:win" in doc, (
            f"{name} docstring must mention the session:window handle format"
        )
        assert "bare window" in doc, (
            f"{name} docstring must warn against bare window names"
        )

    # launch_subagent returns the handle and should mention the format too.
    assert "run_id" in (server.launch_subagent.__doc__ or "")
    assert "session:window" in (server.launch_subagent.__doc__ or "") or "sess:win" in (
        server.launch_subagent.__doc__ or ""
    )


def test_find_uuid_extracts_uuid():
    assert _find_uuid("blah zzz-11111111-2222-3333-4444-555555555555-done") == (
        "11111111-2222-3333-4444-555555555555"
    )
    assert _find_uuid("no uuid here") is None


def test_strip_box_drawing_is_idempotent_on_clean_text():
    assert _strip_box_drawing("plain text\nwith no borders\n") == "plain text\nwith no borders"