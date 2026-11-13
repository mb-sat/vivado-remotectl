#!/bin/bash
# Run every generated OOC run under <runs_dir> through a pool of vrc sessions.
#
#   run_pool.sh <runs_dir> <session>...
#
# The runs must already have their scripts (launch_runs -scripts_only) and the
# sessions must be running. Each session sources tcl/ooc_worker.tcl and pulls
# runs from the shared list. Settings (environment):
#
#   POOL_MAX_RUNS=12     restart a session after this many runs; each run
#                        leaves 40-80 MB behind in the Vivado process
#   POOL_MIN_FREE_GB=10  a session claims no run while less memory is free
#   POOL_WATCH_GB=4      below this, cancel the run in the largest busy
#                        session and put it back in the queue
#
# Exits 1 if any run failed for a reason other than the watchdog.
set -u
here=$(cd "$(dirname "$0")" && pwd)
runs_dir=$(cd "$1" && pwd); shift
sessions="$*"
: "${POOL_MAX_RUNS:=12}" "${POOL_MIN_FREE_GB:=10}" "${POOL_WATCH_GB:=4}"

# The same IP several times in a row (siggen_top_0..3) means the heaviest
# runs start together. Order by occurrence within each name-without-digits
# group, so the copies of one IP end up far apart in the queue.
runs=$(cd "$runs_dir" && ls -d *_synth_1 | grep -v '^synth_1$' |
       awk '{k=$0; gsub(/[0-9]+/, "", k); printf "%04d %s\n", n[k]++, $0}' |
       sort | awk '{printf "%s ", $2}')

unclaimed() {
    for r in $runs; do [ -e "$runs_dir/$r/.vrc_claim" ] || return 0; done
    return 1
}

mem_gb() { awk '/MemAvailable/ {printf "%d", $2/1048576}' /proc/meminfo; }

# While memory is low, cancel the run in the busy session with the largest
# RSS. The worker sees the marker, and puts the run back in the queue.
watchdog() {
    while [ -e "$runs_dir/.vrc_pool_running" ]; do
        if [ "$(mem_gb)" -lt "$POOL_WATCH_GB" ]; then
            victim=$(vrc list --json 2>/dev/null | python3 -c '
import json, os, sys
want, runs_dir = sys.argv[1].split(), sys.argv[2]
busy = [s for s in json.load(sys.stdin)
        if s["name"] in want and s["status"] == "busy"
        and os.path.exists(os.path.join(runs_dir, ".vrc_current_" + s["name"]))]
print(max(busy, key=lambda s: s["rss"])["name"] if busy else "")
' "$sessions" "$runs_dir")
            if [ -n "$victim" ]; then
                echo "watchdog: $(mem_gb) GB free, cancelling the run in $victim"
                touch "$runs_dir/.vrc_cancel_$victim"
                if vrc cancel "$victim" | grep -q 'nothing running'; then
                    rm -f "$runs_dir/.vrc_cancel_$victim"
                fi
                sleep 30
                continue
            fi
        fi
        sleep 3
    done
}

session_loop() {
    s=$1
    while unclaimed; do
        out=$(vrc -s "$s" \
                  -e "set ::ooc_session $s; set ::ooc_max_runs $POOL_MAX_RUNS; set ::ooc_min_free_gb $POOL_MIN_FREE_GB" \
                  -f "$here/tcl/ooc_worker.tcl" -e "ooc_worker $runs_dir $runs" 2>&1)
        rc=$?
        echo "$out" | sed "s/^/$s: /"
        [ $rc -eq 0 ] || return 1
        if unclaimed; then
            echo "$s: restarting after $POOL_MAX_RUNS runs"
            # Same Vivado, directory and options as the session had; the
            # pool's own memory checks replace vrc's 6 GB start refusal.
            vrc restart "$s" --min-free 0 >/dev/null || return 1
        fi
    done
}

t0=$(date +%s)
touch "$runs_dir/.vrc_pool_running"
watchdog &
wd=$!
rc=0
# A run requeued after its session's loop ended needs another round.
while unclaimed; do
    pids=
    for s in $sessions; do session_loop "$s" & pids="$pids $!"; done
    for p in $pids; do wait "$p" || rc=1; done
    [ $rc -eq 0 ] || break
done
rm -f "$runs_dir/.vrc_pool_running"
wait "$wd" 2>/dev/null
echo "pool wall: $(( $(date +%s) - t0 ))s"
exit $rc
