# Concept: a vrc pool for project builds

How the pool in this directory could become a `vrc` feature that any Vivado
project can use, from a build script, from CI, and from the GUI's *Generate
Bitstream* button. The measurements behind the pool are in
[README-batchbuild.md](../README-batchbuild.md); this note is about integration.

Status labels used below: **measured** (tried here), **read** (from code or
UG835), **untested** (a design assumption still to be checked).

## What there is to gain

A warm session saves the part and IP load that every forked OOC run repeats:
about 20 s per run, 31 % of the summed OOC run time on a 91-run block design
(measured, README-batchbuild). Top-level synthesis and implementation are one process
each, so a session gains nothing there. The "build" half of the feature is
therefore orchestration: memory-aware scheduling, one place for logs, and
clean failure and cleanup. It does not speed up synthesis or implementation.

The saving applies to runs that really synthesise. With a warm IP cache most
OOC runs are skipped, so the pool matters for cold builds, cache misses and CI
jobs that start from a fresh clone.

## Where to hook into Vivado

The PoC hooks in with `launch_runs -scripts_only` and then runs the generated
scripts itself. A project would need a script change for that, and the GUI has
no way to use it. Vivado has a better entry point.

### Custom cluster configuration (Vivado 2023.2: measured)

```tcl
create_cluster_configuration -type CUSTOM \
    -submit_cmd vrc-pool-submit -kill_cmd vrc-pool-kill vrc_pool
launch_runs synth_1 -cluster_configuration vrc_pool
```

Tried on the nine-IP test project with logging shell scripts as the submit and
kill commands:

- `create_cluster_configuration` validates the configuration by submitting a
  `cluster_test/test.sh` and then calling the kill command with the job id it
  parsed. A submit command that prints `Job <N> is submitted` (LSF format) is
  understood. `-job_id_proc` exists for other formats.
- `launch_runs synth_1 -cluster_configuration vrc_pool` called the submit
  command once per run with a single argument, the absolute path of that run's
  `runme.sh`. All eight pending OOC runs were submitted within one second;
  `synth_1` was submitted after they had all finished. Vivado keeps the
  dependency order, and the cluster side decides the concurrency.
- With `-jobs 2`, the submissions came two at a time, each one when an
  earlier run finished. Without `-jobs`, everything ready was submitted at
  once. `-jobs` is therefore the cap on what the pool receives.
- Completion is detected from the marker files in the run directory, the
  same files the PoC worker already writes. `wait_on_run` returned and all
  runs showed `100% synth_design Complete!`. In these tests the submit
  command ran the stock `runme.sh` detached. No warm worker has served a
  cluster submission yet; that is the first check below.
- The configuration persists in the user's Vivado settings: a fresh process
  listed it. The file was not found under `~/.Xilinx` or `~/.config`.
  `remove_cluster_configurations` needs the object
  (`[get_cluster_configurations vrc_pool]`); the bare name is rejected.
- The run property `CLUSTER_CONFIGURATION` is written to the `.xpr`
  (`ClusterConfiguration="vrc_pool"` on each `<Run>`) and survives
  `reset_runs`. A plain `launch_runs` ignores it and forks locally, even
  with the property set by hand. A batch launch uses the pool only when
  `-cluster_configuration` is given.

The parent stays in charge of what to run, in what order, and what is out of
date. The pool only executes what it is given, so it never has to work out
the run list or deal with stale runs, which were the weakest parts of the PoC
hook.

### Older Vivado

Vivado 2019.1 has no `create_cluster_configuration`, but `launch_runs -lsf
<bsub command line>` gives the same mechanism (measured). Tried on a 2019.1
copy of the test project (`xc7z020clg400-1`, `blk_mem_gen` in place of
`emb_mem_gen`), with logging stand-ins for `bsub`, `bjobs` and `bkill`:

```tcl
launch_runs synth_1 -jobs 2 -lsf {vrc-pool-submit -q pool}
```

- Vivado runs the given command line with `-J <runme.sh> <runme.sh>`
  appended, e.g. `vrc-pool-submit -q pool -J /…/top_xbar_0_synth_1/runme.sh
  /…/top_xbar_0_synth_1/runme.sh`. The run script is the last argument.
- The submit command printed `Job <N> is submitted to default queue
  <normal>.`
- `-jobs 2` limited the submissions to two at a time. All eight OOC runs and
  then `synth_1` were submitted in dependency order, and all finished at
  `100% synth_design Complete!`.
- `bjobs` and `bkill` were never called. Vivado detected completion from the
  run directory alone.
- Nothing has to be created first and nothing is stored in user settings.
  The `-lsf` string is the whole configuration.

That makes `-lsf` the entry point for Vivado versions without cluster
configurations. Versions older than 2019.1 are assumed to behave the same.
The version that introduced cluster configurations lies between 2019.1 and
2023.2 and has not been looked up. One `vrc-pool-submit` can handle both
calling conventions, since the run script is the last argument in both.
The `-scripts_only` hook from the PoC stays as the last fallback.

### Hooks that were rejected

