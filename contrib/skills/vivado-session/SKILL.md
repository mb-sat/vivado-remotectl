---
name: vivado-session
description: Drive a persistent Vivado session from the shell with `vrc` instead of repeated batch runs. Use for any Vivado work needing more than one run - comparing timing before and after an RTL change, sweeping generics, inspecting a critical path, checking utilization of one IP core, or holding a design open across several queries. Covers the out-of-context synthesis loop, running a baseline and a working tree side by side, starting a long run detached and collecting it with `vrc wait`, cancelling a running synth_design / place_design / route_design without losing the session, restarting a stuck session, running from a sandboxed shell, and cleaning up only your own sessions. Also building the out-of-context IP runs of a block-design project in a pool of sessions. NOT for a full project build, which stays batch.
---

# vivado-session

`vivado -mode batch` pays its forty-second startup on every invocation and
writes reports to disk that then have to be grepped. `vrc` keeps one Vivado
alive with the design loaded and hands back whatever the Tcl returned, so a
second measurement is seconds.

```console
$ vrc start dut
dut: listening on 127.0.0.1:41239 (pid 1439649)
log: /home/you/.local/state/vivado-remotectl/dut.log

$ vrc -e 'version -short'
2023.2.2

$ vrc stop dut
dut: stopped
```

There is one thing to send — Tcl — and it comes back as a string. What to send
is UG835, the Vivado Tcl command reference. This file is the workflow around
it. The tool's own README covers the flags, exit codes and environment
variables; what follows is when to reach for which. `vrc plugin-path` locates
the Tcl half.

## Start a session and send it Tcl

`vrc start` returns only once the session answers, so there is no poll loop to
write and no fixed sleep to guess:

```bash
vrc start dut --cwd ~/work/dut      # names it; kernel picks a free port
vrc -e 'set p [get_timing_paths -max_paths 1 -delay_type max]
        return "WNS=[get_property SLACK $p]"'
```

`-e` takes a script, `-f` a file, `-f -` stdin; both repeat and concatenate in
order. **There is no default timeout** — `vrc` waits as long as the script
takes, so a 30-minute `synth_design` needs no `--timeout` at all. Interpreter
state persists between calls, which is the point: an open design stays open.

`vrc -f script.tcl -- a b` passes `a b` to the script as `$::argv`, as under
`tclsh`.

A failing script prints Tcl's `errorInfo` to stderr and exits 1; the session
survives with its state intact. Exit 3 means no session or an ambiguous one,
4 a `--timeout` you set, 5 the session died, 6 `vrc cancel` stopped it. Tcl
passed without `-e`, or a `vrc start` option such as `--cwd` on an eval, is an
error that says what to type instead. `vrc status` is `vrc list`.

`vrc start` picks the Vivado on `PATH` (source `settings64.sh` first), else the
newest install it finds; `--version VER` or `$VRC_VIVADO_VERSION` pins one, and
`--vivado PATH` names the executable.

Vivado 2023.2 and older embed **Tcl 8.5**, 2024.1 onwards 8.6. Unless you know
the session is a newer one (`vrc info` shows `tcl`), write 8.5: no `try`,
`lmap`, `string cat` or `dict map` in anything you send.

Keep scratch files in one directory named after the task rather than loose in
`/tmp`; the examples below use `/tmp/vrc-timing/`.

## Run vrc from a sandboxed shell

The Claude Code sandbox gives each command its own loopback and pid namespace,
so a sandboxed `vrc` reaches no session at all. Put `vrc *` in the sandbox's
`excludedCommands`; that entry applies only when **every** part of the command
line matches. So make each call a bare command, or join only `vrc` calls with
`&&`. None of these get through:

- `cd dir && vrc ...` or `vrc ...; other` — use `--cwd` or an absolute path
- `vrc ... | tail` — read the whole output, or use `vrc log`
- `VIVADO_REMOTECTL_SESSION=x vrc ...` — use `-s x`
- `for ...; do vrc ...; done` — one call per point, or a Tcl `foreach`
- `until vrc list | grep -q idle; do sleep 15; done` — use `vrc wait NAME`

