"""The full launch path, driven through the fake-Vivado shim.

This is the test that matters most: it exercises start -> readiness poll ->
eval -> stop including setsid, log redirection and killpg, with no Vivado.
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import FAKE_VIVADO, tcl_env

from vivado_remotectl import launcher, registry
from vivado_remotectl.client import Session
from vivado_remotectl.launcher import LaunchError


@pytest.fixture
def fake_vivado_env(monkeypatch, tclsh):
    for key, value in tcl_env(tclsh).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("VRC_TEST_TCLSH", tclsh)
    return str(FAKE_VIVADO)


@pytest.fixture
def started(registry_dir, fake_vivado_env):
    """A session launched exactly the way `vrc start` launches one."""
    info = launcher.start_session(
        "unit", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    yield info
    if registry.pid_alive(info.pid):
        launcher.stop_session(info, force=True)


def test_plugin_path_ships_inside_the_package():
    path = launcher.plugin_path()
    assert path.is_file()
    assert path.name == "vivado_remotectl.tcl"


def test_child_env_identifies_the_session(tmp_path):
    env = launcher.child_env("x", 0, tmp_path)
    # Prompt mode, not a vwait: the only loop in which SIGINT cancels.
    assert env["VRC_SERVE"] == "prompt"
    assert env["VRC_NAME"] == "x"


def test_child_env_does_not_vwait_for_a_gui(tmp_path):
    assert launcher.child_env("x", 0, tmp_path, serve=False)["VRC_SERVE"] == "0"


def test_find_vivado_rejects_a_bad_explicit_path():
    with pytest.raises(LaunchError, match="not executable"):
        launcher.find_vivado("/nonexistent/vivado")


def test_find_vivado_reports_available_versions():
    with pytest.raises(LaunchError, match="no Vivado 1999.1"):
        launcher.find_vivado(version="1999.1")


def test_start_refuses_inside_the_sandbox(registry_dir, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    with pytest.raises(LaunchError, match="sandboxed"):
        launcher.start_session("boxed", directory=registry_dir)
    assert not (registry_dir / "boxed.session").exists()


def test_start_brings_a_session_up(started, registry_dir):
    assert started.port > 0
    assert registry.pid_alive(started.pid)
    assert (registry_dir / "unit.session").exists()
    with Session(started) as client:
        assert client.run("expr {2 + 2}") == "4"


def test_start_writes_a_log(started):
    assert started.log_path.exists()
    assert "vrc start unit" in started.log_path.read_text()


def test_start_refuses_a_duplicate_name(started, fake_vivado_env, registry_dir):
    with pytest.raises(LaunchError, match="already running"):
        launcher.start_session(
            "unit", vivado=fake_vivado_env, wait=30.0, directory=registry_dir
        )


def test_start_reclaims_a_dead_descriptor(registry_dir, fake_vivado_env):
    (registry_dir / "zombie.session").write_text(
        "\n".join(
            [
                "v 1",
                "name zombie",
                "host 127.0.0.1",
                "port 1",
                f"pid {2**22}",
                f"token {'a' * 32}",
                "started 1",
                "cwd /",
                "",
            ]
        )
    )
    info = launcher.start_session(
        "zombie", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    try:
        assert info.port > 1
    finally:
        launcher.stop_session(info, force=True)


def test_two_sessions_get_different_ports(registry_dir, fake_vivado_env):
    first = launcher.start_session(
        "a", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    second = launcher.start_session(
        "b", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    try:
        assert first.port != second.port
        with Session(first) as c1, Session(second) as c2:
            c1.run("set ::who a")
            c2.run("set ::who b")
            # Separate interpreters, so no shared state.
            assert c1.run("set ::who") == "a"
            assert c2.run("set ::who") == "b"
    finally:
        launcher.stop_session(first, force=True)
        launcher.stop_session(second, force=True)


def test_start_on_a_busy_explicit_port_fails_with_the_log(registry_dir, fake_vivado_env):
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]
    try:
        with pytest.raises(LaunchError, match="did not come up|cannot listen"):
            launcher.start_session(
                "clash",
                vivado=fake_vivado_env,
                port=busy_port,
                wait=15.0,
                directory=registry_dir,
            )
    finally:
        holder.close()


def test_start_fails_when_the_executable_dies(registry_dir, tmp_path):
    dud = tmp_path / "dud.sh"
    dud.write_text("#!/bin/bash\nexit 3\n")
    dud.chmod(0o755)
    with pytest.raises(LaunchError):
        launcher.start_session(
            "dud", vivado=str(dud), wait=15.0, directory=registry_dir
        )


def test_stop_is_graceful_and_cleans_up(registry_dir, fake_vivado_env):
    info = launcher.start_session(
        "bye", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    outcome = launcher.stop_session(info, timeout=20.0)
    assert outcome == "stopped"
    assert not registry.pid_alive(info.pid)
    assert not (registry_dir / "bye.session").exists()


def test_stop_force_kills_the_process_group(registry_dir, fake_vivado_env):
    info = launcher.start_session(
        "hard", vivado=fake_vivado_env, wait=60.0, directory=registry_dir
    )
    assert launcher.stop_session(info, force=True) == "killed"
    assert not registry.pid_alive(info.pid)
    assert not (registry_dir / "hard.session").exists()


def test_started_session_is_under_the_prompt_loop(started):
    # It answers at all only because tclsh keeps reading ::vrc::serve lines;
    # the plugin does not vwait in prompt mode.
    assert started.cancel == "sigint"
    with Session(started) as client:
        assert client.run("set ::vrc::serve_mode") == "prompt"


def test_cancel_an_idle_session_sends_nothing(started):
    assert launcher.cancel_session(started, wait=1.0) == "idle"
    assert not launcher.cancel_marker(started).exists()
    with Session(started) as client:
        assert client.run("expr {1 + 1}") == "2"


def test_cancel_refuses_a_session_without_the_prompt_loop(registry_dir):

    info = registry.SessionInfo(
        name="gui", host="127.0.0.1", port=1, pid=os.getpid(), token="a" * 32,
        starttime=registry.proc_starttime(os.getpid()),
    )
    # A vwait or GUI session: SIGINT there wedges it, so never send one.
    with pytest.raises(launcher.CancelError, match="cannot be cancelled"):
        launcher.cancel_session(info)


def test_cancel_refuses_inside_the_sandbox(started, monkeypatch):
    # The sandbox has its own pid namespace; the pid check would only say
    # "gone", which reads as a dead session.
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    with pytest.raises(launcher.CancelError, match="sandboxed"):
        launcher.cancel_session(started)


def test_cancel_refuses_a_recycled_pid(registry_dir, monkeypatch):
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    import subprocess

    victim = subprocess.Popen(["sleep", "30"])
    try:
        info = registry.SessionInfo(
            name="stale", host="127.0.0.1", port=1, pid=victim.pid,
            token="a" * 32, starttime="1", cancel="sigint",
        )
        with pytest.raises(launcher.CancelError, match="no longer"):
            launcher.cancel_session(info)
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_log_tail_returns_the_last_lines(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("\n".join(str(i) for i in range(100)))
    assert launcher.log_tail(path, 3).splitlines() == ["97", "98", "99"]


def test_log_tail_of_a_missing_file_is_empty(tmp_path):
    assert launcher.log_tail(tmp_path / "nope.log") == ""


# -- never signal a process we did not start ---------------------------------


def test_owns_pid_rejects_a_recycled_pid(registry_dir):
    """The pid is alive but the recorded start time does not match."""
    import subprocess

    victim = subprocess.Popen(["sleep", "30"])
    try:
        info = registry.SessionInfo(
            name="impostor",
            host="127.0.0.1",
            port=1,
            pid=victim.pid,
            token="a" * 32,
            starttime="1",  # deliberately wrong
        )
        assert registry.pid_alive(victim.pid)
        assert not registry.owns_pid(info)
        assert launcher._signal_target(victim.pid, "1") is None
    finally:
        victim.kill()
        victim.wait()


def test_stop_refuses_to_kill_a_recycled_pid(registry_dir):
    """A stale descriptor must not take down an unrelated process.

    This is the failure that would otherwise kill somebody's real Vivado:
    the session exits, the kernel reuses its pid, and `vrc stop` aims a
    process-group kill at whatever inherited the number.
    """
    import subprocess

    victim = subprocess.Popen(["sleep", "30"])
    try:
        info = registry.SessionInfo(
            name="stale",
            host="127.0.0.1",
            port=1,
            pid=victim.pid,
            token="a" * 32,
            starttime="1",  # not this process
            path=registry_dir / "stale.session",
        )
        (registry_dir / "stale.session").write_text("v 1\n")
        launcher.stop_session(info, timeout=1.0)
        assert victim.poll() is None, "stop_session killed an unrelated process"
    finally:
        victim.kill()
        victim.wait()


def test_signal_target_uses_the_group_only_for_a_group_leader():
    """We create sessions with start_new_session, so ours lead their group."""
    import subprocess

    own = subprocess.Popen(["sleep", "30"], start_new_session=True)
    child = subprocess.Popen(["sleep", "30"])  # shares our group
    try:
        assert launcher._signal_target(
            own.pid, registry.proc_starttime(own.pid)
        ) == -own.pid
        # Not a group leader: signal the single pid, never the group, or we
        # would take out the caller's own process group.
        assert launcher._signal_target(
            child.pid, registry.proc_starttime(child.pid)
        ) == child.pid
    finally:
        for proc in (own, child):
            proc.kill()
            proc.wait()


def test_signal_target_refuses_an_unverifiable_descriptor():

    assert launcher._signal_target(os.getpid(), "") is None


def test_stop_reports_when_it_cannot_verify(registry_dir):
    import subprocess

    victim = subprocess.Popen(["sleep", "30"])
    try:
        info = registry.SessionInfo(
            name="old",
            host="127.0.0.1",
            port=1,
            pid=victim.pid,
            token="a" * 32,
            starttime="",  # descriptor from before we recorded it
            path=registry_dir / "old.session",
        )
        (registry_dir / "old.session").write_text("v 1\n")
        outcome = launcher.stop_session(info, timeout=1.0)
        assert "cannot verify" in outcome
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


# -- restart -------------------------------------------------------------------


def test_start_writes_a_launch_record(registry_dir, fake_vivado_env, tmp_path):
    info = launcher.start_session(
        "rec", vivado=fake_vivado_env, wait=60.0, directory=registry_dir,
        cwd=str(tmp_path), extra_args=["-notrace"], idle_stop=3600,
    )
    try:
        record = launcher.read_launch_record(info)
        assert record == launcher.LaunchRecord(
            vivado=fake_vivado_env, cwd=str(tmp_path), extra_args=["-notrace"],
            idle_stop=3600, wait=60.0,
        )
    finally:
        launcher.stop_session(info, force=True)
    assert not (registry_dir / "rec.launch").exists()


def test_restart_an_idle_session(started, registry_dir, tmp_path):
    new, was = launcher.restart_session(started)
    try:
        assert was == "idle"
        assert new.name == started.name
        assert new.pid != started.pid
        assert not registry.pid_alive(started.pid)
        assert new.owner == started.owner
        assert launcher.read_launch_record(new) == launcher.read_launch_record(started)
        with Session(new) as client:
            assert client.run("expr {3 * 3}") == "9"
    finally:
        launcher.stop_session(new, force=True)


def test_restart_kills_a_running_command(started):
    with Session(started) as client:
        client.send("after 60000")
    deadline = time.monotonic() + 10
    while registry.probe(started) != registry.BUSY and time.monotonic() < deadline:
        time.sleep(0.1)
    t0 = time.monotonic()
    new, was = launcher.restart_session(started)
    try:
        assert was == "busy"
        # Killed at once, not left to the 20 s graceful exit.
        assert time.monotonic() - t0 < 15
        assert registry.probe(new) == registry.IDLE
    finally:
        launcher.stop_session(new, force=True)


def test_restart_a_session_that_died(started):
    launcher._terminate(started.pid, started.starttime)
    new, was = launcher.restart_session(started)
    try:
        assert was == "dead"
        assert registry.probe(new) == registry.IDLE
    finally:
        launcher.stop_session(new, force=True)


def test_start_gives_the_session_its_own_temp_dir(started, registry_dir):
    tmp = registry_dir / "unit.tmp"
    assert tmp.is_dir()
    assert f"-tempDir {tmp}" in (registry_dir / "unit.log").read_text()
    (tmp / "scratch").write_text("x")
    launcher.stop_session(started, force=True)
    assert not tmp.exists()


def test_a_tempdir_argument_replaces_the_default(registry_dir, fake_vivado_env, tmp_path):
    info = launcher.start_session(
        "own", directory=registry_dir, cwd=str(tmp_path),
        extra_args=["-tempDir", str(tmp_path)],
    )
    try:
        assert not (registry_dir / "own.tmp").exists()
    finally:
        launcher.stop_session(info, force=True)
    assert tmp_path.is_dir()


def test_restart_refuses_without_a_launch_record(started, registry_dir):
    (registry_dir / "unit.launch").unlink()
    with pytest.raises(launcher.RestartError, match="no launch record"):
        launcher.restart_session(started)
    assert registry.probe(started) == registry.IDLE


def test_restart_refuses_inside_the_sandbox(started, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    with pytest.raises(launcher.RestartError, match="sandboxed"):
        launcher.restart_session(started)


def test_restart_that_cannot_start_says_how_to(started, registry_dir):
    def refuse():
        raise launcher.RestartError("not enough memory")

    with pytest.raises(launcher.RestartError) as exc:
        launcher.restart_session(started, before_start=refuse)
    msg = str(exc.value)
    assert "stopped, but not started again: not enough memory" in msg
    assert "start it with: vrc start unit --vivado " in msg
    assert not registry.pid_alive(started.pid)
    assert not (registry_dir / "unit.session").exists()


def test_start_command_round_trips_the_options():
    record = launcher.LaunchRecord(
        vivado="/opt/v/vivado", cwd="/w d", port=4000, extra_args=["-tclargs", "a b"],
        idle_stop=7200,
    )
    assert launcher.start_command("w0", record) == (
        "vrc start w0 --vivado /opt/v/vivado --cwd '/w d' --port 4000 "
        "--idle-stop 7200s --arg=-tclargs '--arg=a b'"
    )
