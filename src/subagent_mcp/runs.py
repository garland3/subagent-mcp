from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunsError(Exception):
    """Invalid run or run directory state."""


@dataclass
class Run:
    """In-memory handle for a single subagent launch."""

    run_id: str
    run_dir: Path
    cwd: Path
    cli: str
    session: str
    window: str
    pane_id: str = ""
    pane_pid: int | None = None
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


def discover_runs(runs_root: Path) -> dict[str, Run]:
    """Load all valid meta.json files under runs_root."""
    registry: dict[str, Run] = {}
    if not runs_root.exists():
        return registry
    for meta_path in runs_root.rglob("meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            run = Run.from_meta(data)
        except Exception:
            continue
        run.run_dir = meta_path.parent
        registry[run.run_id] = run
    return registry


def registry(runs_root: Path, refresh: bool = False) -> dict[str, Run]:
    global _REGISTRY
    if refresh or not _REGISTRY:
        _REGISTRY = discover_runs(runs_root)
    return _REGISTRY


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

    doomed: list[Run] = []
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        doomed.extend(run for run in candidates if _run_sort_key(run) < cutoff)
    if keep_max > 0 and len(everything) > keep_max:
        # Oldest first, so the surplus is the head of the list.
        surplus = {run.run_id for run in everything[: len(everything) - keep_max]}
        doomed.extend(run for run in candidates if run.run_id in surplus)

    removed: list[str] = []
    seen: set[str] = set()
    for run in doomed:
        if run.run_id in seen:
            continue
        seen.add(run.run_id)
        run_dir = run.run_dir.resolve()
        # Only ever delete a real run directory sitting under runs_root.
        if run_dir.parent != runs_root or not (run_dir / "meta.json").is_file():
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError:
            continue
        removed.append(run.run_id)
        _REGISTRY.pop(run.run_id, None)
    return removed


def as_dict(run: Run) -> dict[str, Any]:
    return run.to_meta()
