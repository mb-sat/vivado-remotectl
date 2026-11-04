"""Session registry: the on-disk view of which Vivados are listening.

The Vivado-side plugin is the only writer of ``<name>.session``; everything
here reads. That keeps the format trivial (flat ``key value`` lines) and means
a GUI Vivado that auto-sources the plugin shows up in ``vrc list`` for free.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Statuses reported by :func:`probe`.
IDLE = "idle"
BUSY = "busy"
DEAD = "dead"
#: Not dead, just out of reach: see :func:`sandboxed`.
SANDBOXED = "sandboxed"

#: Appended to errors raised from inside a sandbox.
SANDBOX_HINT = (
    "this shell is sandboxed (SANDBOX_RUNTIME is set): it has its own loopback "
    "and pid namespace, so no session is reachable from here. Run vrc as a "
    "bare command, without cd &&, pipes, env prefixes or loops, so the "
    "sandbox's excludedCommands entry applies"
)


class RegistryError(Exception):
    """No usable session, or an ambiguous request for one."""


def state_dir() -> Path:
    """Where session descriptors live.

    Under XDG *state*, not cache: a cache cleaner removing the token of a
    live 30-minute synthesis would be a real failure.
    """
    for var in ("VRC_DIR", "VIVADO_REMOTECTL_DIR"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "vivado-remotectl"
    return Path.home() / ".local" / "state" / "vivado-remotectl"


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise RegistryError(
            f"invalid session name {name!r}: use 1-64 chars from [A-Za-z0-9._-]"
        )
    return name


@dataclass
class SessionInfo:
    name: str
    host: str
    port: int
    pid: int
    token: str
    started: int = 0
    cwd: str = ""
    tcl: str = ""
    #: /proc start time of the session process, used to prove a pid has not
    #: been recycled before we signal it. Empty for older descriptors.
    starttime: str = ""
    #: How a running command can be cancelled: "sigint" for sessions that
    #: `vrc start` put under Vivado's prompt loop, empty when it cannot be.
    cancel: str = ""
    path: Path | None = None

    @property
    def log_path(self) -> Path:
        """Derived by convention, so the plugin never has to write it."""
        base = self.path.parent if self.path else state_dir()
        return base / f"{self.name}.log"

    @property
    def uptime(self) -> int:
        return max(0, int(time.time()) - self.started) if self.started else 0


def read_session_file(path: Path) -> SessionInfo | None:
    """Parse one descriptor. Returns None if it is unreadable or incomplete."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        fields[key] = value

    try:
        return SessionInfo(
            name=fields["name"],
            host=fields.get("host", "127.0.0.1"),
            port=int(fields["port"]),
            pid=int(fields["pid"]),
            token=fields["token"],
            started=int(fields.get("started", 0) or 0),
            cwd=fields.get("cwd", ""),
            tcl=fields.get("tcl", ""),
            starttime=fields.get("starttime", ""),
            cancel=fields.get("cancel", ""),
            path=path,
        )
    except (KeyError, ValueError):
        # A descriptor caught mid-write, or one claimed by CREAT EXCL but not
        # yet filled in. Treat as not-there rather than an error.
        return None


def list_sessions(directory: Path | None = None) -> list[SessionInfo]:
    directory = directory or state_dir()
    try:
        entries = sorted(directory.glob("*.session"))
    except OSError:
        return []
    out = []
    for path in entries:
        info = read_session_file(path)
        if info is not None:
            out.append(info)
    return out


def sandboxed() -> bool:
    """Are we inside the Claude Code sandbox?

    It runs commands under bwrap with ``--unshare-net``, so 127.0.0.1 is a
    private loopback and every session refuses the connection. Telling that
    apart from a dead session keeps ``prune`` from deleting live descriptors
    and stops callers from starting a second Vivado beside the first.
    """
    return bool(os.environ.get("SANDBOX_RUNTIME"))


