# OOC synthesis runs in a pool of vrc sessions

A Vivado project with a block design synthesises every IP in the design as a
separate out-of-context (OOC) run: a synthesis of that IP alone, whose
netlist the top-level synthesis later reads as a black box. `launch_runs`
starts a new `vivado -mode batch` process for each of these runs, and every
one of those processes loads the part and the IP data again before it
synthesises anything. On a design with 91 such runs this load takes about
20 s per run, 31 % of the summed run time.

The scripts in `batchbuild/` run the same generated run scripts in a few
persistent `vrc` sessions instead, so the load happens once per session. The
parent project accepts the result unchanged, because it learns the state of a
run only from the marker files in the run directory.

```console
# in the parent project (a vrc session, batch Tcl or the GUI's Tcl console):
#   launch_runs [get_runs -filter {IS_SYNTHESIS && NAME != synth_1}] -scripts_only
$ vrc start w0 --version 2023.2 --idle-stop 3h
$ vrc start w1 --version 2023.2 --idle-stop 3h
$ batchbuild/run_pool.sh proj/proj.runs w0 w1
w1: top_bram_0_synth_1                       ok       start=1794569135   24.2s
w1: top_gpio0_0_synth_1                      ok       start=1794569159   15.1s
...
pool wall: 156s
$ vrc stop w0; vrc stop w1
# then, in the parent: launch_runs synth_1   (launches only the top level)
```

