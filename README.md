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

- `launch_subagent` — start `claude`, `opencode`, or `atlas-chat` in a tmux window
- `check_subagent` — capture the last N lines of output from a running subagent
- `watch_subagent` — put a running subagent on screen (or report how to attach)
- `list_subagents` — list all launched subagents and whether their agents are still running
- `send_to_subagent` — paste a follow-up message into a live subagent
- `resume_subagent` — re-engage an idle/finished run with a follow-up (`claude --resume` / `opencode run -s`)
- `stop_subagent` — stop a subagent's CLI, escalating until the process is verifiably gone
- `sweep_stale_subagents` — delete run records for subagents that are no longer running

`launch_subagent` takes `prompt` (or `prompt_file`), `cwd`, `cli`, and optional
`session` / `window` / `task` / `model` / `agent` / `extra_args`. Every launch
gets a directory under `runs/` holding `prompt.md`, `meta.json`, the generated
`run.sh`, a `RESULT.md` the agent is told to write before finishing, and a
`STATUS.json` the wrapper writes on exit (exit code, git SHAs before/after,
changed files, PR url) — so any subagent can be re-run by hand, the prompt is
never lost to a scrollback buffer, and a finished run is readable without
opening tmux.

Handles are interchangeable: a `run_id`, a tmux pane id (`%12`), or a
`session:window` target all resolve to the same run.

### Reading state without inventing it

`list_subagents` reports `total`, `alive`, and `dead` — deliberately not a
single `count`. A bare total was being read as "N agents are running" when most
of the N were dead records still on disk. Two more things the listing does
*not* tell you, and cannot:

- **`state` is not liveness.** It is where the CLI's transcript stopped, so a
  dead run normally reads `idle`. `alive: false` means dead.
- **`alive` is about the process, not the pane.** The two come apart by
  design: `run.sh` ends with `exec "$SHELL" -i`, so the window stays open for
  inspection after the CLI exits. A run's `pane` field says which case it is
  in — `running`, `exited` (agent finished, pane parked at a shell), or
  `gone`. Only `running` is a working agent; `finished_pane_open` counts the
  middle case. Before this was measured from the process table, every
  finished run reported `alive: true` forever, held a concurrency slot, and
  could never be swept.
- **`task` is the launch label, nothing more.** `result: "present"` means the
  run wrote a `RESULT.md`; read it with `check_subagent`. There is no field
  describing what an agent did or concluded, so do not narrate one.

`send_to_subagent` types into a live pane. If the pane is gone — or if it is
still open but its agent has exited, where the keystrokes would land in a bash
prompt and *run your message as a shell command* — it fails with an error
naming `resume_subagent`, which rebuilds the conversation from the CLI's
persisted session on disk and needs no TTY.

### Stopping actually stops

One `Ctrl-C` does not stop a TUI CLI. `claude` and `opencode` run the terminal
in raw mode, so `^C` reaches them as a keystroke meaning "cancel the current
turn" — no `SIGINT` is ever generated. `stop_subagent` used to send one and
report success, which is how a fleet of "stopped" agents kept running.

It now escalates — `Ctrl-C` → second `Ctrl-C` → `SIGTERM` → `SIGKILL` —
re-reading the pane's process subtree after each rung and stopping at the
first one that works. The reply carries `stopped` (verified, not assumed),
`method` (which rung did it), and `attempts`. Signals target the CLI's own
pids rather than the `run.sh` wrapper, so the wrapper still writes
`STATUS.json`; pids orphaned by a dying pane are tracked and killed too.

Stopping does **not** remove the run from `list_subagents`: the record and its
`RESULT.md` survive so they can still be read, and the run simply flips to
`alive: false`. `sweep_stale_subagents` is what deletes records.

### `output.log` is not text

With `pipe_logs` on, each run's pane is teed to `<run_dir>/output.log`. A
full-screen TUI positions the cursor with escape sequences rather than emitting
newlines, so that file is one enormous line of ANSI — an 11 MB log had **zero**
newlines, and reconstructing lines from it would take a terminal emulator. It
is a `less -R` replay artefact for a human. Use `check_subagent`, which
captures the *rendered* screen, for anything programmatic.

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

