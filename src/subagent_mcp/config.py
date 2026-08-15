from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerConfig:
    """Runtime configuration for the subagent-mcp server."""

    # Base cwd used for relative paths and runs storage.
    root: Path

    # Where per-run artefacts live.
    runs_root: Path

    # Parent directories that are valid launch roots unless any_cwd is set.
    allowed_roots: tuple[Path, ...]
    any_cwd: bool = False

    # HTTP transport listen options.
    host: str = "127.0.0.1"
    port: int = 8100

    # Concurrency guard.
    max_concurrent: int = 12

    # Optional tmux socket name (None or empty => default tmux socket).
    tmux_socket: str | None = None

    # Which CLIs the server is willing to invoke.
    cli_allowlist: tuple[str, ...] = ("claude", "opencode")

    # Output limits.
    max_output_chars: int = 200_000

    # How long to wait for a follow-up paste before pressing Enter.
    follow_up_settle_seconds: float = 1.5

    # Default visibility for a freshly launched subagent:
    #   "off"      — detached tmux window, nothing happens on screen
    #   "switch"   — pull an already-attached tmux client to the new window
    #   "terminal" — open a terminal emulator attached to the new window
    watch_default: str = "off"

    # Terminal emulator used by watch="terminal" (None => autodetect).
    terminal_command: str | None = None

    # Tee each pane's output into <run_dir>/output.log so it can be tail -f'd
    # and survives the pane's scrollback limit.
    pipe_logs: bool = True

    # Retention for the runs/ directory. Finished (dead) runs beyond either
    # limit are deleted after a successful launch. Set either to 0 to disable
    # that half of the policy.
    runs_keep_max: int = 200
    runs_max_age_days: float = 14.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "runs_root", self.runs_root.resolve())
        object.__setattr__(
            self, "allowed_roots", tuple(Path(p).expanduser().resolve() for p in self.allowed_roots)
        )


def _default_allowed_roots() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(
        (home / name).expanduser().resolve()
        for name in ("git", "ATLAS-GROUP")
        if (home / name).exists()
    )


_current_config: ServerConfig | None = None


def get_config() -> ServerConfig:
    """Return the current server configuration."""
    global _current_config
    if _current_config is None:
        root = Path.cwd().resolve()
        _current_config = ServerConfig(
            root=root,
            runs_root=root / "runs",
            allowed_roots=_default_allowed_roots(),
        )
    return _current_config


def set_config(cfg: ServerConfig | None = None) -> None:
    """Set (or clear) the active server configuration."""
    global _current_config
    _current_config = cfg


def cwd_is_allowed(cwd: Path, cfg: ServerConfig | None = None) -> bool:
    cfg = cfg or get_config()
    if cfg.any_cwd:
        return True
    resolved = cwd.expanduser().resolve()
    return any(
        resolved == root or resolved.is_relative_to(root) for root in cfg.allowed_roots
    )
