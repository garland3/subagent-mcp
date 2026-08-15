from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .cli_paths import augmented_path


class TmuxError(Exception):
    """A tmux command failed."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class Tmux:
    """Thin wrapper around a tmux binary and an optional named socket."""

    def __init__(self, socket: str | None = None) -> None:
        self.socket = socket
        self._path = shutil.which("tmux", path=augmented_path())
        if self._path is None:
            raise TmuxError("tmux binary not found on PATH")

    @property
    def path(self) -> str:
        """Absolute path to the tmux binary this wrapper drives."""
        return self._path

    def _session_target(self, name: str) -> str:
        """Return an exact-match target string for a session or session:window.

        tmux targets are prefix-matched by default. A leading ``=`` forces an
        exact match. We apply exact matching only to named sessions/windows;
        pane ids (``%N``) and window ids (``@N``) are unambiguous as-is.
        """
        if name.startswith(("%", "@")):
            return name
        if ":" in name:
            session, window = name.split(":", 1)
            return f"={session}:={window}"
        return f"={name}"

    def _args(self, args: list[str]) -> list[str]:
        cmd = [self._path]
        if self.socket:
            cmd.extend(["-L", self.socket])
        cmd.extend(args)
        return cmd

    def _run(
        self,
        args: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                self._args(args),
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(
                f"tmux command timed out: {' '.join(exc.cmd)}"
            ) from exc
        if check and proc.returncode != 0:
            err = proc.stderr.strip() or f"tmux exited {proc.returncode}"
            raise TmuxError(err, returncode=proc.returncode)
        return proc

    def has_session(self, session: str) -> bool:
        proc = subprocess.run(
            self._args(["has-session", "-t", self._session_target(session)]),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def ensure_session(
        self, session: str, cwd: Path
    ) -> tuple[bool, str | None]:
        """Create the session if it does not exist.

        Returns ``(created, initial_window_id)``. ``initial_window_id`` is
        provided only for a freshly created session so callers can close the
        stray shell window that tmux creates by default.
        """
        if self.has_session(session):
            return False, None
        self._run(
            ["new-session", "-d", "-s", session, "-c", str(cwd)]
        )
        # tmux new-session always starts with a default window (index 0). We
        # capture its id so that new_window() can clean it up after creating
        # the real subagent window.
        proc = self._run(
            [
                "list-windows",
                "-t",
                self._session_target(session),
                "-F",
                "#{window_id}",
            ]
        )
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return True, lines[0] if lines else None

    def new_window(
        self, session: str, window_name: str, cwd: Path, command: str
    ) -> str:
        created, initial_window_id = self.ensure_session(session, cwd)
        proc = self._run(
            [
                "new-window",
                "-t",
                self._session_target(session),
                "-n",
                window_name,
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}",
                command,
            ]
        )
        pane_id = proc.stdout.strip().splitlines()[-1].strip()
        if not pane_id.startswith("%"):
            raise TmuxError(f"tmux did not return a pane id: {pane_id!r}")
        if created and initial_window_id:
            # Kill the auto-created shell window so the session only contains
            # the subagent window.
            try:
                self._run(["kill-window", "-t", initial_window_id])
            except TmuxError:
                pass
        return pane_id

    def list_windows(self, session: str) -> list[tuple[str, str]]:
        """Return (window_index, window_name) for every window in session."""
        if not self.has_session(session):
            return []
        proc = self._run(
            [
                "list-windows",
                "-t",
                self._session_target(session),
                "-F",
                "#{window_index} #{window_name}",
            ]
        )
        out: list[tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                out.append(parts)
        return out

    def list_panes(
        self,
    ) -> list[dict[str, str]]:
        """Return every pane on the socket with id/current_command/session/window/pid."""
        proc = self._run(
            [
                "list-panes",
                "-a",
                "-F",
                "#{pane_id} #{pane_current_command} #{session_name} #{window_name} #{pane_pid}",
            ]
        )
        out: list[dict[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.rstrip("\n").split(" ", 4)
            if len(parts) != 5:
                continue
            out.append(
                {
                    "pane_id": parts[0],
                    "command": parts[1],
                    "session": parts[2],
                    "window": parts[3],
                    "pane_pid": parts[4],
                }
            )
        return out

    def pane_alive(self, pane: str) -> bool:
        return any(p["pane_id"] == pane for p in self.list_panes())

    def pane_pid(self, pane: str) -> int | None:
        try:
            proc = self._run(
                ["list-panes", "-t", pane, "-F", "#{pane_pid}"], timeout=10
            )
            return int(proc.stdout.strip())
        except (TmuxError, ValueError):
            return None

    def pane_command(self, pane: str) -> str | None:
        for p in self.list_panes():
            if p["pane_id"] == pane:
                return p["command"]
        return None

    def capture_pane(self, pane: str, lines: int = 100) -> str:
        proc = self._run(
            ["capture-pane", "-p", "-S", f"-{lines}", "-t", pane], timeout=15
        )
        return proc.stdout or ""

    def send_keys(self, pane: str, *keys: str) -> None:
        self._run(["send-keys", "-t", pane, *keys])

    def load_buffer(self, text: str, *, buffer_name: str | None = None) -> None:
        if buffer_name:
            args = ["load-buffer", "-b", buffer_name, "-"]
        else:
            args = ["load-buffer", "-"]
        self._run(args, input=text)

    def paste_buffer(
        self, pane: str, *, buffer_name: str | None = None
    ) -> None:
        args = ["paste-buffer", "-d"]
        if buffer_name:
            args.extend(["-b", buffer_name])
        args.extend(["-t", pane])
        self._run(args)

    def pipe_pane(self, pane: str, command: str) -> None:
        """Tee everything the pane prints into ``command``'s stdin.

        ``-o`` toggles, so a second identical call would stop the pipe; callers
        should invoke this once per pane, right after it is created.
        """
        self._run(["pipe-pane", "-t", pane, command])

    def list_clients(self) -> list[dict[str, str]]:
        """Attached clients as {name, session}. Empty when nobody is watching."""
        proc = self._run(
            ["list-clients", "-F", "#{client_name} #{client_session}"], check=False
        )
        out: list[dict[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                out.append({"name": parts[0], "session": parts[1]})
        return out

    def select_window(self, target: str) -> None:
        self._run(["select-window", "-t", target])

    def switch_client(self, client: str, session: str) -> None:
        self._run(["switch-client", "-c", client, "-t", self._session_target(session)])

    def kill_window(self, target: str) -> None:
        self._run(["kill-window", "-t", target])

    def kill_server(self) -> None:
        if self.socket:
            subprocess.run(self._args(["kill-server"]), capture_output=True)
        else:
            # Never kill the default tmux server from this wrapper.
            raise TmuxError("refusing to kill the default tmux server")
