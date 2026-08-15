"""Locating coding-CLI binaries.

The subagent runs inside a tmux pane whose environment comes from whatever
process started the MCP server — often a desktop launcher or a client with a
stripped ``PATH``. Tools installed under ``~/.opencode/bin`` or ``~/.bun/bin``
are then invisible and the pane dies with ``command not found`` (exit 127).

So we resolve the CLI to an absolute path up front, searching ``PATH`` plus the
usual per-user install roots, and bake both the absolute path and the widened
``PATH`` into the generated wrapper.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Per-user install roots the standard PATH frequently misses.
_EXTRA_DIR_PATTERNS = (
    "~/.opencode/bin",
    "~/.claude/local",
    "~/.local/bin",
    "~/.bun/bin",
    "~/.deno/bin",
    "~/.npm-global/bin",
    "~/.local/share/npm/bin",
    "~/.cargo/bin",
    "~/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/snap/bin",
)


def _version_key(name: str) -> tuple[int, ...]:
    """Numeric sort key for a ``vMAJOR.MINOR.PATCH`` directory name."""
    parts = name.lstrip("v").split(".")
    key: list[int] = []
    for part in parts:
        digits = "".join(c for c in part if c.isdigit())
        key.append(int(digits) if digits else 0)
    return tuple(key)


def _nvm_bins() -> list[str]:
    """Node bin dirs under nvm, newest first — earlier PATH entries win."""
    root = Path.home() / ".nvm" / "versions" / "node"
    if not root.is_dir():
        return []
    try:
        versions = sorted(
            (p for p in root.iterdir() if (p / "bin").is_dir()),
            key=lambda p: _version_key(p.name),
            reverse=True,
        )
    except OSError:
        return []
    return [str(p / "bin") for p in versions]


def extra_dirs() -> list[str]:
    """Existing candidate directories, in search order."""
    out: list[str] = []
    for pattern in _EXTRA_DIR_PATTERNS:
        path = Path(pattern).expanduser()
        if path.is_dir():
            out.append(str(path))
    out.extend(_nvm_bins())
    return out


def augmented_path(base: str | None = None) -> str:
    """``PATH`` with the extra install roots appended (order preserved, deduped).

    Existing entries keep priority: this only makes previously invisible
    binaries findable, it never shadows one the caller already resolves.
    """
    base = os.environ.get("PATH", "") if base is None else base
    seen: set[str] = set()
    parts: list[str] = []
    for entry in [*base.split(os.pathsep), *extra_dirs()]:
        if not entry or entry in seen:
            continue
        seen.add(entry)
        parts.append(entry)
    return os.pathsep.join(parts)


def _env_override(cli: str) -> str | None:
    """An explicit ``SUBAGENT_CLI_<NAME>`` path, if it points at an executable."""
    value = os.environ.get(f"SUBAGENT_CLI_{cli.upper().replace('-', '_')}")
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def resolve_cli(cli: str) -> str | None:
    """Absolute path to ``cli``, or None if it cannot be found anywhere."""
    override = _env_override(cli)
    if override:
        return override
    return shutil.which(cli, path=augmented_path())


def searched_dirs() -> list[str]:
    """Every directory ``resolve_cli`` looks in — for error messages."""
    return [p for p in augmented_path().split(os.pathsep) if p]
