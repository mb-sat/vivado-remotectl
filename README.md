# vivado-remotectl

Vivado takes about forty seconds to start, and a batch run throws away
everything it learned when it exits. `vrc` keeps one Vivado alive with your
design loaded and sends it Tcl over a loopback socket, so the second
measurement costs seconds instead of a minute. You can keep several running at
once, each on its own port, which is what makes comparing a baseline against
your working tree a matter of two sessions rather than two builds.

```console
$ vrc start dut
dut: listening on 127.0.0.1:41239 (pid 2118203)
log: /home/you/.local/state/vivado-remotectl/dut.log

$ vrc -e 'version -short'
2023.2.2

$ vrc -e 'expr {2 + 2}'
4
```

There is one thing to send — Tcl — and it comes back as the string Vivado
produced. No typed wrappers, so nothing sits between you and a command that
already works at the Vivado prompt.

To build a block design's out-of-context IP runs in a few persistent sessions
instead of one Vivado per run, see [README-batchbuild.md](README-batchbuild.md).

## Run some Tcl in a session

`-e` takes a script, `-f` takes a file, and `-f -` reads standard input. Both
flags repeat and are concatenated in the order you wrote them, the way `sed`
treats them:

```sh
vrc -e 'close_design -quiet' -e 'read_verilog {a.v b.v}'
vrc -f setup.tcl -e 'synth_design -top top -part xcvc1902-vsva2197-2MP-e-S'
echo 'report_utilization -return_string' | vrc -f -
```

The result goes to stdout and nothing else does, so `vrc -f ooc.tcl > wns.txt`
gives you a clean file. A script that fails prints Tcl's `errorInfo` to stderr
and exits 1; the session survives and keeps its state.

A script ending in `return` is a normal success — Tcl reports that as a
different result code, and treating it as an error would break the usual way
of getting one value out of a long script:

```sh
vrc -e 'set p [get_timing_paths -max_paths 1 -delay_type max]
        return "WNS=[get_property SLACK $p]"'
```

Interpreter state persists between calls, which is the whole point: an open
design stays open, so the next `report_timing_summary` costs nothing.

## Keep several Vivados at once

Every session has a name and gets a free port assigned by the kernel, so
starting a second one needs no bookkeeping:

```console
$ vrc start baseline --cwd /tmp/head-checkout
$ vrc list
NAME                   PORT      PID STATUS        FOR   UPTIME    RSS OWNER            CWD
baseline              45711  2118440 idle       1m02s    2m31s   4.6G user:you         /tmp/head-checkout
dut                   41239  2118203 busy       3m40s    4m10s   5.2G user:you         /home/you/rtl
2 Vivado(s) hold 9.8G; 18.7G available
```

With more than one session running, say which you mean with `-s`, or set
`VIVADO_REMOTECTL_SESSION` once in the shell. With exactly one, `-s` is
optional. Getting this wrong is an error that lists the candidates rather
than a guess.

Each session is a separate Vivado process holding its own design, 4 to 6 GB
once a design is synthesised. `RSS` counts the
session and the helper processes synthesis starts under it. The line under the
table, on stderr, adds up every Vivado on the machine, including ones that are
not sessions, such as a `vivado -mode batch` run, which it also lists by pid:

```
not a session: pid 2120411 3.8G  vivado -mode batch -source build.tcl
```

`FOR` is how long the session has been in that state: idle since its last
request finished, or busy since the running script started. A session that is
easy to forget can be told to go by itself:

```sh
vrc start scratch --idle-stop 2h    # exits after two hours without a request
```

Only real requests count; `vrc list`, `vrc ping` and `vrc wait` do not keep it
alive, and a script that runs longer than the limit is not cut off. `vrc info`
shows the setting. There is no default, because a session that disappears
under a design you meant to return to costs more than the memory it held.

`vrc start` refuses when less than 6 GB is available, because with no swap
the OOM killer otherwise picks which Vivado dies, and lists what is holding the
memory. `--min-free GB` changes the limit and `--min-free 0` skips the check.
Stop sessions before starting a full build, which sizes its job count from
total memory on the assumption that it owns the machine.

```sh
vrc stop dut      # graceful: asks it to exit, then verifies
vrc stop --mine   # every session you started
vrc stop --all    # every session, but see below
```

`OWNER` is who ran `vrc start`: the Claude Code session, as `claude:<id>`
(its subagents share the id), or otherwise `user:<name>`; `$VRC_OWNER`
overrides both. `--mine` and `vrc list --mine` select by it. `--all` stops
every session except a busy one somebody else started, which it names on
stderr and leaves running, exiting 1; stopping that one takes its name. A
Vivado attached through `Vivado_init.tcl` has no owner and so is never yours.

## Watch a long synthesis

