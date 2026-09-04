"""atlas-chat as a supported subagent CLI.

atlas-chat is ATLAS's own non-interactive chat CLI (the ``atlas-chat`` console
script in atlas-ui-3). It differs from claude/opencode in three ways that this
module pins down: the prompt is a trailing positional, there is no conversation
id (so no resume), and it writes to ATLAS's DuckDB chat history — which the
long-running atlas-server process holds an exclusive lock on.
"""

import json
import time

import pytest

from subagent_mcp.cli_paths import cli_extra_dirs, resolve_cli, searched_dirs
from subagent_mcp.config import ServerConfig, set_config
from subagent_mcp.runners import RunnerError, build_runner
from subagent_mcp.server import launch_subagent, resume_subagent
from subagent_mcp.runs import Run, derive_state


# --------------------------------------------------------------------------
# argv shape
# --------------------------------------------------------------------------


def test_atlas_chat_inline_argv(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "atlas-chat",
        prompt="summarise the design doc",
        prompt_mode="inline",
        run_dir=run_dir,
        model="gpt-4o",
        dangerous=True,
        extra_args=["--json"],
    )
    argv = runner.argv()
    assert argv[0] == "atlas-chat"
    # dangerous maps to --agent-mode: atlas-chat has no permission prompt to skip.
    assert "--agent-mode" in argv
    assert argv[argv.index("--model") + 1] == "gpt-4o"
    assert "--json" in argv
    # The prompt is a trailing positional, not the value of a flag.
    assert argv[-1].startswith("$(cat")


def test_atlas_chat_pointer_argv(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "atlas-chat",
        prompt="instructions",
        prompt_mode="pointer",
        run_dir=run_dir,
        dangerous=False,
    )
    argv = runner.argv()
    assert "--agent-mode" not in argv
    assert "Read the file" in argv[-1]
    assert "$(cat" not in argv[-1]


def test_explicit_mode_flag_wins_over_dangerous(tmp_path):
    """--agent-mode and --only-rag are mutually exclusive in atlas-chat's parser.

    Emitting the dangerous default alongside a caller's explicit choice would
    make argparse reject the whole invocation, so the explicit flag wins.
    """
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    argv = build_runner(
        "atlas-chat",
        prompt="p",
        prompt_mode="pointer",
        run_dir=run_dir,
        dangerous=True,
        extra_args=["--only-rag"],
    ).argv()
    assert "--only-rag" in argv
    assert "--agent-mode" not in argv


def test_atlas_chat_rejects_agent_and_session_id(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    with pytest.raises(RunnerError, match="no --agent equivalent"):
        build_runner(
            "atlas-chat",
            prompt="p",
            prompt_mode="pointer",
            run_dir=run_dir,
            agent="reviewer",
        )
    with pytest.raises(RunnerError, match="no conversation id"):
        build_runner(
            "atlas-chat",
            prompt="p",
            prompt_mode="pointer",
            run_dir=run_dir,
            session_id="abc",
        )


# --------------------------------------------------------------------------
# The DuckDB lock (the one real operational constraint)
# --------------------------------------------------------------------------


def test_wrapper_isolates_chat_history_db(tmp_path):
    """Each run gets its own DuckDB so it does not fight atlas-server's lock.

    ATLAS defaults CHAT_HISTORY_DB_URL to ``duckdb:///data/chat_history.db``
    relative to the cwd. DuckDB is single-writer, so a subagent launched into
    an ATLAS checkout while atlas-server is up dies with "Could not set lock on
    file ... Conflicting lock is held".
    """
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "atlas-chat", prompt="p", prompt_mode="pointer", run_dir=run_dir
    )
    text = runner.write_wrapper("rid", tmp_path).read_text()
    assert "CHAT_HISTORY_DB_URL" in text
    assert str(run_dir / "chat_history.db") in text
    # An operator who exports their own value keeps it.
    assert "${CHAT_HISTORY_DB_URL:-" in text


def test_wrapper_leaves_other_clis_alone(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner("claude", prompt="p", prompt_mode="pointer", run_dir=run_dir)
    assert "CHAT_HISTORY_DB_URL" not in runner.write_wrapper("rid", tmp_path).read_text()


# --------------------------------------------------------------------------
# Path resolution — atlas-chat lives in a venv, never on PATH
# --------------------------------------------------------------------------


def test_env_override_resolves_atlas_chat(tmp_path, monkeypatch):
    fake = tmp_path / "atlas-chat"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SUBAGENT_CLI_ATLAS_CHAT", str(fake))
    assert resolve_cli("atlas-chat") == str(fake)


def test_per_cli_dirs_are_scoped_and_reported():
    # Only atlas-chat has extra roots; claude must not inherit them.
    assert cli_extra_dirs("claude") == []
    # searched_dirs mentions them when asked about that CLI, for the
    # "not found on PATH" error to be actionable.
    for d in cli_extra_dirs("atlas-chat"):
        assert d in searched_dirs("atlas-chat")


# --------------------------------------------------------------------------
# No session => no resume, and say why
# --------------------------------------------------------------------------


def test_derive_state_unknown_for_atlas_chat(tmp_path):
    run = Run(
        run_id="rid",
        run_dir=tmp_path,
        cwd=tmp_path,
        cli="atlas-chat",
        session="s",
        window="w",
    )
    # One-shot CLI: no transcript to read, so state is unknown by design.
    assert derive_state(run) == ("", "")


def test_launch_and_resume_refusal(env):
    cfg = env["cfg"]
    set_config(cfg)
    out = json.loads(
        launch_subagent(
            prompt="hello atlas",
            cwd=str(env["tmp"]),
            cli="atlas-chat",
            session="atlaschat",
            task="ask",
            model="mock-model",
            settle_seconds=1.5,
        )
    )
    assert out["session"] == "atlaschat"
    assert out["session_id_status"] == "unsupported"

    time.sleep(0.5)
    data = json.loads(env["log"].read_text().splitlines()[0])
    assert data["cli"] == "atlas-chat"
    assert data["prompt"].startswith("hello atlas")
    assert "RESULT.md" in data["prompt"]
    assert "--agent-mode" in data["argv"]
    assert data["argv"][data["argv"].index("--model") + 1] == "mock-model"
    # The wrapper's per-run database actually reached the process.
    assert data["chat_history_db_url"].startswith("duckdb:///")
    assert data["chat_history_db_url"].endswith("chat_history.db")

    # Resume must refuse with a reason, not a generic "missing session id".
    with pytest.raises(Exception, match="one-shot"):
        resume_subagent(handle=out["run_id"], message="follow up")


def test_allowlist_can_exclude_atlas_chat(env):
    cfg = env["cfg"]
    narrowed = ServerConfig(
        root=cfg.root,
        runs_root=cfg.runs_root,
        allowed_roots=cfg.allowed_roots,
        any_cwd=True,
        tmux_socket=cfg.tmux_socket,
        cli_allowlist=("claude",),
    )
    set_config(narrowed)
    try:
        with pytest.raises(Exception, match="not in the allowlist"):
            launch_subagent(prompt="p", cwd=str(env["tmp"]), cli="atlas-chat")
    finally:
        set_config(cfg)
