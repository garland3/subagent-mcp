from __future__ import annotations

import os
import shlex
from pathlib import Path

from .cli_paths import augmented_path, cli_extra_dirs


class RunnerError(Exception):
    """Invalid subagent invocation parameters."""


_POINTER = "Read the file at {path} and execute all instructions in it."

# Standing instruction appended to every launch prompt (Phase 1.1). Tells the
# agent to write a short result summary before finishing. The actual file path is
# inlined so the model sees it directly, and $SUBAGENT_RESULT_FILE is exported
# in run.sh so shell commands can reference it too.
#
# The instruction leads with the absolute path and states the negative
# explicitly. An earlier wording opened with "write a short RESULT.md to
# <path>", which invites the model to pattern-match on the bare filename and
# create ./RESULT.md in the working directory instead -- i.e. inside the repo,
# where `git add -A` sweeps it into a commit and (worst case) an upstream PR.
# The run directory is outside every repo, so a correctly-followed instruction
# cannot collide between concurrent agents or dirty a working tree.
# `_reconcile_stray_result` in server.py recovers the case where it is ignored.
_RESULT_INSTRUCTION = """

---
[subagent-mcp standing instruction]
Before finishing, write a short summary of your work to this exact absolute path:

    {result_path}

Write it there and nowhere else. Do NOT create a RESULT.md in the repository or
in your working directory -- that path is outside any repo precisely so your
summary never lands in a commit. The same path is in $SUBAGENT_RESULT_FILE.

Cover:
- What you did
- What you verified (commands run, outputs checked)
- What you could not do or left incomplete
- Open questions for the operator

This file is how the operator reads your work without opening tmux."""

# Sentinel used while building argv so that wrapper_command can render the
# prompt token specially (e.g., as a shell command substitution in inline mode)
# without doing fragile string equality checks against user-provided arguments.
_PROMPT_SENTINEL = object()


