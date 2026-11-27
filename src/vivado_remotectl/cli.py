"""Command line interface.

Tcl injection is spelled with top-level ``-e``/``-f`` flags rather than a
subcommand, following the sed idiom. That is not only shorter -- it removes
the collision between subcommand names and Tcl commands that happen to share
them (``list``, ``info``, ``log`` are all real Tcl).

A subcommand, when used, must be the first argument.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import procinfo, registry
from .client import ConnectFailed, Session
from .launcher import (
    CancelError,
    LaunchError,
    RestartError,
    cancel_session,
    log_tail,
    plugin_path,
    restart_session,
    start_session,
    stop_session,
)
from .protocol import ProtocolError, RemoteTclError, SessionDied
from .registry import RegistryError
from .tclquote import with_argv

EXIT_OK = 0
EXIT_TCL_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SESSION = 3
EXIT_TIMEOUT = 4
EXIT_DIED = 5
EXIT_CANCELLED = 6
EXIT_INTERRUPTED = 130

SUBCOMMANDS = (
    "start", "stop", "restart", "cancel", "wait", "result", "list", "log", "repl", "ping",
    "status", "info", "plugin-path", "pool",
)

USAGE = """\
vrc [-s NAME] [-p PORT] -e SCRIPT | -f FILE  [--timeout SEC] [--tail] [--log]
    [--detach] [-- ARG...]

  Evaluate Tcl in a running Vivado session and print the result.
  -e and -f may both be repeated; they are concatenated in argument order.
  -f - reads from stdin. Anything after -- is the script's $::argv.
  --detach returns once the script has started; collect its result with vrc wait.
  --log prints what the script wrote to the session log (puts, report_*)
  before the result.

Subcommands (must come first):

  vrc start [NAME]   launch a Vivado session
  vrc stop  [NAME]   shut one down (--mine for yours, --all for every one)
  vrc restart [NAME] stop it, running or not, and start it again as before
  vrc cancel [NAME]  cancel the running command, keep the session
  vrc wait  [NAME]   wait until the session is free, print the last result
  vrc result [NAME]  print the last result now
  vrc list           what is running (also: vrc status)
  vrc log   [NAME]   the session's output (-f to follow)
  vrc repl  [NAME]   interactive prompt
  vrc ping  [NAME]   check a session answers
  vrc info  [NAME]   show a session's descriptor
  vrc plugin-path    path to the Tcl plugin, for Vivado_init.tcl
  vrc pool run       run a list of Tcl jobs across several sessions
  vrc pool status    show how a pool's jobs stand
  vrc --version      the version of vrc itself

  -s NAME may stand before a subcommand instead of the NAME after it:
  vrc -s dut info is vrc info dut. list, status and plugin-path ignore it.

Examples:

  vrc start dut
  vrc -e 'version -short'
  vrc -s dut -f ooc.tcl --tail
  vrc -e 'close_design -quiet' -e 'read_verilog {a.v b.v}'
  vrc list && vrc stop --mine
