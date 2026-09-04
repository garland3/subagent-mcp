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
    # The prompt is a trailing positional, not the value of a flag, guarded by
    # "--" so a prompt beginning with "-" is not parsed as an option.
    assert argv[-2] == "--"
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
    assert argv[-2] == "--"
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


def test_dash_leading_prompt_survives_option_parsing(tmp_path):
    """A prompt starting with "-" must reach atlas-chat as text, not as flags."""
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    argv = build_runner(
        "atlas-chat",
        prompt="--json please explain",
        prompt_mode="pointer",
        run_dir=run_dir,
    ).argv()
    # Everything after "--" is positional, so the prompt cannot be mistaken
    # for an option however it starts.
    assert argv.index("--") == len(argv) - 2


# --------------------------------------------------------------------------
# Completion artifacts for a one-shot CLI (PR #1 review, codex P2)
# --------------------------------------------------------------------------


def test_status_json_makes_a_transcriptless_run_idle(env, tmp_path):
    """A one-shot run must become idle so its RESULT.md is actually reachable.

    check_subagent only returns RESULT.md/STATUS.json when state == "idle".
    atlas-chat has no transcript, so without this the advertised completion
    path would never fire for it. STATUS.json is the evidence: run.sh writes
    it only after the CLI exits, so its existence *is* completion.
    """
    from subagent_mcp.server import _state_for

    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    run = Run(
        run_id="rid",
        run_dir=run_dir,
        cwd=tmp_path,
        cli="atlas-chat",
        session="s",
        window="w",
    )
    # No STATUS.json yet: still unknown, not idle.
    assert _state_for(run, env["cfg"])[0] == ""

    (run_dir / "STATUS.json").write_text('{"exit_code": 0}', encoding="utf-8")
    state, last_activity = _state_for(run, env["cfg"])
    assert state == "idle"
    assert last_activity  # the STATUS.json mtime, so "when" is answerable


def test_status_json_does_not_override_a_live_transcript(env, tmp_path):
    """The STATUS.json rung sits *below* the transcript, not above it.

    A claude run whose transcript says "working" must not be declared idle
    just because a STATUS.json from an earlier resume is lying around.
    """
    import subagent_mcp.server as server

    run_dir = tmp_path / "rd2"
    run_dir.mkdir()
    (run_dir / "STATUS.json").write_text("{}", encoding="utf-8")
    run = Run(
        run_id="rid2",
        run_dir=run_dir,
        cwd=tmp_path,
        cli="claude",
        session="s",
        window="w",
    )
    orig = server.derive_state
    server.derive_state = lambda *a, **k: ("working", "2026-01-01T00:00:00Z")
    try:
        assert server._state_for(run, env["cfg"])[0] == "working"
    finally:
        server.derive_state = orig


def test_process_matching_handles_the_console_script_shape():
    """atlas-chat is a shebang console script, so ps shows the interpreter.

    Observed on the host with the real binary:

        /…/atlas-ui-3/.venv/bin/python3 .venv/bin/atlas-chat --list-models

    argv[0] is python3, not atlas-chat — so anything matching only argv[0]
    would see no agent process and report a working run as exited the moment
    it started. Basename matching over every token is what makes it work.
    """
    from subagent_mcp.server import _looks_like_cli

    real = (
        "/home/garlan/ATLAS-GROUP/atlas-ui-3/.venv/bin/python3 "
        ".venv/bin/atlas-chat --list-models"
    )
    assert _looks_like_cli(real, "atlas-chat")
    # Absolute path (how the wrapper invokes it) works the same way.
    assert _looks_like_cli(
        "/usr/bin/python3 /opt/atlas/.venv/bin/atlas-chat --agent-mode -- hi",
        "atlas-chat",
    )
    # A run.sh living under a directory that merely mentions the name is not
    # an atlas-chat process.
    assert not _looks_like_cli(
        "bash /home/x/runs/20260904-add-atlas-chat-launcher/run.sh", "atlas-chat"
    )
    # And it must not answer for a different CLI.
    assert not _looks_like_cli(real, "claude")
