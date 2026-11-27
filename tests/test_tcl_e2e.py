"""End-to-end against the real plugin on the tclsh8.5 and tclsh8.6 Vivado ships."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from vivado_remotectl.client import Session
from vivado_remotectl.protocol import (
    FrameReader,
    RemoteTclError,
    encode_request,
    read_response,
)


def raw_connect(info):
    sock = socket.create_connection((info.host, info.port), timeout=10)
    return sock, FrameReader(sock)


# -- descriptor --------------------------------------------------------------


def test_descriptor_is_complete_and_private(tcl_session, registry_dir):
    info = tcl_session.info
    path = registry_dir / "t.session"
    assert info.port > 0
    assert len(info.token) == 32
    assert info.tcl.startswith(("8.5", "8.6"))
    assert oct(path.stat().st_mode)[-3:] == "600"


# -- happy paths -------------------------------------------------------------


def test_simple_expression(tcl_session):
    with Session(tcl_session.info) as client:
        assert client.run("expr {1 + 1}") == "2"


def test_multi_line_script(tcl_session):
    script = "set a 3\nset b 4\nexpr {$a * $b}"
    with Session(tcl_session.info) as client:
        assert client.run(script) == "12"


def test_return_is_treated_as_success(tcl_session):
    # `return "WNS=..."` is the documented idiom for getting a value out of a
    # script, and Tcl reports it as return code 2 rather than 0.
    with Session(tcl_session.info) as client:
        assert client.run('return "WNS=-0.123"') == "WNS=-0.123"


def test_state_persists_across_requests(tcl_session):
    with Session(tcl_session.info) as client:
        client.run("set ::kept 42")
        assert client.run("set ::kept") == "42"


def test_many_requests_on_one_connection(tcl_session):
    with Session(tcl_session.info) as client:
        for i in range(25):
            assert client.run(f"expr {{{i} * 2}}") == str(i * 2)


def test_utf8_round_trip(tcl_session):
    with Session(tcl_session.info) as client:
        assert client.run('return "€ äöü"') == "€ äöü"


def test_large_payload(tcl_session):
    with Session(tcl_session.info) as client:
        out = client.run("string repeat x 20000000")
    assert len(out) == 20_000_000


def test_large_script_upload(tcl_session):
    script = "set big {" + ("y" * 2_000_000) + "}\nstring length $big"
    with Session(tcl_session.info) as client:
        assert client.run(script) == "2000000"


def test_empty_result(tcl_session):
    with Session(tcl_session.info) as client:
        assert client.run("set _ {}") == ""


# -- errors ------------------------------------------------------------------


def test_tcl_error_carries_errorinfo(tcl_session):
    with Session(tcl_session.info) as client:
        with pytest.raises(RemoteTclError) as excinfo:
            client.run("error {boom}")
    assert "boom" in excinfo.value.errorinfo
    assert "while executing" in excinfo.value.errorinfo


def test_errorinfo_does_not_leak_our_plumbing(tcl_session):
    """The traceback should stop at the user's script.

    Everything runs through `uplevel #0 $script`, and those frames appear in
    every errorInfo without telling the reader anything.
    """
    with Session(tcl_session.info) as client:
        with pytest.raises(RemoteTclError) as excinfo:
            client.run("proc boom {} { error inner }\nboom")
    trace = excinfo.value.errorinfo
    assert "inner" in trace
    assert "boom" in trace
    assert "uplevel #0" not in trace


def test_session_survives_an_error(tcl_session):
    with Session(tcl_session.info) as client:
        with pytest.raises(RemoteTclError):
            client.run("no_such_command")
        assert client.run("expr 1") == "1"


# -- framing -----------------------------------------------------------------


def test_request_split_across_several_sends(tcl_session):
    frame = encode_request("expr {6 * 7}", tcl_session.info.token)
    sock, reader = raw_connect(tcl_session.info)
    try:
        for i in range(0, len(frame), 4):
            sock.sendall(frame[i : i + 4])
            time.sleep(0.01)
        assert read_response(reader) == ("ok", "42")
    finally:
        sock.close()


def test_pipelined_requests_in_one_send(tcl_session):
    token = tcl_session.info.token
    payload = (
        encode_request("expr 1", token)
        + encode_request("expr 2", token)
        + encode_request("expr 3", token)
    )
    sock, reader = raw_connect(tcl_session.info)
    try:
        sock.sendall(payload)
        assert [read_response(reader)[1] for _ in range(3)] == ["1", "2", "3"]
    finally:
        sock.close()


# -- auth and abuse ----------------------------------------------------------


def test_wrong_token_is_refused(tcl_session):
    sock, reader = raw_connect(tcl_session.info)
    try:
        sock.sendall(encode_request("expr 1", "0" * 32))
        status, text = read_response(reader)
        assert status == "proto"
        assert "authentication" in text
    finally:
        sock.close()


def test_server_survives_garbage(tcl_session):
    sock = socket.create_connection(
        (tcl_session.info.host, tcl_session.info.port), timeout=10
    )
    try:
        sock.sendall(b"x" * 100000)
        sock.settimeout(5)
        try:
            sock.recv(4096)
        except (socket.timeout, TimeoutError):
            pass
    finally:
        sock.close()
    with Session(tcl_session.info) as client:
        assert client.run("expr 1") == "1"


def test_server_survives_a_client_vanishing_mid_command(tcl_session):
    sock = socket.create_connection(
        (tcl_session.info.host, tcl_session.info.port), timeout=10
    )
    sock.sendall(encode_request("after 400; expr 1", tcl_session.info.token))
    sock.close()  # gone before the answer is ready; the server must not die
    time.sleep(1.0)
    with Session(tcl_session.info) as client:
        assert client.run("expr 5") == "5"


def test_connect_then_send_nothing_does_not_block_the_server(tcl_session):
    idle = socket.create_connection(
        (tcl_session.info.host, tcl_session.info.port), timeout=10
    )
    try:
        with Session(tcl_session.info) as client:
            assert client.run("expr 9") == "9"
    finally:
        idle.close()


# -- serialisation -----------------------------------------------------------


def test_two_clients_are_serialised_not_interleaved(tcl_session):
    """A slow script must complete before the next client's runs."""
    results: dict[str, str] = {}

    def slow():
        with Session(tcl_session.info) as client:
            results["slow"] = client.run("after 1200; return slow-done")

    def quick():
        time.sleep(0.3)  # land while `slow` is inside its `after`
        with Session(tcl_session.info) as client:
            results["quick"] = client.run("return quick-done")

    threads = [threading.Thread(target=slow), threading.Thread(target=quick)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results == {"slow": "slow-done", "quick": "quick-done"}


def test_reentrant_event_loop_does_not_corrupt_responses(tcl_session):
    """The `busy` guard under test.

    A script that spins the event loop itself (`vwait`, as wait_on_run does)
    lets other clients' readable callbacks fire mid-evaluation. Without the
    guard the two responses interleave on the wire.
    """
    results: dict[str, str] = {}

    def reentrant():
        script = "set ::d 0\nafter 800 {set ::d 1}\nvwait ::d\nreturn reentrant-ok"
        with Session(tcl_session.info) as client:
            results["reentrant"] = client.run(script)

    def hammer():
        time.sleep(0.2)
        with Session(tcl_session.info) as client:
            got = [client.run(f"expr {{{i}}}") for i in range(5)]
        results["hammer"] = ",".join(got)

    threads = [threading.Thread(target=reentrant), threading.Thread(target=hammer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results["reentrant"] == "reentrant-ok"
    assert results["hammer"] == "0,1,2,3,4"


#: A slow script that blocks in `after`, and one that spins the event loop
#: the way wait_on_run does. In the first nothing is read or accepted until it
#: ends; in the second the queued clients are accepted and read meanwhile, so
#: several channels hold complete frames when the script ends.
SLOW_BLOCKING = "after 400; return slow"
SLOW_SPINNING = "set ::d 0; after 400 {set ::d 1}; vwait ::d; return slow"


def _queue_behind_slow(info, slow_script, sends):
    """Run a slow script, then queue one client per entry of `sends`.

    The clients connect one after another, in index order, while the slow
    script runs; `sends` is the order they then send their request in. Each
    request appends its index to ::order. Returns the final ::order.
    """
    n = len(sends)
    slow, slow_reader = raw_connect(info)
    slow.sendall(encode_request(slow_script, info.token))
    time.sleep(0.15)  # let the slow script start before anyone queues
    conns = []
    for _ in range(n):
        conns.append(raw_connect(info))
        time.sleep(0.03)
    for i in sends:
        conns[i][0].sendall(
            encode_request(f"lappend ::order {i}; return {i}", info.token)
        )
        time.sleep(0.02)
    assert read_response(slow_reader) == ("ok", "slow")
    for i, (_sock, reader) in enumerate(conns):
        assert read_response(reader) == ("ok", str(i))
    for sock, _reader in conns + [(slow, slow_reader)]:
        sock.close()
    with Session(info) as client:
        order = client.run("set ::order")
        client.run("set ::order {}")
    return order


def test_queued_requests_run_in_connection_order(tcl_session):
    """Requests that queue behind a running script run in connect order.

    Repeated, with the send order differing from the connect order in most
    rounds, because the old `array names` dispatch only got it right by luck.
    Rounds alternate between a blocking and an event-loop-spinning script.
    """
    with Session(tcl_session.info) as client:
        client.run("set ::order {}")
    sends = [
        [0, 1, 2, 3],
        [3, 2, 1, 0],
        [1, 3, 0, 2],
        [0, 1, 2, 3, 4],
        [4, 2, 0, 3, 1],
        [2, 0, 1, 3],
        [3, 0, 2, 1],
        [0, 1, 2, 3],
        [1, 0, 3, 2],
        [2, 3, 1, 0],
    ]
    for rnd, order in enumerate(sends):
        slow = SLOW_SPINNING if rnd % 2 == 0 else SLOW_BLOCKING
        got = _queue_behind_slow(tcl_session.info, slow, order)
        assert got == " ".join(str(i) for i in range(len(order))), f"round {rnd}"


def test_queued_request_runs_after_its_client_closed(tcl_session):
    """A request still queued when its client hangs up is run, in its turn.

    _readable marks the channel closing at EOF; _pump must still dispatch the
    complete frame it buffered, and before the later connection's.
    """
    info = tcl_session.info
    with Session(info) as client:
        client.run("set ::order {}")
    slow, slow_reader = raw_connect(info)
    slow.sendall(encode_request("after 500; return slow", info.token))
    time.sleep(0.15)
    gone, _ = raw_connect(info)
    time.sleep(0.03)
    last, last_reader = raw_connect(info)
    time.sleep(0.03)
    last.sendall(encode_request("lappend ::order last; return last", info.token))
    gone.sendall(encode_request("lappend ::order gone", info.token))
    gone.close()
    assert read_response(slow_reader) == ("ok", "slow")
    assert read_response(last_reader) == ("ok", "last")
    with Session(info) as client:
        assert client.run("set ::order") == "gone last"
    slow.close()
    last.close()


# -- shutdown ----------------------------------------------------------------


def test_exit_removes_the_descriptor(tcl_session, registry_dir):
    client = Session(tcl_session.info)
    client.connect()
    try:
        client.run("exit", timeout=5)
    except Exception:
        pass  # the socket closes without a reply, which is the point
    finally:
        client.close()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not (registry_dir / "t.session").exists():
            break
        time.sleep(0.1)
    assert not (registry_dir / "t.session").exists()


def test_name_collision_is_refused(tcl_session, registry_dir, tclsh):
    """A second plugin must not steal a live session's name."""
    import os
    import subprocess

    from conftest import tcl_env

    env = tcl_env(tclsh)
    env.update(
        {
            "VRC_NAME": "t",  # same name as the running fixture
            "VRC_PORT": "0",
            "VRC_DIR": str(registry_dir),
            "VRC_SERVE": "1",
            "VRC_AUTOSTART": "1",
        }
    )
    from conftest import PLUGIN

    with open(os.devnull, "rb") as devnull:
        proc = subprocess.run(
            [tclsh, str(PLUGIN)],
            stdin=devnull,
            capture_output=True,
            env=env,
            timeout=30,
        )
    assert proc.returncode != 0
    assert b"already live" in proc.stderr
    # the original is untouched
    with Session(tcl_session.info) as client:
        assert client.run("expr 1") == "1"


# -- vrc cancel's marker -------------------------------------------------------


def _marker(registry_dir):
    return registry_dir / "t.cancel"


def test_cancel_marker_labels_the_error(tcl_session, registry_dir):
    # What a cancelled synth_design looks like from here: an error with an
    # empty message, and the marker `vrc cancel` wrote while it ran.
    marker = _marker(registry_dir)
    with Session(tcl_session.info) as client:
        with pytest.raises(RemoteTclError) as exc:
            client.run(f'close [open {{{marker}}} w]; error ""')
    assert exc.value.errorinfo.startswith("cancelled by vrc cancel\n")
    assert not marker.exists()


def test_stale_cancel_marker_does_not_label_a_later_error(tcl_session, registry_dir):
    # The command finished just before the SIGINT: the marker predates the
    # next request, whose own error must come through unchanged.
    marker = _marker(registry_dir)
    marker.touch()
    old = time.time() - 10
    os.utime(marker, (old, old))
    with Session(tcl_session.info) as client:
        with pytest.raises(RemoteTclError) as exc:
            client.run('error "boom"')
    assert exc.value.errorinfo.startswith("boom")
    assert not marker.exists()


def test_cancel_marker_is_cleared_by_a_successful_request(tcl_session, registry_dir):
    marker = _marker(registry_dir)
    with Session(tcl_session.info) as client:
        assert client.run(f'close [open {{{marker}}} w]; expr 1') == "1"
    assert not marker.exists()


# -- the record of what ran ---------------------------------------------------


def test_last_result_is_kept_on_disk(tcl_session):
    from vivado_remotectl import registry

    info = registry.find("t")
    assert info.results
    with Session(info) as client:
        assert client.run("return grüße") == "grüße"
    last = registry.read_result(info)
    assert (last.status, last.text) == ("ok", "grüße")
    assert last.finished >= last.started > 0
    assert registry.read_running(info) is None
    assert oct(info.sidecar("result").stat().st_mode)[-3:] == "600"


def test_an_error_is_kept_with_its_errorinfo(tcl_session):
    from vivado_remotectl import registry

    info = registry.find("t")
    with Session(info) as client:
        with pytest.raises(RemoteTclError):
            client.run("error kaboom")
    last = registry.read_result(info)
    assert last.status == "err"
    assert last.text.startswith("kaboom")


def test_liveness_checks_leave_the_result_alone(tcl_session):
    from vivado_remotectl import registry

    info = registry.find("t")
    with Session(info) as client:
        client.run("return keep-me")
        for check in ("expr 1", "info patchlevel"):
            client.run(check)
    assert registry.read_result(info).text == "keep-me"


def test_running_names_the_script_while_it_runs(tcl_session):
    from vivado_remotectl import registry

    info = registry.find("t")
    done = threading.Event()

    def slow():
        with Session(info) as client:
            client.run("\n  after 1500 ;# synth_design stand-in\nreturn slow-done")
        done.set()

    threading.Thread(target=slow).start()
    deadline = time.monotonic() + 5
    running = None
    while running is None and time.monotonic() < deadline:
        running = registry.read_running(info)
        time.sleep(0.05)
    assert running is not None
    assert running.first_line == "after 1500 ;# synth_design stand-in"
    assert done.wait(10)
    assert registry.read_running(info) is None
    assert registry.read_result(info).text == "slow-done"


def test_a_result_survives_its_client(tcl_session):
    """The agent's shell dies mid-synthesis; the answer must not."""
    from vivado_remotectl import registry

    info = registry.find("t")
    client = Session(info)
    client.send("after 500; return orphaned")
    client.close()
    with Session(info) as waiter:
        waiter.run("expr 1")  # queues until the orphan has finished
    assert registry.read_result(info).text == "orphaned"


def test_exit_removes_the_record(tcl_session, registry_dir):
    with Session(tcl_session.info) as client:
        client.run("return x")
        assert (registry_dir / "t.result").exists()
        try:
            client.run("exit")
        except Exception:
            pass
    tcl_session.proc.wait(timeout=10)
    assert not (registry_dir / "t.result").exists()


# -- --idle-stop -------------------------------------------------------------------


@pytest.fixture
def idle_session(tclsh, registry_dir, tmp_path):
    """A plugin that exits after 2 s without a request."""
    import subprocess

    from conftest import PLUGIN, tcl_env, wait_for_session

    env = tcl_env(tclsh)
    env.update(
        {
            "VRC_NAME": "idle",
            "VRC_PORT": "0",
            "VRC_DIR": str(registry_dir),
            "VRC_SERVE": "1",
            "VRC_AUTOSTART": "1",
            "VRC_IDLE_STOP": "2",
        }
    )
    with open(tmp_path / "idle.log", "wb") as log, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [tclsh, str(PLUGIN)], stdin=devnull, stdout=log,
            stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    try:
        yield wait_for_session(registry_dir, "idle"), proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_idle_session_exits_and_cleans_up(idle_session, registry_dir):
    info, proc = idle_session
    assert info.idle_stop == 2
    assert proc.wait(timeout=10) == 0
    assert not (registry_dir / "idle.session").exists()


def test_liveness_checks_do_not_keep_it_alive(idle_session):
    info, proc = idle_session
    deadline = time.monotonic() + 10
    with Session(info) as client:
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                client.run("expr 1", timeout=2)
            except Exception:
                break
            time.sleep(0.3)
    assert proc.wait(timeout=5) == 0


def test_a_request_resets_the_clock_and_a_running_one_is_not_cut_off(idle_session):
    info, proc = idle_session
    started = time.monotonic()
    with Session(info) as client:
        # Re-enters the event loop for longer than the idle limit, the way
        # wait_on_run does; the timer fires inside and must hold off.
        assert client.run("after 3500 {set ::d 1}; vwait ::d; return ok") == "ok"
    assert proc.poll() is None
    assert proc.wait(timeout=10) == 0
    assert time.monotonic() - started >= 5


def test_a_new_session_does_not_inherit_a_dead_ones_record(tclsh, registry_dir, tmp_path):
    """Vivado crashed; `vrc start` with the same name must start clean."""
    import subprocess

    from conftest import PLUGIN, tcl_env, wait_for_session

    (registry_dir / "t.result").write_bytes(b"ok 5 1 2\nstale")
    (registry_dir / "t.running").write_text("started 1\nsynth_design\n")
    env = tcl_env(tclsh)
    env.update({"VRC_NAME": "t", "VRC_DIR": str(registry_dir), "VRC_SERVE": "1"})
    with open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [tclsh, str(PLUGIN)], stdin=devnull, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
    try:
        wait_for_session(registry_dir, "t")
        assert not (registry_dir / "t.result").exists()
        assert not (registry_dir / "t.running").exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)
