"""CLI argument routing, output streams and exit codes."""

from __future__ import annotations

import json
import time

import pytest
from conftest import FAKE_VIVADO, tcl_env

from vivado_remotectl import launcher, registry
from vivado_remotectl.cli import (
    EXIT_CANCELLED,
    EXIT_DIED,
    EXIT_NO_SESSION,
    EXIT_OK,
    EXIT_TCL_ERROR,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    assemble_script,
    main,
)


@pytest.fixture
def live(registry_dir, monkeypatch, tclsh):
    """A session reachable through the registry, as the CLI sees it."""
    for key, value in tcl_env(tclsh).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("VRC_TEST_TCLSH", tclsh)
    info = launcher.start_session(
        "cli", vivado=str(FAKE_VIVADO), wait=60.0, directory=registry_dir
    )
    yield info
    if registry.owns_pid(info):
        launcher.stop_session(info, force=True)


# -- script assembly ---------------------------------------------------------


def test_e_and_f_concatenate_in_argument_order(tmp_path):
    first = tmp_path / "a.tcl"
    first.write_text("set a 1")
    parts = [("expr", "set b 2"), ("file", str(first)), ("expr", "set c 3")]
    assert assemble_script(parts) == "set b 2\nset a 1\nset c 3"


def test_f_dash_reads_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert assemble_script([("file", "-")]) == "from stdin"


def test_missing_file_is_reported(tmp_path):
    from vivado_remotectl.registry import RegistryError

    with pytest.raises(RegistryError, match="cannot read"):
        assemble_script([("file", str(tmp_path / "nope.tcl"))])


# -- usage -------------------------------------------------------------------


def test_no_arguments_is_usage(capsys):
    assert main([]) == EXIT_USAGE
    assert "vrc" in capsys.readouterr().err


def test_help_goes_to_stdout(capsys):
    assert main(["--help"]) == EXIT_OK
    assert "Subcommands" in capsys.readouterr().out


