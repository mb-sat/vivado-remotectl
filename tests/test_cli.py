"""CLI argument routing, output streams and exit codes."""

from __future__ import annotations

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
    import json

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


def test_detach_refuses_a_busy_session(live, capsys):
    assert main(["--detach", "-e", "after 2000"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--detach", "-e", "return second"]) == EXIT_NO_SESSION
    err = capsys.readouterr().err
    assert "busy: after 2000" in err
    assert main(["wait"]) == EXIT_OK


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
