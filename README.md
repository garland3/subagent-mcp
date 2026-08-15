# subagent-mcp

An MCP server that collapses "launch a coding assistant in a tmux window" into
one tool call.

Driving a TUI coding CLI from another agent is normally a ceremony: open a
window, send the command, wait for the TUI to boot, load a paste buffer, paste,
wait again, press Enter, then poll `capture-pane` to see what happened. Most of
that is unnecessary — both `claude` and `opencode` accept the prompt on the
command line — and the rest is bookkeeping a server should be doing for you.
`launch_subagent` does the whole thing and hands back a handle.

Requires `tmux` and at least one of `claude` / `opencode` installed (see
[Finding the CLI](#finding-the-cli) — it does not have to be on the server
process's `PATH`).

## Tools

- `launch_subagent` — start `claude` or `opencode` in a tmux window
- `check_subagent` — capture the last N lines of output from a running subagent
- `watch_subagent` — put a running subagent on screen (or report how to attach)
- `list_subagents` — list all launched subagents and whether their panes are alive
- `send_to_subagent` — paste a follow-up message into a live subagent
- `stop_subagent` — send `Ctrl-C` (or kill the tmux window)
- `sweep_stale_subagents` — delete run records for subagents whose panes are gone

`launch_subagent` takes `prompt` (or `prompt_file`), `cwd`, `cli`, and optional
`session` / `window` / `task` / `model` / `agent` / `extra_args`. Every launch
gets a directory under `runs/` holding `prompt.md`, `meta.json`, and the
generated `run.sh` — so any subagent can be re-run by hand, and the prompt is
never lost to a scrollback buffer.

Handles are interchangeable: a `run_id`, a tmux pane id (`%12`), or a
`session:window` target all resolve to the same run.

### Watching a subagent

Runs are detached by default — that is what makes fanning out cheap, but it also
means nothing appears on screen. Pass `watch` to `launch_subagent` (or call
`watch_subagent` on a run that is already going) to change that:

| `watch` | Effect |
| --- | --- |
| `off` (default) | Detached. The response still carries `attach_command`. |
| `switch` | Pulls your already-attached tmux client to the new window. |
| `terminal` | Opens a terminal emulator attached to the window. |

`switch` is the one to reach for if you live inside tmux; it is a no-op that
reports why when no client is attached, and it never fails a launch — the
subagent is running either way, visibility is best-effort.

`--watch-default` / `SUBAGENT_WATCH_DEFAULT` makes a mode the default for every
launch, so "always show me what it's doing" is a server setting, not something
to remember per call.

Every response also hands back:

- `attach_command` — `tmux attach -t sess:win`
- `attach_read_only` — the same with `-r`, so watching cannot disturb the run
- `output_log` — `<run_dir>/output.log`, a `pipe-pane` tee of everything the
  pane printed. `tail -f` it to follow a run without attaching, and read it
  afterwards for output that has already scrolled out of the pane's scrollback.
  Disable with `--no-pipe-logs`.

`terminal` autodetects kitty, wezterm, ghostty, alacritty, foot,
gnome-terminal, konsole, xfce4-terminal, `x-terminal-emulator`, and xterm. Set
`SUBAGENT_TERMINAL` to a template containing `{cmd}` to override, e.g.
`kitty -- bash -c {cmd}`. It needs `DISPLAY`/`WAYLAND_DISPLAY` in the *server's*
environment; without one it reports that and leaves the run detached.

### Finding the CLI

The subagent inherits the environment of whatever started the MCP server, which
is often a desktop launcher with a stripped `PATH` — so a perfectly working
`opencode` on your shell's `PATH` would die in the pane with `command not
found` (exit 127). The server therefore resolves the CLI to an absolute path
before launching, searching `PATH` plus the usual per-user install roots
(`~/.opencode/bin`, `~/.claude/local`, `~/.local/bin`, `~/.bun/bin`,
`~/.cargo/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, nvm's newest node), and
exports that widened `PATH` inside `run.sh` for the CLI's own helpers.

If it still cannot find one, the launch fails immediately with the list of
directories searched, instead of leaving you a dead pane. Point it at a binary
explicitly with `SUBAGENT_CLI_OPENCODE` / `SUBAGENT_CLI_CLAUDE`.

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
| `--watch-default` | `SUBAGENT_WATCH_DEFAULT` | `off` | Default visibility: `off` / `switch` / `terminal` |
| `--terminal` | `SUBAGENT_TERMINAL` | autodetect | Terminal template for `watch=terminal`, e.g. `kitty -- bash -c {cmd}` |
| `--no-pipe-logs` | `SUBAGENT_NO_PIPE_LOGS` | off | Stop teeing pane output into `<run_dir>/output.log` |
| — | `SUBAGENT_CLI_OPENCODE` / `SUBAGENT_CLI_CLAUDE` | autodetect | Absolute path to a CLI binary |
| `--host` / `--port` | `SUBAGENT_HOST` / `SUBAGENT_PORT` | `127.0.0.1:8100` | HTTP transport only |

Retention runs on launch — the only operation that grows `runs/`. Both limits
count every run on disk, but a run whose pane is still alive is never deleted.

`sweep_stale_subagents` is the on-demand complement: it reaps run directories
whose panes are no longer alive (the dead records `list_subagents` reports as
`alive: false`). Pass `dry_run=true` to preview what would be cleared, or
`session="atlas"` to scope it to one tmux session. Live subagents are never
touched.

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
  cli_paths.py   locating CLI binaries a stripped PATH would miss
  watching.py    tmux client switching and terminal-emulator launching
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
