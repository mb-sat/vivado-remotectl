---
name: vivado-session
description: Drive a persistent Vivado session from the shell with `vrc` instead of repeated batch runs. Use for any Vivado work needing more than one run - comparing timing before and after an RTL change, sweeping generics, inspecting a critical path, checking utilization of one IP core, or holding a design open across several queries. Covers the out-of-context synthesis loop and running a baseline and a working tree side by side, and cancelling a running synth_design / place_design / route_design without losing the session. NOT for a full project build, which stays batch.
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
variables; what follows is when to reach for which.

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

A failing script prints Tcl's `errorInfo` to stderr and exits 1; the session
survives with its state intact. Exit 3 means no session or an ambiguous one,
4 a `--timeout` you set, 5 the session died, 6 `vrc cancel` stopped it.

Vivado 2023.2 and older embed **Tcl 8.5**, 2024.1 onwards 8.6. Unless you know
the session is a newer one (`vrc info` shows `tcl`), write 8.5: no `try`,
`lmap`, `string cat` or `dict map` in anything you send.

Keep scratch files in one directory named after the task rather than loose in
`/tmp`; the examples below use `/tmp/vrc-timing/`.

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
NAME                   PORT      PID STATUS     UPTIME  CWD
baseline              33805  1439725 idle          12s  /tmp/vrc-timing/head
work                  37001  1439649 busy        3m41s  /home/you/work/dut
```

Extract the baseline from git rather than stashing the working tree — `git
archive` touches neither your edits nor the index, so nothing has to be put
back afterwards. With more than one
session live, name it with `-s` or set `VIVADO_REMOTECTL_SESSION`; guessing is
an error that lists the candidates.

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

## Watch a long run

Vivado's own progress goes to the session log. `--tail` mirrors it to stderr
while you wait and stdout stays clean, so `vrc -f ooc.tcl > wns.txt` still
gives a usable file. It is on by default on a terminal, off in scripts.

```bash
vrc log work -f          # follow; -n N for the last N lines
vrc list                 # reads "busy" while a synth_design is running
```

Ctrl-C only detaches: `vrc` says which session is still working and exits 130,
and the script keeps running. A busy session cannot answer anything at all,
because Vivado's interpreter is single-threaded; requests queue until it is
free.

**Do not write poll loops** over `vrc list` waiting for `idle`. Start a long
script with `--detach`, which returns once it has started, and collect it with
`vrc wait`, which blocks until the session is free and prints the result with
the exit code the original call would have had:

```bash
vrc -s work --detach -f /tmp/vrc-timing/ooc.tcl
vrc wait work > /tmp/vrc-timing/wns.txt      # 0 ok, 1 Tcl error, 6 cancelled
```

The session keeps the reply to its last request on disk, so the same `vrc wait
work` recovers a result whose client died, timed out or was interrupted. `vrc
result work` prints it without waiting. `--detach` refuses a busy session.

## Cancel a run that is going nowhere

`vrc cancel` stops the running `synth_design`, `place_design` or
`route_design` and keeps the session and its open design. On 2023.2 synthesis
stops within about 7 s, placement and routing within about 2 s. Start the long
script in the background, since the cancel has to come from another shell:

```bash
vrc -s work -f ooc.tcl &
vrc cancel work          # -> "work: cancelled"; the waiting vrc exits 6
```

After a cancelled `place_design` or `route_design` the design is still open and
the step can be run again; a cancelled `synth_design` leaves no design.

| `vrc cancel` result | meaning |
|---|---|
| exit 0, `cancelled` | the session is idle again |
| exit 0, `nothing running` | no signal was sent |
| exit 3, `cannot be cancelled` | a GUI, an attached Vivado, or a session from an older `vrc`; restart it. Do not send it SIGINT by hand, which leaves it unable to answer |
| exit 4, still busy | the script caught the error in a `catch` and continued |
| exit 5, Vivado exited | the session crashed; start it again |

Vivado 2023.2 crashed once in thirteen cancelled `route_design` runs, in the
router. Write a checkpoint before cancelling anything that took long to reach.
Cancel only sessions you started, by name.

## Do not leave sessions behind

A forgotten session holds 4-6 GB until someone notices. `vrc list` shows how
long each has been idle in `FOR`. When you start one for a single question, or
from a subagent that may not get to clean up, give it a limit:

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
— it matches your own shell's command line. To check what is running, use
`ps -eo pid,etime,args | grep unwrapped/lnx64.o/vivado`, not `pgrep -f`.

`vrc list --prune` clears descriptors left by a session that died hard;
nothing is removed while its process is still there.

## Attach to a Vivado you started yourself

An interactive or GUI Vivado registers itself if `Vivado_init.tcl` sources the
plugin — `vrc plugin-path` prints the line to paste, and
`contrib/Vivado_init.tcl.example` has the whole file. It then shows up in
`vrc list` and takes `vrc -e` like any other session, and the GUI stays
responsive.

## What this is not for

A full project build stays batch. Those runs launch dozens of out-of-context
IP syntheses in parallel for hours and manage their own project on disk, so
there is no state worth holding open between invocations and the memory is
better spent on jobs. `vrc` is for the measurements you take around such a
build, not the build.
