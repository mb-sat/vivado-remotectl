"""Registry parsing, liveness and session resolution."""

from __future__ import annotations

import os

import pytest

from vivado_remotectl import registry
from vivado_remotectl.registry import RegistryError


def write_descriptor(directory, name, *, port=12345, pid=None, token="a" * 32, started=0):
    pid = os.getpid() if pid is None else pid
    (directory / f"{name}.session").write_text(
        "\n".join(
            [
                "v 1",
                f"name {name}",
                "host 127.0.0.1",
                f"port {port}",
                f"pid {pid}",
                f"token {token}",
                "tcl 8.5.14",
                f"started {started}",
                "cwd /somewhere",
                "",
            ]
        )
    )


def test_state_dir_prefers_vrc_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VRC_DIR", str(tmp_path))
    assert registry.state_dir() == tmp_path


def test_state_dir_falls_back_to_xdg_state_not_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("VRC_DIR", raising=False)
    monkeypatch.delenv("VIVADO_REMOTECTL_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert registry.state_dir() == tmp_path / "vivado-remotectl"


@pytest.mark.parametrize("name", ["dut", "a", "base-line", "v2023.2", "a_b"])
def test_valid_names(name):
    assert registry.validate_name(name) == name


@pytest.mark.parametrize("name", ["", "with space", "a/b", "x" * 65, "../escape"])
def test_invalid_names(name):
    with pytest.raises(RegistryError):
        registry.validate_name(name)


def test_read_descriptor(registry_dir):
    write_descriptor(registry_dir, "dut", port=5555, started=1000)
    info = registry.read_session_file(registry_dir / "dut.session")
    assert (info.name, info.port, info.tcl) == ("dut", 5555, "8.5.14")
    assert info.log_path == registry_dir / "dut.log"


def test_half_written_descriptor_is_ignored(registry_dir):
    # What `CREAT EXCL` leaves behind between claiming a name and filling it.
    (registry_dir / "partial.session").write_text("v 1\nname partial\n")
    assert registry.read_session_file(registry_dir / "partial.session") is None
    assert registry.list_sessions(registry_dir) == []


def test_empty_descriptor_is_ignored(registry_dir):
    (registry_dir / "empty.session").write_text("")
    assert registry.list_sessions(registry_dir) == []


def test_pid_alive():
    assert registry.pid_alive(os.getpid())
    assert not registry.pid_alive(2**22)


def test_probe_reports_dead_when_nothing_listens(registry_dir, monkeypatch):
    monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    write_descriptor(registry_dir, "gone", port=1, pid=2**22)
    info = registry.read_session_file(registry_dir / "gone.session")
    assert registry.probe(info, timeout=0.2) == registry.DEAD


def test_probe_inside_the_sandbox_is_not_dead(registry_dir, monkeypatch):
    # A refused connect there says nothing about the session, so prune must
    # leave the descriptor alone.
    monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    write_descriptor(registry_dir, "far", port=1, pid=2**22)
    info = registry.read_session_file(registry_dir / "far.session")
    assert registry.probe(info, timeout=0.2) == registry.SANDBOXED
    assert registry.prune(registry_dir) == []
    assert (registry_dir / "far.session").exists()


def test_probe_reports_idle_for_a_live_session(tcl_session):
    assert registry.probe(tcl_session.info) == registry.IDLE


def test_prune_removes_dead_descriptors_only(registry_dir, tcl_session):
    write_descriptor(registry_dir, "dead", port=1, pid=2**22)
    removed = registry.prune(registry_dir)
    assert removed == ["dead"]
    assert not (registry_dir / "dead.session").exists()
    assert (registry_dir / "t.session").exists()


def test_resolve_by_name(registry_dir):
    write_descriptor(registry_dir, "dut")
    assert registry.resolve("dut", registry_dir).name == "dut"


def test_resolve_unknown_name(registry_dir):
    with pytest.raises(RegistryError, match="no session named"):
        registry.resolve("nope", registry_dir)


def test_resolve_with_no_sessions(registry_dir):
    with pytest.raises(RegistryError, match="no Vivado sessions"):
        registry.resolve(None, registry_dir)


def test_resolve_picks_the_only_session(registry_dir):
    write_descriptor(registry_dir, "solo")
    assert registry.resolve(None, registry_dir).name == "solo"


def test_resolve_is_ambiguous_with_two_live_sessions(registry_dir, monkeypatch):
    write_descriptor(registry_dir, "one")
    write_descriptor(registry_dir, "two")
    monkeypatch.setattr(registry, "is_live", lambda info, timeout=0.5: True)
    with pytest.raises(RegistryError, match="several sessions are live"):
        registry.resolve(None, registry_dir)


def test_resolve_honours_the_environment(registry_dir, monkeypatch):
    write_descriptor(registry_dir, "one")
    write_descriptor(registry_dir, "two")
    monkeypatch.setenv("VIVADO_REMOTECTL_SESSION", "two")
    assert registry.resolve(None, registry_dir).name == "two"