"""


def die(message: str, code: int = EXIT_USAGE) -> int:
    print(f"vrc: {message}", file=sys.stderr)
    return code


# ----------------------------------------------------------------------------
# Script assembly: -e and -f must concatenate in the order they were given,
# which argparse will not do for two separate options on its own.
# ----------------------------------------------------------------------------


class _ScriptPart(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        kind = "file" if option_string in ("-f", "--file") else "expr"
        parts = getattr(namespace, "script_parts", None)
        if parts is None:
            parts = []
            namespace.script_parts = parts
        parts.append((kind, values))


def assemble_script(parts: list[tuple[str, str]]) -> str:
    chunks = []
    for kind, value in parts:
        if kind == "expr":
            chunks.append(value)
        elif value == "-":
            chunks.append(sys.stdin.read())
        else:
            try:
                chunks.append(Path(value).read_text(encoding="utf-8"))
            except OSError as exc:
                raise RegistryError(f"cannot read {value}: {exc}") from None
    return "\n".join(chunks)


# ----------------------------------------------------------------------------
# Log tailing, so a 30-minute synthesis is not a silent wait. Vivado writes
# its progress to stdout, which `vrc start` redirected into the session log.
# ----------------------------------------------------------------------------


class LogTailer:
    def __init__(self, path: Path, stream=None):
        self.path = path
        self.stream = stream or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LogTailer:
        try:
            self._fh = open(self.path, encoding="utf-8", errors="replace")
        except OSError:
            return self
        self._fh.seek(0, os.SEEK_END)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            chunk = self._fh.read()
            if chunk:
                self.stream.write(chunk)
                self.stream.flush()
            else:
                self._stop.wait(0.25)

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            chunk = self._fh.read()
            if chunk:
                self.stream.write(chunk)
                self.stream.flush()
            self._fh.close()
        except (OSError, AttributeError, ValueError):
            pass


# ----------------------------------------------------------------------------
# eval mode
# ----------------------------------------------------------------------------


def build_eval_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vrc", usage=USAGE, add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("-s", "--session", metavar="NAME")
    p.add_argument("-p", "--port", type=int, metavar="PORT")
    p.add_argument("-e", "--eval", action=_ScriptPart, metavar="SCRIPT")
    p.add_argument("-f", "--file", action=_ScriptPart, metavar="FILE")
    p.add_argument(
        "--timeout", type=float, default=None, metavar="SEC",
        help="seconds to wait for a result; 0 or omitted means forever",
    )
    p.add_argument("--tail", action="store_true", default=None)
    p.add_argument("--no-tail", dest="tail", action="store_false")
    p.add_argument("--log", action="store_true")
    p.add_argument("--detach", action="store_true")
    return p


def open_session(session: str | None, port: int | None) -> Session:
    """Resolve a session by name, or build an ad-hoc one from --port."""
    if port is not None:
        info = registry.SessionInfo(
            name=f"port-{port}", host="127.0.0.1", port=port, pid=0, token=""
        )
        return Session(info)
    return Session(registry.resolve(session))


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def log_slice(path: Path, start: int, settle: float = 0.05, cap: float = 1.0) -> str:
    """What was appended to the log from byte ``start``, once it stops growing.

    Vivado writes the log from its own buffers, so output of the request can
    land a moment after the reply. Waits until two reads ``settle`` seconds
    apart see the same size, for at most ``cap`` seconds.
    """
    deadline = time.monotonic() + cap
    size = log_size(path)
    while time.monotonic() < deadline:
        time.sleep(settle)
        again = log_size(path)
        if again == size:
            break
        size = again
    if size <= start:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(size - start).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _print_log_slice(text: str) -> None:
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()


#: The plugin starts the error text of a request `vrc cancel` stopped with this.
CANCELLED_PREFIX = "cancelled by vrc cancel"


def _still_running_hint(info: registry.SessionInfo) -> str:
    hint = f"vrc log -f {info.name}"
    if info.cancel:
        hint += f"; vrc cancel {info.name} to stop it"
    return hint


#: Options of `vrc start`, which people also try on an eval.
START_OPTIONS = ("--version", "--vivado", "--cwd", "--gui", "--idle-stop", "--min-free", "--arg")


def _explain_leftovers(leftover: list[str]) -> str:
    """Say what was probably meant by arguments the eval parser did not take."""
    first = leftover[0]
    option = first.split("=", 1)[0]
    if option in START_OPTIONS:
        hint = f"{option} is an option of 'vrc start', not of evaluating Tcl"
        if option == "--cwd":
            hint += "; inside a running session use -e 'cd DIR'"
        return hint
    if first.startswith("-"):
        return f"unknown option {first!r} (see vrc --help)"
    script = " ".join(leftover)
    return (
        f"unexpected argument {first!r}; Tcl goes after -e, quoted as one "
        f"argument: vrc -e {_shell_quote(script)}"
    )


def _shell_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


#: Eval options that take a value, so a `--` after one is that value.
_VALUE_OPTIONS = ("-s", "--session", "-p", "--port", "-e", "--eval", "-f", "--file", "--timeout")


def split_script_args(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Cut ``argv`` at the first ``--``: options before, script arguments after.

    Returns None for the arguments when there is no ``--``, which leaves the
    session's ``::argv`` as it was.
    """
    i = 0
    while i < len(argv):
        if argv[i] == "--":
            return argv[:i], argv[i + 1 :]
        i += 2 if argv[i] in _VALUE_OPTIONS else 1
    return argv, None


