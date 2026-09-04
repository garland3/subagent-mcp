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


def test_finished_earlier_run_is_not_a_rival(tmp_path, env):
    """Declining on ambiguity must not become declining on everything.

    Run dirs are kept (up to runs_keep_max) and people launch into the same
    repo repeatedly, so "any other run that started before this file" would
    match some months-old record and disable reconciliation for that cwd
    forever -- trading a rare misattribution for a permanent loss of the
    feature. A run that had already exited when the file appeared is not a
    rival, and STATUS.json's mtime is when the wrapper recorded that exit.
    """
    import os

    from subagent_mcp.runs import create_run_dir

    cfg = env["cfg"]
    set_config(cfg)
    work = tmp_path / "shared2"
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

    earlier = _mk("earlier")
    mine = _mk("mine")

    # The earlier run exited an hour ago: its wrapper wrote STATUS.json then.
    status = earlier.run_dir / "STATUS.json"
    status.write_text('{"exit_code": 0}', encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(status, (long_ago, long_ago))

    stray = work / "RESULT.md"
    stray.write_text("mine, written just now", encoding="utf-8")

    # It started before the file appeared, but it was already gone, so it is
    # not a rival and reconciliation proceeds.
    assert _reconcile_stray_result(mine) == str(stray)
    assert (mine.run_dir / "RESULT.md").read_text(encoding="utf-8") == (
        "mine, written just now"
    )


def test_file_created_after_the_run_exited_is_not_adopted(tmp_path, env):
    """Reconciliation needs an upper bound, not just a lower one.

    If a run finishes without writing its summary and an unrelated untracked
    RESULT.md appears afterwards -- before anyone calls check_subagent -- its
    mtime still clears the start bound. Adopting it would move a file the run
    cannot have written, and a later sweep would delete it.
    """
    import os

    from subagent_mcp.runs import create_run_dir

    cfg = env["cfg"]
    set_config(cfg)
    work = tmp_path / "bounded"
    work.mkdir()
    run = create_run_dir(
        cfg.runs_root,
        session="s",
        window="w",
        cwd=work,
        cli="claude",
        model=None,
        agent=None,
        task="bounded",
        argv=[],
    )
    # Coherent timeline: started two hours ago, exited an hour ago, wrote no
    # RESULT.md. (create_run_dir stamps "now", so wind it back.)
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    run.start_time = started.isoformat()
    run.write_meta()
    ended = datetime.now(timezone.utc) - timedelta(hours=1)
    (run.run_dir / "STATUS.json").write_text(
        json.dumps({"exit_code": 0, "ended_at": ended.isoformat()}), encoding="utf-8"
    )

    stray = work / "RESULT.md"
    stray.write_text("written by a human, after the run was over", encoding="utf-8")

    assert _reconcile_stray_result(run) is None
    assert stray.is_file()
    assert not (run.run_dir / "RESULT.md").exists()

    # The legitimate ordering -- agent writes the file, then the CLI exits --
    # sits inside [started, ended] and is still reconciled.
    within = time.time() - 3600 - 60
    os.utime(stray, (within, within))
    assert _reconcile_stray_result(run) == str(stray)


def _mk_run(cfg, work, task: str) -> Run:
    from subagent_mcp.runs import create_run_dir

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


def _status(run: Run, started: datetime, ended: datetime) -> None:
    (run.run_dir / "STATUS.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_resume_gap_is_not_part_of_the_runs_window(tmp_path, env):
    """A resumed run's window must come from one invocation, not two.

    `run.start_time` describes the original launch; STATUS.json describes the
    latest resume. Pairing them spans the idle gap in between, so a file some-
    one wrote during that gap would look like this run's output and be adopted
    (then deleted by a later sweep).
    """
    import os

    cfg = env["cfg"]
    set_config(cfg)
    work = tmp_path / "resumed"
    work.mkdir()
    run = _mk_run(cfg, work, "resumed")

    now = datetime.now(timezone.utc)
    # Launched at T-4h; resumed at T-1h and finished at T-30m.
    run.start_time = (now - timedelta(hours=4)).isoformat()
    run.write_meta()
    _status(run, now - timedelta(hours=1), now - timedelta(minutes=30))

    # Someone wrote this during the idle gap, two hours in — inside
    # [original start, latest end] but outside the actual resume.
    stray = work / "RESULT.md"
    stray.write_text("not the agent's", encoding="utf-8")
    gap = (now - timedelta(hours=2)).timestamp()
    os.utime(stray, (gap, gap))

    assert _reconcile_stray_result(run) is None
    assert stray.is_file()

    # Written during the resume itself, it is reconciled.
    during = (now - timedelta(minutes=45)).timestamp()
    os.utime(stray, (during, during))
    assert _reconcile_stray_result(run) == str(stray)


def test_rival_resumed_after_the_file_appeared_is_not_a_rival(tmp_path, env):
    """A rival's window must be coherent too.

    An older run that had finished, then was resumed *after* the candidate
    wrote its file, must not read as continuously active across that gap —
    otherwise the candidate declines even though it was the only run going
    when the file appeared.
    """
    import os

    cfg = env["cfg"]
    set_config(cfg)
    work = tmp_path / "rivalgap"
    work.mkdir()
    rival = _mk_run(cfg, work, "rival")
    mine = _mk_run(cfg, work, "mine")

    now = datetime.now(timezone.utc)
    # The rival ran long ago, then was resumed five minutes ago.
    rival.start_time = (now - timedelta(hours=6)).isoformat()
    rival.write_meta()
    _status(rival, now - timedelta(minutes=5), now - timedelta(minutes=1))

    # Mine ran in between and wrote its file an hour ago.
    mine.start_time = (now - timedelta(hours=2)).isoformat()
    mine.write_meta()
    _status(mine, now - timedelta(hours=2), now - timedelta(minutes=50))

    stray = work / "RESULT.md"
    stray.write_text("mine", encoding="utf-8")
    when = (now - timedelta(hours=1)).timestamp()
    os.utime(stray, (when, when))

    # The rival's *current* invocation began after the file existed, so it is
    # not a rival and reconciliation proceeds.
    assert _reconcile_stray_result(mine) == str(stray)
