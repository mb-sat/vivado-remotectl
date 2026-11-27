"""Run a list of Tcl jobs across several sessions: ``vrc pool run``.

One foreground process hands each job to the next free session, and is the
one place that holds the rules a hand-written worker loop keeps getting
wrong: wait for free memory before a dispatch, restart a session that has
grown, give a job a time limit, and put the job of a session that died back
in the queue. Results go to files as each job finishes, so the caller's
shell may die without losing them.

It is not a daemon. Vivado's own ``launch_runs -cluster_configuration`` hook
needs one; ``batchbuild/concept.md`` describes that next step.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shlex
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import procinfo, registry
from .client import ConnectFailed, Session
from .launcher import (
    CancelError,
    LaunchError,
    RestartError,
    cancel_session,
    log_tail,
    read_launch_record,
    restart_session,
    start_session,
    stop_session,
)
from .protocol import ProtocolError, RemoteTclError, SessionDied
from .registry import RegistryError, SessionInfo
from .tclquote import with_argv

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_WORKERS = 3
EXIT_INTERRUPTED = 130

#: As `vrc start`: a session reaches 4-6 GB once a design is synthesised.
DEFAULT_MIN_FREE_GB = 6.0
#: How long a dispatch or a worker start waits for memory before it fails.
DEFAULT_MEM_WAIT = 600
#: Backstop for workers the pool started, should the pool be killed outright.
DEFAULT_IDLE_STOP = 3600

#: The plugin starts the error text of a request `vrc cancel` stopped with this.
CANCELLED_PREFIX = "cancelled by vrc cancel"

#: Job states that count as finished. Everything but ``ok`` fails the pool.
FINAL = ("ok", "error", "timeout", "died", "no-memory", "no-worker", "cancelled")

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class PoolError(Exception):
    """The job list or the options cannot be used."""


# ----------------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------------


@dataclass
class Job:
    name: str
    script: str
    args: list[str]
    text: str = field(default="", repr=False)
    state: str = "queued"
    worker: str = ""
    attempts: int = 0
    start: int | None = None
    seconds: float | None = None
    detail: str = ""
    began: float = field(default=0.0, repr=False)  # monotonic, for `seconds`

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "script": self.script,
            "args": self.args,
            "state": self.state,
            "worker": self.worker,
            "attempts": self.attempts,
            "start": self.start,
            "seconds": self.seconds,
            "detail": self.detail,
        }


def parse_jobs(lines: list[str]) -> list[Job]:
    """Jobs from lines of ``[name=NAME] script.tcl [arg...]``.

    Each line is split like a shell command line. A job without a name gets
    its script's stem, numbered when several jobs share one. The scripts are
    read here, so a missing file stops the pool before anything has run.
    """
    raw: list[tuple[str, str, list[str]]] = []
    for number, line in enumerate(lines, 1):
        try:
            words = shlex.split(line, comments=True)
        except ValueError as exc:
            raise PoolError(f"job {number}: {exc}: {line.strip()!r}") from None
        if not words:
            continue
        name = ""
        if words[0].startswith("name="):
            name = words.pop(0)[len("name="):]
            if not NAME_RE.match(name):
                raise PoolError(
                    f"job {number}: invalid name {name!r}: use chars from [A-Za-z0-9._-]"
                )
        if not words:
            raise PoolError(f"job {number}: no script: {line.strip()!r}")
        raw.append((name, words[0], words[1:]))
    if not raw:
        raise PoolError("no jobs given")

    stems = collections.Counter(Path(script).stem for name, script, _ in raw if not name)
    seen: collections.Counter[str] = collections.Counter()
    jobs: list[Job] = []
    for name, script, args in raw:
        if not name:
            stem = Path(script).stem
            seen[stem] += 1
            name = f"{stem}-{seen[stem]}" if stems[stem] > 1 else stem
            name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100] or "job"
        try:
            text = Path(script).read_text(encoding="utf-8")
        except OSError as exc:
            raise PoolError(f"job {name}: cannot read {script}: {exc}") from None
        jobs.append(Job(name=name, script=script, args=args, text=text))

    names = collections.Counter(job.name for job in jobs)
    twice = sorted(name for name, count in names.items() if count > 1)
    if twice:
        raise PoolError(f"job names used more than once: {', '.join(twice)}")
    return jobs


# ----------------------------------------------------------------------------
# workers
# ----------------------------------------------------------------------------


@dataclass
class Worker:
    """One session the pool hands jobs to.

    ``owned`` is true for a session the pool started itself; only those are
    stopped when the pool ends.
    """

    info: SessionInfo
    owned: bool = False
    state: str = "idle"  # idle, busy, restarting, done, gone
    runs: int = 0  # since the last (re)start
    total: int = 0
    restarts: int = 0
    job: str = ""

    @property
    def name(self) -> str:
        return self.info.name

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "owned": self.owned,
            "state": self.state,
            "runs": self.runs,
            "total": self.total,
            "restarts": self.restarts,
            "job": self.job,
        }


def memory_holders() -> str:
    """Which Vivados hold memory, for the message of a failed wait."""
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
    return ", ".join(held) if held else "no Vivado"


class Pool:
    """The queue, the workers and the rules between them."""

    #: Seconds between looks at free memory; tests shorten it.
    poll = 2.0
    #: How long an interrupt waits for a cancelled job to let go.
    cancel_wait = 30.0

    def __init__(
        self,
        jobs: list[Job],
        out: Path,
        *,
        min_free_gb: float = DEFAULT_MIN_FREE_GB,
        mem_wait: float = DEFAULT_MEM_WAIT,
        stagger: float = 0.0,
        max_runs: int = 0,
        timeout: float | None = None,
        retries: int = 1,
        init: str | None = None,
        quiet_lines: bool = False,
        directory: Path | None = None,
    ):
        self.jobs = jobs
        self.out = out
        self.min_free = int(min_free_gb * 2**30)
        self.mem_wait = mem_wait
        self.stagger = stagger
        self.max_runs = max_runs
        self.timeout = timeout or None
        self.retries = retries
        self.init = init
        self.quiet_lines = quiet_lines
        self.directory = directory or registry.state_dir()

        self.workers: list[Worker] = []
        self.queue: collections.deque[Job] = collections.deque(jobs)
        self.lock = threading.Condition()  # reentrant; guards everything below
        self.claimed = 0  # jobs a worker holds, dispatched or not yet
        self.live = 0  # workers still in their loop
        self.stopping = threading.Event()
        self.dispatch = threading.Lock()  # one memory check at a time
        self.last_dispatch = 0.0
        self.started = int(time.time())
        self.finished: int | None = None
        self.state = "running"

    # -- reporting ---------------------------------------------------------

    def log(self, message: str) -> None:
        print(f"pool: {message}", file=sys.stderr, flush=True)

    def status(self) -> dict:
        with self.lock:
            return {
                "pool": {
                    "pid": os.getpid(),
                    "state": self.state,
                    "started": self.started,
                    "finished": self.finished,
                    "out": str(self.out),
                },
                "workers": [w.as_dict() for w in self.workers],
                "jobs": [j.as_dict() for j in self.jobs],
            }

    def write_status(self) -> None:
        """Rewrite status.json; a reader never sees half of one."""
        with self.lock:
            path = self.out / "status.json"
            tmp = self.out / f"status.json.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(self.status(), indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def _finish(self, job: Job, state: str, text: str = "", detail: str = "") -> None:
        """Record a job's end: its file first, then the status, then the line."""
        ext, other = ("out", "err") if state == "ok" else ("err", "out")
        try:
            (self.out / f"{job.name}.{other}").unlink(missing_ok=True)
            body = text if state == "ok" else (text or detail)
            (self.out / f"{job.name}.{ext}").write_text(
                body + "\n" if body else "", encoding="utf-8"
            )
        except OSError as exc:
            self.log(f"cannot write the result of {job.name}: {exc}")
        with self.lock:
            job.state = state
            job.detail = detail
            if job.start is not None:
                job.seconds = round(time.monotonic() - job.began, 1)
            self.claimed -= 1
            self.write_status()
            if not self.quiet_lines:
                took = f"{job.seconds:.1f}" if job.seconds is not None else "-"
                print(f"{job.name} {job.worker or '-'} {state} {took}", flush=True)
            self.lock.notify_all()
        if state != "ok" and detail:
            self.log(f"{job.name}: {state}: {detail}")

    def _requeue(self, job: Job, why: str) -> None:
        """Back to the front of the queue, for whichever worker is free next."""
        with self.lock:
            job.state = "queued"
            job.detail = why
            job.worker = ""
            job.start = None
            self.queue.appendleft(job)
            self.claimed -= 1
            self.write_status()
            self.lock.notify_all()

    def _set(self, worker: Worker, state: str, job: str = "") -> None:
        with self.lock:
            worker.state = state
            worker.job = job
            self.write_status()

    # -- memory ------------------------------------------------------------

    def wait_memory(self, what: str) -> bool:
        """Wait until enough memory is available, at most ``mem_wait`` seconds.

        False when the time is up or the pool is stopping. A host whose
        memory cannot be read is taken to have enough.
        """
        if self.min_free <= 0:
            return True
        deadline = time.monotonic() + self.mem_wait
        said = False
        while True:
            free = procinfo.mem_available()
            if free is None or free >= self.min_free:
                return True
            if self.stopping.is_set() or time.monotonic() >= deadline:
                return False
            if not said:
                self.log(
                    f"{what}: {procinfo.fmt_bytes(free)} available, waiting for "
                    f"{procinfo.fmt_bytes(self.min_free)}"
                )
                said = True
            self.stopping.wait(self.poll)

    def _memory_message(self) -> str:
        return (
            f"{procinfo.fmt_bytes(procinfo.mem_available())} available after "
            f"waiting {self.mem_wait:g}s for {procinfo.fmt_bytes(self.min_free)}; "
            f"holding memory: {memory_holders()}"
        )

    def _gate(self, job: Job) -> bool:
        """Hold a dispatch until memory allows it and the last one has settled."""
        with self.dispatch:
            if self.stagger > 0:
                rest = self.last_dispatch + self.stagger - time.monotonic()
                if rest > 0 and self.stopping.wait(rest):
                    return False
            ok = self.wait_memory(f"job {job.name}")
            self.last_dispatch = time.monotonic()
            return ok

    # -- the worker loop ---------------------------------------------------

    def _take(self) -> Job | None:
        """The next job, or None once nothing is left that could still come.

        A job another worker holds may yet be requeued, so the queue being
        empty is not the end while any job is claimed.
        """
        with self.lock:
            while True:
                if self.stopping.is_set():
                    return None
                if self.queue:
                    self.claimed += 1
                    return self.queue.popleft()
                if self.claimed == 0:
                    return None
                self.lock.wait(0.5)

    def _work(self, worker: Worker) -> None:
        try:
            if not self._init(worker):
                return
            while True:
                job = self._take()
                if job is None:
                    break
                self._run(worker, job)
                if worker.state == "gone":
                    break
                with self.lock:
                    more = bool(self.queue) or self.claimed > 0
                if self.max_runs and worker.runs >= self.max_runs and more:
                    if not self._restart(worker, f"after {worker.runs} runs"):
                        break
        finally:
            self._retire(worker)

    def _run(self, worker: Worker, job: Job) -> None:
        if not self._gate(job):
            if self.stopping.is_set():
                self._requeue(job, "")
            else:
                self._finish(job, "no-memory", detail=self._memory_message())
            return

        with self.lock:
            job.attempts += 1
            job.state = "running"
            job.worker = worker.name
            job.start = int(time.time())
            job.began = time.monotonic()
            job.detail = ""
            self._set(worker, "busy", job.name)

        died = ""
        try:
            with Session(worker.info) as session:
                text = session.run(with_argv(job.text, job.args), timeout=self.timeout)
        except RemoteTclError as exc:
            cancelled = exc.errorinfo.startswith(CANCELLED_PREFIX)
            if cancelled and self.stopping.is_set():
                self._finish(job, "cancelled", exc.errorinfo, "the pool was interrupted")
            else:
                self._finish(job, "error", exc.errorinfo, str(exc))
        except TimeoutError:
            self._finish(job, "timeout", detail=f"no result within {self.timeout:g}s")
            self._count(worker)
            self._after_timeout(worker)
            return
        except (ConnectFailed, SessionDied, ProtocolError, OSError) as exc:
            died = str(exc) or type(exc).__name__
        else:
            self._finish(job, "ok", text)

        if not died:
            self._count(worker)
            self._set(worker, "idle")
            return

        if self.stopping.is_set():
            self._finish(job, "cancelled", detail="the pool was interrupted")
            return
        tail = log_tail(worker.info.log_path, 15)
        if job.attempts <= self.retries:
            self.log(f"{worker.name} died running {job.name} ({died}); the job is queued again")
            self._requeue(job, f"{worker.name} died: {died}")
        else:
            self._finish(
                job, "died",
                f"{worker.name} died: {died}\n--- last lines of {worker.info.log_path} ---\n{tail}",
                f"{worker.name} died {job.attempts} time(s) running it: {died}",
            )
        self._restart(worker, "it died")

    def _count(self, worker: Worker) -> None:
        with self.lock:
            worker.runs += 1
            worker.total += 1

    def _init(self, worker: Worker) -> bool:
        """Run the init script in a worker that has just (re)started."""
        if self.init is None:
            return True
        try:
            with Session(worker.info) as session:
                session.run(self.init, timeout=self.timeout)
            return True
        except RemoteTclError as exc:
            why = exc.errorinfo
        except TimeoutError:
            why = f"no result within {self.timeout:g}s"
        except (ConnectFailed, SessionDied, ProtocolError, OSError) as exc:
            why = str(exc) or type(exc).__name__
        self.log(f"{worker.name} leaves the pool, its init script failed: {why}")
        self._set(worker, "gone")
        return False

    def _restart(self, worker: Worker, why: str) -> bool:
        """Restart a worker's session; False when it has left the pool."""
        if self.stopping.is_set():
            return False
        self.log(f"restarting {worker.name}: {why}")
        self._set(worker, "restarting")

        def check_memory() -> None:
            # The pool's floor, counted once the old process has gone, in
            # place of the 6 GB refusal of `vrc restart`.
            if not self.wait_memory(f"restart of {worker.name}"):
                raise RestartError(self._memory_message())

        try:
            new, _was = restart_session(worker.info, before_start=check_memory)
        except (RestartError, RegistryError, LaunchError) as exc:
            self.log(f"{worker.name} leaves the pool, it could not be restarted: {exc}")
            self._set(worker, "gone")
            return False
        with self.lock:
            worker.info = new
            worker.runs = 0
            worker.restarts += 1
            self._set(worker, "idle")
        return self._init(worker)

    def _after_timeout(self, worker: Worker) -> None:
        """Get a worker back whose job ran out of time.

        Restart rather than cancel: after a cancelled synth_design the next
        one in that session fails, so a cancelled worker is not a usable
        one. Only a session that cannot be restarted is cancelled instead.
        """
        record = read_launch_record(worker.info)
        if record is not None and not record.gui:
            self._restart(worker, "its job timed out")
            return
        try:
            outcome = cancel_session(worker.info, wait=self.cancel_wait)
        except (CancelError, RegistryError) as exc:
            outcome = str(exc)
        if outcome in ("cancelled", "idle"):
            self._set(worker, "idle")
            return
        self.log(
            f"{worker.name} leaves the pool: its job timed out and it can "
            f"neither be restarted nor cancelled ({outcome})"
        )
        self._set(worker, "gone")

    def _retire(self, worker: Worker) -> None:
        """A worker's loop has ended; free its memory now, not at the end."""
        if worker.owned:
            self._stop_owned(worker)
        with self.lock:
            if worker.state != "gone":
                worker.state = "done"
            worker.job = ""
            self.live -= 1
            if self.live == 0 and not self.stopping.is_set():
                # Nobody is left to run what is still queued.
                while self.queue:
                    job = self.queue.popleft()
                    self.claimed += 1
                    self._finish(job, "no-worker", detail="every worker has left the pool")
            self.write_status()
            self.lock.notify_all()

    def _stop_owned(self, worker: Worker) -> None:
        try:
            info = registry.find(worker.name, self.directory)
            if info is not None:
                stop_session(info, force=True)
        except (RegistryError, OSError) as exc:
            self.log(f"could not stop {worker.name}: {exc}")

    # -- running the pool --------------------------------------------------

    def run(self) -> int:
        """Run every job; returns the exit code."""
        self.out.mkdir(parents=True, exist_ok=True)
        self.live = len(self.workers)
        self.write_status()
        threads = [
            threading.Thread(target=self._work, args=(w,), name=w.name, daemon=True)
            for w in self.workers
        ]
        for thread in threads:
            thread.start()

        interrupted = False
        while any(t.is_alive() for t in threads):
            if self.stopping.is_set() and not interrupted:
                interrupted = True
                self._interrupt()
                deadline = time.monotonic() + self.cancel_wait + 5
                for thread in threads:
                    thread.join(max(0.0, deadline - time.monotonic()))
                break
            time.sleep(0.1)
        interrupted = interrupted or self.stopping.is_set()

        for worker in self.workers:
            if worker.owned:
                self._stop_owned(worker)
        with self.lock:
            failed = [j for j in self.jobs if j.state != "ok"]
            self.state = "interrupted" if interrupted else ("failed" if failed else "done")
            self.finished = int(time.time())
            self.write_status()
        if interrupted:
            return EXIT_INTERRUPTED
        return EXIT_FAILED if failed else EXIT_OK

    def _interrupt(self) -> None:
        """Stop what the pool itself set running.

        A worker the pool started is killed with its job. A session it was
        only lent keeps running; just the pool's job in it is cancelled.
        """
        self.log("interrupted; stopping the running jobs")
        with self.lock:
            self.lock.notify_all()
            busy = [w for w in self.workers if w.state == "busy"]
        for worker in busy:
            if worker.owned:
                self._stop_owned(worker)
                continue
            try:
                outcome = cancel_session(worker.info, wait=self.cancel_wait)
            except (CancelError, RegistryError) as exc:
                outcome = str(exc)
            if outcome not in ("cancelled", "idle"):
                self.log(f"{worker.name}: its job was left running ({outcome})")


