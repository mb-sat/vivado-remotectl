"""Shared fixtures.

The plugin is exercised on the tclsh each Vivado ships, not the host's: Vivado
2023.2 and older embed Tcl 8.5, where `try`, `lmap` and friends do not exist,
and 2024.1 onwards embed 8.6. Every test that runs the plugin runs once per
interpreter, so code that only one of them accepts fails here first.
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

VIVADO = Path("/tools/Xilinx/Vivado")

#: (test id, Vivado release that ships it, Tcl version, host fallback)
INTERPRETERS = [
    ("tcl8.5", "2023.2", "8.5", "tclsh8.5"),
    ("tcl8.6", "2024.2", "8.6", "tclsh8.6"),
]

PLUGIN = REPO / "src/vivado_remotectl/tcl/vivado_remotectl.tcl"
FAKE_VIVADO = REPO / "tests/fake_vivado.sh"


def _interpreter(release: str, tcl: str, fallback: str) -> tuple[str, dict[str, str]] | None:
    """The tclsh a Vivado release ships, with the environment it needs."""
    root = VIVADO / release
    tclsh = root / f"bin/unwrapped/lnx64.o/tclsh{tcl}"
    if tclsh.exists():
        return str(tclsh), {
            "LD_LIBRARY_PATH": str(root / "lib/lnx64.o"),
            "TCL_LIBRARY": str(root / f"tps/tcl/tcl{tcl}"),
        }
    host = shutil.which(fallback)
    if host:
        return host, {}
    return None


_TCL_ENV: dict[str, dict[str, str]] = {}


def tcl_env(tclsh: str) -> dict[str, str]:
    """Environment that makes this tclsh start cleanly."""
    env = dict(os.environ)
    env.update(_TCL_ENV.get(tclsh, {}))
    return env


@pytest.fixture(autouse=True)
def _outside_sandbox(monkeypatch):
    """Run as if unsandboxed, so the suite also passes from a sandboxed shell.

    The fake sessions live in the same sandbox as the test, so they are
    reachable; only the tests about the sandbox set the variable again.
    """
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)


@pytest.fixture(scope="session", params=INTERPRETERS, ids=[i[0] for i in INTERPRETERS])
def tclsh(request) -> str:
    _id, release, tcl, fallback = request.param
    found = _interpreter(release, tcl, fallback)
    if found is None:
        pytest.skip(f"no tclsh{tcl} available")
    path, extra = found
    _TCL_ENV[path] = extra
    return path


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
    """Start the plugin under each tclsh on an ephemeral port."""
    name = "t"
    log_path = tmp_path / "session.log"
    env = tcl_env(tclsh)
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