class CLIRunner:
    """Builds the command string and wrapper script for a supported CLI."""

    # atlas-chat is ATLAS's own non-interactive chat CLI (atlas-ui-3's
    # ``atlas-chat`` console script). Unlike claude/opencode it is one-shot:
    # no TUI, no conversation id, no resume. It takes the prompt as a trailing
    # positional argument.
    _CLIs = ("claude", "opencode", "atlas-chat")

    # CLIs with no notion of a persisted conversation id, so ``session_id``
    # is meaningless for them and resume_subagent cannot work.
    _NO_SESSION_CLIs = ("atlas-chat",)

    # CLIs with no per-run agent/persona selector.
    _NO_AGENT_CLIs = ("atlas-chat",)

    def __init__(
        self,
        cli: str,
        *,
        prompt: str,
        prompt_mode: str,
        run_dir: Path,
        model: str | None = None,
        agent: str | None = None,
        dangerous: bool = True,
        extra_args: list[str] | None = None,
        executable: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if cli not in self._CLIs:
            raise RunnerError(f"Unsupported cli {cli!r}; choose from {self._CLIs}")
        if prompt_mode not in ("inline", "pointer"):
            raise RunnerError("prompt_mode must be 'inline' or 'pointer'")
        if agent and cli in self._NO_AGENT_CLIs:
            raise RunnerError(f"{cli} has no --agent equivalent; drop the agent argument")
        if session_id and cli in self._NO_SESSION_CLIs:
            raise RunnerError(
                f"{cli} has no conversation id to pre-assign; leave session_id unset"
            )
        self.cli = cli
        # argv[0]: an absolute path when the caller resolved one, so the pane
        # does not depend on tmux having inherited a usable PATH.
        self.executable = executable or cli
        self.prompt = prompt
        self.prompt_mode = prompt_mode
        self.run_dir = run_dir
        self.model = model
        self.agent = agent
        self.dangerous = dangerous
        self.extra_args = extra_args or []
        # CLI conversation id. For claude this is minted by the caller and
        # passed as ``--session-id <uuid>`` so the run is resumable from the
        # first moment. opencode has no pre-assign flag, so this stays None
        # here and the id is captured post-launch instead.
        self.session_id = session_id

        # Phase 1.1: RESULT.md contract. The standing instruction is appended
        # to the user's prompt before persisting, and the env var is exported
        # in the wrapper so shell commands inside the agent can reference it.
        self._result_path = run_dir / "RESULT.md"
        prompt = prompt.rstrip() + _RESULT_INSTRUCTION.format(result_path=self._result_path)

        # Always persist the full prompt (with the standing instruction) to disk.
        self._prompt_file = run_dir / "prompt.md"
        self._prompt_file.write_text(prompt, encoding="utf-8")

        if prompt_mode == "pointer":
            self._effective_prompt = _POINTER.format(path=str(self._prompt_file))
        else:
            # Inline: load the saved prompt into argv through the shell wrapper.
            self._effective_prompt = f"$(cat {shlex.quote(str(self._prompt_file))})"

    def _shell_prompt_token(self) -> str:
        """How the effective prompt should appear in the generated shell wrapper."""
        if self.prompt_mode == "inline":
            # Double-quote the command substitution so it stays one shell word
            # while the $(..) expands to the full prompt content.
            return f'"{self._effective_prompt}"'
        return shlex.quote(self._effective_prompt)

    def _arg_tuples(self) -> list[tuple[str | object, bool]]:
        """Return argv tokens paired with a flag indicating the prompt token.

        The sentinel object marks where the prompt belongs; it never appears in
        the final argv strings.
        """
        argv: list[tuple[str | object, bool]] = [(self.executable, False)]
        if self.cli == "claude":
            if self.dangerous:
                argv.append(("--dangerously-skip-permissions", False))
            # Pre-assign the claude conversation id so the run is resumable
            # from the first moment. opencode has no equivalent flag; its id
            # is captured post-launch instead (see server.py).
            if self.session_id:
                argv.extend([("--session-id", False), (self.session_id, False)])
            if self.model:
                argv.extend([("--model", False), (self.model, False)])
            if self.agent:
                argv.extend([("--agent", False), (self.agent, False)])
            argv.extend((arg, False) for arg in self.extra_args)
            argv.append((_PROMPT_SENTINEL, True))
        elif self.cli == "opencode":
            if self.dangerous:
                argv.append(("--auto", False))
            if self.model:
                argv.extend([("-m", False), (self.model, False)])
            if self.agent:
                argv.extend([("--agent", False), (self.agent, False)])
            argv.extend((arg, False) for arg in self.extra_args)
            argv.append(("--prompt", False))
            argv.append((_PROMPT_SENTINEL, True))
        elif self.cli == "atlas-chat":
            # atlas-chat has no permission prompts to skip -- it is
            # non-interactive by construction. ``dangerous`` maps to the
            # nearest analogue: --agent-mode, which lets the model decide when
            # to call tools rather than answering from the prompt alone. A
            # subagent that cannot act is not much of a subagent.
            #
            # --agent-mode and --only-rag are argparse-mutually-exclusive, so
            # an explicit choice in extra_args wins over the default.
            if self.dangerous and not (
                {"--agent-mode", "--only-rag"} & set(self.extra_args)
            ):
                argv.append(("--agent-mode", False))
            if self.model:
                argv.extend([("--model", False), (self.model, False)])
            argv.extend((arg, False) for arg in self.extra_args)
            # The prompt is a trailing positional, not a flag value -- so a
            # prompt that happens to begin with "-" ("--json is confusing me",
            # "-o means what here?") is otherwise argv the parser tries to
            # interpret. "--" ends option parsing and makes the rest literal.
            argv.append(("--", False))
            argv.append((_PROMPT_SENTINEL, True))
        return argv

    def _render(self, token: str | object, is_prompt: bool) -> str:
        if is_prompt:
            return self._shell_prompt_token()
        return shlex.quote(str(token))

    def _substituted_argv(self) -> list[str]:
        """Replace the prompt sentinel with the real prompt text."""
        return [
            self._effective_prompt if is_prompt else str(token)
            for token, is_prompt in self._arg_tuples()
        ]

    def argv(self) -> list[str]:
        """The argument vector as recorded in meta.json."""
        return self._substituted_argv()

    def wrapper_command(self) -> str:
        """The shell command to run in tmux (single quoting layer)."""
        return " ".join(
            self._render(token, is_prompt) for token, is_prompt in self._arg_tuples()
        )

    def _cli_env_exports(self) -> str:
        """Per-CLI environment the wrapper needs, or "" for CLIs that need none.

        atlas-chat writes to ATLAS's DuckDB chat history, which defaults to
        ``duckdb:///data/chat_history.db`` relative to the cwd -- i.e. the very
        file the long-running atlas-server process holds an exclusive lock on.
        DuckDB is single-writer, so a subagent launched while ATLAS is up dies
        with "Could not set lock on file ... Conflicting lock is held".

        Giving each run its own database sidesteps the lock entirely and keeps
        one subagent's history out of another's. An operator who deliberately
        wants the shared database can export CHAT_HISTORY_DB_URL themselves;
        the ``:-`` default only fills in when it is unset.
        """
        if self.cli != "atlas-chat":
            return ""
        db_path = self.run_dir / "chat_history.db"
        return (
            "# atlas-chat: give this run its own DuckDB so it does not contend\n"
            "# with the lock atlas-server holds on the shared chat history.\n"
            f"export CHAT_HISTORY_DB_URL=\"${{CHAT_HISTORY_DB_URL:-duckdb:///{db_path}}}\"\n"
        )

    def write_wrapper(self, run_id: str, cwd: Path) -> Path:
        """Write an executable run.sh into run_dir and return its path.

        The wrapper exports ``SUBAGENT_RESULT_FILE`` (Phase 1.1) and writes
        ``STATUS.json`` after the CLI exits (Phase 1.3). STATUS.json is the
        unfakeable evidence half — the wrapper, not the model, records the
        exit code, git SHAs, changed files, and branch. RESULT.md is the
        model's own account.
        """
        run_sh = self.run_dir / "run.sh"
        command = self.wrapper_command()
        status_path = self.run_dir / "STATUS.json"
        result_path = self._result_path
        # The CLI itself is invoked by absolute path, but it spawns helpers
        # (node, bun, ripgrep, git hooks) that need the same widened PATH.
        # Per-CLI install roots (e.g. the ATLAS venv for atlas-chat) belong in
        # the pane's PATH too, not just in argv[0] — the CLI's own subprocesses
        # look up siblings there.
        pane_path = os.pathsep.join([augmented_path(), *cli_extra_dirs(self.cli)])
        text = (
            f"#!/usr/bin/env bash\n"
            f"# generated by subagent-mcp — {run_id}\n"
            f"export PATH={shlex.quote(pane_path)}\n"
            f"# Phase 1.1: the agent writes its summary here before finishing.\n"
            f"export SUBAGENT_RESULT_FILE={shlex.quote(str(result_path))}\n"
            f"export SUBAGENT_RUN_ID={shlex.quote(run_id)}\n"
            f"export SUBAGENT_STATUS_FILE={shlex.quote(str(status_path))}\n"
            f"{self._cli_env_exports()}"
            f"cd {shlex.quote(str(cwd))} || exit 1\n"
            f"# Phase 1.3: capture git state before the agent runs.\n"
            f"git_sha_before=$(git rev-parse HEAD 2>/dev/null || echo \"\")\n"
            f"started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            f"{command}\n"
            f"code=$?\n"
            f"# Phase 1.3: capture git state and write STATUS.json (the wrapper,\n"
            f"# not the model, writes this — the unfakeable evidence half).\n"
            f"ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            f"git_sha_after=$(git rev-parse HEAD 2>/dev/null || echo \"\")\n"
            f"git_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo \"\")\n"
            f"if [ -n \"$git_sha_before\" ] && [ -n \"$git_sha_after\" ] \\\n"
            f"   && [ \"$git_sha_before\" != \"$git_sha_after\" ]; then\n"
            f"  changed_files=$(git diff --name-only \"$git_sha_before\" \"$git_sha_after\" 2>/dev/null)\n"
            f"elif [ -n \"$git_sha_before\" ]; then\n"
            f"  changed_files=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)\n"
            f"else\n"
            f"  changed_files=\"\"\n"
            f"fi\n"
            f"pr_url=$(gh pr view --json url -q .url 2>/dev/null || echo \"\")\n"
            f"export SUBAGENT_EXIT_CODE=\"$code\"\n"
            f"export SUBAGENT_STARTED_AT=\"$started_at\"\n"
            f"export SUBAGENT_ENDED_AT=\"$ended_at\"\n"
            f"export SUBAGENT_GIT_SHA_BEFORE=\"$git_sha_before\"\n"
            f"export SUBAGENT_GIT_SHA_AFTER=\"$git_sha_after\"\n"
            f"export SUBAGENT_GIT_BRANCH=\"$git_branch\"\n"
            f"export SUBAGENT_CHANGED_FILES=\"$changed_files\"\n"
            f"export SUBAGENT_PR_URL=\"$pr_url\"\n"
            f"python3 -c \"\n"
            f"import json, os\n"
            f"status = {{\n"
            f"  'run_id': os.environ.get('SUBAGENT_RUN_ID', ''),\n"
            f"  'exit_code': int(os.environ.get('SUBAGENT_EXIT_CODE', '0') or '0'),\n"
            f"  'started_at': os.environ.get('SUBAGENT_STARTED_AT', ''),\n"
            f"  'ended_at': os.environ.get('SUBAGENT_ENDED_AT', ''),\n"
            f"  'git_sha_before': os.environ.get('SUBAGENT_GIT_SHA_BEFORE', ''),\n"
            f"  'git_sha_after': os.environ.get('SUBAGENT_GIT_SHA_AFTER', ''),\n"
            f"  'git_branch': os.environ.get('SUBAGENT_GIT_BRANCH', ''),\n"
            f"  'changed_files': [f for f in os.environ.get('SUBAGENT_CHANGED_FILES', '').splitlines() if f],\n"
            f"  'pr_url': os.environ.get('SUBAGENT_PR_URL', ''),\n"
            f"}}\n"
            f"with open(os.environ.get('SUBAGENT_STATUS_FILE', '/dev/null'), 'w') as fh:\n"
            f"  json.dump(status, fh, indent=2)\n"
            f"\"\n"
            f"printf '\\n[subagent-mcp] exited %d — run dir: %s\\n' \"$code\" {shlex.quote(str(self.run_dir))}\n"
            f'exec "${{SHELL:-/bin/bash}}" -i\n'
        )
        run_sh.write_text(text, encoding="utf-8")
        # Make it executable for easy manual re-run; tmux invokes it through a shell.
        run_sh.chmod(0o755)
        return run_sh


def build_runner(
    cli: str,
    *,
    prompt: str,
    prompt_mode: str,
    run_dir: Path,
    model: str | None = None,
    agent: str | None = None,
    dangerous: bool = True,
    extra_args: list[str] | None = None,
    executable: str | None = None,
    session_id: str | None = None,
) -> CLIRunner:
    return CLIRunner(
        cli=cli,
        prompt=prompt,
        prompt_mode=prompt_mode,
        run_dir=run_dir,
        model=model,
        agent=agent,
        dangerous=dangerous,
        extra_args=extra_args,
        executable=executable,
        session_id=session_id,
    )