def pid_alive(pid: int) -> bool:
    """Is this pid a process we should consider running?

    A zombie answers ``kill(pid, 0)`` and keeps its /proc entry until the
    parent reaps it, so the naive check reports a session that has already
    exited as still alive. Read the process state and exclude ``Z``.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # EPERM means it exists but belongs to somebody else.
        if exc.errno != errno.EPERM:
            return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return True  # no procfs, or it exited just now; trust kill(0)
    # "<pid> (comm) <state> ..."; comm can contain spaces and parentheses,
    # so anchor on the last ')'.
    idx = data.rfind(b")")
    if idx < 0:
        return True
    fields = data[idx + 1 :].split()
    return not (fields and fields[0] == b"Z")


def proc_starttime(pid: int) -> str:
    """Start time of a pid in clock ticks since boot, or "" if unknown."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    fields = data[data.rfind(b")") + 1 :].split()
    if len(fields) < 20:
        return ""
    return fields[19].decode("ascii", errors="replace")


def owns_pid(info: SessionInfo) -> bool:
    """Does ``info.pid`` still refer to the process this descriptor describes?

    Pids are recycled. Without this check a stale descriptor could make
    ``vrc stop`` signal -- and with process groups, kill -- an unrelated
    Vivado that happened to inherit the number. A descriptor with no
    recorded start time cannot be verified, so it is never signalled.
    """
    if info.pid <= 0 or not info.starttime:
        return False
    if not pid_alive(info.pid):
        return False
    return proc_starttime(info.pid) == info.starttime


def probe(info: SessionInfo, timeout: float = 0.5) -> str:
    """Classify a session as idle, busy or dead.

    A busy session cannot answer at all -- its Tcl interpreter is inside
    ``synth_design`` -- so a successful connect with no timely reply is the
    signal for ``busy`` rather than something to hide.
    """
    from .protocol import FrameReader, encode_request, read_response

    try:
        sock = socket.create_connection((info.host, info.port), timeout=timeout)
    except OSError:
        return SANDBOXED if sandboxed() else DEAD
    try:
        sock.sendall(encode_request("expr 1", info.token))
        read_response(FrameReader(sock))
        return IDLE
    except (socket.timeout, TimeoutError):
        return BUSY
    except Exception:
        return DEAD if not pid_alive(info.pid) else BUSY
    finally:
        sock.close()


def is_live(info: SessionInfo, timeout: float = 0.5) -> bool:
    return probe(info, timeout=timeout) != DEAD


def prune(directory: Path | None = None) -> list[str]:
    """Remove descriptors whose session is gone. Returns the names removed."""
    removed = []
    for info in list_sessions(directory):
        if probe(info) == DEAD and not owns_pid(info):
            try:
                if info.path:
                    info.path.unlink()
                removed.append(info.name)
            except OSError:
                pass
    return removed


def find(name: str, directory: Path | None = None) -> SessionInfo | None:
    directory = directory or state_dir()
    return read_session_file(directory / f"{validate_name(name)}.session")


def resolve(name: str | None = None, directory: Path | None = None) -> SessionInfo:
    """Pick the session to talk to.

    Explicit name, then ``$VIVADO_REMOTECTL_SESSION``, then the single live
    session. Anything else is an error that lists the candidates.
    """
    directory = directory or state_dir()
    name = name or os.environ.get("VIVADO_REMOTECTL_SESSION") or None

    if name:
        info = find(name, directory)
        if info is None:
            raise RegistryError(f"no session named {name!r} in {directory}")
        return info

    sessions = list_sessions(directory)
    if not sessions:
        raise RegistryError(
            f"no Vivado sessions in {directory}; start one with 'vrc start'"
        )
    if len(sessions) == 1:
        return sessions[0]

    live = [s for s in sessions if is_live(s)]
    if len(live) == 1:
        return live[0]
    names = ", ".join(sorted(s.name for s in (live or sessions)))
    raise RegistryError(f"several sessions are live ({names}); pick one with -s NAME")
