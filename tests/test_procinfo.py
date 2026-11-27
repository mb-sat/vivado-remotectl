"""Memory figures read from /proc."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from vivado_remotectl import procinfo


@pytest.fixture
def fake_vivado_process(tmp_path):
    """A process whose argv[0] reads like the real Vivado binary."""
    argv0 = str(tmp_path / "Vivado/2023.2/bin/unwrapped/lnx64.o/vivado")
    proc = subprocess.Popen(
        [argv0, "-c", "import time; time.sleep(60)", "-mode", "batch"],
        executable=sys.executable,
    )
    yield proc
    proc.kill()
    proc.wait()


def test_rss_of_this_process():
    assert procinfo.rss(os.getpid()) > 1 << 20


def test_tree_rss_counts_children(fake_vivado_process):
    # The child is still starting up and growing, so compare loosely.
    child = procinfo.rss(fake_vivado_process.pid)
    assert procinfo.tree_rss(os.getpid()) >= procinfo.rss(os.getpid()) + child // 2


def test_tree_rss_of_a_missing_pid():
    assert procinfo.tree_rss(2**22 + 1) is None
    assert procinfo.tree_rss(0) is None


def test_mem_available_is_plausible():
    free = procinfo.mem_available()
    assert free is not None and free > 0


def test_a_vivado_outside_any_session_is_a_stray(fake_vivado_process):
    strays = procinfo.stray_vivados([])
    found = [s for s in strays if s.pid == fake_vivado_process.pid]
    assert len(found) == 1
    assert "-mode batch" in found[0].args
    assert found[0].rss > 0


def test_a_session_and_its_children_are_not_strays(fake_vivado_process):
    # The test process stands in for the session that spawned a helper.
    strays = procinfo.stray_vivados([os.getpid()])
    assert fake_vivado_process.pid not in [s.pid for s in strays]


def test_fmt_bytes():
    assert procinfo.fmt_bytes(None) == "-"
    assert procinfo.fmt_bytes(5 * 2**30 + 2**29) == "5.5G"
    assert procinfo.fmt_bytes(300 * 2**20) == "300M"


OOM_LINE = (
    "Out of memory: Killed process 1105540 (vivado) total-vm:20123456kB, "
    "anon-rss:13438504kB, file-rss:1234kB, shmem-rss:0kB, UID:1000 "
    "pgtables:30000kB oom_score_adj:0"
)


def test_oom_line_gives_the_anon_rss_of_that_pid():
    found = procinfo.parse_oom_line(OOM_LINE, 1105540)
    assert found == procinfo.OomKill(1105540, 13438504 * 1024)


def test_oom_line_of_another_pid_is_ignored():
    assert procinfo.parse_oom_line(OOM_LINE, 1105541) is None
    assert procinfo.parse_oom_line(OOM_LINE, 110554) is None
    assert procinfo.parse_oom_line("usb 1-1: new device", 1105540) is None


def test_oom_line_without_rss_still_matches():
    found = procinfo.parse_oom_line("Killed process 7 (vivado) total-vm:1kB", 7)
    assert found == procinfo.OomKill(7, None)


def test_oom_kill_finds_the_line_in_the_kernel_log(monkeypatch):
    monkeypatch.setattr(
        procinfo, "_kernel_log", lambda since, timeout: "noise\n" + OOM_LINE + "\n"
    )
    assert procinfo.oom_kill(1105540).anon_rss == 13438504 * 1024
    assert procinfo.oom_kill(42) is None


def test_oom_kill_with_no_readable_log_says_nothing(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("no")

    monkeypatch.setattr(procinfo.subprocess, "run", refuse)
    assert procinfo.oom_kill(1105540) is None


def test_kernel_log_falls_back_to_dmesg(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "journalctl":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout=OOM_LINE + "\n", stderr="")

    monkeypatch.setattr(procinfo.subprocess, "run", run)
    assert procinfo.oom_kill(1105540) is not None
    assert calls == ["journalctl", "dmesg"]


def test_oom_kill_ignores_pid_zero(monkeypatch):
    monkeypatch.setattr(procinfo, "_kernel_log", lambda *a: OOM_LINE)
    assert procinfo.oom_kill(0) is None