Inside the sandbox, `vrc list` shows sessions as `sandboxed`, connect errors
say so, and `vrc start` refuses to run. When you see that, re-issue the
command bare. Do not start another session.

## Compare a change against its baseline

Two sessions on separate ports, so the baseline stays synthesised while you
edit the working tree:

```bash
mkdir -p /tmp/vrc-timing/head
git archive HEAD path/to/sources | tar -x -C /tmp/vrc-timing/head
# regenerate anything the build normally generates, into the same tree

vrc start work     --cwd ~/work/dut
vrc start baseline --cwd /tmp/vrc-timing/head
```

```console
$ vrc list
NAME                   PORT      PID STATUS        FOR   UPTIME    RSS OWNER            CWD
baseline              45711  2118440 idle       1m02s    2m31s   4.6G claude:a4e3f104  /tmp/vrc-timing/head
work                  41239  2118203 busy       3m40s    4m10s   5.2G claude:a4e3f104  /home/you/work/dut
2 Vivado(s) hold 9.8G; 18.7G available
```

`FOR` is how long it has been idle or busy, `RSS` counts the Vivado and its
synthesis helpers, and `OWNER` is who started it (a Claude session's
subagents share their parent's id). The line under the table is on stderr, with
a `not a session: pid N` line for every Vivado that is not a session, such as a
batch build. `vrc list --mine` shows only yours.

Extract the baseline from git rather than stashing the working tree: `git
archive` touches neither your edits nor the index, so nothing has to be put
back afterwards. With more than one
session live, name it with `-s` (or set `VIVADO_REMOTECTL_SESSION` outside a
sandbox); a name that does not exist is an error that lists the ones that do.

Each session is its own Vivado holding its own design, so budget 4-6 GB each
once a design is synthesised. `vrc list` shows each one's `RSS` and, on
stderr, the total and what is still available; `vrc start` refuses below 6 GB
available (`--min-free GB` to change it), so there is no need to run a
separate free-memory check first.

## Synthesise one IP out of context

The loop that answers "did this RTL change cost timing":

```bash
mkdir -p /tmp/vrc-timing
cat > /tmp/vrc-timing/ooc.xdc <<'XDC'
create_clock -period 2.000 -name clk [get_ports clk]
create_clock -period 4.000 -name s_axi_aclk [get_ports s_axi_aclk]
set_clock_groups -asynchronous -group [get_clocks clk] -group [get_clocks s_axi_aclk]
XDC

cat > /tmp/vrc-timing/ooc.tcl <<'TCL'
close_design -quiet
close_project -quiet
read_verilog [list /path/to/ip/sources/*.v /path/to/ip/build/regs.v]
read_xdc /tmp/vrc-timing/ooc.xdc
synth_design -top <top> -part xcvc1902-vsva2197-2MP-e-S -mode out_of_context \
    -generic G_NSAMPLES=2048 -generic G_LANES=4 -generic G_DATA_WIDTH=8
set p [get_timing_paths -max_paths 1 -delay_type max]
return "WNS=[get_property SLACK $p] from=[get_property STARTPOINT_PIN $p] \
        to=[get_property ENDPOINT_PIN $p] levels=[get_property LOGIC_LEVELS $p]"
TCL

vrc -s work -f /tmp/vrc-timing/ooc.tcl --tail
```

Synthesis messages stream to stderr; the returned line is the only thing on
stdout. The backslash continuations inside the quotes leave the run of spaces,
so split on whitespace rather than a single space:

```
WNS=0.421 from=g_lane[0].acc_reg/DSP58C<0>_INST/DSP_A_B_DATA58_INST/CLK  to=g_lane[0].acc_reg/DSP58C<0>_INST/DSP_OUTPUT58_INST/ALU_OUT[0] levels=5
```

`close_design -quiet` first, then re-read: that is what makes the second
measurement cheap. `close_project -quiet` too — files read with `read_verilog`
land in an implicit in-memory project and stay there, so without it a later run
still holds the earlier one's files, an older definition of the module can win,
and the only sign is a `CRITICAL WARNING: [Synth 8-9873] overwriting previous
definition of module` in a log nobody is reading. `-generic` sweeps build
parameters without touching the packaging Tcl.

## Sweep a parameter without paying startup again

One request, one session, every point measured. `puts` goes to the session log,
so `--tail` shows each result as it lands instead of nothing for ten minutes:

```bash
vrc -s work --tail -e '
  foreach lanes {2 4 8 16} {
    close_design -quiet
    close_project -quiet
    read_verilog [list /path/to/ip/sources/*.v]
    read_xdc /tmp/vrc-timing/ooc.xdc
    synth_design -top <top> -part xcvc1902-vsva2197-2MP-e-S \
        -mode out_of_context -generic G_LANES=$lanes
    set p [get_timing_paths -max_paths 1 -delay_type max]
    puts "SWEEP lanes=$lanes WNS=[get_property SLACK $p]"
  }
  return done'
```

## Read results without being misled

`get_property SLACK` / `STARTPOINT_PIN` / `ENDPOINT_PIN` / `LOGIC_LEVELS` on a
`get_timing_paths` object is usually all you need, and the endpoint alone often
identifies the problem. For the summary line:

```bash
vrc -e 'foreach l [split [report_timing_summary -no_detailed_paths -return_string] "\n"] {
          if {[regexp {^\s+-?[0-9]+\.[0-9]+\s+-?[0-9]+\.[0-9]+\s+[0-9]+\s+[0-9]+} $l]} {
            return [string trim $l] } }'
```

That line is three groups of four — setup, hold, then pulse width — so the
first field is WNS and the fifth is WHS. The real fields are padded far wider
than this; split on whitespace:

```
0.421  0.000  0  1268    0.088  0.000  0  1268    0.394  0.000  0  821
```

**Do not count DSPs or BRAMs with a name glob.** `get_cells -hier -filter
{REF_NAME =~ DSP*}` counts a DSP58's internal primitives (`DSP_ALU`,
`DSP_MULTIPLIER`, `DSP_PREADD`, …), so it reports 14 cells per slice: measured
on a Versal out-of-context synthesis, 112 against 8 real `DSP Slices`. Read
`report_utilization`'s own rows instead:

