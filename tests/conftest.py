"""Shared fixtures.

The plugin is exercised on Vivado's own tclsh8.5, not the host's 8.6: Vivado
2023.2 embeds Tcl 8.5.14, where `try`, `lmap` and friends do not exist, so
testing against 8.6 would test an interpreter we never run on.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

VIVADO_ROOT = Path("/tools/Xilinx/Vivado/2023.2")
TCLSH = VIVADO_ROOT / "bin/unwrapped/lnx64.o/tclsh8.5"
TCL_LIB = VIVADO_ROOT / "tps/tcl/tcl8.5"
LD_LIB = VIVADO_ROOT / "lib/lnx64.o"

PLUGIN = REPO / "src/vivado_remotectl/tcl/vivado_remotectl.tcl"
FAKE_VIVADO = REPO / "tests/fake_vivado.sh"


def tcl_env() -> dict[str, str]:
    """Environment that makes Vivado's bundled tclsh8.5 start cleanly."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(LD_LIB)
    env["TCL_LIBRARY"] = str(TCL_LIB)
    return env


def tclsh_path() -> str:
    if TCLSH.exists():
        return str(TCLSH)
    fallback = shutil.which("tclsh8.5") or shutil.which("tclsh")
    if fallback:
        return fallback
    pytest.skip("no tclsh available")


@pytest.fixture(scope="session")
def tclsh() -> str:
    return tclsh_path()


@pytest.fixture
def registry_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "registry"
    directory.mkdir()
    monkeypatch.setenv("VRC_DIR", str(directory))
    monkeypatch.delenv("VIVADO_REMOTECTL_SESSION", raising=False)
    return directory


def wait_for_session(directory: Path, name: str, timeout: float = 20.0):
    from vivado_remotectl import registry

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = registry.read_session_file(directory / f"{name}.session")
        if info is not None and info.port:
            try:
                socket.create_connection((info.host, info.port), timeout=0.5).close()
                return info
            except OSError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"session {name} never came up in {directory}")


class TclSession:
    """A plugin instance running under tclsh, standing in for Vivado."""

    def __init__(self, info, proc, log_path: Path):
        self.info = info
        self.proc = proc
        self.log_path = log_path

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""


@pytest.fixture
def tcl_session(tclsh, registry_dir, tmp_path):
    """Start the plugin under tclsh8.5 on an ephemeral port."""
    name = "t"
    log_path = tmp_path / "session.log"
    env = tcl_env()
    env.update(
        {
            "VRC_NAME": name,
            "VRC_PORT": "0",
            "VRC_DIR": str(registry_dir),
            "VRC_SERVE": "1",
            "VRC_AUTOSTART": "1",
        }
    )
    with open(log_path, "wb") as log, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [tclsh, str(PLUGIN)],
            stdin=devnull,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    try:
        info = wait_for_session(registry_dir, name)
        yield TclSession(info, proc, log_path)
    finally:
        proc.kill()
        proc.wait(timeout=10)
