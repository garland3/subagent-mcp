from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class RunsError(Exception):
    """Invalid run or run directory state."""


@dataclass
class Run:
    """In-memory handle for a single subagent launch.

    Field naming note for downstream consumers (e.g. a future resume_subagent):

    ``session`` is the **tmux** session name — a short string like ``atlas``.
    ``session_id`` is the **CLI conversation id** — a UUID that makes the run
    resumable via ``claude --resume <id>`` / ``opencode run -s <id>``. They are
    different things and must not be conflated.
    """

    run_id: str
    run_dir: Path
    cwd: Path
    cli: str
    # tmux session name (NOT the CLI conversation id — see session_id).
    session: str
    window: str
    pane_id: str = ""
    pane_pid: int | None = None
    # CLI session id — the durable, resumable conversation handle.
    # For claude this is minted by us and passed as --session-id at launch.
    # For opencode this is captured post-launch from ``opencode session list``
    # or the SQLite DB at ~/.local/share/opencode/opencode.db (opencode has no
    # pre-assign flag). Empty if the id was never obtained.
    session_id: str = ""
    # Provenance of session_id:
    #   "minted"         — we generated the uuid and passed --session-id (claude)
    #   "captured"       — we found the id post-launch (opencode)
    #   "capture_failed" — opencode capture failed; the run is NOT resumable
    #                      through us and the failure is visible in this field
    #   "unsupported"    — the CLI has no conversation id at all (atlas-chat is
    #                      one-shot). Distinct from capture_failed: nothing was
    #                      lost, there was never anything to capture.
    #   ""               — not attempted (e.g. dry run, or old record)
    session_id_status: str = ""
    model: str | None = None
    agent: str | None = None
    task: str | None = None
    argv: list[str] | None = None
    start_time: str = ""

    def to_meta(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "cwd": str(self.cwd),
            "cli": self.cli,
            "session": self.session,
            "window": self.window,
            "pane_id": self.pane_id,
            "pane_pid": self.pane_pid,
            "session_id": self.session_id,
            "session_id_status": self.session_id_status,
            "model": self.model,
            "agent": self.agent,
            "task": self.task,
            "argv": self.argv or [],
            "start_time": self.start_time,
        }

    @classmethod
    def from_meta(cls, data: dict[str, Any]) -> "Run":
        return cls(
            run_id=data["run_id"],
            run_dir=Path(data["run_dir"]),
            cwd=Path(data["cwd"]),
            cli=data["cli"],
            session=data["session"],
            window=data["window"],
            pane_id=data.get("pane_id", ""),
            pane_pid=data.get("pane_pid"),
            session_id=data.get("session_id", ""),
            session_id_status=data.get("session_id_status", ""),
            model=data.get("model"),
            agent=data.get("agent"),
            task=data.get("task"),
            argv=data.get("argv", []),
            start_time=data.get("start_time", ""),
        )

    def write_meta(self) -> None:
        meta_path = self.run_dir / "meta.json"
        meta_path.write_text(json.dumps(self.to_meta(), indent=2), encoding="utf-8")


_REGISTRY: dict[str, Run] = {}


