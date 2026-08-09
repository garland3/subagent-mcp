import json
import os
import stat
import time
from pathlib import Path

from subagent_mcp.config import set_config
from subagent_mcp.runs import create_run_dir, discover_runs, prune_runs
from subagent_mcp.server import _detect_blocked_output, check_subagent, launch_subagent, stop_subagent


def _make_run(runs_root: Path, name: str, age_days: float) -> str:
    run = create_run_dir(
        runs_root,
        session="s",
        window="w",
        cwd=runs_root,
        cli="claude",
        model=None,
        agent=None,
        task=name,
        argv=[],
    )
    stamp = time.time() - age_days * 86400
    run.start_time = ""  # force the mtime fallback path
    run.write_meta()
    os.utime(run.run_dir, (stamp, stamp))
    return run.run_id


def test_prune_runs_age_and_count(tmp_path):
    runs_root = tmp_path / "runs"
    old = _make_run(runs_root, "old", age_days=30)
    fresh = _make_run(runs_root, "fresh", age_days=1)

    removed = prune_runs(runs_root, keep_max=0, max_age_days=14)
    assert removed == [old]
    assert set(discover_runs(runs_root)) == {fresh}

    # keep_max trims the oldest surplus regardless of age: of fresh (1d),
    # a (3d) and b (12h), only a is old enough to be the odd one out.
    a = _make_run(runs_root, "a", age_days=3)
    b = _make_run(runs_root, "b", age_days=0.5)
    removed = prune_runs(runs_root, keep_max=2, max_age_days=0)
    assert removed == [a]
    assert set(discover_runs(runs_root)) == {fresh, b}


def test_prune_runs_never_touches_protected(tmp_path):
    runs_root = tmp_path / "runs"
    old = _make_run(runs_root, "old", age_days=99)
    removed = prune_runs(runs_root, keep_max=0, max_age_days=1, protect={old})
    assert removed == []
    assert old in discover_runs(runs_root)


def test_prune_runs_ignores_foreign_directories(tmp_path):
    """Anything without a meta.json under runs_root is left alone."""
    runs_root = tmp_path / "runs"
    _make_run(runs_root, "old", age_days=99)
    stray = runs_root / "not-a-run"
    stray.mkdir()
    (stray / "keep.txt").write_text("hi")

    prune_runs(runs_root, keep_max=0, max_age_days=1)
    assert stray.is_dir() and (stray / "keep.txt").exists()


def test_launch_prunes_but_spares_live_runs(env):
    """The GC that runs on launch must not delete a still-running subagent."""
    cfg = env["cfg"]
    set_config(cfg)
    object.__setattr__(cfg, "runs_keep_max", 1)
    object.__setattr__(cfg, "runs_max_age_days", 0)

    first = json.loads(
        launch_subagent(
            prompt="first", cwd=str(env["tmp"]), cli="claude",
            session="gc", window="one", settle_seconds=1,
        )
    )
    _make_run(cfg.runs_root, "stale", age_days=99)

    second = json.loads(
        launch_subagent(
            prompt="second", cwd=str(env["tmp"]), cli="claude",
            session="gc", window="two", settle_seconds=1,
        )
    )
    assert second["pruned_runs"] >= 1

    surviving = discover_runs(cfg.runs_root)
    assert first["run_id"] in surviving  # live, protected despite keep_max=1
    assert second["run_id"] in surviving

    stop_subagent(handle=first["pane_id"], kill_window=True)
    stop_subagent(handle=second["pane_id"], kill_window=True)


def test_permission_modal_detected():
    """The mid-run permission modal is distinct from the startup trust dialog."""
    modal = (
        "● Edit(src/app.py)\n"
        "  Do you want to make this edit to app.py?\n"
        "  1. Yes\n"
        "  2. Yes, and don't ask again this session\n"
        "  3. No, and tell Claude what to do differently (esc)\n"
    )
    blocked, reason = _detect_blocked_output(modal)
    assert blocked is True and reason

    blocked, _ = _detect_blocked_output("● Editing src/app.py ... done\n")
    assert blocked is False


def test_check_subagent_surfaces_midrun_block(env, monkeypatch, tmp_path):
    """A modal that appears after launch must show up in check_subagent."""
    cfg = env["cfg"]
    set_config(cfg)

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        'echo "Welcome to Claude Code"\n'
        "sleep 3\n"
        'echo "Do you want to proceed?"\n'
        "sleep 120\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    out = json.loads(
        launch_subagent(
            prompt="do work",
            cwd=str(env["tmp"]),
            cli="claude",
            session="midrun",
            settle_seconds=1,
        )
    )
    assert out["blocked"] is False, out["initial_output"]

    time.sleep(3.5)
    snap = json.loads(check_subagent(handle=out["pane_id"], lines=40))
    assert snap["blocked"] is True
    assert "Do you want to" in snap["blocked_reason"]

    stop_subagent(handle=out["pane_id"], kill_window=True)
