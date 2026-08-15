"""CLI resolution and watch-mode behaviour."""

import json
import os
import subprocess

import pytest
from fastmcp.exceptions import ToolError
from subagent_mcp import cli_paths
from subagent_mcp.runners import build_runner
from subagent_mcp.server import launch_subagent, watch_subagent


def test_augmented_path_appends_extras_without_shadowing(monkeypatch, tmp_path):
    extra = tmp_path / "opencode-bin"
    extra.mkdir()
    monkeypatch.setattr(cli_paths, "extra_dirs", lambda: [str(extra)])
    monkeypatch.setenv("PATH", "/usr/bin:/usr/bin:/bin")

    parts = cli_paths.augmented_path().split(os.pathsep)

    assert parts[0] == "/usr/bin", "existing PATH entries keep priority"
    assert parts.count("/usr/bin") == 1, "duplicates are collapsed"
    assert parts[-1] == str(extra)


def test_resolve_cli_finds_binary_outside_path(monkeypatch, tmp_path):
    """The exact failure mode: opencode installed where PATH does not look."""
    extra = tmp_path / ".opencode" / "bin"
    extra.mkdir(parents=True)
    binary = extra / "opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("SUBAGENT_CLI_OPENCODE", raising=False)
    monkeypatch.setattr(cli_paths, "extra_dirs", lambda: [])
    assert cli_paths.resolve_cli("opencode") is None

    monkeypatch.setattr(cli_paths, "extra_dirs", lambda: [str(extra)])
    assert cli_paths.resolve_cli("opencode") == str(binary)


def test_env_override_wins(monkeypatch, tmp_path):
    binary = tmp_path / "my-opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SUBAGENT_CLI_OPENCODE", str(binary))
    assert cli_paths.resolve_cli("opencode") == str(binary)


def test_nvm_bins_are_newest_first(monkeypatch, tmp_path):
    root = tmp_path / ".nvm" / "versions" / "node"
    for version in ("v9.1.0", "v22.22.1", "v20.5.0"):
        (root / version / "bin").mkdir(parents=True)
    monkeypatch.setattr(cli_paths.Path, "home", staticmethod(lambda: tmp_path))

    assert [p.split("/")[-2] for p in cli_paths._nvm_bins()] == [
        "v22.22.1",
        "v20.5.0",
        "v9.1.0",
    ]


def test_wrapper_uses_absolute_executable_and_exports_path(tmp_path):
    runner = build_runner(
        "opencode",
        prompt="hi",
        prompt_mode="inline",
        run_dir=tmp_path,
        executable="/opt/opencode/bin/opencode",
    )
    assert runner.argv()[0] == "/opt/opencode/bin/opencode"

    text = runner.write_wrapper("run-1", tmp_path).read_text()
    assert "export PATH=" in text
    assert "/opt/opencode/bin/opencode" in text


def test_missing_cli_fails_fast_with_guidance(env, monkeypatch):
    """A missing binary must not become an opaque exit-127 pane."""
    # tmux must stay findable; only the coding CLI disappears.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("SUBAGENT_CLI_OPENCODE", raising=False)
    monkeypatch.setattr(cli_paths, "extra_dirs", lambda: [])

    with pytest.raises(ToolError) as exc:
        launch_subagent(prompt="hello", cli="opencode", cwd=str(env["tmp"]))

    message = str(exc.value)
    assert "was not found on PATH" in message
    assert "SUBAGENT_CLI_OPENCODE" in message


def test_launch_writes_output_log(env):
    out = json.loads(
        launch_subagent(prompt="hello log", cli="opencode", cwd=str(env["tmp"]), settle_seconds=1.5)
    )
    assert out["alive"] is True
    log = out["output_log"]
    assert log.endswith("output.log")
    assert out["hint"].endswith(f"tail -f {log}")

    # The fake CLI is silent, so assert the tee is armed rather than waiting on
    # bytes: tmux reports pane_pipe=1 while a pipe-pane target is attached.
    piped = subprocess.run(
        [
            "tmux", "-L", env["socket"], "display-message",
            "-p", "-t", out["pane_id"], "#{pane_pipe}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert piped == "1"


def test_watch_switch_reports_when_nobody_is_attached(env):
    out = json.loads(
        launch_subagent(
            prompt="hello watch",
            cli="opencode",
            cwd=str(env["tmp"]),
            watch="switch",
            settle_seconds=1.0,
        )
    )
    # The test tmux server has no attached client, so watching is a no-op that
    # still hands back a usable attach command instead of failing the launch.
    assert out["alive"] is True
    assert out["watch"] == "switch"
    assert out["watch_applied"] is False
    assert "no tmux client is attached" in out["watch_detail"]
    assert out["attach_command"].startswith("tmux -L ")
    assert " -r " in out["attach_read_only"]

    again = json.loads(watch_subagent(out["run_id"], mode="switch"))
    assert again["pane_id"] == out["pane_id"]
    assert again["attach_command"] == out["attach_command"]


def test_invalid_watch_mode_rejected(env):
    with pytest.raises(ToolError):
        launch_subagent(
            prompt="hello", cli="opencode", cwd=str(env["tmp"]), watch="sideways", dry_run=True
        )


def test_watch_aliases_normalize(env):
    out = json.loads(
        launch_subagent(
            prompt="hello", cli="opencode", cwd=str(env["tmp"]), watch="none", dry_run=True
        )
    )
    assert out["watch"] == "off"