```bash
vrc -e 'return [join [regexp -all -inline -line \
                {^\|\s+(?:DSP Slices|Block RAM Tile|CLB LUTs\*?)\s+\|[^\n]*} \
                [report_utilization -return_string]] "\n"]'
```

`-all` matters — plain `regexp -inline` returns only the first match, which
silently gives you the LUT row and drops the DSP and BRAM rows you asked for.
The rows come back verbatim, `| name | used | fixed | prohibited | available | % |`:

```
| CLB LUTs*                  |  216 |     0 |          0 |    899840 |  0.02 |
| Block RAM Tile           |    0 |     0 |          0 |       967 |  0.00 |
| DSP Slices         |    8 |     0 |          0 |      1968 |  0.41 |
```

## Run something long without waiting on it

Vivado's own progress goes to the session log. `--tail` mirrors it to stderr
while you wait and stdout stays clean, so `vrc -f ooc.tcl > wns.txt` still
gives a usable file. It is on by default on a terminal, off in scripts.

```bash
vrc log work -f          # follow; -n N for the last N lines
vrc list                 # reads "busy" while a synth_design is running
```

Ctrl-C only detaches: `vrc` says which session is still working and exits 130,
and the script keeps running. A busy session cannot answer anything else,
because Vivado's interpreter is single-threaded.

**Do not write poll loops** over `vrc list` waiting for `idle`. Start a long
script with `--detach`, which returns once it has started, do something else,
then collect it with `vrc wait`, which blocks until the session is free and
prints the result with the exit code the original call would have had:

```bash
vrc -s work --detach -f /tmp/vrc-timing/ooc.tcl   # returns once it has started
vrc wait work > /tmp/vrc-timing/wns.txt           # 0 ok, 1 Tcl error, 6 cancelled
```

`vrc wait work --timeout 600` gives up with exit 4 and names what is still
running; exit 5 means Vivado died, with the log tail. `vrc result work` prints
the last result at once, and says on stderr when a newer script is still
running. A busy session serves requests in the order their clients connected,
so `--detach` on one queues the script behind what is running. Only the last
result is kept: `vrc wait` returns the last script queued, so a script whose
result you need must write it to a file or be collected before the next.