- `launch_runs -host {localhost N} -remote_cmd <shim>` fails on Ubuntu before
  it calls the shim. Vivado's host check runs through `/bin/sh` with bash-only
  redirection (`sh: 1: Syntax error: Bad fd number`, then `Could not connect
  to host`) (measured). It may work where `/bin/sh` is bash, but it is not
  portable.
- Renaming and wrapping `launch_runs` in `Vivado_init.tcl` would catch the GUI
  too. However, it depends on how the GUI dispatches commands internally, and
  it changes behaviour for every project. Not pursued.

## Components in vrc

```
 parent Vivado (GUI, batch, or vrc session)
   │ launch_runs ... -cluster_configuration vrc_pool
   ▼
 vrc-pool-submit <run>/runme.sh   ──►  pool manager (one per user or per CI job)
 vrc-pool-kill <id>                     │ queue, memory checks, watchdog, logs
                                        ├─► worker sessions: OOC synth runs (ooc_worker.tcl)
                                        └─► forked runme.sh: everything else
```

**Pool manager** (`vrc pool start|stop|status`). A small long-lived process
that owns N worker sessions. It holds the queue in memory and replaces the
`.vrc_claim` files. It also holds the memory policy, the restart rules, the
watchdog and the per-run timeout that `run_pool.sh` and `ooc_worker.tcl` have
now, and the queue order that keeps copies of one IP apart: Vivado submits
every ready OOC run at once, so the manager still sees the whole set to
reorder. It starts on the first submit if it is not already running, and stops
after an idle period.

**Submit and kill commands.** Two thin executables that talk to the manager
and return at once.

- `vrc-pool-submit` gets the path of a `runme.sh`. It works out what kind of
  run that is from the run script: OOC synthesis has `-mode out_of_context`
  and `project.vivado.isBlockSynthRun`. It queues the run and prints
  `Job <id> is submitted`.
- Creating the configuration submits a validation job
  (`cluster_test/test.sh`) and kills it straight away. The submit command must
  accept a script that is not a run, and the kill command must handle that
  job id.
- `vrc-pool-kill <id>` drops a queued run or cancels a running one: `vrc
  cancel` and a session restart for a worker run, or killing the process group
  for a forked one.
- **The kill command alone is not enough.** In batch, `reset_runs` on a
  running pool job called neither the cluster kill command (2023.2) nor
  `bkill` (2019.1) (measured). The run was reported `Not started`, while the
  job kept running and later wrote its markers into the run directory. The
  manager therefore also watches each running job's run directory. If the
  directory is deleted or its `.vivado.begin.rst` changes, it cancels the job,
  so that it neither wastes a worker nor writes into a fresh run.
