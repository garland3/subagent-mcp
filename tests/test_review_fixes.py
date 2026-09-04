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
