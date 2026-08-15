from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .cli_paths import resolve_cli, searched_dirs
from .config import ServerConfig, cwd_is_allowed, get_config
from .runners import RunnerError, build_runner
from .runs import (
    Run,
    choose_window_name,
    create_run_dir,
    delete_run_dirs,
    derive_state,
    prune_runs,
    registry as runs_registry,
    update_run_pane,
    update_run_session,
    window_base,
)
from .tmuxio import Tmux, TmuxError
from .watching import (
    WATCH_MODES,
    WatchError,
    attach_command,
    open_in_terminal,
    read_only_command,
)


_DEFAULT_INSTRUCTIONS = (
    "Launch coding-CLI subagents (claude, opencode) in tmux windows — detached by "
    "default, or visible with watch='switch'/'terminal'. Tools: launch_subagent, "
    "check_subagent, watch_subagent, list_subagents, send_to_subagent, "
    "stop_subagent, sweep_stale_subagents."
)

# Output snippets that indicate the subagent CLI is blocked waiting for human
# interaction instead of accepting the injected prompt.
#
# These must be specific enough that ordinary agent output never trips them.
# Bare words like "authentication" or "sign in" are far too broad: the TUI
# echoes the submitted prompt back into the pane, so "fix the authentication
# bug" would report itself as blocked.
_BLOCKED_PATTERNS = [
    re.compile(r"Quick safety check", re.IGNORECASE),
    re.compile(r"Is this a project you created", re.IGNORECASE),
    re.compile(r"Do you trust (?:this|the) (?:project|folder|files)", re.IGNORECASE),
    re.compile(r"Yes, I trust this folder", re.IGNORECASE),
    re.compile(r"Select login method", re.IGNORECASE),
    re.compile(r"Sign in to (?:Claude|your account)", re.IGNORECASE),
    re.compile(r"Log in with (?:a )?Claude account", re.IGNORECASE),
    re.compile(r"Invalid API key", re.IGNORECASE),
    # Permission modals. These only appear mid-run (and only when the CLI was
    # launched without dangerous=True), so check_subagent is where they matter.
    # The wording is verbatim CLI chrome, not anything an agent narrates.
    re.compile(r"Do you want to (?:proceed|make this edit|create)", re.IGNORECASE),
    re.compile(r"Yes, and don.t ask again", re.IGNORECASE),
    re.compile(r"No, and tell Claude what to do differently", re.IGNORECASE),
]

# Leading decoration the TUIs put in front of echoed input and status lines.
_TUI_MARKS = "❯>●·▶│┃*- \t"

mcp = FastMCP("subagent-mcp", instructions=_DEFAULT_INSTRUCTIONS)


def _tip(name: str) -> dict[str, Any]:
    """Build annotations hint for an MCP tool."""
    if name == "launch_subagent":
        return {"readOnlyHint": False, "openWorldHint": True}
    if name == "watch_subagent":
        # Not destructive, but it moves the user's terminal view / opens a window.
        return {"readOnlyHint": False}
    destructive = name in {"stop_subagent", "send_to_subagent", "sweep_stale_subagents"}
    return {"destructiveHint": True} if destructive else {"readOnlyHint": True}


def _cfg() -> ServerConfig:
    return get_config()


def _tmux(cfg: ServerConfig | None = None) -> Tmux:
    cfg = cfg or _cfg()
    socket = cfg.tmux_socket
    return Tmux(socket=socket if socket else None)


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    return str(value)


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _load_registry(cfg: ServerConfig | None = None) -> dict[str, Run]:
    cfg = cfg or _cfg()
    return runs_registry(cfg.runs_root, refresh=True)


def _resolve_handle(handle: str, cfg: ServerConfig | None = None) -> Run:
    cfg = cfg or _cfg()
    reg = _load_registry(cfg)

    if handle in reg:
        return reg[handle]

    if handle.startswith("%"):
        for run in reg.values():
            if run.pane_id == handle:
                return run
        raise ToolError(f"Unknown pane id: {handle!r}")

    if ":" in handle:
        parts = handle.split(":", 1)
        session, window = parts[0], parts[1]
        for run in reg.values():
            if run.session == session and run.window == window:
                return run
        raise ToolError(f"No tracked run for target {handle!r}")

    raise ToolError(f"No tracked run matching handle {handle!r}")