# ----------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------


def _duration(text: str) -> int:
    from .cli import _duration as parse

    return parse(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vrc pool",
        description="Run a list of Tcl jobs across several sessions.",
    )
    sub = p.add_subparsers(dest="command", metavar="run|status")

    run = sub.add_parser(
        "run",
        help="run jobs and wait for them",
        description="Run each job in the next free session. A job is a line "
        "'[name=NAME] script.tcl [arg...]'; the arguments are the script's "
        "$::argv. Results go to --out as each job finishes.",
    )
    run.add_argument("job", nargs="*", help="a job line, quoted as one argument")
    run.add_argument("--jobs", metavar="FILE", help="read job lines from FILE (- for stdin)")
    who = run.add_mutually_exclusive_group(required=True)
    who.add_argument("--sessions", metavar="A,B,...", help="run in these idle sessions")
    who.add_argument("--workers", type=int, metavar="N",
                     help="start N sessions for the pool and stop them afterwards")
    run.add_argument("--prefix", metavar="P",
                     help="name --workers sessions P-1, P-2, ... (default pool<pid>)")
    run.add_argument("--vivado", metavar="PATH", help="for --workers, as vrc start")
    run.add_argument("--version", metavar="VER", help="for --workers, as vrc start")
    run.add_argument("--cwd", metavar="DIR", help="for --workers, as vrc start")
    run.add_argument("--arg", action="append", default=[], metavar="ARG",
                     help="for --workers, as vrc start (repeatable)")
    run.add_argument("--idle-stop", type=_duration, default=DEFAULT_IDLE_STOP, metavar="DUR",
                     help="--workers sessions exit after this long unused, should "
                     "the pool be killed (default 1h)")
    run.add_argument("--start-wait", type=float, default=180.0, metavar="SEC",
                     help="how long to wait for a --workers session to come up")
    run.add_argument("--min-free", type=float, default=DEFAULT_MIN_FREE_GB, metavar="GB",
                     help=f"start no job or worker with less memory available "
                     f"(default {DEFAULT_MIN_FREE_GB:g}; 0 skips the check)")
    run.add_argument("--mem-wait", type=_duration, default=DEFAULT_MEM_WAIT, metavar="DUR",
                     help="fail a job that has waited this long for memory (default 10m)")
    run.add_argument("--stagger", type=_duration, default=0, metavar="DUR",
                     help="leave this long between two dispatches, so the first "
                     "job's memory shows before the next check (default 0)")
    run.add_argument("--max-runs", type=int, default=0, metavar="M",
                     help="restart a session after M jobs (default never)")
    run.add_argument("--init", metavar="FILE",
                     help="Tcl to run in each session before its first job and "
                     "after every restart")
    run.add_argument("--timeout", type=_duration, default=0, metavar="DUR",
                     help="fail a job that runs longer and restart its session "
                     "(default none)")
    run.add_argument("--retries", type=int, default=1, metavar="N",
                     help="queue a job again up to N times when its session dies (default 1)")
    run.add_argument("--out", metavar="DIR",
                     help="where results and status.json go (default ./vrc-pool-<time>)")
    run.add_argument("--json", action="store_true",
                     help="print the final status.json instead of a line per job")

    status = sub.add_parser("status", help="show a pool's status.json as a table")
    status.add_argument("dir", metavar="DIR", help="the pool's --out directory")
    status.add_argument("--json", action="store_true")
    return p