def cmd_eval(argv: list[str]) -> int:
    parser = build_eval_parser()
    argv, script_args = split_script_args(argv)
    args, leftover = parser.parse_known_args(argv)
    if args.help:
        print(USAGE, end="")
        return EXIT_OK
    if leftover:
        return die(_explain_leftovers(leftover))

    parts = getattr(args, "script_parts", None)
    if not parts:
        return die("nothing to evaluate; pass -e SCRIPT or -f FILE (see --help)")

    try:
        script = assemble_script(parts)
    except RegistryError as exc:
        return die(str(exc))
    if script_args is not None:
        script = with_argv(script, script_args)

    timeout = args.timeout if args.timeout else None  # 0 means forever

    try:
        client = open_session(args.session, args.port)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    if args.log and args.detach:
        return die("--log cannot be combined with --detach; the output is "
                   "read when the reply arrives (vrc log NAME afterwards)")

    if args.detach:
        if args.port is not None:
            return die("--detach needs a named session, not --port")
        return _detach(client.info, script)

    tail = args.tail
    if tail is None:
        tail = sys.stderr.isatty() and args.port is None

    try:
        client.connect()
    except ConnectFailed as exc:
        return die(str(exc), EXIT_NO_SESSION)

    log_start = log_size(client.info.log_path) if args.log else 0
    tailer = (
        LogTailer(client.info.log_path)
        if tail and client.info.log_path.exists()
        else None
    )
    try:
        if tailer is not None:
            with tailer:
                result = client.run(script, timeout=timeout)
        else:
            result = client.run(script, timeout=timeout)
    except RemoteTclError as exc:
        if args.log:
            _print_log_slice(log_slice(client.info.log_path, log_start))
        print(exc.errorinfo, file=sys.stderr)
        if exc.errorinfo.startswith(CANCELLED_PREFIX):
            return EXIT_CANCELLED
        return EXIT_TCL_ERROR
    except TimeoutError:
        return die(
            f"timed out after {timeout:g}s; the script is still running in "
            f"session '{client.info.name}' ({_still_running_hint(client.info)})",
            EXIT_TIMEOUT,
        )
    except SessionDied:
        return _session_died(client.info)
    except ProtocolError as exc:
        return die(f"protocol error: {exc}", EXIT_DIED)
    except KeyboardInterrupt:
        return die(
            f"interrupted; the script keeps running in session "
            f"'{client.info.name}' ({_still_running_hint(client.info)})",
            EXIT_INTERRUPTED,
        )
    except OSError as exc:
        return die(str(exc), EXIT_DIED)
    finally:
        client.close()

    if args.log:
        _print_log_slice(log_slice(client.info.log_path, log_start))
    if result:
        print(result)
    return EXIT_OK


# ----------------------------------------------------------------------------
# results that outlive the client
# ----------------------------------------------------------------------------

#: How long --detach waits for the plugin to say the script has started.
DETACH_CONFIRM = 10.0


def _result_stamp(info: registry.SessionInfo) -> tuple[int, int] | None:
    """Identity of the result file: changes whenever the plugin rewrites it.

    The timestamps inside are whole seconds, so a result from the same second
    as a new request cannot be told apart by them; the file's own mtime can.
    """
    try:
        st = info.sidecar("result").stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ino)


def _detach(info: registry.SessionInfo, script: str) -> int:
    """Hand a script to a session and return without waiting for it to finish.

    An idle session starts it at once, and we return when `<name>.running`
    (or a new `<name>.result`) shows that it has. A busy session serves
    requests in the order their connections were made, so the script is
    queued behind what is running: it is sent and the connection closed, since
    the plugin runs a request whose client has gone. `vrc wait` then queues
    behind it and so returns this script's result, not the running one's.
    """
    if not info.results:
        return die(
            f"session '{info.name}' was started by an older vrc and keeps no "
            f"result to collect; restart it, or run without --detach",
            EXIT_NO_SESSION,
        )
    status = registry.probe(info)
    if status not in (registry.IDLE, registry.BUSY):
        return die(
            f"session '{info.name}' is {status}; cannot hand it a script",
            EXIT_NO_SESSION,
        )

    if status == registry.BUSY:
        running = registry.read_running(info)
        try:
            with Session(info) as client:
                client.send(script)
        except ConnectFailed as exc:
            return die(str(exc), EXIT_NO_SESSION)
        except OSError as exc:
            return die(str(exc), EXIT_DIED)
        what = f"'{running.first_line}'" if running else "the running script"
        print(
            f"{info.name}: queued behind {what}; collect it with vrc wait {info.name}",
            file=sys.stderr,
        )
        return EXIT_OK

    sent = int(time.time())
    before = _result_stamp(info)
    try:
        with Session(info) as client:
            client.send(script)
            deadline = time.monotonic() + DETACH_CONFIRM
            while time.monotonic() < deadline:
                running = registry.read_running(info)
                if (running and running.started >= sent) or (
                    _result_stamp(info) not in (None, before)
                ):
                    break
                time.sleep(0.1)
            else:
                return die(
                    f"sent to '{info.name}', but it did not report the script "
                    f"as started within {DETACH_CONFIRM:g}s (vrc log {info.name})",
                    EXIT_TIMEOUT,
                )
    except ConnectFailed as exc:
        return die(str(exc), EXIT_NO_SESSION)
    except OSError as exc:
        return die(str(exc), EXIT_DIED)
    print(f"{info.name}: running; collect it with vrc wait {info.name}", file=sys.stderr)
    return EXIT_OK


