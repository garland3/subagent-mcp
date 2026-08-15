import json
import time
from pathlib import Path

import pytest

from subagent_mcp.config import ServerConfig, cwd_is_allowed, set_config
from subagent_mcp.server import (
    check_subagent,
    launch_subagent,
    list_subagents,
    register_tools,
    stop_subagent,
)


def test_cwd_guard(env):
    cfg = ServerConfig(
        root=env["tmp"],
        runs_root=env["tmp"] / "runs",
        allowed_roots=(Path.home() / "never-exists-12345",),
        any_cwd=False,
        tmux_socket=env["socket"],
    )
    assert not cwd_is_allowed(env["tmp"], cfg)


def test_pointer_run_sh(env):
    import json
    from subagent_mcp.runners import build_runner

    run_dir = env["tmp"] / "run_test"
    run_dir.mkdir()
    cfg = env["cfg"]
    set_config(cfg)
    runner = build_runner(
        "claude",
        prompt="Hello\nworld",
        prompt_mode="pointer",
        run_dir=run_dir,
        model="opus",
        agent="reviewer",
        dangerous=True,
        extra_args=["--bare"],
    )
    run_sh = runner.write_wrapper("rid", env["tmp"])
    text = run_sh.read_text()
    assert "claude" in text
    assert "--dangerously-skip-permissions" in text
    assert "--model" in text
    assert "--bare" in text
    assert "Read the file at" in text
    assert str(run_dir / "prompt.md") in text


def test_inline_run_sh(env):
    from subagent_mcp.runners import build_runner

    run_dir = env["tmp"] / "run_test2"
    run_dir.mkdir()
    cfg = env["cfg"]
    set_config(cfg)
    runner = build_runner(
        "opencode",
        prompt="do it",
        prompt_mode="inline",
        run_dir=run_dir,
        model="x/a",
        agent="reviewer",
        dangerous=True,
        extra_args=[],
    )
    run_sh = runner.write_wrapper("rid2", env["tmp"])
    text = run_sh.read_text()
    assert "opencode" in text
    assert "--auto" in text
    assert "-m" in text
    assert "--agent" in text
    assert "--prompt" in text
    assert "$(cat" in text


def test_dry_run(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="test dry run",
            cwd=str(env["tmp"]),
            cli="claude",
            dry_run=True,
        )
    )
    assert out["dry_run"] is True
    assert out["session"] == env["tmp"].name
    assert "run_sh" in out
    assert "$(cat" in out["command"]


def test_launch_and_capture(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="hello there fake claude!",
            cwd=str(env["tmp"]),
            cli="claude",
            session="testlaunch",
            task="first-task",
            model="opus",
            settle_seconds=1.5,
        )
    )
    assert out["session"] == "testlaunch"
    assert out["alive"] is True
    pane_id = out["pane_id"]

    # Inspect the side-channel log: the exact prompt should have arrived.
    time.sleep(0.5)
    data = json.loads(env["log"].read_text().splitlines()[0])
    # Phase 1.1: the standing RESULT.md instruction is appended to the prompt.
    assert data["prompt"].startswith("hello there fake claude!")
    assert "RESULT.md" in data["prompt"]
    assert "--dangerously-skip-permissions" in data["argv"]
    assert data["argv"][data["argv"].index("--model") + 1] == "opus"

    # check_subagent returns live state.
    check = json.loads(check_subagent(handle=pane_id, lines=20))
    assert check["alive"] is True

    stop = json.loads(stop_subagent(handle=pane_id))
    # Ctrl-C typically closes the wrapper pane; the tool reports it as not alive.
    assert "alive" in stop


def test_window_dedup(env):
    cfg = env["cfg"]
    set_config(cfg)
    o1 = json.loads(
        launch_subagent(
            prompt="first",
            cwd=str(env["tmp"]),
            cli="claude",
            session="dedup",
            window="fixit",
            settle_seconds=1,
        )
    )
    o2 = json.loads(
        launch_subagent(
            prompt="second",
            cwd=str(env["tmp"]),
            cli="claude",
            session="dedup",
            window="fixit",
            settle_seconds=1,
        )
    )
    assert o1["window"] == "fixit"
    assert o2["window"] == "fixit-2"

    stop_subagent(handle=o1["pane_id"], kill_window=True)
    stop_subagent(handle=o2["pane_id"], kill_window=True)


def test_concurrency_cap(env):
    cfg = env["cfg"].__class__(**{**env["cfg"].__dict__, "max_concurrent": 1})
    set_config(cfg)
    out1 = json.loads(
        launch_subagent(
            prompt="one",
            cwd=str(env["tmp"]),
            cli="claude",
            session="concurrent",
            settle_seconds=1,
        )
    )
    assert out1["alive"] is True
    with pytest.raises(Exception) as exc_info:
        launch_subagent(
            prompt="two",
            cwd=str(env["tmp"]),
            cli="claude",
            session="concurrent2",
            settle_seconds=1,
        )
    assert "concurrent" in str(exc_info.value).lower()
    stop_subagent(handle=out1["pane_id"], kill_window=True)


def test_list_subagents(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="listing test",
            cwd=str(env["tmp"]),
            cli="claude",
            session="listtest",
            settle_seconds=1,
        )
    )
    entries = json.loads(list_subagents())
    assert any(e["run_id"] == out["run_id"] for e in entries["subagents"])
    stop_subagent(handle=out["pane_id"], kill_window=True)