Some CLIs are not on anyone's `PATH` by design. `atlas-chat` is a console
script inside the ATLAS virtualenv, so it also gets searched for under
`~/ATLAS-GROUP/atlas-ui-3/.venv/bin` and `~/git/atlas/atlas-ui-3/.venv/bin`.
Those per-CLI roots are searched last, so a copy already on `PATH` still wins,
and they are never added to another CLI's search path.

If it still cannot find one, the launch fails immediately with the list of
directories searched, instead of leaving you a dead pane. Point it at a binary
explicitly with `SUBAGENT_CLI_OPENCODE` / `SUBAGENT_CLI_CLAUDE` /
`SUBAGENT_CLI_ATLAS_CHAT` (the CLI name uppercased, `-` becoming `_`).

### atlas-chat

`atlas-chat` is ATLAS's own non-interactive chat CLI (the console script in
[atlas-ui-3](https://github.com/sandialabs/atlas-ui-3)). It is supported as a
`cli` value alongside `claude` and `opencode`, but it is a different kind of
animal and the differences are worth knowing before you launch one:

| | `claude` / `opencode` | `atlas-chat` |
| --- | --- | --- |
| Prompt | flag value (`--prompt`) or positional | trailing positional |
| Session | resumable conversation id | none — one-shot, answers and exits |
| `agent` | `--agent <name>` | rejected; no equivalent flag |
| `dangerous` | skip permission prompts | `--agent-mode` (let the model call tools) |
| Completion signal | Stop hook / transcript | pane exit + `STATUS.json` |

Because there is no conversation to resume, `resume_subagent` refuses an
`atlas-chat` run outright (its `session_id_status` is `unsupported`, which is
distinct from `capture_failed` — nothing was lost, there was never anything to
capture). Fold any follow-up into a fresh launch.

`dangerous` defaults to true and therefore adds `--agent-mode`. If you pass
`--agent-mode` or `--only-rag` yourself in `extra_args`, your choice wins:
atlas-chat's parser makes those two mutually exclusive, so emitting both would
make argparse reject the whole invocation.

#### The DuckDB lock

ATLAS keeps chat history in DuckDB, defaulting to `duckdb:///data/chat_history.db`
relative to the working directory. DuckDB is single-writer, so an `atlas-chat`
launched into an ATLAS checkout while `atlas-server` is running hits:

```
IO Error: Could not set lock on file ".../data/chat_history.db":
Conflicting lock is held in .../python3.14 (PID 1090794) by user garlan.
```

(`atlas-chat --help` and `--version` short-circuit in argparse before the
database is touched, so they succeed either way — the lock only shows up once
a subcommand actually initialises the database, e.g. `--list-models`.)

The generated `run.sh` therefore points each run at its own database:

```sh
export CHAT_HISTORY_DB_URL="${CHAT_HISTORY_DB_URL:-duckdb:///<run_dir>/chat_history.db}"
```

That sidesteps the lock and keeps one subagent's history out of another's. The
`:-` default means an operator who deliberately exports `CHAT_HISTORY_DB_URL`
— to share the production database, say — keeps their value.

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
| `--cli-allowlist` | `SUBAGENT_CLI_ALLOWLIST` | `claude,opencode,atlas-chat` | CLIs the server will invoke |
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

`sweep_stale_subagents` is the on-demand complement: it reaps the run
directories of subagents that are no longer running — the records
`list_subagents` reports as `alive: false`, whether their pane went with them
(`pane: "gone"`) or is still parked at a shell (`pane: "exited"`). A leftover
pane for a reaped run is closed too, so the session does not fill with dead
windows. **The run directory includes `RESULT.md`**, so read anything you
still need first; `dry_run=true` previews the deletions and lists which runs
have a result to lose. Pass `session="atlas"` to scope it to one tmux session.
Running subagents are never touched.

Two fields worth knowing in the sweep reply. `remaining_total` counts what is
left *after* the sweep (it used to subtract the deletion twice and could report
a negative number). `unsweepable` lists dead runs that were discovered but
cannot be reaped: `meta.json` is found at any depth under `runs/`, while
deletion only ever touches a directory sitting directly beneath it. A run in
that state is named rather than silently retried forever.

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