def _print_result(info: registry.SessionInfo) -> int:
    """Print a session's last result as `vrc -e` would have, same exit code."""
    last = registry.read_result(info)
    if last is None:
        why = "" if info.results else " (started by an older vrc, which keeps none)"
        return die(f"session '{info.name}' has no recorded result{why}", EXIT_NO_SESSION)
    if last.status == "ok":
        if last.text:
            print(last.text)
        return EXIT_OK
    print(last.text, file=sys.stderr)
    if last.text.startswith(CANCELLED_PREFIX):
        return EXIT_CANCELLED
    return EXIT_TCL_ERROR


def _oom_note(info: registry.SessionInfo) -> str:
    """", killed by the OOM killer (anon-rss 12.8G)" when the kernel log says so."""
    try:
        found = procinfo.oom_kill(info.pid)
    except Exception:
        return ""
    if found is None:
        return ""
    rss = f" (anon-rss {procinfo.fmt_bytes(found.anon_rss)})" if found.anon_rss else ""
    return f"killed by the kernel OOM killer{rss}"


def _session_died(info: registry.SessionInfo, what: str = "closed the connection") -> int:
    tail_text = log_tail(info.log_path)
    msg = f"session '{info.name}' {what}"
    oom = _oom_note(info)
    if oom:
        msg = f"session '{info.name}' was {oom}; the machine ran out of memory"
    if tail_text:
        msg += f"\n--- last lines of {info.log_path} ---\n{tail_text}"
    return die(msg, EXIT_DIED)


def cmd_wait(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="vrc wait",
        description="Wait until the session has finished what it is running, "
        "then print its last result with that result's exit code.",
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--timeout", type=float, default=None, metavar="SEC",
                   help="give up after SEC seconds (exit 4); default forever")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    # Requests queue until the interpreter is free, so a liveness check that
    # comes back is the moment the running script finished. No polling.
    client = Session(info)
    try:
        client.connect()
        client.run("expr 1", timeout=args.timeout or None)
    except ConnectFailed as exc:
        return die(str(exc), EXIT_NO_SESSION)
    except TimeoutError:
        running = registry.read_running(info)
        what = f" ({running.first_line})" if running else ""
        return die(
            f"'{info.name}' still busy after {args.timeout:g}s{what}",
            EXIT_TIMEOUT,
        )
    except KeyboardInterrupt:
        return die(f"interrupted; '{info.name}' keeps running", EXIT_INTERRUPTED)
    except (SessionDied, OSError, ProtocolError):
        return _session_died(info)
    finally:
        client.close()
    return _print_result(info)


def cmd_result(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="vrc result",
        description="Print the last result the session finished, without waiting.",
    )
    p.add_argument("name", nargs="?")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    running = registry.read_running(info)
    if running is not None and registry.probe(info) == registry.BUSY:
        print(
            f"vrc: '{info.name}' has been running {running.first_line!r} for "
            f"{_fmt_uptime(int(time.time()) - running.started)}; this is the "
            f"result before it (vrc wait {info.name} for the new one)",
            file=sys.stderr,
        )
    return _print_result(info)


# ----------------------------------------------------------------------------
# subcommands
# ----------------------------------------------------------------------------


