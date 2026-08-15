"""Making a detached subagent visible.

Runs live in detached tmux windows, which is what makes them cheap to fan out
but also means nothing appears on screen. These helpers turn a run into
something a human can actually watch: reuse an already-attached tmux client,
or spawn a terminal emulator attached to the pane.
"""

from __future__ import annotations

import os
import shlex
import subprocess

WATCH_MODES = ("off", "switch", "terminal")


class WatchError(Exception):
    """Could not put the run on screen."""


def _tmux_prefix(tmux_bin: str, socket: str | None) -> list[str]:
    return [tmux_bin, "-L", socket] if socket else [tmux_bin]


def attach_command(session: str, window: str, socket: str | None) -> str:
    """Copy-pasteable command that attaches a human to the run's window."""
    sock = f" -L {shlex.quote(socket)}" if socket else ""
    return f"tmux{sock} attach -t {shlex.quote(f'{session}:{window}')}"


def read_only_command(session: str, window: str, socket: str | None) -> str:
    """Attach without a keyboard, so watching cannot disturb the subagent."""
    return attach_command(session, window, socket).replace(" attach ", " attach -r ")


def _watch_shell_command(tmux_bin: str, socket: str | None, session: str, pane_id: str) -> str:
    """Select the run's window, then attach — as one shell string."""
    prefix = " ".join(shlex.quote(p) for p in _tmux_prefix(tmux_bin, socket))
    return (
        f"{prefix} select-window -t {shlex.quote(pane_id)} 2>/dev/null; "
        f"exec {prefix} attach-session -t {shlex.quote('=' + session)}"
    )


# How each emulator wants the command handed to it. The argv we append is
# always ["bash", "-c", "<shell string>"], so anything that takes a trailing
# argv works uniformly.
_TERMINALS: tuple[tuple[str, list[str]], ...] = (
    ("kitty", ["--"]),
    ("wezterm", ["start", "--"]),
    ("ghostty", ["-e"]),
    ("alacritty", ["-e"]),
    ("foot", []),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("x-terminal-emulator", ["-e"]),
    ("xterm", ["-e"]),
)


def _detect_terminal() -> tuple[str, list[str]] | None:
    import shutil

    from .cli_paths import augmented_path

    path = augmented_path()
    preferred = os.environ.get("SUBAGENT_TERMINAL_BIN") or os.environ.get("TERMINAL")
    candidates: list[tuple[str, list[str]]] = list(_TERMINALS)
    if preferred:
        # An explicitly named emulator wins; assume the common "-e argv" form
        # unless it is one we already know.
        known = dict(_TERMINALS).get(os.path.basename(preferred))
        candidates.insert(0, (preferred, known if known is not None else ["-e"]))
    for name, args in candidates:
        found = shutil.which(name, path=path)
        if found:
            return found, args
    return None


def open_in_terminal(
    *,
    tmux_bin: str,
    socket: str | None,
    session: str,
    pane_id: str,
    terminal_command: str | None = None,
) -> str:
    """Spawn a terminal emulator attached to ``pane_id``. Returns what it ran."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise WatchError(
            "no DISPLAY/WAYLAND_DISPLAY — cannot open a terminal window from here; "
            "use watch='switch' or attach manually"
        )

    shell_cmd = _watch_shell_command(tmux_bin, socket, session, pane_id)

    if terminal_command:
        # Template form, e.g. SUBAGENT_TERMINAL='kitty -- bash -c {cmd}'
        if "{cmd}" in terminal_command:
            rendered = terminal_command.replace("{cmd}", shlex.quote(shell_cmd))
        else:
            rendered = f"{terminal_command} bash -c {shlex.quote(shell_cmd)}"
        argv = shlex.split(rendered)
    else:
        detected = _detect_terminal()
        if detected is None:
            raise WatchError(
                "no terminal emulator found (tried: "
                + ", ".join(name for name, _ in _TERMINALS)
                + "); set SUBAGENT_TERMINAL to a command template containing {cmd}"
            )
        binary, args = detected
        argv = [binary, *args, "bash", "-c", shell_cmd]

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise WatchError(f"could not launch terminal: {exc}") from exc
    return " ".join(shlex.quote(a) for a in argv)
