"""Launching, waiting for and stopping Vivado sessions."""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .client import ConnectFailed, Session
from .registry import (
    SANDBOX_HINT,
    RegistryError,
    SessionInfo,
    current_owner,
    find,
    owns_pid,
    pid_alive,
    probe,
    proc_starttime,
    remove_files,
    sandboxed,
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


#: What `vrc start` pipes into a -mode tcl Vivado. Each line hands the
#: plugin's event loop half a second and returns to Vivado's prompt; see
#: ``::vrc::serve`` in the plugin for why that, not a vwait, is what makes
#: ``vrc cancel`` work.
SERVE_LINE = "::vrc::serve"


def child_env(
    name: str, port: int, directory: Path, serve: bool = True, owner: str | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    env["VRC_NAME"] = name
    env["VRC_PORT"] = str(port)
    env["VRC_DIR"] = str(directory)
    env["VRC_SERVE"] = "prompt" if serve else "0"
    env["VRC_AUTOSTART"] = "1"
    env["VRC_OWNER"] = owner if owner is not None else current_owner()
    return env


def log_tail(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


@dataclass
class LaunchRecord:
    """How `vrc start` launched a session, so `vrc restart` can repeat it.

    Kept in ``<name>.launch`` beside the descriptor. The plugin writes the
    descriptor and knows nothing of this: an attached Vivado, or a session
    from an older `vrc`, has no record and cannot be restarted.
    """

    vivado: str
    cwd: str
    port: int = 0
    gui: bool = False
    extra_args: list[str] = field(default_factory=list)
    idle_stop: int = 0
    wait: float = 180.0


def temp_dir(directory: Path, name: str) -> Path:
    """The ``-tempDir`` a session gets unless ``--arg -tempDir`` names another."""
    return directory / f"{name}.tmp"


def launch_record_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.launch"


def write_launch_record(directory: Path, name: str, record: LaunchRecord) -> None:
    path = launch_record_path(directory, name)
    tmp = path.with_suffix(".launch.tmp")
    tmp.write_text(json.dumps(asdict(record), indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_launch_record(info: SessionInfo) -> LaunchRecord | None:
    """The record for this session, or None if it has none or it is unreadable."""
    path = info.sidecar("launch")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LaunchRecord(**data)
    except (OSError, ValueError, TypeError):
        return None


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
    idle_stop: int = 0,
    owner: str | None = None,
) -> SessionInfo:
    """Launch a Vivado with the plugin loaded and wait until it answers.

    ``idle_stop`` seconds without a request make the session exit; 0 never.
    ``owner`` overrides who the session is recorded as started by; a restart
    keeps the original owner.
    """
    validate_name(name)
    if idle_stop and gui:
        raise LaunchError("--idle-stop does not apply to a GUI, which you close yourself")
    if sandboxed():
        raise LaunchError(f"not starting '{name}': {SANDBOX_HINT}")
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
    if "-tempDir" not in (extra_args or []):
        # Vivado's scratch files go beside the descriptor rather than into
        # the shared /tmp, so stopping the session can delete them.
        tmp = temp_dir(directory, name)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(mode=0o700)
        argv += ["-tempDir", str(tmp)]
    argv += extra_args or []

    # A GUI Vivado owns its own event loop, so the plugin must not vwait.
    env = child_env(name, port, directory, serve=not gui, owner=owner)
    if idle_stop > 0:
        env["VRC_IDLE_STOP"] = str(int(idle_stop))

    if not gui:
        # `yes` blocks on the full pipe, so the stream costs nothing, and it
        # dies of SIGPIPE when Vivado exits. Both sit in the one process
        # group that start_new_session gives the shell.
        argv = ["sh", "-c", f'yes {SERVE_LINE} | exec "$@"', "sh", *argv]

    workdir = os.path.abspath(cwd or os.getcwd())
    write_launch_record(
        directory,
        name,
        LaunchRecord(
            vivado=exe, cwd=workdir, port=port, gui=gui,
            extra_args=list(extra_args or []), idle_stop=idle_stop, wait=wait,
        ),
    )

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
            cwd=workdir,
            env=env,
            start_new_session=True,  # own process group, so we can killpg later
        )

    try:
        return wait_ready(name, proc, deadline=time.monotonic() + wait, directory=directory)
    except LaunchError:
        # Our own child, just spawned: read its start time directly rather
        # than trusting a descriptor that may not have been written.
        _terminate(proc.pid, proc_starttime(proc.pid))
        launch_record_path(directory, name).unlink(missing_ok=True)
        shutil.rmtree(temp_dir(directory, name), ignore_errors=True)
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

    Never `pkill -f vivado`, which also matches the shell that invoked us.

    With a real Vivado the descriptor's pid is the inner
    `unwrapped/lnx64.o/vivado`, not the group leader -- the `bin/vivado` and
    `bin/loader` shell wrappers sit above it -- so `_signal_target` declines
    the group and signals that pid alone. That is sufficient: the wrappers are
    only waiting on the child and exit with it, verified by checking the group
    is empty after a `--force` stop. Do not "fix" this by killing the group
    unconditionally; the guard is what keeps a recycled pid from taking out
    somebody else's Vivado.
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

    remove_files(info)
    return outcome


class CancelError(Exception):
    pass


def cancel_marker(info: SessionInfo) -> Path:
    """The file the plugin checks to label a cancelled request as such."""
    base = info.path.parent if info.path else state_dir()
    return base / f"{info.name}.cancel"


def cancel_session(info: SessionInfo, *, wait: float = 60.0) -> str:
    """SIGINT the session so Vivado cancels its running command.

    Returns "cancelled", "idle" when nothing was running, "still-busy" when
    the session has not come back within ``wait`` seconds, or "died" when
    Vivado exited instead -- 2023.2 has been seen to segfault in the router
    on one route_design cancel in thirteen.

    Only sessions whose descriptor says ``cancel sigint`` are signalled.
    Anything else -- a GUI, a Vivado attached via Vivado_init.tcl, a session
    from before prompt mode -- sits in a vwait or a GUI loop, where a
    SIGINT cancels the command but wedges the session for good.
    """
    if info.cancel != "sigint":
        raise CancelError(
            f"session '{info.name}' cannot be cancelled: only sessions "
            f"started by `vrc start` (not --gui) support it; restart it"
        )
    if sandboxed():
        raise CancelError(f"not cancelling '{info.name}': {SANDBOX_HINT}")
    if not owns_pid(info):
        raise CancelError(
            f"session '{info.name}': pid {info.pid} is gone or no longer "
            f"the process this descriptor describes"
        )
    if probe(info) == "idle":
        return "idle"

    marker = cancel_marker(info)
    marker.touch()
    # The pid alone, never the group: the group also holds the `yes` that
    # feeds Vivado's prompt and the sh around it, and losing either ends
    # the session at its next prompt read.
    try:
        os.kill(info.pid, signal.SIGINT)
    except OSError as exc:
        marker.unlink(missing_ok=True)
        raise CancelError(f"cannot signal pid {info.pid}: {exc}") from None

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        _reap(info.pid)
        if not pid_alive(info.pid):
            return "died"
        if probe(info, timeout=1.0) == "idle":
            return "cancelled"
        time.sleep(0.5)
    return "still-busy"


class RestartError(Exception):
    pass


def restart_session(
    info: SessionInfo,
    *,
    wait: float | None = None,
    stop_timeout: float = 20.0,
    before_start=None,
) -> tuple[SessionInfo, str]:
    """Stop a session and start it again under the same name and options.

    The Vivado executable, working directory, port, extra arguments and
    idle stop come from the session's launch record, and the owner from its
    descriptor, so the new session is the one `vrc start` made before. An
    idle session gets the graceful exit; a busy one is killed, and whatever
    it was running is lost -- a cancel first would gain nothing, since a
    cancelled synth_design leaves the session unable to synthesise again.

    ``before_start`` is called between the stop and the start, once the old
    process's memory is free; the CLI checks free memory there. It may
    raise to abandon the start.

    Returns the new session and the old one's state ("idle", "busy", "dead").
    """
    if sandboxed():
        raise RestartError(f"not restarting '{info.name}': {SANDBOX_HINT}")
    record = read_launch_record(info)
    if record is None:
        raise RestartError(
            f"session '{info.name}' has no launch record: it was attached, or "
            f"started by an older vrc. Stop it and start it again yourself"
        )
    if record.gui:
        raise RestartError(
            f"session '{info.name}' is a GUI; close it and start it again yourself"
        )
    if pid_alive(info.pid) and not owns_pid(info):
        raise RestartError(
            f"session '{info.name}': pid {info.pid} is no longer the process this "
            f"descriptor describes; refusing to signal it"
        )

    directory = info.path.parent if info.path else state_dir()
    was = probe(info)
    stop_session(info, timeout=stop_timeout, force=(was != "idle"))
    try:
        if before_start is not None:
            before_start()
        new = start_session(
            info.name,
            vivado=record.vivado,
            port=record.port,
            cwd=record.cwd,
            wait=record.wait if wait is None else wait,
            extra_args=record.extra_args,
            directory=directory,
            idle_stop=record.idle_stop,
            owner=info.owner or None,
        )
    except (LaunchError, RestartError, RegistryError) as exc:
        # The old descriptor is gone with the old process, so there is
        # nothing left to restart; say how to start it by hand.
        raise RestartError(
            f"{info.name}: stopped, but not started again: {exc}\n"
            f"start it with: {start_command(info.name, record)}"
        ) from None
    return new, was


def start_command(name: str, record: LaunchRecord) -> str:
    """The `vrc start` line that launches a session like ``record``."""
    argv = ["vrc", "start", name, "--vivado", record.vivado, "--cwd", record.cwd]
    if record.port:
        argv += ["--port", str(record.port)]
    if record.idle_stop:
        argv += ["--idle-stop", f"{record.idle_stop}s"]
    for arg in record.extra_args:
        argv += [f"--arg={arg}"]
    return shlex.join(argv)
