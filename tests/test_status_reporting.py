"""Regression tests for the 2026-08-21 conversation-analysis findings.

Each test here pins a behaviour that transcript evidence showed going wrong in
production (see REPORT-CONVO-ANALYSIS-2026-08-21.md in the group folder):

  * a listing whose bare ``count`` was read as "N agents are running" when most
    of the N were dead
  * ``send_to_subagent`` failing on a vanished pane with a raw tmux error, when
    ``resume_subagent`` exists for exactly that case
  * ``sweep_stale_subagents`` reporting an impossible ``remaining_total``
  * a nested duplicate run_id shadowing the real record, so a swept run could
    reappear on the next listing
"""

import json

import pytest
from fastmcp.exceptions import ToolError
from subagent_mcp.config import set_config
from subagent_mcp.runs import discover_runs, is_sweepable
from subagent_mcp.server import (
    _strip_box_drawing,
    launch_subagent,
    list_subagents,
    send_to_subagent,
    stop_subagent,
    sweep_stale_subagents,
)


def _launch(env, window):
    return json.loads(
        launch_subagent(
            prompt=f"work on {window}",
            cwd=str(env["tmp"]),
            cli="claude",
            session="status",
            window=window,
            task=window,
            settle_seconds=1,
        )
    )


# ---------------------------------------------------------------------------
# list_subagents: the counts must not admit "N agents are running"
# ---------------------------------------------------------------------------

def test_list_reports_alive_and_dead_separately(env):
    cfg = env["cfg"]
    set_config(cfg)

    alive = _launch(env, "still-alive")
    dead = _launch(env, "now-dead")
    stop_subagent(handle=dead["pane_id"], kill_window=True)

    listing = json.loads(list_subagents())

    # A bare total is the field that got misread; it must not stand alone.
    assert "count" not in listing
    assert listing["total"] == listing["alive"] + listing["dead"]
    assert listing["alive"] == 1
    assert listing["dead"] == 1

    by_id = {entry["run_id"]: entry for entry in listing["subagents"]}
    assert by_id[alive["run_id"]]["alive"] is True
    assert by_id[dead["run_id"]]["alive"] is False

    stop_subagent(handle=alive["pane_id"], kill_window=True)


def test_list_surfaces_result_file_presence(env):
    """`result` tells the caller whether there is anything real to report."""
    cfg = env["cfg"]
    set_config(cfg)

    out = _launch(env, "with-result")
    entry = next(
        e for e in json.loads(list_subagents())["subagents"] if e["run_id"] == out["run_id"]
    )
    assert entry["result"] == "none"

    (cfg.runs_root / out["run_id"] / "RESULT.md").write_text("# done\n", encoding="utf-8")
    entry = next(
        e for e in json.loads(list_subagents())["subagents"] if e["run_id"] == out["run_id"]
    )
    assert entry["result"] == "present"

    stop_subagent(handle=out["pane_id"], kill_window=True)


def test_list_drops_redundant_fields(env):
    """target already carries session:window; session_id is never an argument."""
    cfg = env["cfg"]
    set_config(cfg)

    out = _launch(env, "lean-payload")
    entry = next(
        e for e in json.loads(list_subagents())["subagents"] if e["run_id"] == out["run_id"]
    )
    assert entry["target"] == f"status:{out['run_id'].split('-', 2)[-1]}" or ":" in entry["target"]
    for dropped in ("session", "window", "session_id"):
        assert dropped not in entry
    # The signal that a resume is possible must survive the trim.
    assert "session_id_status" in entry

    stop_subagent(handle=out["pane_id"], kill_window=True)


# ---------------------------------------------------------------------------
# send_to_subagent: a dead pane is a redirect, not a raw tmux failure
# ---------------------------------------------------------------------------

def test_send_to_dead_pane_points_at_resume(env):
    cfg = env["cfg"]
    set_config(cfg)

    out = _launch(env, "gone-away")
    stop_subagent(handle=out["pane_id"], kill_window=True)

    with pytest.raises(ToolError) as exc_info:
        send_to_subagent(handle=out["run_id"], text="are you there?")

    message = str(exc_info.value)
    assert "not alive" in message
    assert "resume_subagent" in message
    # The old failure mode was a bare tmux error with no next step.
    assert "can't find pane" not in message


# ---------------------------------------------------------------------------
# sweep_stale_subagents: the arithmetic has to be self-consistent
# ---------------------------------------------------------------------------

def test_sweep_remaining_total_is_consistent(env):
    cfg = env["cfg"]
    set_config(cfg)

    alive = _launch(env, "keeper")
    for window in ("doomed-a", "doomed-b", "doomed-c"):
        out = _launch(env, window)
        stop_subagent(handle=out["pane_id"], kill_window=True)

    result = json.loads(sweep_stale_subagents())

    assert result["cleared"] == 3
    # Previously this subtracted the deletion twice and could go negative.
    assert result["remaining_total"] == 1
    assert result["remaining_total"] >= result["alive"]
    assert result["remaining_total"] == len(discover_runs(cfg.runs_root))

    stop_subagent(handle=alive["pane_id"], kill_window=True)


# ---------------------------------------------------------------------------
# Nested duplicate run_id: the shadowing that let a swept run come back
# ---------------------------------------------------------------------------

def test_nested_duplicate_does_not_shadow_top_level(env):
    cfg = env["cfg"]
    set_config(cfg)

    out = _launch(env, "shadowed")
    run_id = out["run_id"]
    top_level = cfg.runs_root / run_id
    assert is_sweepable(top_level, cfg.runs_root)

    # A second meta.json for the same run_id, one level deeper. delete_run_dirs
    # will never remove it, so it must not win discovery.
    nested = cfg.runs_root / "archive" / run_id
    nested.mkdir(parents=True)
    meta = json.loads((top_level / "meta.json").read_text())
    meta["pane_id"] = "%999999"
    (nested / "meta.json").write_text(json.dumps(meta))

    assert not is_sweepable(nested, cfg.runs_root)
    assert discover_runs(cfg.runs_root)[run_id].run_dir == top_level

    stop_subagent(handle=out["pane_id"], kill_window=True)
    swept = json.loads(sweep_stale_subagents())
    assert run_id in swept["cleared_runs"]

    # With the top-level copy gone the nested one is all that is left, and it
    # must be reported as unreapable rather than silently swept forever.
    listing = json.loads(list_subagents())
    if any(entry["run_id"] == run_id for entry in listing["subagents"]):
        again = json.loads(sweep_stale_subagents())
        assert run_id in again["unsweepable"]
        assert run_id not in again["cleared_runs"]


# ---------------------------------------------------------------------------
# Payload hygiene
# ---------------------------------------------------------------------------

def test_strip_box_drawing_collapses_padding():
    """Removing borders leaves pane-width padding; that padding is not free."""
    line = "│ Working on the thing" + " " * 60 + "│"
    cleaned = _strip_box_drawing(line)
    assert cleaned == " Working on the thing"
    assert "   " not in cleaned


def test_strip_box_drawing_keeps_short_gaps():
    """Two spaces still separate columns; only runs of three or more collapse."""
    assert _strip_box_drawing("a  b") == "a  b"
    assert _strip_box_drawing("a     b") == "a  b"