An out-of-context synthesis runs for half an hour, and `vrc` waits as long as
the script takes — there is no default timeout, because guessing one wrong
just loses you the result. Vivado's own progress messages go to the session
log, and `--tail` mirrors them to stderr while you wait:

```sh
vrc -s dut -f ooc.tcl --tail > wns.txt
```

`--tail` is on by default when stderr is a terminal and off when it is not, so
it stays out of the way in scripts. The log is also there afterwards:

```sh
vrc log dut           # last 40 lines
vrc log dut -f        # follow it
```

Ctrl-C detaches `vrc` and leaves the script running: `vrc` reports which
session is still working and exits 130. Passing `--timeout` behaves the same
way and exits 4. To stop the script itself, use `vrc cancel`, described in the
next section.

## Collect a result after the client has gone

The session keeps the reply to its last request on disk, so a result is not
lost with the shell that asked for it. `vrc wait` blocks until the session is
free and prints that reply with the exit code `vrc -e` would have given; `vrc
result` prints it at once, and says so on stderr when a newer script is still
running.

```sh
vrc -s dut --detach -f ooc.tcl     # returns once synthesis has started
...                                # edit, read, do something else
vrc wait dut > wns.txt             # exits 0, 1 or 6 like the original call
```

`--detach` only hands a script to an idle session, because two queued scripts
run in no fixed order and `vrc wait` could otherwise return the earlier one.
The same works for a client that died, was interrupted or timed out: `vrc wait`
picks up where it stopped. `vrc wait --timeout SEC` exits 4 if the session is
still busy and names what it is running.

## Cancel a synthesis, placement or routing

`vrc cancel` stops the command a session is running and keeps the session and
whatever design it holds. It sends Vivado the same SIGINT as Ctrl-C at its own
Tcl prompt, so no run or second Vivado is involved, and the next request is
served at once:

```console
$ vrc -s dut -f ooc.tcl &
$ vrc cancel dut
dut: cancelled
```

The waiting client prints `cancelled by vrc cancel` with the Tcl traceback and
exits 6. Vivado 2023.2 reacted within 7 s in `synth_design` and within 2 s in
`place_design` and `route_design`. The design that was open stays open; a
cancelled `place_design` or `route_design` can be run again on it, and a
cancelled `synth_design` leaves no design behind.

Three cases behave differently:

| situation | what happens |
|---|---|
| nothing running | prints `nothing running`; no signal is sent |
| the script wraps the command in `catch` | the `catch` returns 1 with an empty message and the script continues; `vrc cancel` waits up to `--wait` seconds (default 60) for the session to be free, then exits 4 |
| Vivado crashes while handling the interrupt | `vrc cancel` prints the log tail and exits 5; start the session again |

The crash was seen once in thirteen cancelled `route_design` runs, as a
segfault in the router (`HARTFlow::buildRouteDesign`) during route setup. It
did not recur in twelve attempts at the same point, so it is a race inside
Vivado. Save a checkpoint before cancelling anything that took long to
reach.

Only sessions started by `vrc start` without `--gui` can be cancelled; their
descriptor carries `cancel sigint`, which `vrc info` shows. A GUI session or a
Vivado attached through `Vivado_init.tcl` is refused, because there a SIGINT
cancels the command but leaves the plugin unable to answer anything again.
Sessions started by an older `vrc` are refused for the same reason and need a
restart.

## Restart a session that is stuck or no longer usable

`vrc restart` stops a session and starts it again under the same name, with
the Vivado, working directory, port, `--arg` values and `--idle-stop` that
`vrc start` gave it:

```console
$ vrc restart w2
w2: restarted, its running command killed; listening on 127.0.0.1:41233 (pid 88120)
```

An idle session gets the graceful exit, and a busy one is killed at once, so
whatever it was running is lost. It also brings back a session that has died,
as long as its descriptor is still there.

Restart rather than cancel when the session will not be used for the same
work again. After a cancelled `synth_design`, the next one in that session
fails with `invalid command name "NULL"`. A long series of runs leaves 40 to
80 MB per run behind. A pool worker whose job was withdrawn has nothing worth
keeping.

`vrc start` writes how it launched the session to `<name>.launch` beside the
descriptor. A Vivado attached through `Vivado_init.tcl`, a GUI session, and a
session from an older `vrc` have no usable record and are refused.

`--min-free` works as for `vrc start` (default 6 GB), but it counts the
memory after the old process has exited. If the new start fails or is
refused, the old session is already gone. `vrc restart` then exits 3 and
prints the `vrc start` line that brings it back.

## Attach to a Vivado you started yourself

An interactive or GUI Vivado can register itself, and then it shows up in
`vrc list` like any other session. Add one line to your `Vivado_init.tcl`:

```tcl
source /path/to/vivado_remotectl.tcl
```