def cmd_start(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc start")
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--vivado", metavar="PATH")
    p.add_argument("--version", metavar="VER", help="e.g. 2024.2; default $VRC_VIVADO_VERSION")
    p.add_argument("--port", type=int, default=0, help="0 (default) = any free port")
    p.add_argument("--cwd", metavar="DIR")
    p.add_argument("--gui", action="store_true", help="launch -mode gui")
    p.add_argument("--wait", type=float, default=180.0, metavar="SEC")
    p.add_argument(
        "--arg", action="append", default=[], metavar="ARG",
        help="extra argument passed through to vivado (repeatable)",
    )
    p.add_argument(
        "--idle-stop", type=_duration, default=0, metavar="DUR",
        help="exit after this long without a request, e.g. 90m or 2h (default never)",
    )
    p.add_argument(
        "--min-free", type=float, default=DEFAULT_MIN_FREE_GB, metavar="GB",
        help=f"refuse to start with less memory available than this "
        f"(default {DEFAULT_MIN_FREE_GB:g}; 0 skips the check)",
    )
    args = p.parse_args(argv)

    if args.min_free > 0:
        refusal = _memory_refusal(args.name, args.min_free)
        if refusal:
            return die(refusal, EXIT_NO_SESSION)

    try:
        info = start_session(
            args.name,
            vivado=args.vivado,
            version=args.version,
            port=args.port,
            cwd=args.cwd,
            gui=args.gui,
            wait=args.wait,
            extra_args=args.arg,
            idle_stop=args.idle_stop,
        )
    except (LaunchError, RegistryError) as exc:
        return die(str(exc), EXIT_NO_SESSION)

    print(f"{info.name}: listening on {info.host}:{info.port} (pid {info.pid})")
    print(f"log: {info.log_path}")
    return EXIT_OK


def _duration(text: str) -> int:
    """Seconds from ``90``, ``90s``, ``45m`` or ``2h``."""
    units = {"s": 1, "m": 60, "h": 3600}
    scale = units.get(text[-1:].lower(), None)
    number = text[:-1] if scale else text
    try:
        value = float(number) * (scale or 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"negative duration: {text!r}")
    return int(value)


#: A session reaches 4-6 GB once a design is synthesised; with no swap, a
#: start that leaves less than that free ends with the OOM killer choosing.
DEFAULT_MIN_FREE_GB = 6.0


def _memory_refusal(name: str, min_free_gb: float) -> str | None:
    free = procinfo.mem_available()
    if free is None or free >= min_free_gb * 2**30:
        return None
    sessions = registry.list_sessions()
    tree = procinfo.children_map()
    held = [
        f"{i.name} {procinfo.fmt_bytes(procinfo.tree_rss(i.pid, tree))}"
        for i in sessions
    ]
    held += [
        f"pid {s.pid} {procinfo.fmt_bytes(s.rss)} (not a session)"
        for s in procinfo.stray_vivados([i.pid for i in sessions], tree)
    ]
    msg = (
        f"not starting '{name}': {procinfo.fmt_bytes(free)} available, and a "
        f"session needs 4-6 GB once a design is loaded"
    )
    if held:
        msg += f"; Vivados holding memory: {', '.join(held)}"
    return msg + ". Stop one, or pass --min-free 0 to start anyway"


def cmd_stop(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc stop")
    p.add_argument("name", nargs="?")
    which = p.add_mutually_exclusive_group()
    which.add_argument(
        "--mine", action="store_true",
        help="every session started from this Claude session or by this user",
    )
    which.add_argument(
        "--all", action="store_true",
        help="every session on the machine, except busy ones somebody else started",
    )
    p.add_argument("--force", action="store_true", help="skip the graceful exit")
    p.add_argument("--timeout", type=float, default=20.0, metavar="SEC")
    args = p.parse_args(argv)
    if args.name and (args.all or args.mine):
        return die("give a session name or --mine/--all, not both")

    me = registry.current_owner()
    skipped = 0
    if args.all or args.mine:
        targets = registry.list_sessions()
        if args.mine:
            targets = [i for i in targets if i.owner == me]
        if not targets:
            print("no sessions" + (" of yours" if args.mine else ""), file=sys.stderr)
            return EXIT_OK
    else:
        try:
            targets = [registry.resolve(args.name)]
        except RegistryError as exc:
            return die(str(exc), EXIT_NO_SESSION)

    for info in targets:
        # --all once killed another agent's synthesis mid-run. A busy session
        # that is not ours is left alone; naming it still stops it.
        if args.all and info.owner != me and registry.probe(info) == registry.BUSY:
            print(
                f"{info.name}: left running, busy and started by "
                f"{registry.short_owner(info.owner)}; stop it by name if you must",
                file=sys.stderr,
            )
            skipped += 1
            continue
        outcome = stop_session(info, timeout=args.timeout, force=args.force)
        print(f"{info.name}: {outcome}")
    return EXIT_TCL_ERROR if skipped else EXIT_OK


def cmd_restart(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="vrc restart",
        description="Stop a session, killing whatever it is running, and start "
        "it again with the Vivado, directory and options `vrc start` gave it.",
    )
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--wait", type=float, default=None, metavar="SEC",
        help="how long to wait for the new session (default: as at its start)",
    )
    p.add_argument(
        "--min-free", type=float, default=DEFAULT_MIN_FREE_GB, metavar="GB",
        help=f"do not start it again with less memory available than this, "
        f"counted after the old one has exited (default {DEFAULT_MIN_FREE_GB:g}; "
        f"0 skips the check)",
    )
    args = p.parse_args(argv)

    def check_memory() -> None:
        if args.min_free > 0:
            refusal = _memory_refusal(info.name, args.min_free)
            if refusal:
                raise RestartError(refusal)

    try:
        info = registry.resolve(args.name)
        new, was = restart_session(info, wait=args.wait, before_start=check_memory)
    except (RegistryError, RestartError) as exc:
        return die(str(exc), EXIT_NO_SESSION)

    note = {"busy": ", its running command killed", "dead": ", it had died"}.get(was, "")
    print(f"{new.name}: restarted{note}; listening on {new.host}:{new.port} (pid {new.pid})")
    return EXIT_OK


def cmd_cancel(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc cancel")
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--wait", type=float, default=60.0, metavar="SEC",
        help="how long to wait for the session to come back (default 60)",
    )
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
        outcome = cancel_session(info, wait=args.wait)
    except (RegistryError, CancelError) as exc:
        return die(str(exc), EXIT_NO_SESSION)

    if outcome == "still-busy":
        return die(
            f"{info.name}: SIGINT sent, still busy after {args.wait:g}s; "
            f"Vivado only checks for it between steps, or the script caught "
            f"the error and carried on (vrc log -f {info.name})",
            EXIT_TIMEOUT,
        )
    if outcome == "died":
        tail_text = log_tail(info.log_path)
        msg = f"{info.name}: Vivado exited while cancelling; vrc start it again"
        oom = _oom_note(info)
        if oom:
            msg = f"{info.name}: {oom} while cancelling; vrc start it again"
        if tail_text:
            msg += f"\n--- last lines of {info.log_path} ---\n{tail_text}"
        return die(msg, EXIT_DIED)
    print(f"{info.name}: {'nothing running' if outcome == 'idle' else outcome}")
    return EXIT_OK


def _fmt_uptime(seconds: int) -> str:
    if seconds < 0:
        return "-"
    if seconds == 0:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def cmd_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc list")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mine", action="store_true", help="only sessions you started")
    p.add_argument("--prune", action="store_true", help="delete dead descriptors")
    args = p.parse_args(argv)

    if args.prune:
        for name in registry.prune():
            print(f"pruned {name}", file=sys.stderr)

    sessions = registry.list_sessions()
    if args.mine:
        me = registry.current_owner()
        sessions = [i for i in sessions if i.owner == me]
    rows = [(info, registry.probe(info)) for info in sessions]
    now = int(time.time())
    since = {}
    for info, status in rows:
        changed = registry.last_change(info, status)
        since[info.name] = max(0, now - changed) if changed else None
    tree = procinfo.children_map()
    mem = {info.name: procinfo.tree_rss(info.pid, tree) for info in sessions}
    strays = procinfo.stray_vivados([info.pid for info in sessions], tree)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": i.name,
                        "host": i.host,
                        "port": i.port,
                        "pid": i.pid,
                        "status": status,
                        "for": since[i.name],
                        "uptime": i.uptime,
                        "cwd": i.cwd,
                        "tcl": i.tcl,
                        "rss": mem[i.name],
                        "owner": i.owner,
                        "log": str(i.log_path),
                    }
                    for i, status in rows
                ],
                indent=2,
            )
        )
        return EXIT_OK

    if rows:
        print(
            f"{'NAME':<20} {'PORT':>6} {'PID':>8} {'STATUS':<9} {'FOR':>7} "
            f"{'UPTIME':>8} {'RSS':>6} {'OWNER':<15}  CWD"
        )
        for info, status in rows:
            took = since[info.name]
            print(
                f"{info.name:<20} {info.port:>6} {info.pid:>8} {status:<9} "
                f"{_fmt_uptime(took) if took is not None else '-':>7} "
                f"{_fmt_uptime(info.uptime) if info.started else '-':>8} "
                f"{procinfo.fmt_bytes(mem[info.name]):>6} "
                f"{registry.short_owner(info.owner):<15}  {info.cwd}"
            )
    else:
        print(f"no sessions in {registry.state_dir()}", file=sys.stderr)
    _print_memory_footer(list(mem.values()), strays)
    return EXIT_OK


