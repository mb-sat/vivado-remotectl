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
