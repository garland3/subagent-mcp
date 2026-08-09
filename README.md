# subagent-mcp

An MCP server that collapses "launch a coding assistant in a tmux window" into
one tool call.

Driving a TUI coding CLI from another agent is normally a ceremony: open a
window, send the command, wait for the TUI to boot, load a paste buffer, paste,
wait again, press Enter, then poll `capture-pane` to see what happened. Most of
that is unnecessary — both `claude` and `opencode` accept the prompt on the
command line — and the rest is bookkeeping a server should be doing for you.
`launch_subagent` does the whole thing and hands back a handle.

Requires `tmux` and at least one of `claude` / `opencode` on `PATH`.

## Tools

- `launch_subagent` — start `claude` or `opencode` in a detached tmux window
- `check_subagent` — capture the last N lines of output from a running subagent
- `list_subagents` — list all launched subagents and whether their panes are alive
- `send_to_subagent` — paste a follow-up message into a live subagent
- `stop_subagent` — send `Ctrl-C` (or kill the tmux window)

`launch_subagent` takes `prompt` (or `prompt_file`), `cwd`, `cli`, and optional
`session` / `window` / `task` / `model` / `agent` / `extra_args`. Every launch
gets a directory under `runs/` holding `prompt.md`, `meta.json`, and the
generated `run.sh` — so any subagent can be re-run by hand, and the prompt is
never lost to a scrollback buffer.

Handles are interchangeable: a `run_id`, a tmux pane id (`%12`), or a
`session:window` target all resolve to the same run.

### Blocked detection

A launched CLI can sit waiting on a human instead of working — the folder-trust
prompt on a directory `claude` hasn't seen before, a login screen, or a
mid-run permission modal. A pane in that state still looks alive, so
`launch_subagent` and `check_subagent` both report `blocked` and
`blocked_reason`.

The detector matches verbatim CLI chrome, line by line, and skips lines that
came from the submitted prompt — the TUI echoes it back, so without that a
prompt like *"fix the authentication bug"* would report itself as blocked.

Note that `--dangerously-skip-permissions` does **not** bypass the folder-trust
prompt. Trust the directory once by hand, or expect `blocked: true`.

## Running

```bash
uv sync

# stdio (for an MCP client)
.venv/bin/subagent-mcp --stdio --runs-root ./runs

# HTTP for poking
.venv/bin/subagent-mcp --http --port 8100
```

### Options

Every flag has a matching environment variable.

| Flag | Env | Default | Meaning |
| --- | --- | --- | --- |
| `--allowed-roots` | `SUBAGENT_ALLOWED_ROOTS` | `~/git,~/ATLAS-GROUP` | Parent dirs a subagent `cwd` may live under |
| `--any-cwd` | — | off | Disable the `cwd` allowlist entirely |
| `--cli-allowlist` | `SUBAGENT_CLI_ALLOWLIST` | `claude,opencode` | CLIs the server will invoke |
| `--max-concurrent` | `SUBAGENT_MAX_CONCURRENT` | `12` | Cap on live launched panes |
| `--runs-root` | `SUBAGENT_RUNS_ROOT` | `./runs` | Where per-run artefacts go |
| `--runs-keep-max` | `SUBAGENT_RUNS_KEEP_MAX` | `200` | Keep at most this many run dirs (`0` disables) |
| `--runs-max-age-days` | `SUBAGENT_RUNS_MAX_AGE_DAYS` | `14` | Delete run dirs older than this (`0` disables) |
| `--tmux-socket` | `SUBAGENT_TMUX_SOCKET` | default socket | Use a private tmux server |
| `--host` / `--port` | `SUBAGENT_HOST` / `SUBAGENT_PORT` | `127.0.0.1:8100` | HTTP transport only |

Retention runs on launch — the only operation that grows `runs/`. Both limits
count every run on disk, but a run whose pane is still alive is never deleted.

## Client registration

```json
"subagent": {
  "command": [
    "/path/to/subagent-mcp/.venv/bin/subagent-mcp",
    "--stdio",
    "--runs-root", "/path/to/subagent-mcp/runs"
  ],
  "cwd": "/path/to/subagent-mcp",
  "transport": "stdio",
  "description": "Launch and supervise coding-CLI subagents (claude, opencode) in detached tmux windows."
}
```

## Safety

This is intentionally an unsandboxed, permission-checks-off subagent launcher.
It runs coding agents with `--dangerously-skip-permissions` / `--auto` by
default, which means anything it launches can modify your files and run
arbitrary commands. **It only belongs on a single-user trusted dev box.** The
guards are mistake-catchers, not a security boundary:

- `--allowed-roots` restricts where `cwd` can point.
- `--max-concurrent` limits live launched panes.
- Only CLIs listed in `--cli-allowlist` can be invoked.

Pass `dangerous=false` per launch if you want the CLI's own permission prompts
back — `check_subagent` will surface the resulting modals as `blocked`.

## Layout

```
src/subagent_mcp/
  __main__.py    CLI entrypoint (stdio | HTTP)
  config.py      ServerConfig
  server.py      FastMCP tool definitions
  runners.py     claude/opencode argv and wrapper generation
  tmuxio.py      thin wrapper around the tmux binary
  runs.py        per-run directory bookkeeping and retention
```

`plan.md` carries the design notes and the review findings behind each phase.

## Tests

```bash
uv sync --extra dev
uv run pytest
```

Tests spin a private tmux socket (`-L subagent-test-*`) and a fake `claude` /
`opencode` on PATH, so they do not touch your real tmux sessions.

## License

MIT — see [LICENSE](./LICENSE).
