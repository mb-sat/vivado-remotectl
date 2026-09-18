"""Launching, waiting for and stopping Vivado sessions."""

from __future__ import annotations

import glob
import os
import signal
import subprocess
import time
from pathlib import Path

from .client import ConnectFailed, Session
from .registry import (
    RegistryError,
    SessionInfo,
    find,
    owns_pid,
    pid_alive,
    probe,
    proc_starttime,
    state_dir,
    validate_name,
)

#: Preferred when several installs are present; it is the version the
#: existing tooling and constraints were built against.
PREFERRED_VERSION = "2023.2"

VIVADO_GLOB = "/tools/Xilinx/Vivado/*/bin/vivado"


class LaunchError(Exception):
    pass


def plugin_path() -> Path:
    return Path(__file__).resolve().parent / "tcl" / "vivado_remotectl.tcl"


def find_vivado(path: str | None = None, version: str | None = None) -> str:
    """Locate a vivado executable.

    ``vivado`` is normally not on PATH on this host -- it lives under
    /tools/Xilinx and needs settings64.sh -- so the glob comes first.
    """
    if path:
        if not os.access(path, os.X_OK):
            raise LaunchError(f"not executable: {path}")
        return path

    env = os.environ.get("VRC_VIVADO")
    if env and not version:
        if not os.access(env, os.X_OK):
            raise LaunchError(f"$VRC_VIVADO is not executable: {env}")
        return env

    found: dict[str, str] = {}
    for candidate in glob.glob(VIVADO_GLOB):
        ver = Path(candidate).parent.parent.name
        if os.access(candidate, os.X_OK):
            found[ver] = candidate

    if version:
        if version not in found:
            have = ", ".join(sorted(found)) or "none"
            raise LaunchError(f"no Vivado {version} under /tools/Xilinx (have: {have})")
        return found[version]

    if PREFERRED_VERSION in found:
        return found[PREFERRED_VERSION]
    if found:
        return found[sorted(found)[-1]]

    from shutil import which

    on_path = which("vivado")
    if on_path:
        return on_path
    raise LaunchError(
        "no vivado found; pass --vivado PATH, --version VER, or set $VRC_VIVADO"
    )


def child_env(name: str, port: int, directory: Path, serve: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    env["VRC_NAME"] = name
    env["VRC_PORT"] = str(port)
    env["VRC_DIR"] = str(directory)
    env["VRC_SERVE"] = "1" if serve else "0"
    env["VRC_AUTOSTART"] = "1"
    return env


def log_tail(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def start_session(
    name: str,
    *,
    vivado: str | None = None,
    version: str | None = None,
    port: int = 0,
    cwd: str | None = None,
    gui: bool = False,
    wait: float = 180.0,
    extra_args: list[str] | None = None,
    directory: Path | None = None,
) -> SessionInfo:
    """Launch a Vivado with the plugin loaded and wait until it answers."""
    validate_name(name)
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)

    existing = find(name, directory)
    if existing is not None:
        if probe(existing) != "dead" or owns_pid(existing):
            raise LaunchError(
                f"session '{name}' is already running (pid {existing.pid}, "
                f"port {existing.port})"
            )
        if existing.path:
            existing.path.unlink(missing_ok=True)

    exe = find_vivado(vivado, version)
    plugin = plugin_path()
    log_path = directory / f"{name}.log"

    mode = "gui" if gui else "tcl"
    argv = [exe, "-mode", mode, "-nolog", "-nojournal", "-source", str(plugin)]
    argv += extra_args or []

    # A GUI Vivado owns its own event loop, so the plugin must not vwait.
    env = child_env(name, port, directory, serve=not gui)

    with open(log_path, "ab") as log, open(os.devnull, "rb") as devnull:
        log.write(
            f"\n=== vrc start {name} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            f"=== {' '.join(argv)}\n".encode()
        )
        log.flush()
        proc = subprocess.Popen(
            argv,
            stdin=devnull,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=cwd or os.getcwd(),
            env=env,
            start_new_session=True,  # own process group, so we can killpg later
        )

    try:
        return wait_ready(name, proc, deadline=time.monotonic() + wait, directory=directory)
    except LaunchError:
        # Our own child, just spawned: read its start time directly rather
        # than trusting a descriptor that may not have been written.
        _terminate(proc.pid, proc_starttime(proc.pid))
        tail = log_tail(log_path)
        raise LaunchError(
            f"session '{name}' did not come up within {wait:g}s.\n"
            f"--- last lines of {log_path} ---\n{tail}"
        ) from None


def wait_ready(
    name: str,
    proc: subprocess.Popen | None,
    deadline: float,
    directory: Path | None = None,
) -> SessionInfo:
    """Poll for the descriptor, then for a connection that actually answers."""
    directory = directory or state_dir()
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise LaunchError(f"vivado exited with status {proc.returncode}")
        info = find(name, directory)
        if info is not None and info.port:
            try:
                with Session(info) as session:
                    session.ping(timeout=5.0)
                return info
            except Exception:
                pass  # not answering yet
        time.sleep(0.2)
    raise LaunchError("timed out")


def _reap(pid: int) -> None:
    """Reap the child if it is ours, so it does not linger as a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _signal_target(pid: int, expect_starttime: str) -> int | None:
    """Return the pid or process group safe to signal, or None.

    Two guards, both aimed at never touching a Vivado we did not start:

    * the recorded /proc start time must still match, so a recycled pid is
      refused rather than killed;
    * we only signal the *group* when the pid is its own group leader, which
      is true exactly for the sessions we launched with ``start_new_session``.
      Otherwise we signal the single pid and leave the group alone.
    """
    if not expect_starttime:
        return None
    if proc_starttime(pid) != expect_starttime:
        return None
    try:
        if os.getpgid(pid) == pid:
            return -pid  # negative means "the process group", as for kill(2)
    except OSError:
        return None
    return pid


def _terminate(pid: int, expect_starttime: str, grace: float = 5.0) -> None:
    """SIGTERM then SIGKILL the session, if it is provably still ours.

    The process group rather than the pid, because Vivado forks children --
    but only after `_signal_target` has confirmed the identity. And never
    `pkill -f vivado`, which also matches the shell that invoked us.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        target = _signal_target(pid, expect_starttime)
        if target is None:
            return
        try:
            if target < 0:
                os.killpg(-target, sig)
            else:
                os.kill(target, sig)
        except OSError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            _reap(pid)
            if not pid_alive(pid):
                return
            time.sleep(0.1)
        grace = 2.0


def stop_session(
    info: SessionInfo, *, timeout: float = 20.0, force: bool = False
) -> str:
    """Shut a session down. Returns a one-word outcome for the caller to print."""
    outcome = "stopped"

    if not force:
        try:
            with Session(info) as session:
                try:
                    session.run("exit", timeout=timeout)
                except Exception:
                    # `exit` closes the socket without replying; that is success.
                    pass
        except (ConnectFailed, RegistryError):
            outcome = "was-unreachable"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _reap(info.pid)
            if not pid_alive(info.pid):
                break
            time.sleep(0.1)

    if owns_pid(info):
        _terminate(info.pid, info.starttime)
        outcome = "killed"
    elif pid_alive(info.pid) and not info.starttime:
        # Pre-starttime descriptor, or one we cannot verify. Refuse to signal
        # rather than risk hitting an unrelated Vivado that reused the pid.
        outcome = "left-running (cannot verify pid)"

    if info.path:
        try:
            info.path.unlink(missing_ok=True)
        except OSError:
            pass
    return outcome