The scripts work and have built complete designs, but they are not yet a
`vrc` command. [batchbuild/concept.md](batchbuild/concept.md) describes how
they become one, and the section [What is not handled
yet](#what-is-not-handled-yet) lists what a production build would miss.

## Build a project's OOC runs in the pool

The pool executes runs that already have their scripts and does not create
them. The procedure has four steps:

1. In the parent project, generate the block design and create the OOC runs,
   then write their scripts without launching them:

   ```tcl
   foreach bd [get_files -quiet -of_objects [get_filesets sources_1] *.bd] {
       # Block designs nested inside IPs (the NoC, for example) sit under
       # .gen/; their parent generates them, and they refuse generate_target.
       if {[string first ".gen/" $bd] >= 0} continue
       generate_target all $bd
       create_ip_run $bd
   }
   launch_runs [get_runs -filter {IS_SYNTHESIS && NAME != synth_1}] -scripts_only
   ```

2. Start the worker sessions with the same Vivado version as the parent
   project. Each session needs about 1 GB after its first run and grows from
   there (see [Keep the pool within memory](#keep-the-pool-within-memory)).

3. Run `batchbuild/run_pool.sh <proj>.runs <session>...`. It returns when every
   run has finished, prints one line per run, and exits 1 if any run failed.
   A failed run appears as `ERROR` in the parent, with the Tcl traceback in
   `vrc_error.txt` in its run directory.

4. Stop the sessions before `launch_runs synth_1`, so that the top-level
   synthesis has their memory, then launch it as usual. Vivado finds every
   OOC run complete and launches only `synth_1`.

`run_pool.sh` uses the run directories it finds under `<proj>.runs`
(`*_synth_1` except `synth_1`). A long-lived project can hold directories of
runs that no longer exist or are already complete. Delete those, or reset the
runs, before starting the pool.

## Add the pool to a build script

A build script that ends in `launch_runs synth_1` needs the four steps above
between creating the project and that call. Two details are specific to
calling the pool from inside Vivado.

Vivado exports `PYTHONHOME` and `PYTHONPATH` for its bundled Python to every
child process. `vrc` runs on the system Python and fails to start with them
set (`No module named 'encodings'`). Clear them for every call:

```tcl
set clean_env [list env -u PYTHONHOME -u PYTHONPATH]
set runs_dir [get_property DIRECTORY [current_project]]/[current_project].runs
set pool_rc [catch {exec {*}$clean_env $pool_cmd $runs_dir {*}$sessions >@ stdout 2>@ stderr} msg]
foreach s $sessions { catch {exec {*}$clean_env vrc stop $s} }
if {$pool_rc} { puts "ERROR: OOC pool failed: $msg"; exit 1 }
launch_runs synth_1 -jobs $jobs
```

If the pool fails, the build should stop rather than fall back to forked
runs. `launch_runs synth_1` would rebuild every failed run as a forked
Vivado, and in one test the fallback ran while the workers still held their
memory and the host ran out of memory. Stopping the sessions first and then
falling back would also be correct.

## Keep the pool within memory

A worker keeps part of what its runs leave behind. Measured in one session on
a Versal design, `VmRSS` rises by about 0.9 GB on the first run (part and IP
data) and then by 40 to 80 MB per run, with no design or project open. Large
IPs also start parallel-synthesis helper processes of about 1.6 GB each, as
they do in a forked run. Four copies of one large signal-generator IP started
together took free memory from 16.7 GB to 5 GB in 15 seconds.

`run_pool.sh` has four controls for this. Three are set in the environment:

| setting | default | effect |
|---|---|---|
| `POOL_MIN_FREE_GB` | 10 | a worker claims no run while less memory is available |
| `POOL_MAX_RUNS` | 12 | a worker is restarted (`vrc restart`) after this many runs |
| `POOL_WATCH_GB` | 4 | below this, the run in the busy worker with the largest RSS is cancelled and put back in the queue |
| queue order | | copies of one IP (`siggen_top_0` … `_3`) are spread through the queue, not adjacent |

A cancelled `synth_design` leaves the session unable to synthesise again: the
next one fails with `invalid command name "NULL"`. A worker whose run was
cancelled therefore returns at once, and `run_pool.sh` restarts it before it
takes another run.

## What the pool saves

The saving is the part and IP load that a forked run repeats. It is roughly
constant per run, so it matters for designs with many small IPs and little
for a few large ones. It applies only to runs that really synthesise: with a
warm IP cache, Vivado skips most OOC runs in either flow.

On a test project with nine IPs on `xcvc1902-vsva2197-2MP-e-S`
(`batchbuild/tcl/create_project.tcl`), with Vivado 2023.2, four parallel jobs
in both flows and the IP cache disabled:

| flow | 9 OOC runs, wall | per run |
|---|---|---|
| `launch_runs -jobs 4` | 115 s | 34–58 s, of which 10–14 s outside `synth_design` |
| pool of 4, fresh sessions (including priming) | 88 s | 24–26 s first, 15–22 s after |
| pool of 4, warm sessions | 65 s | 15–17 s small IPs, 34 s SmartConnect |

On a Versal design with 91 OOC runs (synthesis only, IP cache disabled, four
workers, a 62 GB host), both flows were run to completion:

| | forked `launch_runs` | pool of 4 sessions |
|---|---|---|
| sum of OOC run times, 91 runs | 5659 s | 3921 s |
| OOC wall time | about 1660 s | 1049 s |
| top-level `synth_1` | 115 s | 117 s |
| utilization (LUT, FF, BRAM, URAM, DSP) of all runs and the top | | identical |
| lowest free memory | ran short at 4 jobs, finished at 3 | 7.2 GB, watchdog never fired |

Runs under 100 s each saved about 20 s. The nine runs over 100 s saved
8.8 s each on average, and three of them (two ILAs and an AXI SmartConnect)
were 3 to 8 s slower in the pool. The forked wall time includes the work lost
when that build ran short of memory, so the per-run sums are the comparison.

## Results that differ from a forked run

Eight of the nine test-project netlists are identical to the forked flow. The
SmartConnect has 662 LUTs from the pool and 658 from `launch_runs`, with all
4 LUTs inside `sc_mmu_v1_0_12_top`. The cause is inside `synth_design`: the
first synthesis in a process produces 658, and every later one 662, even
when the earlier one synthesised a single XOR gate. A plain batch Vivado that
first synthesises a trivial module also gives 662.

The Tcl state that `synth_design` leaves behind is not the cause. It leaves
about 300 variables in `::rt` and 40 globals. Unsetting the new globals does
not change the result, and unsetting or restoring the `::rt` variables makes
the next `synth_design` fail with `can't set "rt::checksumFileList": parent
namespace doesn't exist`.

The worker therefore runs one throwaway synthesis (`ooc_prime`) before its
first real run, so that every run in the pool is a later run. Two pool rounds
then gave identical results for all nine IPs, and on the 91-run design no IP
differed from the forked flow. The pool is deterministic, but it is not
guaranteed to be bit-identical to `launch_runs`. Any repeated `synth_design`
in one process behaves the same way, including OOC measurements in an
ordinary `vrc` session: the first measurement in a session can differ from
the later ones. One IP cache should be filled by one flow.

## How a run is executed

`launch_runs -scripts_only` writes `runme.sh`, `ISEWrap.sh` and `<ip>.tcl`
into each run directory and starts nothing. `batchbuild/tcl/ooc_worker.tcl`
does inside the session what the two shell scripts would do:

1. It changes into the run directory and writes `.vivado.begin.rst`.
2. It sources `<ip>.tcl` at global scope, as `vivado -source` would.
3. It writes `.vivado.end.rst`, or `.vivado.error.rst` and `vrc_error.txt`.
4. It closes the in-memory project and resets the parameters the run script
   set for a fresh process.

Every session gets the same list of runs. A session claims a run by creating
`.vrc_claim` in its directory with `O_EXCL`, which only one process can do,
so the list works as a shared queue and a slow IP does not hold up the
others. `reset_runs` deletes the run directory and the claim with it.

## What `vrc pool run` covers, and what it does not

`vrc pool run` (see the main [README](README.md)) is the generic part of this
pool as a `vrc` command: a queue of Tcl jobs over several sessions, with a
memory floor before every dispatch, a restart after N jobs, a per-job timeout
and a retry when a session dies. It does not replace `run_pool.sh` for a
project's OOC runs. Three things here have no counterpart in it yet: the
claim files that take the run list from a `.runs` directory, the reset of
parameters and messages that `ooc_worker.tcl` does between two runs, and the
watchdog that cancels the largest run when memory gets low mid-run.

## What is not handled yet

These gaps matter for unattended builds. Each is addressed in
[batchbuild/concept.md](batchbuild/concept.md).

- **Logs.** A run's output goes to the session log
  (`vrc log <session>`), so `runme.log` and `<ip>.vds` are not written and the
  parent's log view of the run is empty.
- **Interrupted pools.** A claim left by an interrupted pool counts as done.
  A second `run_pool.sh` on the same directory then skips that run and can
  exit 0 with it unbuilt. Reset the runs before running the pool again.
- **Hung runs.** There is no per-run timeout. Waiting for free memory has no
  time limit either, so on a shared host another process can stall every
  worker.
- **Crashed workers.** A worker that dies mid-run fails the pool; the run is
  not retried.
- **Session lifecycle.** The caller starts and stops the sessions. Sessions
  are started in their own process group, so killing the build does not stop
  them; `--idle-stop` is the backstop.
- **Vivado versions.** Tested with 2023.2. The parameters the worker resets
  after a run were collected on that version.

## Where this is going

Vivado can hand runs to an external scheduler itself. With
`launch_runs -cluster_configuration` (Vivado 2023.2) or `launch_runs -lsf`
(Vivado 2019.1), it calls a submit command once per run with the path of the
run's `runme.sh`, keeps the dependency order, honours `-jobs`, and detects
completion from the same marker files. Both were tested with a logging
stand-in for the submit command. A `vrc pool` that acts as that scheduler
removes the `-scripts_only` step and the run list, and it works from the
GUI's *Generate Bitstream* as well as from scripts.
[batchbuild/concept.md](batchbuild/concept.md) has the measurements, the
proposed components and the checks still to make.

## Files

| file | purpose |
|---|---|
| `batchbuild/run_pool.sh` | `run_pool.sh <runs_dir> <session>...`: shares all runs under `runs_dir` across the named sessions, checks memory, restarts and guards the workers, waits for the end |
| `batchbuild/tcl/ooc_worker.tcl` | `ooc_worker <runs_dir> <run>...`, sourced into each session by `run_pool.sh` |
| `batchbuild/tcl/create_project.tcl` | builds the nine-IP test project: `vivado -mode batch -source create_project.tcl -tclargs <dir> [part]` |
| `batchbuild/concept.md` | how the pool becomes a `vrc pool` and `vrc build` feature |