def test_unknown_subcommand_suggests_eval(capsys):
    assert main(["report_timing"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "unknown subcommand" in err
    assert "-e 'report_timing'" in err


def test_nothing_to_evaluate_is_usage(capsys):
    assert main(["-s", "whatever"]) == EXIT_USAGE
    assert "nothing to evaluate" in capsys.readouterr().err


def test_list_is_a_subcommand_not_tcl(registry_dir, capsys):
    # The reason -e exists: `list` is both a subcommand and a Tcl command.
    assert main(["list"]) == EXIT_OK
    assert "no sessions" in capsys.readouterr().err


def test_plugin_path_is_printed(capsys):
    assert main(["plugin-path"]) == EXIT_OK
    assert capsys.readouterr().out.strip().endswith("vivado_remotectl.tcl")


# -- no session --------------------------------------------------------------


def test_eval_without_a_session(registry_dir, capsys):
    assert main(["-e", "expr 1"]) == EXIT_NO_SESSION
    assert "no Vivado sessions" in capsys.readouterr().err


def test_named_session_that_does_not_exist(registry_dir, capsys):
    assert main(["-s", "ghost", "-e", "expr 1"]) == EXIT_NO_SESSION
    assert "no session named" in capsys.readouterr().err


def test_port_that_nothing_listens_on(registry_dir, capsys):
    assert main(["-p", "1", "-e", "expr 1"]) == EXIT_NO_SESSION
    assert "cannot reach" in capsys.readouterr().err


# -- against a live session --------------------------------------------------


def test_eval_prints_the_result(live, capsys):
    assert main(["-e", "expr {6 * 7}"]) == EXIT_OK
    out = capsys.readouterr()
    assert out.out.strip() == "42"
    assert out.err == ""


def test_eval_picks_the_only_session_without_s(live, capsys):
    assert main(["-e", "return picked"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "picked"


def test_repeated_e_flags_run_in_order(live, capsys):
    assert main(["-e", "set ::x 5", "-e", "expr {$::x * 3}"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "15"


def test_eval_from_a_file(live, tmp_path, capsys):
    script = tmp_path / "s.tcl"
    script.write_text("set q 11\nexpr {$q + 1}\n")
    assert main(["-f", str(script)]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "12"


def test_tcl_error_exits_one_with_errorinfo_on_stderr(live, capsys):
    assert main(["-e", "error {kaboom}"]) == EXIT_TCL_ERROR
    out = capsys.readouterr()
    assert out.out == ""
    assert "kaboom" in out.err


def test_timeout_exits_four_and_says_where_it_is_still_running(live, capsys):
    assert main(["-e", "after 5000", "--timeout", "1", "--no-tail"]) == EXIT_TIMEOUT
    err = capsys.readouterr().err
    assert "timed out" in err
    assert "vrc log -f cli" in err
    assert "vrc cancel cli" in err


def test_a_cancelled_request_exits_six(live, registry_dir, capsys):
    marker = registry_dir / "cli.cancel"
    script = f'close [open {{{marker}}} w]; error ""'
    assert main(["-e", script, "--no-tail"]) == EXIT_CANCELLED
    assert capsys.readouterr().err.startswith("cancelled by vrc cancel")


def test_cancel_with_nothing_running(live, capsys):
    assert main(["cancel", "cli"]) == EXIT_OK
    assert capsys.readouterr().out == "cli: nothing running\n"


def test_explicit_port_bypasses_the_registry(live, capsys):
    # No token is available this way, so the server must refuse it. That is
    # the documented trade-off of -p, not a silent failure.
    assert main(["-p", str(live.port), "-e", "expr 1"]) == EXIT_DIED
    assert "authentication" in capsys.readouterr().err


def test_ping_reports_the_version(live, capsys):
    assert main(["ping"]) == EXIT_OK
    assert "2023.2.2 (fake)" in capsys.readouterr().out


def test_list_shows_the_session(live, capsys):
    assert main(["list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "cli" in out
    assert "idle" in out
    assert str(live.port) in out


def test_list_json_is_machine_readable(live, capsys):
    assert main(["list", "--json"]) == EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["cli"]
    assert rows[0]["status"] == "idle"
    assert rows[0]["port"] == live.port


def test_info_shows_the_descriptor(live, capsys):
    assert main(["info"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "starttime" not in out  # not interesting to a human
    assert "status" in out and "idle" in out


def test_info_names_the_open_project(live, capsys):
    assert main(["-e", "proc current_project {args} { return dut }"]) == EXIT_OK
    assert main(["-e", "set kept 42"]) == EXIT_OK
    capsys.readouterr()
    assert main(["info", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["project"] == "dut"
    # Asking must not replace the last result.
    assert main(["result"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "42"


def test_info_without_a_project(live, capsys):
    assert main(["info", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["project"] == "-"


def test_log_prints_the_session_log(live, capsys):
    assert main(["log"]) == EXIT_OK
    assert "vrc start cli" in capsys.readouterr().out


def test_stop_then_list_is_empty(live, capsys):
    assert main(["stop", "cli"]) == EXIT_OK
    assert "cli: stopped" in capsys.readouterr().out
    assert main(["list"]) == EXIT_OK
    assert "no sessions" in capsys.readouterr().err


def test_prune_removes_a_dead_descriptor(live, registry_dir, capsys):
    (registry_dir / "dead.session").write_text(
        "\n".join(
            [
                "v 1",
                "name dead",
                "host 127.0.0.1",
                "port 1",
                f"pid {2**22}",
                "starttime 1",
                f"token {'a' * 32}",
                "started 1",
                "cwd /",
                "",
            ]
        )
    )
    assert main(["list", "--prune"]) == EXIT_OK
    assert not (registry_dir / "dead.session").exists()


# -- results that outlive the client -----------------------------------------


def test_result_prints_the_last_one(live, capsys):
    assert main(["-e", "return first"]) == EXIT_OK
    capsys.readouterr()
    assert main(["result", "cli"]) == EXIT_OK
    assert capsys.readouterr().out == "first\n"


def test_result_keeps_the_exit_code_of_an_error(live, capsys):
    assert main(["-e", "error kaboom"]) == EXIT_TCL_ERROR
    capsys.readouterr()
    assert main(["result"]) == EXIT_TCL_ERROR
    assert capsys.readouterr().err.startswith("kaboom")


def test_result_before_anything_ran(live, capsys):
    assert main(["result"]) == EXIT_NO_SESSION
    assert "no recorded result" in capsys.readouterr().err


def test_detach_then_wait(live, capsys):
    assert main(["-s", "cli", "--detach", "-e", "after 1000; return late"]) == EXIT_OK
    out = capsys.readouterr()
    assert out.out == ""
    assert "vrc wait cli" in out.err
    assert registry.probe(live) == registry.BUSY
    assert main(["wait", "cli"]) == EXIT_OK
    assert capsys.readouterr().out == "late\n"


def test_detach_queues_behind_a_busy_session(live, capsys):
    assert main(["--detach", "-e", "after 1500; return first"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--detach", "-e", "return second"]) == EXIT_OK
    out = capsys.readouterr()
    assert out.out == ""
    assert "queued behind 'after 1500; return first'" in out.err
    assert "vrc wait cli" in out.err
    # wait queues behind both, so it reports the last one, not the running one
    assert main(["wait"]) == EXIT_OK
    assert capsys.readouterr().out == "second\n"


def test_detach_queues_several_in_order(live, capsys):
    assert main(["-e", "set ::log {}"]) == EXIT_OK
    assert main(["--detach", "-e", "after 1000; lappend ::log a"]) == EXIT_OK
    for tag in "bcd":
        assert main(["--detach", "-e", f"lappend ::log {tag}"]) == EXIT_OK
    capsys.readouterr()
    assert main(["wait"]) == EXIT_OK
    capsys.readouterr()
    assert main(["-e", "set ::log"]) == EXIT_OK
    assert capsys.readouterr().out == "a b c d\n"


def test_detach_confirmation_ignores_a_result_from_the_same_second(live, capsys):
    # Two quick detaches back to back: the second must not take the first's
    # result file, written in the same second, as proof that it has started.
    assert main(["--detach", "-e", "return one"]) == EXIT_OK
    assert main(["wait"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--detach", "-e", "after 1500; return two"]) == EXIT_OK
    assert registry.read_running(live) is not None
    assert main(["wait"]) == EXIT_OK
    assert capsys.readouterr().out == "two\n"


def test_wait_times_out_naming_the_script(live, capsys):
    assert main(["--detach", "-e", "after 3000"]) == EXIT_OK
    capsys.readouterr()
    assert main(["wait", "--timeout", "0.5"]) == EXIT_TIMEOUT
    assert "after 3000" in capsys.readouterr().err
    assert main(["wait"]) == EXIT_OK


def test_result_while_busy_says_a_newer_one_is_running(live, capsys):
    assert main(["-e", "return old"]) == EXIT_OK
    assert main(["--detach", "-e", "after 2000; return new"]) == EXIT_OK
    capsys.readouterr()
    assert main(["result"]) == EXIT_OK
    out = capsys.readouterr()
    assert out.out == "old\n"
    assert "vrc wait cli" in out.err
    assert main(["wait"]) == EXIT_OK
    assert capsys.readouterr().out == "new\n"


def test_wait_on_an_idle_session_returns_at_once(live, capsys):
    assert main(["-e", "return done"]) == EXIT_OK
    capsys.readouterr()
    assert main(["wait", "--timeout", "2"]) == EXIT_OK
    assert capsys.readouterr().out == "done\n"


# -- memory --------------------------------------------------------------------


def test_list_shows_memory_per_session_and_in_total(live, capsys, monkeypatch):
    import json

    # Vivados running on this machine outside the test would be counted too.
    monkeypatch.setattr("vivado_remotectl.procinfo.stray_vivados", lambda *a, **k: [])

    assert main(["list"]) == EXIT_OK
    out = capsys.readouterr()
    header, row = out.out.splitlines()[:2]
    assert "RSS" in header
    assert row.split()[6].endswith(("M", "G"))
    assert "1 Vivado(s) hold" in out.err
    assert "available" in out.err
    assert main(["list", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)[0]["rss"] > 0


def test_start_refuses_when_memory_is_short(registry_dir, capsys):
    assert main(["start", "big", "--min-free", "1e9"]) == EXIT_NO_SESSION
    err = capsys.readouterr().err
    assert "not starting 'big'" in err
    assert "--min-free 0" in err
    assert not (registry_dir / "big.session").exists()


# -- owners ----------------------------------------------------------------------


@pytest.fixture
def foreign(live, registry_dir, monkeypatch):
    """A second session, started by somebody else."""
    monkeypatch.setenv("VRC_OWNER", "claude:someone-else")
    info = launcher.start_session(
        "other", vivado=str(FAKE_VIVADO), wait=60.0, directory=registry_dir
    )
    monkeypatch.delenv("VRC_OWNER")
    yield info
    if registry.owns_pid(info):
        launcher.stop_session(info, force=True)


def test_start_records_the_owner(live):
    assert registry.find("cli").owner == registry.current_owner()


def test_owner_comes_from_the_claude_session(monkeypatch):
    monkeypatch.delenv("VRC_OWNER", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "0123456789abcdef")
    assert registry.current_owner() == "claude:0123456789abcdef"
    assert registry.short_owner("claude:0123456789abcdef") == "claude:01234567"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert registry.current_owner().startswith("user:")


def test_list_mine_and_the_owner_column(foreign, capsys):
    assert main(["list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "OWNER" in out and "claude:someone-" in out
    assert main(["list", "--mine"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "cli" in out and "other" not in out


def test_stop_mine_leaves_other_sessions(foreign, capsys):
    assert main(["stop", "--mine"]) == EXIT_OK
    assert capsys.readouterr().out == "cli: stopped\n"
    assert registry.find("other") is not None
    assert registry.probe(foreign) == registry.IDLE


def test_stop_all_spares_somebody_elses_busy_session(foreign, capsys):
    assert main(["-s", "other", "--detach", "-e", "after 3000"]) == EXIT_OK
    capsys.readouterr()
    assert main(["stop", "--all"]) == EXIT_TCL_ERROR
    out = capsys.readouterr()
    assert "cli: stopped" in out.out
    assert "other: left running" in out.err
    assert registry.probe(foreign) == registry.BUSY
    assert main(["wait", "other"]) == EXIT_OK


def test_stop_all_takes_somebody_elses_idle_session(foreign, capsys):
    assert main(["stop", "--all"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "cli: stopped" in out and "other: stopped" in out


# -- idle time -------------------------------------------------------------------


def test_list_shows_how_long_it_has_been_idle_or_busy(live, capsys):
    import json

    assert main(["-e", "return x"]) == EXIT_OK
    assert main(["--detach", "-e", "after 2500"]) == EXIT_OK
    time.sleep(1.2)
    capsys.readouterr()
    assert main(["list", "--json"]) == EXIT_OK
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "busy" and row["for"] >= 1
    assert main(["wait"]) == EXIT_OK
    capsys.readouterr()
    assert main(["list"]) == EXIT_OK
    header, line = capsys.readouterr().out.splitlines()[:2]
    assert header.split()[4] == "FOR"
    status, took = line.split()[3:5]
    assert status == "idle" and took in ("0s", "1s", "2s")


def test_idle_stop_parses_durations():
    from vivado_remotectl.cli import _duration

    assert [_duration(t) for t in ("90", "90s", "45m", "2h", "1.5h")] == [
        90, 90, 2700, 7200, 5400,
    ]


def test_start_passes_idle_stop_to_the_session(registry_dir, monkeypatch, tclsh, capsys):
    for key, value in tcl_env(tclsh).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("VRC_TEST_TCLSH", tclsh)
    monkeypatch.setenv("VRC_VIVADO", str(FAKE_VIVADO))
    assert main(["start", "brief", "--idle-stop", "1h", "--min-free", "0"]) == EXIT_OK
    try:
        assert registry.find("brief").idle_stop == 3600
        capsys.readouterr()
        assert main(["info", "brief"]) == EXIT_OK
        assert "1h00m" in capsys.readouterr().out
    finally:
        assert main(["stop", "brief"]) == EXIT_OK


# -- the misses people make ------------------------------------------------------


def test_status_is_list(live, capsys):
    assert main(["status"]) == EXIT_OK
    assert "cli" in capsys.readouterr().out


def test_version_of_vrc_itself(capsys):
    from vivado_remotectl import __version__

    assert main(["--version"]) == EXIT_OK
    assert capsys.readouterr().out == f"vrc {__version__}\n"


def test_a_start_option_on_an_eval_says_so(capsys):
    assert main(["-s", "x", "--version", "2023.2", "-e", "expr 1"]) == EXIT_USAGE
    assert "--version is an option of 'vrc start'" in capsys.readouterr().err
    assert main(["--cwd", "/tmp", "-e", "expr 1"]) == EXIT_USAGE
    assert "-e 'cd DIR'" in capsys.readouterr().err


def test_tcl_without_e_suggests_it(capsys):
    argv = ["-s", "x", "llength", "[get_cells -hier]"]
    assert main(argv) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "vrc -e 'llength [get_cells -hier]'" in err


def test_an_unknown_option_is_named(capsys):
    assert main(["-log", "x.log", "-e", "expr 1"]) == EXIT_USAGE
    assert "unknown option '-log'" in capsys.readouterr().err


def test_a_missing_name_lists_the_sessions_there_are(live, capsys):
    assert main(["-s", "rw", "-e", "expr 1"]) == EXIT_NO_SESSION
    assert "there are: cli" in capsys.readouterr().err


def test_restart_prints_the_new_session(live, capsys):
    assert main(["restart", "cli", "--min-free", "0"]) == EXIT_OK
    out = capsys.readouterr().out
    new = registry.resolve("cli")
    assert new.pid != live.pid
    assert out.startswith(f"cli: restarted; listening on 127.0.0.1:{new.port} (pid {new.pid})")
    launcher.stop_session(new, force=True)


def test_restart_refuses_to_start_short_of_memory(live, capsys, monkeypatch):
    from vivado_remotectl import procinfo

    monkeypatch.setattr(procinfo, "mem_available", lambda: 2**30)
    assert main(["restart", "cli", "--min-free", "6"]) == EXIT_NO_SESSION
    err = capsys.readouterr().err
    assert "stopped, but not started again: not starting 'cli'" in err
    assert "start it with: vrc start cli --vivado " in err


# -- script arguments ---------------------------------------------------------

AWKWARD_ARGS = ["plain", "", "two words", "a{b", "c}d", "$x [exit] \"q\"", "back\\slash\\",
                "semi;colon", "#hash", "line\nbreak", "-dash", "ünï"]


def test_arguments_after_dashdash_become_argv(live, capsys):
    assert main(["-e", "return [llength $::argv]:$::argc", "--", *AWKWARD_ARGS]) == EXIT_OK
    n = len(AWKWARD_ARGS)
    assert capsys.readouterr().out.strip() == f"{n}:{n}"
    for i, arg in enumerate(AWKWARD_ARGS):
        assert main(["-e", f"lindex $::argv {i}", "--", *AWKWARD_ARGS]) == EXIT_OK
        assert capsys.readouterr().out == (arg + "\n" if arg else "")


def test_dashdash_with_no_arguments_empties_argv(live, capsys):
    assert main(["-e", "set ::argv stale", "-e", "return ok"]) == EXIT_OK
    capsys.readouterr()
    assert main(["-e", "llength $::argv", "--"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "0"
