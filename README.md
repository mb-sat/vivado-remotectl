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
NAME                   PORT      PID STATUS     UPTIME  CWD
baseline              45711  2118440 idle          31s  /tmp/head-checkout
dut                   41239  2118203 idle       4m10s  /home/you/rtl
```

With more than one session running, say which you mean with `-s`, or set
`VIVADO_REMOTECTL_SESSION` once in the shell. With exactly one, `-s` is
optional. Getting this wrong is an error that lists the candidates rather
than a guess.

Each session is a separate Vivado process holding its own design, so they cost
about 3 GB each. Stop them before starting a full build, which sizes its job
count from total memory on the assumption that it owns the machine.

```sh
vrc stop dut      # graceful: asks it to exit, then verifies
vrc stop --all
```

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

Ctrl-C does not stop the script. Nothing can interrupt `synth_design` once it
has started, so `vrc` says which session is still working and gets out of the
way; reattach with `vrc log -f`. Passing `--timeout` behaves the same way and
exits 4.

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
| 1 | the script raised; `errorInfo` is on stderr |
| 2 | bad usage |
| 3 | no session, an ambiguous one, or nothing listening |
| 4 | `--timeout` expired; the script is still running |
| 5 | the session closed the connection |
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

One thing to know: Vivado runs one script at a time and nothing can interrupt
`synth_design`. So a second evaluation during a synthesis is refused outright
rather than queued behind twenty-five minutes of work, and `C-c v k` only
detaches this end — the script keeps going, which is why a long one should
`puts` its result as well as `return` it, so the answer survives in the log.

Tcl goes to the session through `vrc -f -` rather than through the prompt,
which is what makes a multi-line `foreach` work: `vrc repl` reads a line at a
time and the editor would split the block. The prompt is there for one-liners.

[contrib/emacs/README.md](contrib/emacs/README.md) works a real design through
the whole loop, and has the rest of the keys and settings.

## Commands

```
vrc [-s NAME] [-p PORT] -e SCRIPT | -f FILE  [--timeout SEC] [--tail]

vrc start [NAME]   launch a session   --vivado PATH --version VER --port N
                                      --cwd DIR --gui --wait SEC --arg ARG
vrc stop  [NAME]   shut one down      --all --force --timeout SEC
vrc list           what is running    --json --prune
vrc log   [NAME]   session output     -f/--follow -n LINES
vrc repl  [NAME]   interactive prompt
vrc ping  [NAME]   check it answers
vrc info  [NAME]   show the descriptor
vrc plugin-path    path to the Tcl plugin
```

A subcommand must be the first argument. That is why evaluating Tcl is spelled
`-e` rather than `vrc run`: `list`, `info` and `log` are all real Tcl commands
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
| `XDG_STATE_HOME` | base for the default descriptor directory |

Descriptors default to `~/.local/state/vivado-remotectl/`, under *state*
rather than cache on purpose: a cache cleaner removing the token of a live
half-hour synthesis would be a bad afternoon.

Without `--vivado` or `--version`, `vrc` looks at `$VRC_VIVADO`, then
`/tools/Xilinx/Vivado/*/bin/vivado` preferring 2023.2, then `PATH`.

## How it is put together

The Vivado side is one file of Tcl 8.5 — Vivado 2023.2 embeds 8.5.14, not 8.6
— that opens a loopback socket and evaluates what arrives at global scope. The
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

## Develop it

```sh
pip install -e '.[dev]'
pytest
```

The suite needs no Vivado and no hardware. It runs the real plugin on the
`tclsh8.5` that ships inside Vivado, so the interpreter under test is the one
production uses, and `tests/fake_vivado.sh` stands in for the executable so
the whole launch path — `setsid`, log redirection, readiness polling, killing
the process group — is exercised for real. If that interpreter is missing the
suite skips rather than silently testing 8.6.

One path the tests cannot cover is a real synthesis. After a change to the
plugin, check it by hand:

```sh
vrc start real --version 2023.2
vrc -e 'version -short'
vrc -f ooc.tcl --tail       # a genuine OOC synth
vrc list                    # reads "busy" while it runs
vrc stop real
```