The session keeps the reply to its last request on disk, so a result is not
lost with its client. When a shell call is killed or times out, Ctrl-C detaches
`vrc`, or a subagent dies mid-run, `vrc wait NAME` from any shell, including a
later session, gets the answer. Name the session in a handoff and that is all
the next session needs.

## Cancel a run that is going nowhere

`vrc cancel` stops the running `synth_design`, `place_design` or
`route_design` and keeps the session and its open design. On 2023.2 synthesis
stops within about 7 s, placement and routing within about 2 s. Start the long
script detached or in the background, since the cancel has to come from another
shell:

```bash
vrc -s work --detach -f ooc.tcl
vrc cancel work          # -> "work: cancelled", exit 0
vrc wait work            # exit 6, "cancelled by vrc cancel"
```

A call that was waiting exits 6 with `cancelled by vrc cancel` on stderr. After
a cancelled `place_design` or `route_design` the design is still open and the
step can be run again; a cancelled `synth_design` leaves no design.

| `vrc cancel` result | meaning, what to do |
|---|---|
| exit 0, `cancelled` | the session is idle again |
| exit 0, `nothing running` | no signal was sent |
| exit 3, `cannot be cancelled` | a GUI, an attached Vivado, or a session from an older `vrc` (`vrc info` lacks `cancel sigint`); restart it. Do not send it SIGINT by hand, which leaves it unable to answer |
| exit 3, `sandboxed` | re-issue `vrc cancel` as a bare command |
| exit 4, still busy | the script caught the error in a `catch` and continued; cancel again for the next step, or wait |
| exit 5, Vivado exited | the session crashed; `vrc restart NAME` starts it again with the same options |

Vivado 2023.2 crashed once in thirteen cancelled `route_design` runs, in the
router. Write a checkpoint before cancelling anything that took long to reach.
Cancel only sessions you started, by name.

## Restart a session that is stuck

After a cancelled `synth_design`, the next one in that session fails with
`invalid command name "NULL"`. Run `vrc restart NAME` before synthesising
again; it stops the session and starts it under the same name with the same
Vivado, working directory, `--arg` values and `--idle-stop`, but the design is
gone, so read it in again. A long series of runs also leaves 40 to 80 MB per
run behind, which a restart gives back. A busy session is killed at once, so
whatever it was running is lost. An attached Vivado, a GUI session and a
session from an older `vrc` cannot be restarted. The README section "Restart a
session that is stuck or no longer usable" has the rest.

## Do not leave sessions behind

A forgotten session holds 4-6 GB until someone notices. `vrc list` shows how
long each has been idle in `FOR`. `vrc start` itself refuses below 6 GB
available and lists which Vivados hold what. When you start one for a single
question, or from a subagent that may not get to clean up, give it a limit:

```bash
vrc start probe --idle-stop 30m --cwd /tmp/vrc-timing
```

It exits after that long without a request; `vrc list` and `vrc wait` do not
count, and a running synthesis is never cut off. Stop it yourself when done
anyway.

## Stop sessions before a full build

Each session holds 4-6 GB, and a project build script that sizes its job count
from `MemTotal` assumes it owns the host.

```bash
vrc stop work            # graceful, then verified
vrc stop --mine          # every session this Claude session started
vrc stop --all           # MACHINE-WIDE -- see below
```

To tidy up after yourself, use **`--mine`**, not `--all`. Every session records
who started it — this Claude session as `claude:<id>`, shared with its
subagents — and `vrc list` shows it in `OWNER`. On a shared host other people
and agents hold their own Vivados. `--all` leaves a busy session somebody else
started running, and says so, but it still stops their idle ones, which may
hold a design they mean to come back to.

The reverse also catches people: someone driving `vivado -mode batch` directly
never registers as a session. `vrc list` reports such processes on stderr as
`not a session: pid N`, with their memory, so read that part too before
starting a build that sizes itself from `MemTotal`. From a sandboxed shell the
pids are not visible and those lines are missing.

`vrc stop` only signals a process whose recorded `/proc` start time still
matches, so it cannot hit somebody else's Vivado on a recycled pid; an
unverifiable descriptor is reported and left alone. **Never `pkill -f vivado`**
— it matches your own shell's command line. Use `vrc list`.