`vrc plugin-path` prints the path to paste, and
`contrib/Vivado_init.tcl.example` has the whole file. The plugin detects that
it is inside a GUI and does not block — the GUI already has an event loop, so
taking it over would freeze the window.

## Work out what went wrong

Exit codes are distinct so a script can branch on them:

| code | meaning |
|---|---|
| 0 | fine |
| 1 | the script raised; `errorInfo` is on stderr. From `vrc stop --all`: it left a busy session of somebody else's running |
| 2 | bad usage |
| 3 | no session, an ambiguous one, nothing listening, or no recorded result |
| 4 | `--timeout` expired; the script is still running |
| 5 | the session closed the connection |
| 6 | `vrc cancel` stopped the script |
| 130 | interrupted; the script is still running |

Exit 5 usually means Vivado died, so `vrc` prints the tail of the session log
with it. `vrc -e exit` also produces it, because the process closes the socket
instead of replying — use `vrc stop` instead.

A session that is mid-synthesis cannot answer anything at all: Vivado's Tcl
interpreter is single-threaded, so `vrc list` shows it as `busy` rather than
pretending otherwise, and requests queue until it is free.

If a descriptor is left behind by a session that died hard, `vrc list --prune`
clears it. Nothing is deleted while the process is still there.

## Hand it to a coding agent

An agent that only knows `vivado -mode batch` will start a fresh Vivado per
question and never compare anything. `contrib/skills/vivado-session/` is a
Claude Code skill describing the workflow this tool exists for — two sessions
for a baseline comparison, the out-of-context synthesis loop, sweeping a
generic in one request — along with the traps that cost real time, like
counting DSP58 primitives instead of slices.

```sh
cp -r contrib/skills/vivado-session ~/.claude/skills/
```

## Drive a session from Emacs

`contrib/emacs/vrc.el` puts the loop in the editor: change the RTL, press one
key, read the WNS that came back. Synthesis runs in the background, Vivado's
progress appears in a second buffer, and an `ERROR:` in that buffer jumps
straight to the line of Verilog that caused it.

```elisp
(add-to-list 'load-path "~/git/vivado-remotectl/contrib/emacs")
(require 'vrc)
(add-hook 'tcl-mode-hook #'vrc-mode)
(add-hook 'verilog-mode-hook #'vrc-mode)
```

Everything hangs off `C-c v`. A single user prefix rather than the usual
`C-c C-<letter>` bindings, because verilog-mode and vhdl-mode have taken every
one worth having.

| key | does |
|---|---|
| `C-c v r` / `C-c v b` | evaluate the region or the whole buffer |
| `C-c v e` | evaluate one line typed in the minibuffer |
| `C-c v o` | synthesise the module at point out of context, report its WNS |
| `C-c v g` | save every buffer and run the last script again |
| `C-c v l` | follow the session log |
| `C-c v z` | a prompt on the session |
| `C-c v k` | stop waiting for a job, which keeps running |

`C-c v o` and `C-c v g` are the loop. The first runs the out-of-context script
from the skill against the module around point; the second repeats it after an
edit and reports the difference, `WNS=0.421 (was 0.380, +0.041)`, which is the
comparison the tool exists to make. The part number and constraints live in
`vrc-ooc-part` and `vrc-ooc-xdc`, so a `.dir-locals.el` sets them once per
project.

That same file is how a baseline and a working tree stop being a bookkeeping
problem. Point each checkout at its own session and the same key does the
right thing in both:

```elisp
;; .dir-locals.el in the baseline checkout
((nil . ((vrc-session . "baseline")
         (vrc-ooc-part . "xcvc1902-vsva2197-2MP-e-S"))))
```

A job that outlasts five seconds pops up `*vrc-log: NAME*`, which follows the
session log a quarter of a second behind Vivado. Scroll back in it and it
stops following until you return to the end, so reading an error mid-run does
not fight the output. `M-g M-n` walks the errors of the current run only, and
lands in the RTL.

Vivado runs one script at a time, so a second evaluation during a synthesis is
refused rather than queued behind twenty-five minutes of work. `C-c v k` only
detaches this end and the script keeps going, which is why a long one should
`puts` its result as well as `return` it, so the answer remains in the log. To
stop the script, run `vrc cancel` from a shell.

Tcl goes to the session through `vrc -f -` rather than through the prompt,
which is what makes a multi-line `foreach` work: `vrc repl` reads a line at a
time and the editor would split the block. The prompt is there for one-liners.

[contrib/emacs/README.md](contrib/emacs/README.md) works a real design through
the whole loop, and has the rest of the keys and settings.

## Commands

