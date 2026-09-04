"""Fixes from the PR #1 review pass.

Three findings, all about promising more certainty than the evidence supports:
a stray file attributed to a run that cannot have written it, a boolean field
that sometimes held a string, and a launch response reporting the pane rather
than the agent.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from subagent_mcp.config import set_config
from subagent_mcp.runs import Run
from subagent_mcp.server import _reconcile_stray_result, launch_subagent


def _run_at(tmp_path, start: datetime) -> Run:
    run_dir = tmp_path / "rd"
    run_dir.mkdir(exist_ok=True)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return Run(
        run_id="rid",
        run_dir=run_dir,
        cwd=cwd,
        cli="claude",
        session="s",
        window="w",
        start_time=start.isoformat(),
    )


def test_stray_result_predating_the_run_is_left_alone(tmp_path):
    """An untracked RESULT.md older than the run cannot be this run's output.

    Moving it would silently remove someone's local file from their working
    tree — and a later sweep_stale_subagents would delete it for good.
    """
    run = _run_at(tmp_path, datetime.now(timezone.utc))
    stray = run.cwd / "RESULT.md"
    stray.write_text("my own notes, thanks", encoding="utf-8")
    old = time.time() - 3600
    import os

    os.utime(stray, (old, old))

    assert _reconcile_stray_result(run) is None
    assert stray.is_file()  # still where its owner left it
    assert not (run.run_dir / "RESULT.md").exists()


def test_stray_result_written_during_the_run_is_reconciled(tmp_path):
    run = _run_at(tmp_path, datetime.now(timezone.utc) - timedelta(minutes=5))
    stray = run.cwd / "RESULT.md"
    stray.write_text("agent output", encoding="utf-8")

    moved = _reconcile_stray_result(run)
    assert moved == str(stray)
    assert not stray.exists()
    assert (run.run_dir / "RESULT.md").read_text(encoding="utf-8") == "agent output"


def test_stopped_is_always_a_boolean(env):
    """`stopped` must not be a bool in some paths and the string "unverified"
    in another — callers cannot type-check a field that shifts shape."""
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="exit-now",
            cwd=str(env["tmp"]),
            cli="claude",
            session="stopschema",
            task="schema",
            settle_seconds=1.5,
        )
    )
    from subagent_mcp.server import stop_subagent

    stop = json.loads(stop_subagent(handle=out["run_id"]))
    assert isinstance(stop["stopped"], bool)
    assert isinstance(stop["verified"], bool)


def test_launch_reports_agent_liveness_not_pane_liveness(env):
    """A one-shot CLI that finishes inside settle_seconds is not alive.

    run.sh parks the pane at a shell afterwards, so pane existence says yes
    while the agent says no — launch used to disagree with an immediate
    check_subagent for exactly this reason.
    """
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="answer and exit",
            cwd=str(env["tmp"]),
            cli="atlas-chat",
            session="oneshot",
            task="quick",
            settle_seconds=3.0,
        )
    )
    # The fake atlas-chat returns immediately unless asked to stay open.
    assert out["alive"] is False


def test_wrapper_clears_stale_status_json(tmp_path):
    """STATUS.json means "the CLI tracked by this run dir has exited".

    A resume reuses the run dir, so the previous turn's STATUS.json is already
    sitting there — and `_state_for` reads its presence as completion. Without
    clearing it, a resumed run whose transcript is unreadable would report
    idle from the moment it started. The wrapper removes it up front so the
    file only ever describes the run now starting.
    """
    from subagent_mcp.runners import build_runner

    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "atlas-chat", prompt="p", prompt_mode="pointer", run_dir=run_dir
    )
    text = runner.write_wrapper("rid", tmp_path).read_text()
    status = str(run_dir / "STATUS.json")
    # The removal must precede the CLI invocation, or it would delete the
    # status the run just wrote.
    assert f"rm -f {status}" in text
    assert text.index(f"rm -f {status}") < text.index("code=$?")


def test_stale_status_json_is_actually_removed_at_runtime(tmp_path):
    """End-to-end through bash, not just a string match on the wrapper."""
    import subprocess

    from subagent_mcp.runners import build_runner

    run_dir = tmp_path / "r2"
    run_dir.mkdir()
    stale = run_dir / "STATUS.json"
    stale.write_text('{"exit_code": 0, "run_id": "an-earlier-turn"}', encoding="utf-8")

    runner = build_runner(
        "atlas-chat",
        prompt="p",
        prompt_mode="pointer",
        run_dir=run_dir,
        executable="true",
    )
    wrapper = runner.write_wrapper("rid2", tmp_path)
    # Drop the trailing interactive shell so this can run headless.
    body = "\n".join(
        ln for ln in wrapper.read_text().splitlines() if not ln.startswith("exec ")
    )
    subprocess.run(["bash", "-c", body], cwd=str(tmp_path), timeout=60, check=False)

    # The wrapper wrote a fresh one describing *this* run, not the old turn.
    written = json.loads(stale.read_text(encoding="utf-8"))
    assert written["run_id"] == "rid2"


def test_overlapping_run_in_same_cwd_blocks_reconciliation(tmp_path, env):
    """Two runs sharing a cwd must not claim each other's RESULT.md.

    mtime rules out files older than the run, but not a *rival* run that was
    already going when the file appeared. Reconciling the wrong one moves the
    result under the wrong name, where a later sweep deletes it. When more
    than one run could plausibly have written it, nobody claims it.
    """
    import shutil

    from subagent_mcp.runs import create_run_dir

    cfg = env["cfg"]
    set_config(cfg)
    work = tmp_path / "shared"
    work.mkdir()

    def _mk(task: str) -> Run:
        return create_run_dir(
            cfg.runs_root,
            session="s",
            window=task,
            cwd=work,
            cli="claude",
            model=None,
            agent=None,
            task=task,
            argv=[],
        )

    mine = _mk("mine")
    rival = _mk("rival")  # same cwd, started before the file appeared

    stray = work / "RESULT.md"
    stray.write_text("whose is this?", encoding="utf-8")

    assert _reconcile_stray_result(mine) is None
    assert stray.is_file()  # untouched, because authorship is ambiguous

    # With the rival's record gone, the file is unambiguously this run's.
    shutil.rmtree(rival.run_dir)
    assert _reconcile_stray_result(mine) == str(stray)
    assert (mine.run_dir / "RESULT.md").read_text(encoding="utf-8") == "whose is this?"


def test_rejected_cli_args_leave_no_phantom_run(env):
    """A rejected launch must not leave a run directory behind.

    Validation used to happen inside CLIRunner, by which point create_run_dir
    had already written meta.json and registered the run -- so a launch that
    never happened showed up in list_subagents and in the retention sweep.
    """
    from subagent_mcp.server import list_subagents

    cfg = env["cfg"]
    set_config(cfg)
    before = len(json.loads(list_subagents())["subagents"])

    try:
        launch_subagent(
            prompt="p",
            cwd=str(env["tmp"]),
            cli="atlas-chat",
            agent="reviewer",
            session="phantom",
            task="nope",
        )
        raise AssertionError("expected the launch to be rejected")
    except Exception as exc:
        assert "agent" in str(exc)

    after = json.loads(list_subagents())["subagents"]
    assert len(after) == before
    assert not any(s.get("task") == "nope" for s in after)
