import json
import os
import subprocess
import time
import uuid

import pytest
from subagent_mcp.config import ServerConfig, set_config
from subagent_mcp.server import (  # noqa: F401  — side-effects registration
    check_subagent,
    launch_subagent,
    list_subagents,
    register_tools,
    stop_subagent,
    sweep_stale_subagents,
)
from subagent_mcp.tmuxio import Tmux


@pytest.fixture
def env(tmp_path_factory, monkeypatch):
    """Provide an isolated tmux server, fake CLI on PATH, and server config."""
    tmp = tmp_path_factory.mktemp("subagent-run")
    log = tmp / "fake-cli.log"
    monkeypatch.setenv("SUBAGENT_TEST_LOG", str(log))

    bin_dir = __file__.rsplit("/", 1)[0] + "/bin"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    socket = f"subagent-test-{uuid.uuid4().hex[:8]}"
    cfg = ServerConfig(
        root=tmp,
        runs_root=tmp / "runs",
        allowed_roots=(tmp.resolve(),),
        any_cwd=True,
        tmux_socket=socket,
        max_concurrent=5,
        cli_allowlist=("claude", "opencode"),
        # Point transcript roots at tmp so tests never read the host's real
        # ~/.claude/projects or ~/.local/share/opencode state. State-derivation
        # tests write synthetic transcripts under these.
        claude_projects_root=tmp / "claude-projects",
        opencode_state_root=tmp / "opencode-state",
    )
    set_config(cfg)
    register_tools(cfg)

    tmux = Tmux(socket=socket)
    subprocess.run(["tmux", "-L", socket, "new-session", "-d"], check=True, capture_output=True)

    yield {"tmp": tmp, "log": log, "socket": socket, "tmux": tmux, "cfg": cfg}

    tmux.kill_server()