- Both clear `PYTHONHOME`/`PYTHONPATH` first. A Vivado parent exports them for
  its bundled Python, and a system-Python `vrc` cannot start with them
  (measured in the PoC's build-script hook).

**Two kinds of execution.**

- OOC synthesis runs go to a warm worker, as `ooc_worker.tcl` does now.
- Every other run (`synth_1`, `impl_1`, child implementation runs) is executed
  as the plain `runme.sh` in a forked process. It is still counted against the
  manager's memory budget, so one policy covers the whole build.
- An unknown run type always takes the forked path. That keeps the pool
  correct for run types it was never tested with.

**Worker Tcl.** `ooc_worker.tcl` largely as it is. The run comes from the
manager instead of from claim files. The worker also copies its section of the
session log into the run's `runme.log`, so the parent's log view, the GUI's
*Log* tab and CI artifact globs find it.

**`vrc build`.** A driver for scripted builds and CI:
`vrc build proj.xpr --to synth|impl|bitstream [--workers N]`, or with a project
Tcl script in place of the `.xpr`. It opens the project in a session, ensures
the cluster configuration exists, launches with it, waits, prints a per-run
summary and the build report, stops its pool, and exits non-zero on any failed
run.

## Workflow: scripted builds

Two levels of adoption:

1. **One-line change to an existing build script.** Create the configuration
   with `-force` at the start of the script (where Vivado stores it is not
   known yet, so do not rely on it being there), and add
   `-cluster_configuration vrc_pool` to the existing `launch_runs synth_1` /
   `launch_runs impl_1` calls. Set `-jobs` high, or leave it out, so the pool
   decides the concurrency from free memory. Nothing else in the script
   changes: dependencies, `wait_on_run` and progress checks behave as before.
2. **`vrc build`** for projects without their own driver, and for CI.

A switch keeps the plain flow available: if `VRC_POOL` is unset, the script
launches as it does today. The build itself stays correct without the pool.

## Workflow: GUI, "Generate Bitstream"

No GUI step has been tried yet. This section describes the intended flow;
everything in it is untested.

1. One-time setup: run `vrc_pool_setup` in the Tcl console. A `Vivado_init.tcl`
   line or a custom toolbar command (`create_gui_custom_command`) can provide
   it. It creates the cluster configuration pointing at the user's installed
   `vrc-pool-submit`/`-kill`.
2. In the *Launch Runs* dialog that *Generate Bitstream* opens, choose the
   cluster option and `vrc_pool`. Since a plain batch `launch_runs` ignores
   the stored run property, expect to choose it on each launch, unless the
   dialog preselects it from that property.
3. The GUI is expected to show run progress as usual, because it reads the
   same markers. *Reset Runs* probably does not call `vrc-pool-kill`, as in
   batch, so the manager's watch on the run directory takes care of it.

To check:

- whether the dialog lists CUSTOM configurations, what it is called there,
  and whether it preselects the run's stored configuration;
- progress display, *Reset Runs* and cancel with a pool run;
- on versions without cluster configurations: whether the dialog offers LSF
  with a free-form `bsub` command line;
- what Vivado does with a configuration name it does not know. A committed
  `.xpr` carries `ClusterConfiguration="vrc_pool"` to machines without the
  pool. In batch the property is ignored, so builds should be unaffected,
  but the GUI dialog may complain. Projects regenerated from Tcl do not carry
  it.

## CI

`vrc build` in the job. What the job needs:

- **Isolation.** Set `XDG_STATE_HOME` to a directory per job, and prefix the
  session names with the job id. Concurrent jobs on a shell runner share one
  user, so `vrc stop --mine` must not be used there.
- **Cleanup on cancel or timeout.** vrc starts Vivado in its own process
  group, so the CI system's kill of the job does not reach the workers (read). Three
  layers:
  - `--idle-stop` on every session;
  - an `after_script` running `vrc pool stop`;
  - in the vrc Tcl plugin, a watch on the manager's pid so that a worker exits
    when its manager is gone.
- **Memory on a shared host.** The free-memory checks see other jobs' memory
  use. Waiting for memory needs a time limit after which the run fails with a
  clear message, not an unbounded wait.
- **Licences.** A session probably keeps its synthesis licence until it exits
  (untested), so N workers hold N floating seats for the whole OOC phase,
  including idle gaps.
- **Artifacts.** Collect `runme.log` per run (written by the worker, see
  above), the pool manager's log, and the session logs from the per-job
  state directory.
- **Vivado version.** Workers must use the parent's Vivado. Pass it to the
  first `vrc start` (`--vivado` or `--version`); `vrc restart` keeps it.

## Robustness requirements

Before this can be the default in a build, the pool must guarantee:

1. **Every accepted job ends with a marker.** The parent waits on
   `.vivado.end.rst` or `.vivado.error.rst`. If a worker dies (OOM kill,
   crash, cancel), the manager writes the error marker or requeues the run. A
   job that never gets a marker hangs the parent's `wait_on_run` or the GUI.
2. **A per-run timeout and a capped number of retries.** Each requeue (after
   the watchdog fires or a worker dies) counts; the run fails after the
   second.
3. **Restarts never block the pool.** Restart workers with `vrc restart
   --min-free 0` and the pool's own memory policy, not the 6 GB default
   refusal (the lowest free memory in the 91-run measurement was 7.2 GB).
   `vrc restart` keeps the worker's Vivado, directory and options, and it
   kills a busy worker outright. Measured on 2023.2 in the middle of a
   SmartConnect `synth_design`: back up in 3 s, with no
   `parallel_synth_helper` left behind. A worker that cannot be restarted stops
   taking runs, and the remaining runs move to the other workers or to the
   forked path.
4. **Session state is reset by snapshot, not by a list.** The run scripts set
   parameters and message configuration that the current reset list misses
   (`set_msg_config -msgmgr_mode ooc_run`, an `HDL-1065` limit). Snapshot
   `report_param` and the message configuration when a worker starts, and
   restore whatever differs after each run. This also holds when another
   Vivado version adds new parameters.
5. **Priming stays.** One throwaway `synth_design` per worker, so that every
   run is a "later" synthesis and the results are deterministic (README-batchbuild).
6. **Machine-readable status.** The watchdog uses `vrc list --json`, not
   column positions.
7. **A forked fallback per run.** An unknown run type, a Vivado version
   without a tested worker, or a worker failure moves the run to plain
   `runme.sh`. This replaces the PoC's "stop the build" rule, which was only
   needed because the old fallback ran beside workers that still held their
   memory. Under one manager, the memory budget covers both paths.

## Equivalence policy

A pool-built netlist is deterministic, but it is not always identical to a
forked one. A SmartConnect came out 4 LUTs larger in every synthesis after a
process's first, in any persistent Vivado (measured, README-batchbuild). On the 91-run
block design no IP differed. As a policy:

- one IP cache is filled by one flow;
- a release is built with one flow, recorded in the build report;
- a periodic CI job builds the same commit both ways and diffs the
  utilization per run. That catches a Vivado update that changes the
  behaviour.

## Checks before building it, in order

1. On 2023.2, use a submit command that puts runs into the existing worker,
   on the test project and on a large block design. Confirm the parent
   accepts the results.
2. Check how the IP cache combines with a cluster launch: are cache hits ever
   submitted, and does a worker-built run fill the cache?
3. The GUI questions listed above.
4. Vivado 2024.2 behaves like 2023.2 (2019.1 with `-lsf`: done, see above).
5. Kill the parent mid-build, and kill a worker mid-run; both must leave
   nothing running and no run without a marker.