def die(message: str, code: int = EXIT_USAGE) -> int:
    print(f"vrc pool: {message}", file=sys.stderr)
    return code


def _fmt_table(status: dict) -> str:
    lines = [f"{'JOB':<28} {'WORKER':<16} {'STATE':<10} {'TRIES':>5} {'SECONDS':>8}"]
    for job in status.get("jobs", []):
        took = f"{job['seconds']:.1f}" if job.get("seconds") is not None else "-"
        lines.append(
            f"{job['name']:<28} {job.get('worker') or '-':<16} {job['state']:<10} "
            f"{job.get('attempts', 0):>5} {took:>8}"
        )
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.dir) / "status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return die(f"cannot read {path}: {exc}", EXIT_NO_WORKERS)
    if args.json:
        print(json.dumps(status, indent=1))
        return EXIT_OK
    print(_fmt_table(status))
    pool = status.get("pool", {})
    states = collections.Counter(job["state"] for job in status.get("jobs", []))
    summary = ", ".join(f"{n} {state}" for state, n in sorted(states.items()))
    workers = ", ".join(
        f"{w['name']} {w['state']}" for w in status.get("workers", [])
    )
    print(f"pool {pool.get('state', '?')} (pid {pool.get('pid', '?')}): {summary}",
          file=sys.stderr)
    if workers:
        print(f"workers: {workers}", file=sys.stderr)
    return EXIT_OK


