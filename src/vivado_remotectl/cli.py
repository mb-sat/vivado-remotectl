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
import socket
import sys
import threading
import time
from pathlib import Path

from . import registry
from .client import ConnectFailed, Session
from .launcher import (
    LaunchError,
    log_tail,
    plugin_path,
    start_session,
    stop_session,
)
from .protocol import ProtocolError, RemoteTclError, SessionDied
from .registry import RegistryError

EXIT_OK = 0
EXIT_TCL_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SESSION = 3
EXIT_TIMEOUT = 4
EXIT_DIED = 5
EXIT_INTERRUPTED = 130

SUBCOMMANDS = ("start", "stop", "list", "log", "repl", "ping", "info", "plugin-path")

USAGE = """\
vrc [-s NAME] [-p PORT] -e SCRIPT | -f FILE  [--timeout SEC] [--tail]

  Evaluate Tcl in a running Vivado session and print the result.
  -e and -f may both be repeated; they are concatenated in argument order.
  -f - reads from stdin.

Subcommands (must come first):

  vrc start [NAME]   launch a Vivado session
  vrc stop  [NAME]   shut one down (--all for every session)
  vrc list           what is running
  vrc log   [NAME]   the session's output (-f to follow)
  vrc repl  [NAME]   interactive prompt
  vrc ping  [NAME]   check a session answers
  vrc info  [NAME]   show a session's descriptor
  vrc plugin-path    path to the Tcl plugin, for Vivado_init.tcl

Examples:

  vrc start dut
  vrc -e 'version -short'
  vrc -s dut -f ooc.tcl --tail
  vrc -e 'close_design -quiet' -e 'read_verilog {a.v b.v}'
  vrc list && vrc stop --all
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
            setattr(namespace, "script_parts", parts)
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
            self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
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
    return p


def open_session(session: str | None, port: int | None) -> Session:
    """Resolve a session by name, or build an ad-hoc one from --port."""
    if port is not None:
        info = registry.SessionInfo(
            name=f"port-{port}", host="127.0.0.1", port=port, pid=0, token=""
        )
        return Session(info)
    return Session(registry.resolve(session))


def cmd_eval(argv: list[str]) -> int:
    parser = build_eval_parser()
    args = parser.parse_args(argv)
    if args.help:
        print(USAGE, end="")
        return EXIT_OK

    parts = getattr(args, "script_parts", None)
    if not parts:
        return die("nothing to evaluate; pass -e SCRIPT or -f FILE (see --help)")

    try:
        script = assemble_script(parts)
    except RegistryError as exc:
        return die(str(exc))

    timeout = args.timeout if args.timeout else None  # 0 means forever

    try:
        client = open_session(args.session, args.port)
    except RegistryError as exc:
        return die(str(exc), EXIT_NO_SESSION)

    tail = args.tail
    if tail is None:
        tail = sys.stderr.isatty() and args.port is None

    try:
        client.connect()
    except ConnectFailed as exc:
        return die(str(exc), EXIT_NO_SESSION)

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
        print(exc.errorinfo, file=sys.stderr)
        return EXIT_TCL_ERROR
    except (socket.timeout, TimeoutError):
        return die(
            f"timed out after {timeout:g}s; the script is still running in "
            f"session '{client.info.name}' (vrc log -f {client.info.name})",
            EXIT_TIMEOUT,
        )
    except SessionDied:
        tail_text = log_tail(client.info.log_path)
        msg = f"session '{client.info.name}' closed the connection"
        if tail_text:
            msg += f"\n--- last lines of {client.info.log_path} ---\n{tail_text}"
        return die(msg, EXIT_DIED)
    except ProtocolError as exc:
        return die(f"protocol error: {exc}", EXIT_DIED)
    except KeyboardInterrupt:
        return die(
            f"interrupted; the script keeps running in session "
            f"'{client.info.name}' (vrc log -f {client.info.name})",
            EXIT_INTERRUPTED,
        )
    except OSError as exc:
        return die(str(exc), EXIT_DIED)
    finally:
        client.close()

    if result:
        print(result)
    return EXIT_OK


# ----------------------------------------------------------------------------
# subcommands
# ----------------------------------------------------------------------------


def cmd_start(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc start")
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--vivado", metavar="PATH")
    p.add_argument("--version", metavar="VER", help="e.g. 2023.2")
    p.add_argument("--port", type=int, default=0, help="0 (default) = any free port")
    p.add_argument("--cwd", metavar="DIR")
    p.add_argument("--gui", action="store_true", help="launch -mode gui")
    p.add_argument("--wait", type=float, default=180.0, metavar="SEC")
    p.add_argument(
        "--arg", action="append", default=[], metavar="ARG",
        help="extra argument passed through to vivado (repeatable)",
    )
    args = p.parse_args(argv)

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
        )
    except (LaunchError, RegistryError) as exc:
        return die(str(exc), EXIT_NO_SESSION)

    print(f"{info.name}: listening on {info.host}:{info.port} (pid {info.pid})")
    print(f"log: {info.log_path}")
    return EXIT_OK


def cmd_stop(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vrc stop")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--force", action="store_true", help="skip the graceful exit")
    p.add_argument("--timeout", type=float, default=20.0, metavar="SEC")
    args = p.parse_args(argv)

    if args.all:
        targets = registry.list_sessions()
        if not targets:
            print("no sessions", file=sys.stderr)
            return EXIT_OK
    else:
        try:
            targets = [registry.resolve(args.name)]
        except RegistryError as exc:
            return die(str(exc), EXIT_NO_SESSION)

    for info in targets:
        outcome = stop_session(info, timeout=args.timeout, force=args.force)
        print(f"{info.name}: {outcome}")
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
    p.add_argument("--prune", action="store_true", help="delete dead descriptors")
    args = p.parse_args(argv)

    if args.prune:
        for name in registry.prune():
            print(f"pruned {name}", file=sys.stderr)

    sessions = registry.list_sessions()
    rows = [(info, registry.probe(info)) for info in sessions]

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
                        "uptime": i.uptime,
                        "cwd": i.cwd,
                        "tcl": i.tcl,
                        "log": str(i.log_path),
                    }
                    for i, status in rows
                ],
                indent=2,
            )
        )
        return EXIT_OK

    if not rows:
        print(f"no sessions in {registry.state_dir()}", file=sys.stderr)
        return EXIT_OK

    print(f"{'NAME':<20} {'PORT':>6} {'PID':>8} {'STATUS':<8} {'UPTIME':>8}  CWD")
    for info, status in rows:
        print(
            f"{info.name:<20} {info.port:>6} {info.pid:>8} {status:<8} "
            f"{_fmt_uptime(info.uptime) if info.started else '-':>8}  {info.cwd}"
        )
    return EXIT_OK


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
                    "interrupted; the script keeps running in the session",
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
    except (socket.timeout, TimeoutError):
        return die(f"'{info.name}' is busy (no answer in {args.timeout:g}s)", EXIT_TIMEOUT)

    elapsed = (time.monotonic() - started) * 1000
    print(f"{info.name}: {version.strip()} on port {info.port} ({elapsed:.0f} ms)")
    return EXIT_OK


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
        "uptime": info.uptime,
        "tcl": info.tcl,
        "cwd": info.cwd,
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


HANDLERS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "list": cmd_list,
    "log": cmd_log,
    "repl": cmd_repl,
    "ping": cmd_ping,
    "info": cmd_info,
    "plugin-path": cmd_plugin_path,
}


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