`vrc list --prune` clears descriptors left by a session that died hard;
nothing is removed while its process is still there.

## Build a block design's OOC runs in a pool

A project with a block design synthesises each IP as its own out-of-context
run, and `launch_runs` starts a batch Vivado per run that reloads the part and
IP data (about 20 s per run on Versal). `batchbuild/run_pool.sh` in the
vivado-remotectl checkout executes the same generated run scripts in a few
persistent sessions instead. It pays when many OOC runs really synthesise; with
a warm IP cache Vivado skips most runs in either flow and there is little to
save. It is a proof of concept, not a `vrc` command and not on `PATH`. Read
`README-batchbuild.md` in the checkout before putting it into a build script,
since it covers calling it from inside Vivado (where `PYTHONHOME` and
`PYTHONPATH` must be cleared) and its memory settings.

1. In the parent project, generate the block designs, `create_ip_run` each,
   then `launch_runs [get_runs -filter {IS_SYNTHESIS && NAME != synth_1}]
   -scripts_only`. Skip block designs under `.gen/`.
2. Start the workers with the parent's Vivado version, after checking
   `vrc list` for free memory: `vrc start w0 --version <ver> --idle-stop 3h`,
   and the same for `w1`... A worker holds about 1 GB after its first run and
   grows 40-80 MB per run; large IPs add 1.6 GB helper processes.
3. Run `batchbuild/run_pool.sh <proj>.runs w0 w1 ...`. It can outlast one
   shell call, so run it in the background and read its output when it exits.
   It writes one line per run and `pool wall: Ns` at the end, and exits 1 if a
   run failed; that run reads `ERROR` in the parent and leaves its traceback in
   `vrc_error.txt` in its run directory. In a sandboxed shell it needs the same
   `excludedCommands` entry as `vrc`.
4. `vrc stop` the workers, then `launch_runs synth_1` in the parent. Vivado
   finds the OOC runs complete and launches only the top level.

What goes wrong:

- **Stale run directories.** The pool takes every `*_synth_1` directory under
  `<proj>.runs`. After an interrupted pool the `.vrc_claim` files count as
  done, so a second call skips those runs and can exit 0 with them unbuilt.
  `reset_runs` them first.
- **A failed pool.** Stop the build. `launch_runs synth_1` would rebuild the
  failed runs as forked Vivados while the workers still hold their memory.
- **No run logs.** Output goes to `vrc log <session>`; `runme.log` and
  `<ip>.vds` are not written.
- **No timeout, no retry.** A hung run or a wait for free memory blocks the
  pool; a worker that dies mid-run fails it.
- **Memory.** `POOL_MIN_FREE_GB` (10), `POOL_MAX_RUNS` (12, then `vrc restart`)
  and `POOL_WATCH_GB` (4, cancels the largest run and requeues it) are
  environment settings of `run_pool.sh`. In a sandboxed shell a `VAR=x` prefix
  breaks the bare-command rule, so export them where the shell starts instead.
- **A cancelled synthesis.** After `vrc cancel` of a `synth_design` the next
  one in that session fails with `invalid command name "NULL"`; the pool
  restarts such a worker itself.
- **Results.** Deterministic, but not guaranteed bit-identical to forked
  runs: the first `synth_design` in a process can differ by a few LUTs from
  later ones, so the worker primes itself with a throwaway synthesis. Fill
  one IP cache from one flow. Tested on 2023.2 only.

## Attach to a Vivado you started yourself

An interactive or GUI Vivado registers itself if `Vivado_init.tcl` sources the
plugin — `vrc plugin-path` prints the line to paste, and
`contrib/Vivado_init.tcl.example` has the whole file. It then shows up in
`vrc list` and takes `vrc -e` like any other session, and the GUI stays
responsive.

## What this is not for

A full project build stays batch; the pool above only takes over its
out-of-context runs. A project build launches dozens of out-of-context
IP syntheses in parallel for hours and manages its own project on disk, so
there is no state worth holding open between invocations and the memory is
better spent on jobs. `vrc` is for the measurements you take around such a
build, not the build.