def _named_sessions(names: str, directory: Path) -> list[SessionInfo]:
    wanted = [n for n in names.split(",") if n]
    if not wanted:
        raise PoolError("--sessions names no session")
    if len(set(wanted)) != len(wanted):
        raise PoolError("--sessions names a session twice")
    infos = []
    for name in wanted:
        info = registry.find(name, directory)
        if info is None:
            raise RegistryError(f"no session named {name!r} in {directory}")
        status = registry.probe(info)
        if status != registry.IDLE:
            # A request would queue behind what it is running, and a job
            # timeout would then stop somebody else's script.
            raise RegistryError(
                f"session '{name}' is {status}; the pool only takes idle sessions"
            )
        infos.append(info)
    return infos


def _start_workers(pool: Pool, args: argparse.Namespace) -> None:
    """Start the pool's own sessions, one after another.

    Each start waits for the memory floor like a dispatch does. A start that
    fails ends the series; the pool runs with what came up before it.
    """
    prefix = args.prefix or f"pool{os.getpid()}"
    for index in range(1, args.workers + 1):
        name = f"{prefix}-{index}"
        if pool.stopping.is_set():
            return
        if not pool.wait_memory(f"start of {name}"):
            if not pool.stopping.is_set():
                pool.log(f"not starting {name}: {pool._memory_message()}")
            return
        try:
            info = start_session(
                name,
                vivado=args.vivado,
                version=args.version,
                cwd=args.cwd,
                wait=args.start_wait,
                extra_args=args.arg,
                directory=pool.directory,
                idle_stop=args.idle_stop,
            )
        except (LaunchError, RegistryError) as exc:
            pool.log(f"not starting {name}: {exc}")
            return
        pool.workers.append(Worker(info=info, owned=True))
        pool.log(f"started {name} (pid {info.pid})")


