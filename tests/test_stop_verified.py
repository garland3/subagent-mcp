"""stop_subagent must actually stop the agent, and liveness must mean the agent.

The bug these cover: the launcher's run.sh ends with ``exec "$SHELL" -i``, so a
pane outlives its CLI. Pane-existence was being reported as `alive`, and
stop_subagent sent a single Ctrl-C — which a TUI CLI reads as "cancel the
current turn", not "exit" — and then reported success. Stopped agents kept
running, kept showing `alive: true`, and could never be swept.
"""

import json
import time

import pytest
from subagent_mcp.config import set_config
from subagent_mcp.runs import discover_runs
from subagent_mcp.server import (
    check_subagent,
    launch_subagent,
    list_subagents,
    send_to_subagent,
    stop_subagent,
    sweep_stale_subagents,
)


def _launch_finished(env, session):
    """A run in the state production is always in: agent exited, pane still open.

    run.sh execs an interactive shell after the CLI returns, so the window
    stays for inspection — which is exactly why pane-existence is not agent
    liveness.
    """
    from subagent_mcp.server import _agent_pids, _load_registry

    out = _launch(env, session, prompt="exit-now")
    run = _load_registry(env["cfg"])[out["run_id"]]
    deadline = time.time() + 10
    while time.time() < deadline:
        if not _agent_pids(run.pane_pid, run.cli):
            break
        time.sleep(0.3)
    assert env["tmux"].pane_alive(out["pane_id"]), "fixture needs a surviving pane"
    return out


def _launch(env, session, prompt="work", cli="claude"):
    set_config(env["cfg"])
    return json.loads(
        launch_subagent(
            prompt=prompt,
            cwd=str(env["tmp"]),
            cli=cli,
            session=session,
            settle_seconds=1,
        )
    )


def _entry(run_id):
    listing = json.loads(list_subagents())
    for item in listing["subagents"]:
        if item["run_id"] == run_id:
            return item
    return None


def test_stop_verifies_the_process_is_gone(env):
    out = _launch(env, "stop-verify")
    result = json.loads(stop_subagent(handle=out["run_id"]))

    assert result["stopped"] is True
    assert result["method"] == "ctrl-c"
    assert result["alive"] is False
    # And the process really is gone, not merely signalled.
    assert _entry(out["run_id"])["alive"] is False


def test_stop_escalates_past_a_ctrl_c_proof_agent(env):
    """A CLI that ignores SIGINT — like a real TUI — is escalated to a signal."""
    (env["tmp"] / ".ignore-sigint").write_text("1")
    out = _launch(env, "stop-stubborn")

    result = json.loads(stop_subagent(handle=out["run_id"]))

    assert result["stopped"] is True, result.get("note")
    # Ctrl-C could not have done it; the ladder had to reach a real signal.
    assert result["method"] in ("sigterm", "sigkill")
    assert [a["method"] for a in result["attempts"]][:1] == ["ctrl-c"]
    assert result["attempts"][0]["stopped"] is False
    assert result["alive"] is False


def test_pane_outliving_the_agent_is_not_alive(env):
    """The regression itself: agent exits, pane stays, run must read as dead."""
    out = _launch_finished(env, "stop-liveness")

    # The pane is still there — that is the launcher's design, not a failure.
    assert env["tmux"].pane_alive(out["pane_id"])

    entry = _entry(out["run_id"])
    assert entry is not None
    assert entry["alive"] is False
    assert entry["pane"] == "exited"

    listing = json.loads(list_subagents())
    assert listing["alive"] == 0
    assert listing["finished_pane_open"] >= 1

    check = json.loads(check_subagent(handle=out["run_id"]))
    assert check["alive"] is False
    assert check["pane"] == "exited"


def test_send_refuses_a_pane_whose_agent_exited(env):
    """Typing into the leftover shell would run the message as a shell command."""
    out = _launch_finished(env, "stop-send")

    with pytest.raises(Exception) as exc:
        send_to_subagent(handle=out["run_id"], text="echo pwned")
    assert "not alive" in str(exc.value)


def test_sweep_reaps_stopped_runs_and_closes_their_panes(env):
    """The user-visible complaint: stopped agents never left the listing."""
    out = _launch_finished(env, "stop-sweep")
    stop_subagent(handle=out["run_id"])

    preview = json.loads(sweep_stale_subagents(dry_run=True))
    assert out["run_id"] in [s["run_id"] for s in preview["stale"]]
    assert preview["would_close_panes"] >= 1

    result = json.loads(sweep_stale_subagents())
    assert out["run_id"] in result["cleared_runs"]
    assert out["pane_id"] in result["panes_closed"]

    assert out["run_id"] not in discover_runs(env["cfg"].runs_root)
    assert _entry(out["run_id"]) is None
    assert not env["tmux"].pane_alive(out["pane_id"])


def test_sweep_never_touches_a_running_agent(env):
    running = _launch(env, "stop-keep-a")
    stopped = _launch_finished(env, "stop-keep-b")
    stop_subagent(handle=stopped["run_id"])

    result = json.loads(sweep_stale_subagents())

    assert stopped["run_id"] in result["cleared_runs"]
    assert running["run_id"] not in result["cleared_runs"]
    assert result["alive"] == 1
    assert running["pane_id"] not in result["panes_closed"]
    assert env["tmux"].pane_alive(running["pane_id"])

    stop_subagent(handle=running["run_id"], kill_window=True)


def test_stop_with_kill_window_closes_the_pane_but_keeps_the_record(env):
    out = _launch_finished(env, "stop-killwin")
    result = json.loads(stop_subagent(handle=out["run_id"], kill_window=True))

    assert result["stopped"] is True
    assert result["method"] == "already_exited"
    assert result["pane_closed"] is True
    assert not env["tmux"].pane_alive(out["pane_id"])
    # RESULT.md and the transcript must survive a stop; sweep is what deletes.
    assert out["run_id"] in discover_runs(env["cfg"].runs_root)


def test_finished_agent_does_not_consume_a_concurrency_slot(env):
    """A pane parked at a shell was counted as an active subagent."""
    from subagent_mcp.server import _active_count

    _launch_finished(env, "stop-slots")
    # The pane is alive and the run is in the registry, but no agent is
    # working in it, so it must not hold a concurrency slot.
    assert _active_count(env["tmux"], env["cfg"]) == 0