def _sanitize(s: str) -> str:
    """Make a string safe for tmux window/session names."""
    s = re.sub(r"[^a-zA-Z0-9_\- ]+", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s.lower()[:40] or "subagent"


def _dedup_window_name(session: str, base: str, list_windows) -> str:
    """Append -2, -3 … if base window already exists in session."""
    existing = {name for _, name in list_windows(session)}
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _window_name(task: str | None, prompt: str) -> str:
    if task:
        return _sanitize(task)
    first_line = prompt.splitlines()[0] if prompt else ""
    slug = _sanitize(first_line)
    return slug or "subagent"


def is_sweepable(run_dir: Path, runs_root: Path) -> bool:
    """True when ``run_dir`` is a run directory ``delete_run_dirs`` will remove.

    ``discover_runs`` finds ``meta.json`` at any depth, but ``delete_run_dirs``
    only ever removes a directory sitting *directly* under ``runs_root`` (that
    guard is deliberate — it is what stops a stray meta.json from turning the
    sweep into an arbitrary rmtree). A record found deeper is therefore
    discoverable but not reapable, which is a state the tools have to be able
    to name rather than silently mishandle.
    """
    try:
        return run_dir.resolve().parent == runs_root.resolve()
    except OSError:
        return False


def discover_runs(runs_root: Path) -> dict[str, Run]:
    """Load all valid meta.json files under runs_root.

    On a ``run_id`` collision — the same id present at two depths — the
    sweepable top-level copy wins. Without that rule the deeper copy shadowed
    the real record, so a sweep could delete the top-level directory and the
    very next listing would resurrect the run from the nested one, reporting a
    different ``pane_id`` for the same id.
    """
    registry: dict[str, Run] = {}
    for meta_path in sorted(runs_root.rglob("meta.json")) if runs_root.exists() else []:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            run = Run.from_meta(data)
        except Exception:
            continue
        run.run_dir = meta_path.parent
        existing = registry.get(run.run_id)
        if existing is not None and is_sweepable(existing.run_dir, runs_root):
            # Already holding the canonical top-level copy; keep it.
            continue
        registry[run.run_id] = run
    return registry


def registry(runs_root: Path, refresh: bool = False) -> dict[str, Run]:
    """Return the run registry.

    The result is a **copy**. Callers hold it across mutating operations
    (``delete_run_dirs`` pops from the cache), and handing out the live dict
    made those callers' own arithmetic wrong — ``sweep_stale_subagents``
    subtracted its deletions twice and could report a negative remainder.
    """
    global _REGISTRY
    if refresh or not _REGISTRY:
        _REGISTRY = discover_runs(runs_root)
    return dict(_REGISTRY)


def make_run_id(slug: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{_sanitize(slug)}"


def create_run_dir(
    runs_root: Path,
    *,
    session: str,
    window: str,
    cwd: Path,
    cli: str,
    model: str | None,
    agent: str | None,
    task: str | None,
    argv: list[str],
    session_id: str = "",
    session_id_status: str = "",
) -> Run:
    runs_root.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(f"{session}-{window}" if task is None else task)
    run_dir = runs_root / run_id
    # Extremely unlikely collision from same-second launches; append a counter.
    counter = 1
    while run_dir.exists():
        run_dir = runs_root / f"{run_id}-{counter}"
        counter += 1
    run_id = run_dir.name
    run_dir.mkdir(parents=True)
    run = Run(
        run_id=run_id,
        run_dir=run_dir,
        cwd=cwd,
        cli=cli,
        session=session,
        window=window,
        session_id=session_id,
        session_id_status=session_id_status,
        model=model,
        agent=agent,
        task=task,
        argv=argv,
        start_time=datetime.now(timezone.utc).isoformat(),
    )
    run.write_meta()
    _REGISTRY[run_id] = run
    return run


def choose_window_name(
    session: str,
    base: str,
    list_windows,
) -> str:
    return _dedup_window_name(session, base, list_windows)


def window_base(task: str | None, prompt: str) -> str:
    return _window_name(task, prompt)


def update_run_pane(run: Run, pane_id: str, pane_pid: int | None = None) -> None:
    run.pane_id = pane_id
    run.pane_pid = pane_pid
    run.write_meta()


def update_run_session(
    run: Run, session_id: str, session_id_status: str
) -> None:
    """Record the CLI conversation id on the run, persisting to meta.json.

    Used after a post-launch capture (opencode) or to update the status if a
    capture fails. The status is what makes a failed capture visible rather
    than silent — see ``Run.session_id_status``.
    """
    run.session_id = session_id
    run.session_id_status = session_id_status
    run.write_meta()


def _run_sort_key(run: Run) -> float:
    """Age ordering key: start_time when parseable, else directory mtime."""
    if run.start_time:
        try:
            return datetime.fromisoformat(run.start_time).timestamp()
        except ValueError:
            pass
    try:
        return run.run_dir.stat().st_mtime
    except OSError:
        return 0.0


def delete_run_dirs(runs_root: Path, run_ids: Iterable[str]) -> list[str]:
    """Safely delete run directories for the given ids, preserving order.

    Only real run directories sitting directly under runs_root that contain a
    meta.json are touched. Deduplicates. Returns the removed ids in the order
    given. Used by both ``prune_runs`` (retention GC) and the
    ``sweep_stale_subagents`` tool (liveness GC).
    """
    runs_root = runs_root.resolve()
    if not runs_root.is_dir():
        return []
    reg = discover_runs(runs_root)
    removed: list[str] = []
    seen: set[str] = set()
    for run_id in run_ids:
        if run_id in seen:
            continue
        seen.add(run_id)
        run = reg.get(run_id)
        if run is None:
            continue
        run_dir = run.run_dir.resolve()
        # Only ever delete a real run directory sitting under runs_root.
        if run_dir.parent != runs_root or not (run_dir / "meta.json").is_file():
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError:
            continue
        removed.append(run_id)
        _REGISTRY.pop(run_id, None)
    return removed


def prune_runs(
    runs_root: Path,
    *,
    keep_max: int = 200,
    max_age_days: float = 14.0,
    protect: set[str] | None = None,
) -> list[str]:
    """Delete finished run directories that exceed the retention policy.

    ``protect`` holds run ids that must never be removed (live subagents).
    Either limit is disabled by passing 0 or less. Returns the removed ids.
    """
    protect = protect or set()
    runs_root = runs_root.resolve()
    if not runs_root.is_dir():
        return []

    # Both limits are measured against every run on disk — a live subagent
    # still occupies a slot — but only unprotected runs are ever deleted.
    everything = sorted(discover_runs(runs_root).values(), key=_run_sort_key)
    candidates = [run for run in everything if run.run_id not in protect]

    doomed_ids: list[str] = []
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        doomed_ids.extend(run.run_id for run in candidates if _run_sort_key(run) < cutoff)
    if keep_max > 0 and len(everything) > keep_max:
        # Oldest first, so the surplus is the head of the list.
        surplus = {run.run_id for run in everything[: len(everything) - keep_max]}
        doomed_ids.extend(run.run_id for run in candidates if run.run_id in surplus)

    return delete_run_dirs(runs_root, doomed_ids)


def as_dict(run: Run) -> dict[str, Any]:
    return run.to_meta()


# ---------------------------------------------------------------------------
# Transcript-based state derivation (Phase 0.2).
#
# The CLI's own persisted transcript is the idle/working signal — NOT the tmux
# TUI. A file works identically whether the pane is alive, closed, or running
# in a sandboxed pod, which is why this mechanism is the one that has to
# survive containerisation.
#
# claude writes ~/.claude/projects/<cwd-with-slashes-as-dashes>/<uuid>.jsonl
#   e.g. cwd /home/garlan/ATLAS-GROUP -> dir -home-garlan-ATLAS-GROUP
# opencode stores sessions in ~/.local/share/opencode/opencode.db (SQLite)
#   plus snapshot/ and tool-output/ subdirectories.
# ---------------------------------------------------------------------------

# Quiescence window: if the transcript mtime is older than this, the run is
# treated as idle even if we cannot parse the last record. Tuned to the
# streaming cadence of both CLIs (a working agent touches the file every few
# seconds); a paused-for-thought turn may briefly look idle, which is safe —
# idle is the conservative, low-noise verdict.
_QUIESCE_SECONDS = 15.0


def _claude_transcript_path(run: Run, root: Path) -> Path:
    """Path to claude's per-session JSONL transcript for this run.

    claude encodes the cwd by replacing ``/`` with ``-`` (so
    ``/home/garlan/ATLAS-GROUP`` -> ``-home-garlan-ATLAS-GROUP``) and stores
    one ``<session-uuid>.jsonl`` file per session under that directory.
    """
    cwd_slug = str(run.cwd).replace("/", "-")
    return root / cwd_slug / f"{run.session_id}.jsonl"


def _read_last_jsonl_line(path: Path) -> str:
    """Read the last non-empty line of a (possibly large) JSONL file.

    Reads only the trailing 64 KiB so a multi-megabyte transcript does not
    have to be loaded in full on every check.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size == 0:
                return ""
            read = min(65536, size)
            fh.seek(size - read)
            chunk = fh.read(read)
    except OSError:
        return ""
    for line in reversed(chunk.split(b"\n")):
        if line.strip():
            return line.decode("utf-8", errors="replace")
    return ""


def _claude_state(run: Run, root: Path) -> tuple[str, str]:
    """Derive (state, last_activity_iso) from claude's JSONL transcript.

    Returns ``("", "")`` when there is no transcript to read — e.g. the
    session_id was never captured, or the transcript file has not appeared
    yet. ``state`` is ``"working"`` or ``"idle"``.
    """
    if not run.session_id:
        return "", ""
    transcript = _claude_transcript_path(run, root)
    if not transcript.is_file():
        return "", ""
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return "", ""
    last_activity = datetime.fromtimestamp(mtime, timezone.utc).isoformat()

    last_line = _read_last_jsonl_line(transcript)
    if not last_line:
        # File exists but is empty — the agent has just started and not
        # written a record yet. Call it working rather than idle.
        return "working", last_activity

    try:
        record = json.loads(last_line)
    except json.JSONDecodeError:
        # Unparseable tail: assume working so a transient write does not
        # produce a false idle verdict (a wrong idle is worse than none).
        return "working", last_activity

    # Heuristic: an assistant record whose content has no tool_use block is a
    # completed turn -> idle. Anything else (user follow-up, tool_result,
    # assistant mid-turn with tool_use) is working. The JSONL schema is not
    # a public contract, so this is intentionally tolerant of shape variation.
    if isinstance(record, dict) and record.get("type") == "assistant":
        message = record.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", [])
            if isinstance(content, str):
                # Plain-text assistant turn -> idle.
                return "idle", last_activity
            if isinstance(content, list):
                has_tool_use = any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in content
                )
                if not has_tool_use:
                    return "idle", last_activity
    # Fall back to mtime: a stale transcript is idle even mid-record.
    if (time.time() - mtime) > _QUIESCE_SECONDS:
        return "idle", last_activity
    return "working", last_activity


def _opencode_state(run: Run, root: Path) -> tuple[str, str]:
    """Best-effort state from opencode's SQLite store.

    The opencode DB schema is not a published contract, so this probes a few
    common table/column names and returns ``("", "")`` on any mismatch rather
    than crashing. Phase 1's ``Stop``-hook confirmation is the fallback that
    does not depend on parsing this; the hook is what makes the idle verdict
    robust for opencode. This function is the file-based rung of the same
    ladder and is structured so the schema can be filled in once it is known.
    """
    if not run.session_id:
        return "", ""
    db_path = root / "opencode.db"
    if not db_path.is_file():
        return "", ""
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=2)
    except Exception:
        return "", ""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        msg_table = next(
            (t for t in ("messages", "message", "chat_messages", "events") if t in tables),
            None,
        )
        if msg_table is None:
            return "", ""
        cursor.execute(f"PRAGMA table_info({msg_table})")
        cols = {row[1] for row in cursor.fetchall()}
        sid_col = next(
            (c for c in ("session_id", "session", "sessionId") if c in cols), None
        )
        if sid_col is None:
            return "", ""
        ts_col = next(
            (c for c in ("created_at", "created", "timestamp", "time", "updated_at") if c in cols),
            None,
        )
        role_col = next(
            (c for c in ("role", "type", "kind") if c in cols), None
        )
        select_expr = role_col if role_col is not None else "'*'"
        if ts_col:
            query = (
                f"SELECT {select_expr}, {ts_col} FROM {msg_table} "
                f"WHERE {sid_col} = ? ORDER BY {ts_col} DESC LIMIT 1"
            )
        else:
            query = (
                f"SELECT {select_expr} FROM {msg_table} "
                f"WHERE {sid_col} = ? ORDER BY rowid DESC LIMIT 1"
            )
        cursor.execute(query, (run.session_id,))
        row = cursor.fetchone()
        if row is None:
            return "", ""
        last_ts = str(row[-1]) if ts_col and row else ""
        role = str(row[0]) if role_col and row else ""
        # Heuristic mirror of the claude path: an assistant's last record is
        # idle unless it carries a tool call (which we cannot cheaply detect
        # without knowing the content column). Be conservative: assistant =>
        # idle only if the row is old; otherwise working.
        if role.lower() == "assistant" and ts_col:
            return "idle", last_ts
        return "working", last_ts
    except Exception:
        return "", ""
    finally:
        try:
            conn.close()
        except Exception:
            pass


def derive_state(
    run: Run,
    *,
    claude_projects_root: Path | None = None,
    opencode_state_root: Path | None = None,
) -> tuple[str, str]:
    """Derive ``(state, last_activity)`` from the CLI's own transcript file.

    ``state`` is ``"working"``, ``"idle"``, or ``""`` (unknown — no transcript
    available yet). ``last_activity`` is an ISO timestamp string or ``""``.

    This reads the CLI's persisted transcript, not the tmux TUI, so it works
    the same whether the pane is alive, closed, or running in a sandboxed
    pod without a TTY. The pane-alive check (in server.py) is a separate
    signal that this complements, not replaces.
    """
    if run.cli == "claude":
        root = claude_projects_root or (Path.home() / ".claude" / "projects")
        return _claude_state(run, root)
    if run.cli == "opencode":
        root = opencode_state_root or (Path.home() / ".local" / "share" / "opencode")
        return _opencode_state(run, root)
    # atlas-chat (and any future one-shot CLI) keeps no transcript to read:
    # it answers once and exits. "" means "unknown", and the caller falls back
    # to pane liveness plus the wrapper's STATUS.json exit code — which is the
    # whole truth for a process that either finished or did not.
    return "", ""