def cmd_run(args: argparse.Namespace) -> int:
    lines = list(args.job)
    if args.jobs:
        try:
            text = sys.stdin.read() if args.jobs == "-" else Path(args.jobs).read_text("utf-8")
        except OSError as exc:
            return die(f"cannot read {args.jobs}: {exc}")
        lines += text.splitlines()
    init = None
    try:
        jobs = parse_jobs(lines)
        if args.init:
            try:
                init = Path(args.init).read_text(encoding="utf-8")
            except OSError as exc:
                raise PoolError(f"cannot read {args.init}: {exc}") from None
        if args.workers is not None and args.workers < 1:
            raise PoolError("--workers needs at least 1")
        if args.prefix and not registry.NAME_RE.match(f"{args.prefix}-1"):
            raise PoolError(f"invalid --prefix {args.prefix!r}")
    except PoolError as exc:
        return die(str(exc))

    if registry.sandboxed():
        return die(f"not starting a pool: {registry.SANDBOX_HINT}", EXIT_NO_WORKERS)

    out = Path(args.out) if args.out else Path(time.strftime("vrc-pool-%Y%m%d-%H%M%S"))
    pool = Pool(
        jobs,
        out,
        min_free_gb=args.min_free,
        mem_wait=args.mem_wait,
        stagger=args.stagger,
        max_runs=args.max_runs,
        timeout=args.timeout,
        retries=args.retries,
        init=init,
        quiet_lines=args.json,
    )

    def on_signal(signum, frame) -> None:
        pool.stopping.set()

    previous = {
        sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if args.sessions:
            try:
                infos = _named_sessions(args.sessions, pool.directory)
                pool.workers = [Worker(info=i) for i in infos]
            except PoolError as exc:
                return die(str(exc))
            except RegistryError as exc:
                return die(str(exc), EXIT_NO_WORKERS)
        else:
            _start_workers(pool, args)
            if pool.stopping.is_set():
                for worker in pool.workers:
                    pool._stop_owned(worker)
                return EXIT_INTERRUPTED
            if not pool.workers:
                return die("no worker could be started", EXIT_NO_WORKERS)
            if len(pool.workers) < args.workers:
                pool.log(f"running with {len(pool.workers)} of {args.workers} workers")

        pool.log(
            f"{len(jobs)} job(s) on {len(pool.workers)} session(s); results in {out}"
        )
        code = pool.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    if args.json:
        print(json.dumps(pool.status(), indent=1))
    states = collections.Counter(job.state for job in jobs)
    pool.log(", ".join(f"{n} {state}" for state, n in sorted(states.items())))
    return code


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE
