"""PTY-backed interactive terminal sessions.

Each channel is a real shell (a login zsh) running in the repo root, so the
user can type the exact commands from DEMO_RUNBOOK_FULL.md themselves --
nothing here runs commands automatically. The WebSocket carries raw
terminal bytes in both directions (binary frames) plus JSON control
messages (text frames) for resize.
"""
import asyncio
import fcntl
import os
import signal
import struct
import subprocess
import termios
from typing import Optional

SCROLLBACK_BYTES = 200_000
DEFAULT_SHELL = os.environ.get("SHELL", "/bin/zsh")


def _descendant_pids(root_pid: int) -> list[int]:
    """Every process transitively parented by root_pid.

    A shell's own process group only covers the shell itself -- any
    foreground job it runs (something a user typed, or a command we wrote
    in programmatically, e.g. orchestrator/listener.py or producer.py) gets
    its OWN process group under normal job control. Killing just the
    shell's group leaves those long-running children as orphans, still
    running. Walking the real parent/child tree via `ps` is what actually
    reaches them, regardless of which process group they ended up in.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid"], capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    children_of: dict[int, list[int]] = {}
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children_of.setdefault(ppid, []).append(pid)

    descendants = []
    frontier = [root_pid]
    while frontier:
        kids = children_of.get(frontier.pop(), [])
        descendants.extend(kids)
        frontier.extend(kids)
    return descendants


class PtySession:
    def __init__(self, name: str, cwd: str, env: dict, init_commands: Optional[list] = None):
        self.name = name
        self.cwd = cwd
        self.env = env
        self.init_commands = init_commands or []
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.subscribers: set = set()
        self.scrollback = bytearray()

    def start(self) -> None:
        self.stop()
        pid, fd = os.forkpty()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                os.execvpe(DEFAULT_SHELL, [DEFAULT_SHELL, "-l"], self.env)
            except Exception:
                os._exit(1)
        self.pid = pid
        self.master_fd = fd
        os.set_blocking(fd, False)
        self.scrollback = bytearray()
        asyncio.get_running_loop().add_reader(fd, self._on_readable)
        # Sent after the shell's own rc files run, so this always wins even
        # if e.g. .zshrc re-prepends another Python onto PATH at login.
        for command in self.init_commands:
            self.write(command)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._detach_reader()
            return
        self.scrollback.extend(data)
        overflow = len(self.scrollback) - SCROLLBACK_BYTES
        if overflow > 0:
            del self.scrollback[:overflow]
        for ws in list(self.subscribers):
            asyncio.create_task(self._safe_send(ws, data))

    async def _safe_send(self, ws, data: bytes) -> None:
        try:
            await ws.send_bytes(data)
        except Exception:
            self.subscribers.discard(ws)

    def _detach_reader(self) -> None:
        if self.master_fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self.master_fd)
            except (ValueError, RuntimeError):
                pass

    def write(self, data: bytes) -> None:
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            if self.pid:
                os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def stop(self) -> None:
        self._detach_reader()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            # Kill real descendants first (see _descendant_pids) -- a long-
            # running foreground job like `docker logs -f`, listener.py, or
            # producer.py otherwise survives the shell's own group being
            # killed below, running on as an orphan.
            for pid in _descendant_pids(self.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self.pid = None
        self.subscribers = set()


class PtyRegistry:
    def __init__(self, names: list[str], cwd: str, env: dict, init_commands: Optional[list] = None):
        self.sessions = {
            name: PtySession(name, cwd, env, init_commands=init_commands) for name in names
        }

    def get(self, name: str) -> PtySession:
        return self.sessions[name]

    def restart_all(self) -> None:
        for session in self.sessions.values():
            session.start()
