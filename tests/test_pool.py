"""`vrc pool run`: jobs across several sessions, on the fake Vivado."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import FAKE_VIVADO, INTERPRETERS, REPO, _interpreter, tcl_env

from vivado_remotectl import launcher, pool, registry
from vivado_remotectl.cli import main

AWKWARD = 'two words {x [exit] $y "q" \\ ;#'


def _fake_env(monkeypatch, tclsh: str, extra: dict[str, str]) -> None:
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("VRC_TEST_TCLSH", tclsh)
    monkeypatch.setattr(pool.Pool, "poll", 0.05)


def _stop_all(directory: Path) -> None:
    for info in registry.list_sessions(directory):
        if registry.owns_pid(info):
            launcher.stop_session(info, force=True)


@pytest.fixture
def fake(registry_dir, monkeypatch):
    """The fake Vivado on one interpreter; most pool logic is Python."""
    for _id, release, tcl, fallback in INTERPRETERS:
        found = _interpreter(release, tcl, fallback)
        if found is not None:
            break
    else:
        pytest.skip("no tclsh available")
    _fake_env(monkeypatch, *found)
    yield registry_dir
    _stop_all(registry_dir)


@pytest.fixture
def fake_both(registry_dir, monkeypatch, tclsh):
    """The same, once per interpreter Vivado ships."""
    extra = {k: v for k, v in tcl_env(tclsh).items() if os.environ.get(k) != v}
    _fake_env(monkeypatch, tclsh, extra)
    yield registry_dir
    _stop_all(registry_dir)


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def run_pool(tmp_path: Path, *argv: str, workers: int = 2) -> tuple[int, Path]:
    out = tmp_path / "out"
    who = [] if "--sessions" in argv else ["--workers", str(workers)]
    code = main(
        ["pool", "run", *who, "--vivado", str(FAKE_VIVADO), "--min-free", "0",
         "--out", str(out), *argv]
    )
    return code, out


def status_of(out: Path) -> dict:
    return json.loads((out / "status.json").read_text())


def jobs_of(out: Path) -> dict[str, dict]:
    return {job["name"]: job for job in status_of(out)["jobs"]}


# -- the job list --------------------------------------------------------------


def test_job_lines_are_split_like_a_shell_and_named(tmp_path):
    a = script(tmp_path, "synth.tcl", "return a")
    b = script(tmp_path, "route.tcl", "return b")
    jobs = pool.parse_jobs(
        ["# a comment", "", f"{a} top_a 'two words'", f"{a} top_b  # trailing",
         f"name=last {b}", f"{b}"]
    )
    assert [j.name for j in jobs] == ["synth-1", "synth-2", "last", "route"]
    assert jobs[0].args == ["top_a", "two words"]
    assert jobs[1].args == ["top_b"]
    assert jobs[0].text == "return a"


@pytest.mark.parametrize(
    "lines, message",
    [
        ([], "no jobs"),
        (["/nonexistent/x.tcl"], "cannot read"),
        (["name=a/b x.tcl"], "invalid name"),
        (["name=only"], "no script"),
        (["'unbalanced"], "quotation"),
    ],
)
def test_bad_job_lines_are_refused(lines, message):
    with pytest.raises(pool.PoolError, match=message):
        pool.parse_jobs(lines)


def test_a_job_name_used_twice_is_refused(tmp_path):
    a = script(tmp_path, "a.tcl", "return a")
    with pytest.raises(pool.PoolError, match="more than once: x"):
        pool.parse_jobs([f"name=x {a}", f"name=x {a}"])


def test_a_bad_job_list_is_usage_before_any_session_starts(fake, tmp_path, capsys):
    code, out = run_pool(tmp_path, "/nonexistent/x.tcl")
    assert code == pool.EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err
    assert not registry.list_sessions(fake)


def test_pool_refuses_inside_the_sandbox(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    code, _ = run_pool(tmp_path, script(tmp_path, "a.tcl", "return a"))
    assert code == pool.EXIT_NO_WORKERS
    assert "sandboxed" in capsys.readouterr().err


# -- running -------------------------------------------------------------------


def test_every_job_runs_once_and_its_result_lands_in_a_file(fake_both, tmp_path, capsys):
    ran = tmp_path / "ran.txt"
    job = script(
        tmp_path, "job.tcl",
        'set f [open [lindex $::argv 1] a]; puts $f [lindex $::argv 0]; close $f\n'
        'after 150\nreturn "r=[lindex $::argv 0] third=[lindex $::argv 2]"\n',
    )
    jobs_file = tmp_path / "jobs.txt"
    jobs_file.write_text(
        "".join(f"name=j{i} {job} {i} {ran} '{AWKWARD}'\n" for i in range(5))
    )

    # status.json must parse whenever somebody looks.
    out = tmp_path / "out"
    bad: list[str] = []
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            try:
                json.loads((out / "status.json").read_text())
            except FileNotFoundError:
                pass
            except ValueError as exc:
                bad.append(str(exc))
            time.sleep(0.01)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        code, out = run_pool(tmp_path, "--jobs", str(jobs_file), f"name=j5 {job} 5 {ran} x")
    finally:
        done.set()
        watcher.join()

    assert code == pool.EXIT_OK
    assert not bad
    assert sorted(ran.read_text().split()) == ["0", "1", "2", "3", "4", "5"]
    assert (out / "j3.out").read_text() == f"r=3 third={AWKWARD}\n"
    assert not (out / "j3.err").exists()

    lines = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert sorted(line[0] for line in lines) == [f"j{i}" for i in range(6)]
    assert {line[2] for line in lines} == {"ok"}
    assert len({line[1] for line in lines}) == 2  # both workers took jobs

    status = status_of(out)
    assert status["pool"]["state"] == "done"
    assert all(j["state"] == "ok" and j["attempts"] == 1 for j in status["jobs"])
    assert sum(w["total"] for w in status["workers"]) == 6
    # The pool's own sessions are gone when it returns.
    assert not registry.list_sessions(fake_both)


def test_a_tcl_error_fails_only_its_job(fake, tmp_path, capsys):
    good = script(tmp_path, "good.tcl", "return fine")
    bad = script(tmp_path, "bad.tcl", "error boom")
    code, out = run_pool(tmp_path, good, bad, f"name=again {good}", workers=1)
    assert code == pool.EXIT_FAILED
    jobs = jobs_of(out)
    assert [jobs[n]["state"] for n in ("good", "bad", "again")] == ["ok", "error", "ok"]
    assert "boom" in (out / "bad.err").read_text()
    assert not (out / "bad.out").exists()
    assert status_of(out)["pool"]["state"] == "failed"
    assert "bad" in capsys.readouterr().err


def test_json_prints_the_final_status_instead_of_lines(fake, tmp_path, capsys):
    code, out = run_pool(tmp_path, "--json", script(tmp_path, "a.tcl", "return a"), workers=1)
    assert code == pool.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed == status_of(out)
    assert printed["jobs"][0]["state"] == "ok"


def test_a_job_past_its_timeout_fails_and_its_session_is_restarted(fake, tmp_path):
    slow = script(tmp_path, "slow.tcl", "after 30000")
    quick = script(tmp_path, "quick.tcl", "return [pid]")
    code, out = run_pool(tmp_path, "--timeout", "1", slow, quick, workers=1)
    assert code == pool.EXIT_FAILED
    jobs = jobs_of(out)
    assert jobs["slow"]["state"] == "timeout"
    assert jobs["slow"]["attempts"] == 1  # not retried
    assert jobs["quick"]["state"] == "ok"
    assert status_of(out)["workers"][0]["restarts"] == 1


DIE_ONCE = (
    "set marker [lindex $::argv 0]\n"
    "if {![file exists $marker]} { close [open $marker w]; exec kill -9 [pid] }\n"
    "return survived\n"
)


def test_the_job_of_a_session_that_died_is_run_again(fake, tmp_path, capsys):
    die = script(tmp_path, "die.tcl", DIE_ONCE)
    code, out = run_pool(tmp_path, f"{die} {tmp_path / 'marker'}", workers=1)
    assert code == pool.EXIT_OK
    job = jobs_of(out)["die"]
    assert job["state"] == "ok" and job["attempts"] == 2
    assert (out / "die.out").read_text() == "survived\n"
    assert status_of(out)["workers"][0]["restarts"] == 1
    assert "queued again" in capsys.readouterr().err


def test_a_job_that_keeps_killing_its_session_fails_as_died(fake, tmp_path):
    die = script(tmp_path, "die.tcl", "exec kill -9 [pid]")
    after = script(tmp_path, "after.tcl", "return ok")
    code, out = run_pool(tmp_path, "--retries", "1", die, after, workers=1)
    assert code == pool.EXIT_FAILED
    jobs = jobs_of(out)
    assert jobs["die"]["state"] == "died" and jobs["die"]["attempts"] == 2
    assert "last lines of" in (out / "die.err").read_text()
    # The worker came back each time and still ran the job behind it.
    assert jobs["after"]["state"] == "ok"


def test_max_runs_restarts_a_session_between_jobs(fake, tmp_path):
    job = script(tmp_path, "pid.tcl", "return [pid]")
    code, out = run_pool(tmp_path, "--max-runs", "1", job, job, job, workers=1)
    assert code == pool.EXIT_OK
    pids = {(out / f"pid-{i}.out").read_text() for i in (1, 2, 3)}
    assert len(pids) == 3
    worker = status_of(out)["workers"][0]
    # No restart after the last job: nothing is left to run in it.
    assert worker["restarts"] == 2 and worker["total"] == 3


def test_init_runs_in_every_session_and_again_after_a_restart(fake, tmp_path):
    init = script(tmp_path, "init.tcl", "proc primed {} { return primed }")
    job = script(tmp_path, "job.tcl", "return [primed]")
    code, out = run_pool(tmp_path, "--init", init, "--max-runs", "1", job, job, workers=1)
    assert code == pool.EXIT_OK
    assert (out / "job-2.out").read_text() == "primed\n"


def test_a_failing_init_takes_the_worker_out_and_fails_what_is_left(fake, tmp_path, capsys):
    init = script(tmp_path, "init.tcl", "error {no licence}")
    job = script(tmp_path, "job.tcl", "return x")
    code, out = run_pool(tmp_path, "--init", init, job, workers=1)
    assert code == pool.EXIT_FAILED
    assert jobs_of(out)["job"]["state"] == "no-worker"
    assert status_of(out)["workers"][0]["state"] == "gone"
    assert "no licence" in capsys.readouterr().err
    assert not registry.list_sessions(fake)


# -- memory --------------------------------------------------------------------


def test_a_dispatch_waits_for_memory(fake, tmp_path, monkeypatch, capsys):
    job = script(tmp_path, "job.tcl", "return x")
    looks = []

    def available():
        looks.append(1)
        return (1 if len(looks) < 4 else 8) * 2**30

    info = launcher.start_session("lent", vivado=str(FAKE_VIVADO), wait=60.0, directory=fake)
    monkeypatch.setattr("vivado_remotectl.procinfo.mem_available", available)
    out = tmp_path / "out2"
    code = main(["pool", "run", "--sessions", "lent", "--min-free", "6", "--out", str(out), job])
    assert code == pool.EXIT_OK
    assert len(looks) >= 4
    assert "waiting for 6.0G" in capsys.readouterr().err
    # A lent session is left running.
    assert registry.owns_pid(info)


def test_a_job_that_never_gets_memory_fails_and_says_who_holds_it(
    fake, tmp_path, monkeypatch, capsys
):
    launcher.start_session("lent", vivado=str(FAKE_VIVADO), wait=60.0, directory=fake)
    monkeypatch.setattr("vivado_remotectl.procinfo.mem_available", lambda: 2**30)
    monkeypatch.setattr("vivado_remotectl.procinfo.stray_vivados", lambda *a, **k: [])
    job = script(tmp_path, "job.tcl", "return x")
    out = tmp_path / "out"
    code = main(
        ["pool", "run", "--sessions", "lent", "--min-free", "6", "--mem-wait", "1",
         "--out", str(out), job]
    )
    assert code == pool.EXIT_FAILED
    assert jobs_of(out)["job"]["state"] == "no-memory"
    text = (out / "job.err").read_text()
    assert "1.0G available" in text and "holding memory: lent" in text


def test_no_worker_is_started_below_the_memory_floor(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("vivado_remotectl.procinfo.mem_available", lambda: 2**30)
    out = tmp_path / "out"
    code = main(
        ["pool", "run", "--workers", "2", "--vivado", str(FAKE_VIVADO), "--min-free", "6",
         "--mem-wait", "1", "--out", str(out), script(tmp_path, "job.tcl", "return x")]
    )
    assert code == pool.EXIT_NO_WORKERS
    assert "no worker could be started" in capsys.readouterr().err
    assert not registry.list_sessions(fake)


# -- sessions that are lent ----------------------------------------------------


def test_named_sessions_must_exist_and_be_idle(fake, tmp_path, capsys):
    job = script(tmp_path, "job.tcl", "return x")
    assert run_pool(tmp_path, "--sessions", "nope", job)[0] == pool.EXIT_NO_WORKERS
    assert "no session named 'nope'" in capsys.readouterr().err

    info = launcher.start_session("lent", vivado=str(FAKE_VIVADO), wait=60.0, directory=fake)
    assert main(["-s", "lent", "--detach", "-e", "after 3000"]) == 0
    assert run_pool(tmp_path, "--sessions", "lent", job)[0] == pool.EXIT_NO_WORKERS
    assert "only takes idle sessions" in capsys.readouterr().err
    assert registry.owns_pid(info)


def test_pool_status_prints_the_table(fake, tmp_path, capsys):
    code, out = run_pool(tmp_path, script(tmp_path, "a.tcl", "return a"), workers=1)
    capsys.readouterr()
    assert main(["pool", "status", str(out)]) == 0
    printed = capsys.readouterr()
    assert printed.out.splitlines()[1].split()[:3] == ["a", "pool" + str(os.getpid()) + "-1", "ok"]
    assert "pool done" in printed.err
    assert main(["pool", "status", str(tmp_path / "nowhere")]) == pool.EXIT_NO_WORKERS


# -- interrupt -----------------------------------------------------------------


def _wait_for(condition, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition never came true")


def test_sigterm_stops_the_pools_sessions_and_reports_the_jobs(fake, tmp_path):
    slow = script(tmp_path, "slow.tcl", "after 60000")
    out = tmp_path / "out"
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "vivado_remotectl", "pool", "run", "--workers", "2",
         "--prefix", "term", "--vivado", str(FAKE_VIVADO), "--min-free", "0",
         "--out", str(out), slow, slow, slow],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        def two_running():
            try:
                states = [j["state"] for j in status_of(out)["jobs"]]
            except (OSError, ValueError):
                return False
            return states.count("running") == 2

        _wait_for(two_running)
        pids = [info.pid for info in registry.list_sessions(fake)]
        assert len(pids) == 2
        proc.send_signal(signal.SIGTERM)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == pool.EXIT_INTERRUPTED, stderr
    assert not registry.list_sessions(fake)
    _wait_for(lambda: not any(registry.pid_alive(pid) for pid in pids))
    status = status_of(out)
    assert status["pool"]["state"] == "interrupted"
    assert sorted(j["state"] for j in status["jobs"]) == ["cancelled", "cancelled", "queued"]


def test_an_interrupt_leaves_a_lent_session_running(fake, tmp_path):
    info = launcher.start_session("lent", vivado=str(FAKE_VIVADO), wait=60.0, directory=fake)
    slow = script(tmp_path, "slow.tcl", "after 60000")
    out = tmp_path / "out"
    jobs = pool.parse_jobs([slow])
    p = pool.Pool(jobs, out, min_free_gb=0, directory=fake)
    p.cancel_wait = 2.0
    p.workers = [pool.Worker(info=info)]
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(p.run()))
    runner.start()
    _wait_for(lambda: jobs[0].state == "running")
    p.stopping.set()
    runner.join(timeout=30)
    assert result == [pool.EXIT_INTERRUPTED]
    # The fake session dies of the SIGINT a real Vivado turns into a cancel;
    # either way the pool has not stopped it and has removed nothing of it.
    assert jobs[0].state == "cancelled"
    assert (fake / "lent.launch").exists()