```
vrc [-s NAME] [-p PORT] -e SCRIPT | -f FILE  [--timeout SEC] [--tail] [--detach]

vrc start [NAME]   launch a session   --vivado PATH --version VER --port N
                                      --min-free GB --idle-stop DUR
                                      --cwd DIR --gui --wait SEC --arg ARG
vrc stop  [NAME]   shut one down      --mine --all --force --timeout SEC
vrc restart [NAME] stop, start again  --min-free GB --wait SEC
vrc cancel [NAME]  cancel the command --wait SEC
vrc wait  [NAME]   wait, last result  --timeout SEC
vrc result [NAME]  last result now
vrc list           what is running    --json --prune --mine  (alias: status)
vrc log   [NAME]   session output     -f/--follow -n LINES
vrc repl  [NAME]   interactive prompt
vrc ping  [NAME]   check it answers
vrc info  [NAME]   show the descriptor
vrc plugin-path    path to the Tcl plugin
vrc --version      version of vrc
```

A subcommand must be the first argument. Anything after it that `vrc` does not
take is an error that says what was probably meant: Tcl given without `-e`
comes back as the quoted `vrc -e '...'` to run instead, and a `vrc start`
option on an eval is named as one. Evaluating Tcl is spelled `-e` rather than
`vrc run` because `list`, `info` and `log` are all real Tcl commands
as well as subcommands here, and a bare first argument would have to guess.

Under `log`, `-f` means *follow*, as in `tail -f`. At the top level it means
*file*. That is the one inconsistency in the interface, kept because both
spellings are what people already type.

## Environment

| variable | effect |
|---|---|
| `VIVADO_REMOTECTL_SESSION` | default session name, instead of `-s` |
| `VRC_VIVADO` | vivado executable to launch |
| `VRC_DIR` | where session descriptors live |
| `VRC_OWNER` | owner `vrc start` records, instead of the Claude session or user |
| `XDG_STATE_HOME` | base for the default descriptor directory |

Descriptors default to `~/.local/state/vivado-remotectl/`, under *state*
rather than cache on purpose: a cache cleaner removing the token of a live
half-hour synthesis would be a bad afternoon.

Without `--vivado` or `--version`, `vrc` looks at `$VRC_VIVADO`, then
`/tools/Xilinx/Vivado/*/bin/vivado` preferring 2023.2, then `PATH`.

## How it is put together

The Vivado side is one file of Tcl that runs on both 8.5 and 8.6 — Vivado
2023.2 and older embed 8.5, 2024.1 onwards 8.6 — that opens a loopback socket and evaluates what arrives at global scope. The
Python side is the CLI, with no third-party dependencies. Between them is a
length-prefixed frame rather than JSON, so a multi-megabyte
`report_* -return_string` needs no escaping and the Tcl side needs no JSON
codec. [docs/protocol.md](docs/protocol.md) has the format.

Each session writes a `0600` descriptor holding its port and a random
32-character token, which every request must present. Loopback alone would let
any local process evaluate arbitrary Tcl as you. The descriptor also records
the process start time, so `vrc stop` can prove a pid is still the process it
started before signalling it — pids get recycled, and the alternative is a
stale descriptor aiming a kill at an unrelated Vivado.

A session started by `vrc start` does not block in a `vwait`. Its stdin is
`yes ::vrc::serve`, and Vivado's own prompt runs that command over and over;
each call services the socket for half a second and returns. This is what
makes `vrc cancel` possible. Vivado clears a SIGINT only when control returns
to its prompt. Under a `vwait` that never happens: the cancelled command's
error passes through the plugin's `catch`, no response is sent, and every
later request fails the same way. The comment on `::vrc::serve` in the plugin
has the details. Vivado reports a cancelled command as an error with an empty
message, so `vrc cancel` writes `<name>.cancel` beside the descriptor before
signalling, and the plugin turns that into the `cancelled by vrc cancel` text.

## Develop it

```sh
pip install -e '.[dev]'
pytest
```

The suite needs no Vivado and no hardware. It runs the real plugin twice, on
the `tclsh8.5` of Vivado 2023.2 and the `tclsh8.6` of 2024.2, so the
interpreters under test are the ones production uses, and `tests/fake_vivado.sh` stands in for the executable so
the whole launch path — `setsid`, log redirection, readiness polling, killing
the process group — is exercised for real. Where a Vivado is missing the host's
`tclsh8.5` or `tclsh8.6` stands in, and with neither those tests skip.

One path the tests cannot cover is a real synthesis. After a change to the
plugin, check it by hand:

```sh
vrc start real --version 2023.2
vrc -e 'version -short'
vrc -f ooc.tcl --tail       # a genuine OOC synth
vrc list                    # reads "busy" while it runs
vrc -f ooc.tcl & sleep 30; vrc cancel real   # client exits 6
vrc -e 'expr 6*7'           # still answers
vrc stop real
```

## License

MIT, see [LICENSE](LICENSE).
