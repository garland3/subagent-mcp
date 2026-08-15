import shlex

from subagent_mcp.runners import build_runner


def test_claude_inline_argv(tmp_path):
    prompt = "line1\nline2 `cmd` $(echo hi) 'quote'"
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt=prompt,
        prompt_mode="inline",
        run_dir=run_dir,
        model="opus",
        agent="reviewer",
        dangerous=True,
        extra_args=["--bare"],
    )
    argv = runner.argv()
    assert argv[0] == "claude"
    assert "--dangerously-skip-permissions" in argv
    assert "--bare" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == "reviewer"
    # The prompt is the last positional argument.
    assert argv[-1].startswith("$(cat")


def test_opencode_pointer_argv(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "opencode",
        prompt="instructions",
        prompt_mode="pointer",
        run_dir=run_dir,
        model="x/y",
        dangerous=True,
        extra_args=["--pure"],
    )
    argv = runner.argv()
    assert argv[0] == "opencode"
    assert "--auto" in argv
    assert "--pure" in argv
    assert "--prompt" in argv
    prompt_token = argv[argv.index("--prompt") + 1]
    assert "Read the file" in prompt_token
    # Pointer text should not contain a shell substitution.
    assert "$(cat" not in prompt_token


def test_wrapper_quoting(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="hello 'world'",
        prompt_mode="inline",
        run_dir=run_dir,
        model="sonnet",
        dangerous=True,
        extra_args=["--add-dir", "/tmp/a b"],
    )
    cmd = runner.wrapper_command()
    # Quotes around --add-dir value come from shlex and keep it a single token.
    assert "--add-dir" in cmd
    assert shlex.split(cmd)  # should be valid shell
    assert "$(cat" in cmd


def test_prompt_persisted(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner = build_runner(
        "claude",
        prompt="保存\nПроверка",
        prompt_mode="inline",
        run_dir=run_dir,
        dangerous=True,
        extra_args=[],
    )
    runner.write_wrapper("rid", tmp_path / "cwd")
    # Phase 1.1: the standing RESULT.md instruction is appended to the prompt.
    content = (run_dir / "prompt.md").read_text(encoding="utf-8")
    assert content.startswith("保存\nПроверка")
    assert "RESULT.md" in content
