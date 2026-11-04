"""End-to-end against the real plugin running on Vivado's tclsh8.5."""

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
    assert info.tcl.startswith("8.5")
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

    env = tcl_env()
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