def _detect_blocked_output(output: str, prompt_text: str = "") -> tuple[bool, str | None]:
    """Return (is_blocked, reason) if the output looks like a login/trust dialog.

    Scans line by line and ignores lines that came from the prompt we just
    submitted — the TUI echoes it back into the pane, so a prompt that happens
    to discuss a trust dialog must not be mistaken for one.
    """
    prompt_lines = [line.strip() for line in prompt_text.splitlines() if line.strip()]

    for raw_line in output.splitlines():
        line = raw_line.strip(_TUI_MARKS).strip()
        if not line:
            continue
        if any(line in prompt_line for prompt_line in prompt_lines):
            continue
        for pattern in _BLOCKED_PATTERNS:
            if pattern.search(line):
                return True, f"blocked by interactive prompt: {pattern.pattern}"
    return False, None


def _run_prompt_text(run: Run) -> str:
    """The prompt this run was launched with, for echo-suppression.

    Every launch persists prompt.md, so this is normally available; a missing
    file just means no echo suppression, not an error.
    """
    try:
        return (run.run_dir / "prompt.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _live_panes(tm: Tmux) -> set[tuple[str, int]]:
    """Snapshot of live panes as (pane_id, pane_pid) tuples."""
    live: set[tuple[str, int]] = set()
    for pane in tm.list_panes():
        try:
            live.add((pane["pane_id"], int(pane["pane_pid"])))
        except (KeyError, ValueError):
            continue
    return live


def _is_alive(run: Run, live: set[tuple[str, int]]) -> bool:
    if not run.pane_id:
        return False
    # Prefer pane-pid verification when available to avoid treating a recycled
    # pane id from a restarted tmux server as the same run.
    if run.pane_pid is not None:
        return (run.pane_id, run.pane_pid) in live
    return any(pane_id == run.pane_id for pane_id, _ in live)


def _active_count(tm: Tmux, cfg: ServerConfig | None = None) -> int:
    cfg = cfg or _cfg()
    reg = _load_registry(cfg)
    live = _live_panes(tm)
    return sum(1 for run in reg.values() if _is_alive(run, live))


def _state_for(run: Run, cfg: ServerConfig) -> tuple[str, str]:
    """Transcript-derived (state, last_activity) for a run, via runs.derive_state.

    Reads the CLI's own persisted transcript, not the tmux TUI, so this works
    whether or not the pane is alive. See runs.derive_state for the per-CLI
    paths and the quiescence/idle heuristics.
    """
    return derive_state(
        run,
        claude_projects_root=cfg.claude_projects_root,
        opencode_state_root=cfg.opencode_state_root,
    )


# A failed opencode capture must be visible in the run record, not silent —
# an unresumable run is the failure mode the design calls out (§3.3 risk 5).
# This helper never raises; it returns the id, or None if no matching session
# was found. The caller records the outcome on the Run.
def _capture_opencode_session_id(
    run: Run,
    executable: str,
    *,
    opencode_state_root: Path | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Best-effort discovery of the opencode session id right after launch.

    opencode does not expose a pre-assign flag (see §3.1 of the design), so
    the id has to be captured post-launch. Tries ``opencode session list``
    first (preferring JSON output), then falls back to scanning the SQLite
    DB under ``opencode_state_root``. Returns None on any failure — the
    caller records ``capture_failed``.

    ``opencode_state_root`` defaults to ``~/.local/share/opencode`` for
    production use; tests override it so they never touch the host DB.
    """
    # Try the CLI's own session enumeration first.
    for argv in (
        [executable, "session", "list", "--json"],
        [executable, "session", "list"],
    ):
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        sid = _match_opencode_session(proc.stdout, run)
        if sid:
            return sid

    # Fall back to the SQLite DB directly. Use the configured root so tests
    # never touch the host's real opencode state.
    root = opencode_state_root or (Path.home() / ".local" / "share" / "opencode")
    db_path = root / "opencode.db"
    if db_path.is_file():
        sid = _match_opencode_session_db(db_path, run)
        if sid:
            return sid
    return None


def _match_opencode_session(text: str, run: Run) -> str | None:
    """Find the session id in ``opencode session list`` output for this run."""
    raw = text.strip()
    if not raw:
        return None
    # Prefer a structured JSON response.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _find_uuid(raw)

    sessions: list[Any]
    if isinstance(data, list):
        sessions = data
    elif isinstance(data, dict) and isinstance(data.get("sessions"), list):
        sessions = data["sessions"]
    elif isinstance(data, dict):
        sessions = [data]
    else:
        sessions = []

    cwd_str = str(run.cwd)
    fallback: str | None = None
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("session_id") or s.get("uuid")
        if not sid:
            continue
        s_cwd = s.get("cwd") or s.get("path") or s.get("directory") or ""
        if s_cwd:
            try:
                if Path(s_cwd).resolve() == Path(cwd_str).resolve():
                    return str(sid)
            except Exception:
                pass
        if fallback is None:
            fallback = str(sid)
    return fallback


def _match_opencode_session_db(db_path: Path, run: Run) -> str | None:
    """Last-resort: probe the opencode SQLite DB for the session id."""
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=2)
    except Exception:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        sess_table = next(
            (t for t in ("sessions", "session") if t in tables), None
        )
        if sess_table is None:
            return None
        cursor.execute(f"PRAGMA table_info({sess_table})")
        cols = {row[1] for row in cursor.fetchall()}
        id_col = next(
            (c for c in ("id", "session_id", "uuid") if c in cols), None
        )
        ts_col = next(
            (c for c in ("created_at", "created", "timestamp", "time") if c in cols),
            None,
        )
        if id_col is None:
            return None
        # Take the most recent row; matching by cwd is unreliable without
        # knowing the column name, so the caller prefers `opencode session list`.
        order = ts_col or "rowid"
        cursor.execute(
            f"SELECT {id_col} FROM {sess_table} ORDER BY {order} DESC LIMIT 5"
        )
        rows = cursor.fetchall()
        if rows:
            return str(rows[0][0])
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _find_uuid(text: str) -> str | None:
    """Extract the first UUID-like substring from text, or None."""
    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        text,
    )
    return match.group(0) if match else None


def _tail(text: str, lines: int = 40, *, strip_box: bool = True) -> str:
    """Return the last ``lines`` of ``text``, with TUI chrome removed.

    Box-drawing and block-element characters are ~14% of pane-capture tokens
    and carry no signal about whether the agent is alive or blocked. Stripping
    them (and dropping lines that become whitespace-only) keeps the
    ``initial_output`` payload focused on what the model actually needs to
    see. The default ``lines`` cap is 40; ``launch_subagent`` asks for fewer.
    """
    if strip_box:
        text = _strip_box_drawing(text)
    parts = text.splitlines()
    if len(parts) > lines:
        return "\n".join(parts[-lines:])
    return text


# Unicode box-drawing block (U+2500–U+257F) and block elements (U+2580–U+259F).
# These are the TUI border/shade/progress characters; stripping them does not
# lose any agent output. The leading-deco marks used by _detect_blocked_output
# (❯>●·▶) are handled separately there and are NOT stripped here — they are
# sometimes meaningful inside an output line.
_BOX_CHARS = frozenset(chr(c) for c in range(0x2500, 0x25A0))


def _strip_box_drawing(text: str) -> str:
    """Remove TUI border characters and drop the lines that become empty."""
    kept: list[str] = []
    for line in text.splitlines():
        cleaned = "".join(c for c in line if c not in _BOX_CHARS)
        if cleaned.strip():
            kept.append(cleaned.rstrip())
    return "\n".join(kept)


def _read_prompt(
    prompt: str | None,
    prompt_file: str | None,
) -> str:
    prompt_text: str | None = None

    if prompt and prompt.strip():
        prompt_text = prompt
    elif prompt is not None:
        # A non-None but empty/whitespace-only string was supplied explicitly.
        raise ToolError("prompt cannot be empty or whitespace only")

    if prompt_text is None and prompt_file:
        path = Path(prompt_file).expanduser().resolve()
        if not path.is_file():
            raise ToolError(f"prompt_file not found: {prompt_file!r}")
        try:
            prompt_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            raise ToolError(f"Could not read {prompt_file!r}: {exc}") from exc

    if prompt_text is None:
        raise ToolError("prompt or prompt_file is required")

    if not prompt_text:
        raise ToolError("resolved prompt cannot be empty")

    return prompt_text


def _parse_extra_args(extra_args: str | list[str] | None) -> list[str]:
    if extra_args is None:
        return []
    if isinstance(extra_args, list):
        return [str(x) for x in extra_args]
    # Allow passing a single shell-like string. This should be rare and is only
    # a convenience; prefer a JSON list.
    return shlex.split(extra_args)


def _resolve_executable(cli: str) -> str:
    """Absolute path to the CLI, or a ToolError explaining where we looked.

    Failing here beats failing in the pane: a bare name that tmux cannot
    resolve dies with an opaque ``command not found`` (exit 127) that the
    caller only sees as a dead window.
    """
    path = resolve_cli(cli)
    if path:
        return path
    raise ToolError(
        f"{cli!r} was not found on PATH. Searched: {', '.join(searched_dirs())}. "
        f"Set SUBAGENT_CLI_{cli.upper()}=/absolute/path/to/{cli} (or add its "
        "directory to the PATH of the process running subagent-mcp)."
    )


def _normalize_watch(watch: str | None, cfg: ServerConfig) -> str:
    mode = (watch or cfg.watch_default or "off").strip().lower()
    if mode in ("none", "false", "detached"):
        mode = "off"
    if mode in ("attach", "client", "here"):
        mode = "switch"
    if mode in ("window", "term", "gui"):
        mode = "terminal"
    if mode not in WATCH_MODES:
        raise ToolError(f"watch must be one of {WATCH_MODES}, got {watch!r}")
    return mode


def _apply_watch(tm: Tmux, cfg: ServerConfig, run: Run, mode: str) -> dict[str, Any]:
    """Put the run on screen according to ``mode``. Never fatal to a launch."""
    info: dict[str, Any] = {
        "watch": mode,
        "watch_applied": False,
        "watch_detail": "",
        "attach_command": attach_command(run.session, run.window, cfg.tmux_socket),
        "attach_read_only": read_only_command(run.session, run.window, cfg.tmux_socket),
    }
    if mode == "off" or not run.pane_id:
        return info

    try:
        if mode == "switch":
            clients = tm.list_clients()
            if not clients:
                info["watch_detail"] = (
                    "no tmux client is attached; run the attach_command above, "
                    "or use watch='terminal' to open one"
                )
                return info
            for client in clients:
                tm.switch_client(client["name"], run.session)
            tm.select_window(run.pane_id)
            info["watch_applied"] = True
            info["watch_detail"] = (
                f"switched {len(clients)} attached client(s) to {run.session}:{run.window}"
            )
        else:  # terminal
            info["watch_detail"] = open_in_terminal(
                tmux_bin=tm.path,
                socket=cfg.tmux_socket,
                session=run.session,
                pane_id=run.pane_id,
                terminal_command=cfg.terminal_command,
            )
            info["watch_applied"] = True
    except (TmuxError, WatchError) as exc:
        # The subagent is running regardless; visibility is best-effort.
        info["watch_detail"] = f"could not watch: {exc}"
    return info


def _start_output_log(tm: Tmux, run: Run) -> str:
    """Tee the pane into <run_dir>/output.log so it can be tail -f'd."""
    log_path = run.run_dir / "output.log"
    try:
        tm.pipe_pane(run.pane_id, f"cat >> {shlex.quote(str(log_path))}")
    except TmuxError:
        return ""
    return str(log_path)


def launch_subagent(
    prompt: Annotated[str | None, Field(description="Task prompt for the subagent")] = None,
    prompt_file: Annotated[str | None, Field(description="Path to a prompt file (fallback if prompt not provided)")] = None,
    prompt_mode: Annotated[str, Field(description="'inline' feeds the prompt on argv; 'pointer' feeds a bootstrap read-the-file prompt")] = "inline",
    cwd: Annotated[str | None, Field(description="Working directory for the subagent")] = None,
    cli: Annotated[str, Field(description="CLI to launch: claude or opencode")] = "claude",
    session: Annotated[str | None, Field(description="tmux session name; defaults to basename(cwd)")] = None,
    window: Annotated[str | None, Field(description="tmux window name; defaults to task or prompt slug")] = None,
    task: Annotated[str | None, Field(description="Short human label for the window name")] = None,
    model: Annotated[str | None, Field(description="Model argument passed to the CLI")] = None,
    agent: Annotated[str | None, Field(description="Agent argument passed to the CLI")] = None,
    dangerous: Annotated[bool, Field(description="Skip permission prompts (--dangerously-skip-permissions / --auto)")] = True,
    extra_args: Annotated[str | list[str] | None, Field(description="Additional CLI arguments (list preferred)")] = None,
    settle_seconds: Annotated[float, Field(description="Seconds to wait before capturing initial output")] = 3.0,
    watch: Annotated[str | None, Field(description="'off' = detached (default); 'switch' = pull an attached tmux client to this window; 'terminal' = open a terminal emulator on it")] = None,
    dry_run: Annotated[bool, Field(description="Return the generated wrapper without launching")] = False,
) -> str:
    """Launch a coding-CLI subagent (claude or opencode) in a detached tmux window.

    Use this instead of `tmux new-window` so the run gets a minted or captured
    session_id (resumable later via claude --resume / opencode run -s), a run
    directory holding prompt.md and output.log, and liveness reconciliation.
    Returns a run_id (e.g. 20260815-130009-fixit) plus the sess:win target;
    pass either back to the other subagent_* tools.

    The handle is the run_id or the full session:window (e.g. atlas:fixit) —
    never a bare window name, which can resolve to the wrong session.
    """
    cfg = _cfg()
    tm = _tmux(cfg)

    cwd_path = Path(cwd or cfg.root).expanduser().resolve()
    if not cwd_path.is_dir():
        raise ToolError(f"cwd is not a directory: {cwd!r}")
    if not cwd_is_allowed(cwd_path, cfg):
        allowed = [str(p) for p in cfg.allowed_roots]
        raise ToolError(
            f"cwd {str(cwd_path)!r} is outside the allowed roots ({allowed}). "
            "Pass --any-cwd at server startup to disable this guard."
        )

    if cli not in cfg.cli_allowlist:
        raise ToolError(f"cli {cli!r} is not in the allowlist: {cfg.cli_allowlist}")

    watch_mode = _normalize_watch(watch, cfg)
    executable = _resolve_executable(cli)

    prompt_text = _read_prompt(prompt, prompt_file)
    if not prompt_text:
        raise ToolError("prompt cannot be empty")

    if prompt_mode not in ("inline", "pointer"):
        raise ToolError("prompt_mode must be 'inline' or 'pointer'")

    # Mint the CLI conversation id up front when the CLI supports it. claude
    # takes --session-id <uuid>; opencode has no pre-assign flag and the id is
    # captured post-launch instead. Recording both on the Run is the key gap
    # from §3.3: without this, resuming a run means finding the session by
    # hand. A failed opencode capture is recorded as "capture_failed" — the
    # visible failure mode — rather than left silently empty.
    session_id = ""
    session_id_status = ""
    if cli == "claude":
        session_id = str(uuid.uuid4())
        session_id_status = "minted"
    # opencode: session_id stays "" here; captured after the pane is up.

    effective_session = session or cwd_path.name or "subagent"
    window_base_name = window or window_base(task, prompt_text)
    window = choose_window_name(
        effective_session,
        window_base_name,
        tm.list_windows,
    )

    if not dry_run and _active_count(tm, cfg) >= cfg.max_concurrent:
        raise ToolError(
            f"Maximum concurrent subagents reached ({cfg.max_concurrent}). "
            "Stop one before launching another."
        )

    try:
        run = create_run_dir(
            cfg.runs_root,
            session=effective_session,
            window=window,
            cwd=cwd_path,
            cli=cli,
            model=model,
            agent=agent,
            task=task,
            argv=[],  # filled after we build the runner
            session_id=session_id,
            session_id_status=session_id_status,
        )
        runner = build_runner(
            cli=cli,
            prompt=prompt_text,
            prompt_mode=prompt_mode,
            run_dir=run.run_dir,
            model=model,
            agent=agent,
            dangerous=dangerous,
            extra_args=_parse_extra_args(extra_args),
            executable=executable,
            session_id=session_id,
        )
        run.argv = runner.argv()
        run.write_meta()
        run_sh = runner.write_wrapper(run.run_id, cwd_path)
    except RunnerError as exc:
        raise ToolError(str(exc)) from exc

    if dry_run:
        return _json(
            {
                "run_id": run.run_id,
                "run_dir": str(run.run_dir),
                "run_sh": str(run_sh),
                "command": runner.wrapper_command(),
                "argv": run.argv,
                "executable": executable,
                "cwd": str(cwd_path),
                "session": run.session,
                "window": run.window,
                "session_id": run.session_id,
                "session_id_status": run.session_id_status,
                "watch": watch_mode,
                "dry_run": True,
            }
        )

    try:
        pane_id = tm.new_window(
            session=run.session,
            window_name=run.window,
            cwd=cwd_path,
            command=str(run_sh),
        )
    except TmuxError as exc:
        raise ToolError(f"tmux launch failed: {exc}") from exc

    pane_pid = tm.pane_pid(pane_id)
    update_run_pane(run, pane_id, pane_pid=pane_pid)

    # Start logging before the settle wait so nothing printed during startup is
    # lost, then put the run on screen if the caller asked to see it.
    output_log = _start_output_log(tm, run) if cfg.pipe_logs else ""
    watch_info = _apply_watch(tm, cfg, run, watch_mode)

    time.sleep(settle_seconds)

    # opencode: capture the session id post-launch (no pre-assign flag). A
    # failed capture is recorded visibly on the Run so the run is not
    # silently unresumable.
    if cli == "opencode" and not run.session_id:
        captured = _capture_opencode_session_id(
            run, executable, opencode_state_root=cfg.opencode_state_root
        )
        if captured:
            update_run_session(run, captured, "captured")
        else:
            update_run_session(run, "", "capture_failed")

    live: set[tuple[str, int]] = set()
    try:
        raw_output = tm.capture_pane(pane_id, lines=100)
        live = _live_panes(tm)
        alive = _is_alive(run, live)
        current_command = tm.pane_command(pane_id)
    except TmuxError:
        raw_output = ""
        alive = False
        current_command = None

    blocked, blocked_reason = _detect_blocked_output(raw_output, prompt_text)

    # Launch is the natural place to garbage-collect: it is the only tool that
    # grows runs/, and the live set is already in hand to protect running panes.
    pruned = prune_runs(
        cfg.runs_root,
        keep_max=cfg.runs_keep_max,
        max_age_days=cfg.runs_max_age_days,
        protect={r.run_id for r in _load_registry(cfg).values() if _is_alive(r, live)}
        | {run.run_id},
    )

    return _json(
        {
            "run_id": run.run_id,
            "pane_id": pane_id,
            "target": f"{run.session}:{run.window}",
            "session": run.session,
            "window": run.window,
            "cwd": str(cwd_path),
            "cli": cli,
            "executable": executable,
            "run_dir": str(run.run_dir),
            "output_log": output_log,
            # The CLI conversation id — the durable, resumable handle.
            # Empty with status="capture_failed" means the run is NOT
            # resumable through us; the human must find the session by hand.
            "session_id": run.session_id,
            "session_id_status": run.session_id_status,
            "alive": alive,
            "current_command": current_command or "",
            # Box-drawing stripped and capped to keep the result focused on
            # the alive/blocked signal rather than TUI borders.
            "initial_output": _tail(raw_output, 20),
            "blocked": blocked,
            "blocked_reason": blocked_reason or "",
            "pruned_runs": len(pruned),
            **watch_info,
            "hint": (
                watch_info["attach_command"]
                + (f"  |  tail -f {output_log}" if output_log else "")
            ),
        }
    )


def check_subagent(
    handle: Annotated[str, Field(description="run_id, pane id, or sess:win target")],
    lines: Annotated[int, Field(description="How many lines to capture from the pane top")] = 100,
) -> str:
    """Capture the last output from a subagent and report state, liveness, and whether it is blocked on an interactive prompt.

    Use this instead of `tmux capture-pane` so you get the transcript-derived
    state (working|idle, not just TUI chrome) and so the run is reconciled
    if the pane is gone (a closed pane returns a dead snapshot, not an error).

    The handle is the run_id from launch_subagent or the full session:window
    (e.g. atlas:fixit) — never a bare window name, which can resolve to the
    wrong session.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    run = _resolve_handle(handle, cfg)

    if not run.pane_id:
        raise ToolError(f"Run {run.run_id!r} has no pane id (dry run?)")

    # Transcript state is derived from the CLI's own persisted transcript, so
    # it is available even when the pane is no longer alive.
    state, last_activity = _state_for(run, cfg)

    try:
        output = tm.capture_pane(run.pane_id, lines=lines)
        alive = _is_alive(run, _live_panes(tm))
        current_command = tm.pane_command(run.pane_id)
    except TmuxError:
        # The pane has closed (e.g., Ctrl-C killed the wrapper). Report it as
        # no longer alive instead of raising a useless error.
        return _json(
            {
                "run_id": run.run_id,
                "pane_id": run.pane_id,
                "target": f"{run.session}:{run.window}",
                "session": run.session,
                "window": run.window,
                "session_id": run.session_id,
                "session_id_status": run.session_id_status,
                "alive": False,
                "current_command": "",
                "output": "",
                "state": state,
                "last_activity": last_activity,
                "blocked": False,
                "blocked_reason": "",
            }
        )

    # A trust dialog at startup is not the only way a run stalls: a permission
    # modal can appear at any point. Re-run the detector on every check so a
    # waiting subagent is visible without a human reading the pane.
    blocked, blocked_reason = _detect_blocked_output(output, _run_prompt_text(run))

    return _json(
        {
            "run_id": run.run_id,
            "pane_id": run.pane_id,
            "target": f"{run.session}:{run.window}",
            "session": run.session,
            "window": run.window,
            "session_id": run.session_id,
            "session_id_status": run.session_id_status,
            "alive": alive,
            "current_command": current_command or "",
            "output": output,
            "state": state,
            "last_activity": last_activity,
            "blocked": blocked,
            "blocked_reason": blocked_reason or "",
        }
    )


def watch_subagent(
    handle: Annotated[str, Field(description="run_id, pane id, or sess:win target")],
    mode: Annotated[str, Field(description="'switch' pulls an attached tmux client to this window; 'terminal' opens a terminal emulator on it; 'off' just returns the attach commands")] = "switch",
) -> str:
    """Put a running subagent on screen, or report how to attach to it.

    Use this instead of `tmux attach`/`tmux switch-client` so the run is
    resolved by handle (not by guessing the window name) and the attach
    command is returned even when no client is attached.

    The handle is the run_id or the full session:window (e.g. atlas:fixit) —
    never a bare window name, which can resolve to the wrong session.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    run = _resolve_handle(handle, cfg)

    if not run.pane_id:
        raise ToolError(f"Run {run.run_id!r} has no pane id (dry run?)")

    watch_mode = _normalize_watch(mode, cfg)
    info = _apply_watch(tm, cfg, run, watch_mode)
    log_path = run.run_dir / "output.log"

    return _json(
        {
            "run_id": run.run_id,
            "pane_id": run.pane_id,
            "target": f"{run.session}:{run.window}",
            "alive": _is_alive(run, _live_panes(tm)),
            "output_log": str(log_path) if log_path.exists() else "",
            **info,
        }
    )


def list_subagents(
    session: Annotated[str | None, Field(description="Filter to one tmux session")] = None,
) -> str:
    """List all launched subagents with their run_id, state (working|idle), liveness, and age.

    Use this instead of `tmux list-windows` so you get the recorded run
    metadata and the transcript-derived state, not just live window names.
    Returns run_id handles you can pass to the other subagent_* tools. The
    state is derived from the CLI's own transcript and is reported even for
    runs whose panes are no longer alive.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    reg = _load_registry(cfg)

    # One tmux round-trip for the whole listing; _is_alive handles both the
    # pid-verified and the legacy pane-id-only cases from this single snapshot.
    live = _live_panes(tm)
    items: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for run in sorted(reg.values(), key=lambda r: r.start_time or ""):
        if session and run.session != session:
            continue
        alive = _is_alive(run, live)
        age = ""
        if run.start_time:
            try:
                start = datetime.fromisoformat(run.start_time)
                age = str(int((now - start).total_seconds())) + "s"
            except Exception:
                age = ""
        # Transcript state is derived from the CLI's persisted transcript and
        # is independent of pane liveness — this is the signal that has to
        # survive containerisation (no TTY required).
        state, last_activity = _state_for(run, cfg)
        items.append(
            {
                "run_id": run.run_id,
                "pane_id": run.pane_id,
                "target": f"{run.session}:{run.window}",
                "session": run.session,
                "window": run.window,
                "cli": run.cli,
                "cwd": str(run.cwd),
                "task": run.task,
                "session_id": run.session_id,
                "session_id_status": run.session_id_status,
                "alive": alive,
                "age": age,
                "state": state,
                "last_activity": last_activity,
            }
        )

    return _json({"count": len(items), "subagents": items})


def send_to_subagent(
    handle: Annotated[str, Field(description="run_id, pane id, or sess:win target")],
    text: Annotated[str, Field(description="Text to type into the subagent")],
    submit: Annotated[bool, Field(description="Press Enter after pasting")] = True,
) -> str:
    """Send a follow-up message to a running subagent's pane (paste + optional Enter).

    Use this instead of `tmux send-keys` so the run is resolved by handle and
    the paste uses a named buffer (no shell expansion of the text). The
    handle is the run_id or the full session:window (e.g. atlas:fixit) —
    never a bare window name, which can resolve to the wrong session.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    run = _resolve_handle(handle, cfg)

    if not run.pane_id:
        raise ToolError(f"Run {run.run_id!r} has no pane id")

    try:
        tm.load_buffer(text, buffer_name=run.run_id)
        tm.paste_buffer(run.pane_id, buffer_name=run.run_id)
        time.sleep(cfg.follow_up_settle_seconds)
        if submit:
            tm.send_keys(run.pane_id, "Enter")
    except TmuxError as exc:
        raise ToolError(f"tmux send failed: {exc}") from exc

    # Echo back a fresh snapshot.
    return check_subagent(handle=run.pane_id, lines=40)


def stop_subagent(
    handle: Annotated[str, Field(description="run_id, pane id, or sess:win target")],
    kill_window: Annotated[bool, Field(description="Kill the tmux window instead of sending Ctrl-C")] = False,
) -> str:
    """Stop a subagent by sending Ctrl-C (default) or killing its tmux window.

    A pane that is already gone is SUCCESS, not an error: the run directory is
    reconciled (removed from the registry) and a dead snapshot is returned.
    This fixes the measured 33% stop_subagent error rate, whose cause was
    stale run_ids pointing at panes already gone — not lingering panes.

    Use this instead of `tmux send-keys C-c`/`tmux kill-window` so the run is
    resolved by handle and dead panes are cleaned up rather than raising.

    The handle is the run_id or the full session:window (e.g. atlas:fixit) —
    never a bare window name, which can resolve to the wrong session.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    run = _resolve_handle(handle, cfg)

    def _dead_snapshot(*, reconciled: bool) -> str:
        return _json(
            {
                "run_id": run.run_id,
                "pane_id": run.pane_id,
                "target": f"{run.session}:{run.window}",
                "session": run.session,
                "window": run.window,
                "alive": False,
                "reconciled": reconciled,
                "current_command": "",
                "output": "",
                "state": "",
                "blocked": False,
                "blocked_reason": "",
            }
        )

    # No pane id means the run was a dry run or never attached — reconcile the
    # registry entry rather than trying to stop nothing.
    if not run.pane_id:
        delete_run_dirs(cfg.runs_root, [run.run_id])
        return _dead_snapshot(reconciled=True)

    live = _live_panes(tm)
    if not _is_alive(run, live):
        # The pane is already gone. The user asked us to stop it; success is
        # the right answer, and we drop the stale registry entry instead of
        # raising ToolError (the old 33%-failure mode).
        delete_run_dirs(cfg.runs_root, [run.run_id])
        return _dead_snapshot(reconciled=True)

    try:
        if kill_window:
            # Kill by pane id so a reused window name in another session is never hit.
            tm.kill_window(run.pane_id)
        else:
            tm.send_keys(run.pane_id, "C-c")
    except TmuxError:
        # Race: the pane died between the alive check and the signal. Re-check
        # and reconcile if it is now gone; otherwise re-raise the real error.
        if not _is_alive(run, _live_panes(tm)):
            delete_run_dirs(cfg.runs_root, [run.run_id])
            return _dead_snapshot(reconciled=True)
        raise

    time.sleep(0.5)
    try:
        return check_subagent(handle=run.pane_id, lines=20)
    except ToolError:
        return _dead_snapshot(reconciled=False)


def sweep_stale_subagents(
    session: Annotated[str | None, Field(description="Only clear stale runs in this tmux session (None = all sessions)")] = None,
    dry_run: Annotated[bool, Field(description="Report what would be cleared without deleting")] = False,
) -> str:
    """Delete run records for subagents whose tmux panes are no longer alive.

    list_subagents marks dead panes but leaves their run directories on disk;
    this tool reaps them. Live subagents are never touched. Use this instead
    of manually pruning runs/ so only real runs with meta.json are touched.
    Pass dry_run=True to preview, or use list_subagents first to see what is
    stale.
    """
    cfg = _cfg()
    tm = _tmux(cfg)
    reg = _load_registry(cfg)
    live = _live_panes(tm)

    stale: list[dict[str, Any]] = []
    stale_ids: list[str] = []
    alive_count = 0
    for run in sorted(reg.values(), key=lambda r: r.start_time or ""):
        if session and run.session != session:
            continue
        if _is_alive(run, live):
            alive_count += 1
            continue
        stale_ids.append(run.run_id)
        stale.append(
            {
                "run_id": run.run_id,
                "pane_id": run.pane_id,
                "target": f"{run.session}:{run.window}",
                "session": run.session,
                "window": run.window,
                "cli": run.cli,
                "task": run.task,
            }
        )

    if dry_run:
        return _json(
            {
                "dry_run": True,
                "would_clear": len(stale),
                "stale": stale,
                "alive": alive_count,
            }
        )

    removed = delete_run_dirs(cfg.runs_root, stale_ids)
    removed_set = set(removed)
    not_found = [rid for rid in stale_ids if rid not in removed_set]

    return _json(
        {
            "dry_run": False,
            "cleared": len(removed),
            "cleared_runs": removed,
            "not_found": not_found,
            "alive": alive_count,
            "remaining_total": len(reg) - len(removed),
        }
    )


def register_tools(cfg: ServerConfig | None = None) -> None:
    """Register tools on the FastMCP instance."""
    if cfg is None:
        cfg = get_config()

    mcp.instructions = (
        _DEFAULT_INSTRUCTIONS
        + " cwd is restricted to these parent directories by default: "
        + ", ".join(str(p) for p in cfg.allowed_roots)
        + "."
    )

    for name in (
        "launch_subagent",
        "check_subagent",
        "watch_subagent",
        "list_subagents",
        "send_to_subagent",
        "stop_subagent",
        "sweep_stale_subagents",
    ):
        try:
            mcp.local_provider.remove_tool(name)
        except Exception:
            pass

    mcp.tool(launch_subagent, annotations=_tip("launch_subagent"))
    mcp.tool(check_subagent, annotations=_tip("check_subagent"))
    mcp.tool(watch_subagent, annotations=_tip("watch_subagent"))
    mcp.tool(list_subagents, annotations=_tip("list_subagents"))
    mcp.tool(send_to_subagent, annotations=_tip("send_to_subagent"))
    mcp.tool(stop_subagent, annotations=_tip("stop_subagent"))
    mcp.tool(sweep_stale_subagents, annotations=_tip("sweep_stale_subagents"))


register_tools()