def _print_memory_footer(
    session_rss: list[int | None], strays: list[procinfo.Stray]
) -> None:
    """Totals and unregistered Vivados, on stderr so the table stays parseable."""
    sys.stdout.flush()  # so a terminal shows them under the table, not above
    for stray in strays:
        print(
            f"not a session: pid {stray.pid} {procinfo.fmt_bytes(stray.rss)}  "
            f"vivado {stray.args[:100]}",
            file=sys.stderr,
        )
    held = sum(r or 0 for r in session_rss) + sum(s.rss for s in strays)
    count = len(session_rss) + len(strays)
    free = procinfo.mem_available()
    if count or registry.sandboxed():
        line = f"{count} Vivado(s) hold {procinfo.fmt_bytes(held)}"
        if registry.sandboxed():
            line += " (pids not visible from this sandbox)"
        print(f"{line}; {procinfo.fmt_bytes(free)} available", file=sys.stderr)


def cmd_log(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc log")
    p.add_argument("name", nargs="?")
    # -f is `follow` here, as in tail -f. At top level -f means `file`; this
    # is the one deliberate inconsistency in the CLI.
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=40, metavar="N")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    path = info.log_path
    if not path.exists():
        return die(f"no log at {path}", EXIT_NO_SESSION)

    text = log_tail(path, args.lines)
    if text:
        print(text)
    if not args.follow:
        return EXIT_OK

    try:
        with LogTailer(path, stream=sys.stdout):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_repl(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc repl")
    p.add_argument("name", nargs="?")
    p.add_argument("--timeout", type=float, default=None, metavar="SEC")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    client = Session(info)
    try:
        client.connect()
    except ConnectFailed as exc:
        return die(str(exc), EXIT_NO_SESSION)

    print(f"connected to '{info.name}' on {info.host}:{info.port}", file=sys.stderr)
    print("end a line with \\ to continue it; Ctrl-D to quit", file=sys.stderr)

    buffer: list[str] = []
    try:
        while True:
            prompt = "... " if buffer else f"{info.name}> "
            try:
                line = input(prompt)
            except EOFError:
                print(file=sys.stderr)
                break
            except KeyboardInterrupt:
                buffer.clear()
                print(file=sys.stderr)
                continue

            if line.endswith("\\"):
                buffer.append(line[:-1])
                continue
            buffer.append(line)
            script = "\n".join(buffer).strip()
            buffer.clear()
            if not script:
                continue
            if script in ("quit", "exit", "q"):
                break

            try:
                result = client.run(script, timeout=args.timeout)
            except RemoteTclError as exc:
                print(exc.errorinfo, file=sys.stderr)
                continue
            except KeyboardInterrupt:
                print(
                    f"interrupted; the script keeps running in the session "
                    f"({_still_running_hint(info)})",
                    file=sys.stderr,
                )
                break
            except (SessionDied, OSError, ProtocolError) as exc:
                print(f"vrc: {exc}", file=sys.stderr)
                return EXIT_DIED
            if result:
                print(result)
    finally:
        client.close()
    return EXIT_OK


def cmd_ping(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc ping")
    p.add_argument("name", nargs="?")
    p.add_argument("--timeout", type=float, default=2.0, metavar="SEC")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    started = time.monotonic()
    try:
        with Session(info) as client:
            version = client.run("version -short", timeout=args.timeout)
    except RemoteTclError:
        version = "(not Vivado)"
    except (ConnectFailed, SessionDied, OSError) as exc:
        return die(str(exc), EXIT_NO_SESSION)
    except TimeoutError:
        return die(f"'{info.name}' is busy (no answer in {args.timeout:g}s)", EXIT_TIMEOUT)

    elapsed = (time.monotonic() - started) * 1000
    print(f"{info.name}: {version.strip()} on port {info.port} ({elapsed:.0f} ms)")
    return EXIT_OK


def _open_project(info: registry.SessionInfo) -> str:
    """Name of the project open in an idle session, or "-".

    A busy session is not asked, because the question would queue behind
    its running script. A plugin older than ``::vrc::project`` answers with
    an error, which is shown as "-" too.
    """
    try:
        with Session(info) as session:
            return session.run("::vrc::project", timeout=2.0).strip() or "-"
    except Exception:
        return "-"


def cmd_info(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc info")
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        info = registry.resolve(args.name)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    status = registry.probe(info)
    fields = {
        "name": info.name,
        "host": info.host,
        "port": info.port,
        "pid": info.pid,
        "status": status,
        "project": _open_project(info) if status == "idle" else "-",
        "uptime": info.uptime,
        "tcl": info.tcl,
        "cwd": info.cwd,
        "cancel": info.cancel or "-",
        "owner": info.owner or "-",
        "idle_stop": _fmt_uptime(info.idle_stop) if info.idle_stop else "never",
        "log": str(info.log_path),
        "descriptor": str(info.path) if info.path else "",
    }
    if args.json:
        print(json.dumps(fields, indent=2))
    else:
        width = max(len(k) for k in fields)
        for key, value in fields.items():
            print(f"{key:<{width}}  {value}")
    return EXIT_OK


def cmd_plugin_path(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="vrc plugin-path").parse_args(argv)
    print(plugin_path())
    return EXIT_OK


def cmd_pool(argv: list[str]) -> int:
    # Imported here: the pool is the one subcommand with its own module.
    from .pool import main as pool_main

    return pool_main(argv)


HANDLERS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "cancel": cmd_cancel,
    "wait": cmd_wait,
    "result": cmd_result,
    "list": cmd_list,
    # What people type first when they want `list`; nothing else claims it.
    "status": cmd_list,
    "log": cmd_log,
    "repl": cmd_repl,
    "ping": cmd_ping,
    "info": cmd_info,
    "plugin-path": cmd_plugin_path,
    "pool": cmd_pool,
}


#: Options that take a value, per subcommand, so a value is not mistaken for
#: the session name when `-s NAME` in front is checked against it.
_SUBCOMMAND_VALUE_OPTIONS = {
    "start": ("--vivado", "--version", "--port", "--cwd", "--wait", "--arg",
              "--idle-stop", "--min-free"),
    "stop": ("--timeout",),
    "restart": ("--wait", "--min-free"),
    "cancel": ("--wait",),
    "wait": ("--timeout",),
    "log": ("-n", "--lines"),
    "repl": ("--timeout",),
    "ping": ("--timeout",),
}

#: Subcommands that take no session, and so ignore a leading -s.
_NO_SESSION_SUBCOMMANDS = ("list", "status", "plugin-path")


def _positional(command: str, args: list[str]) -> str | None:
    values = _SUBCOMMAND_VALUE_OPTIONS.get(command, ())
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if arg in values:
            i += 2
            continue
        if not arg.startswith("-"):
            return arg
        i += 1
    return None


def _leading_session(argv: list[str]) -> tuple[str, list[str]] | None:
    """Split ``-s NAME`` / ``--session NAME`` / ``--session=NAME`` off the front.

    Returns the name and the rest, only when the rest starts with a
    subcommand; otherwise the arguments are an eval and are left alone.
    """
    if argv[0] in ("-s", "--session") and len(argv) >= 3:
        name, rest = argv[1], argv[2:]
    elif argv[0].startswith("--session=") and len(argv) >= 2:
        name, rest = argv[0].split("=", 1)[1], argv[1:]
    else:
        return None
    if rest[0] not in HANDLERS:
        return None
    return name, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Restore the default SIGPIPE behaviour so `vrc -e ... | head` exits
    # quietly instead of raising BrokenPipeError.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass

    if not argv:
        print(USAGE, end="", file=sys.stderr)
        return EXIT_USAGE

    leading = _leading_session(argv)
    if leading is not None:
        name, argv = leading
        command = argv[0]
        if command not in _NO_SESSION_SUBCOMMANDS:
            given = _positional(command, argv[1:])
            if given is None:
                argv = [command, name, *argv[1:]]
            elif given != name:
                return die(
                    f"-s {name} and the name {given} given to '{command}' differ; "
                    f"use one of them"
                )

    if argv[0] in HANDLERS:
        try:
            return HANDLERS[argv[0]](argv[1:])
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        except BrokenPipeError:
            return EXIT_OK

    if argv[0] in ("help", "--help", "-h"):
        print(USAGE, end="")
        return EXIT_OK

    if argv == ["--version"]:
        from . import __version__

        print(f"vrc {__version__}")
        return EXIT_OK

    if not argv[0].startswith("-"):
        return die(
            f"unknown subcommand {argv[0]!r}; to evaluate Tcl use: "
            f"vrc -e {argv[0]!r}"
        )

    try:
        return cmd_eval(argv)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
