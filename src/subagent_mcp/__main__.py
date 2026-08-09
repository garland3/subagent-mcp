from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ServerConfig, set_config
from .server import mcp, register_tools


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _split_roots(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


def main(argv: list[str] | None = None) -> None:
    root_default = Path.cwd().resolve()
    allowed_default = _split_roots(os.environ.get("SUBAGENT_ALLOWED_ROOTS")) or (
        str(p) for p in [Path.home() / "git", Path.home() / "ATLAS-GROUP"] if p.exists()
    )

    parser = argparse.ArgumentParser(
        prog="subagent-mcp",
        description="MCP server that launches coding-CLI subagents in detached tmux windows.",
    )
    parser.add_argument("--stdio", action="store_true", help="Use stdio transport (default)")
    parser.add_argument(
        "--http", dest="http", action="store_true", help="Use HTTP transport instead of stdio"
    )
    parser.add_argument("--host", default=os.environ.get("SUBAGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int("SUBAGENT_PORT", 8100))
    parser.add_argument("--runs-root", type=Path, default=_env_path("SUBAGENT_RUNS_ROOT", root_default / "runs"))
    parser.add_argument("--root", type=Path, default=_env_path("SUBAGENT_ROOT", root_default))
    parser.add_argument(
        "--allowed-roots",
        type=str,
        default=",".join(allowed_default),
        help="Comma-separated parent dirs allowed as cwd (default: ~/git,~/ATLAS-GROUP)",
    )
    parser.add_argument("--any-cwd", action="store_true", help="Disable the cwd allowlist")
    parser.add_argument("--max-concurrent", type=int, default=_env_int("SUBAGENT_MAX_CONCURRENT", 12))
    parser.add_argument("--tmux-socket", default=os.environ.get("SUBAGENT_TMUX_SOCKET", ""))
    parser.add_argument(
        "--cli-allowlist",
        type=str,
        default=os.environ.get("SUBAGENT_CLI_ALLOWLIST", "claude,opencode"),
        help="Comma-separated CLIs the server is allowed to invoke",
    )
    parser.add_argument(
        "--follow-up-settle",
        type=float,
        default=float(os.environ.get("SUBAGENT_FOLLOW_UP_SETTLE", 1.5)),
        help="Seconds to pause after pasting follow-up text",
    )

    parser.add_argument(
        "--runs-keep-max",
        type=int,
        default=_env_int("SUBAGENT_RUNS_KEEP_MAX", 200),
        help="Keep at most this many finished run dirs (0 disables)",
    )
    parser.add_argument(
        "--runs-max-age-days",
        type=float,
        default=float(os.environ.get("SUBAGENT_RUNS_MAX_AGE_DAYS", 14.0)),
        help="Delete finished run dirs older than this (0 disables)",
    )

    args = parser.parse_args(argv)

    if args.http and args.stdio:
        parser.error("--http and --stdio are mutually exclusive")
    transport = "http" if args.http else "stdio"

    allowed = _split_roots(args.allowed_roots)
    if not allowed:
        allowed = (str(root_default),)

    cfg = ServerConfig(
        root=args.root,
        runs_root=args.runs_root,
        allowed_roots=tuple(Path(p).expanduser().resolve() for p in allowed),
        any_cwd=args.any_cwd,
        host=args.host,
        port=args.port,
        max_concurrent=args.max_concurrent,
        tmux_socket=args.tmux_socket if args.tmux_socket else None,
        cli_allowlist=tuple(a.strip() for a in args.cli_allowlist.split(",") if a.strip()),
        follow_up_settle_seconds=args.follow_up_settle,
        runs_keep_max=args.runs_keep_max,
        runs_max_age_days=args.runs_max_age_days,
    )

    set_config(cfg)
    register_tools(cfg)

    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(transport="http", host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
