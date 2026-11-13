# Run Vivado-generated OOC synthesis runs inside a persistent session.
#
# Source this in a vrc session, then call
#
#   ooc_worker <runs_dir> <run_name>...
#
# Several sessions may be given the same run list: each run is claimed with an
# atomic O_EXCL create of .vrc_claim, so the sessions share the list as a work queue. A run is
# executed the way runme.sh + ISEWrap.sh would: cd into the run directory, the
# .vivado.begin.rst marker, the generated <ip>.tcl, then .vivado.end.rst or
# .vivado.error.rst. The project that launched the runs reads only these files,
# so it cannot tell who ran them.
#
# Returns one line per run this session executed: name, status, seconds.
# If any of them failed, the same lines come back as a Tcl error.

# A session's first synth_design and every later one do not give the same
# netlist for every IP: the smartconnect comes out 658 LUTs on the first and
# 662 on any later run, even when the earlier run was a two-gate module. The
# state is inside synth_design; unsetting the Tcl variables it leaves in ::rt
# and :: does not change the result, and restoring them breaks the next run.
# ooc_prime runs one throwaway synthesis so that every real run is a "later"
# one and the pool is deterministic.

proc ooc_prime {part} {
    if {[info exists ::ooc_primed]} return
    # Named per process: sessions restarted together share a working directory.
    set f [file join [pwd] .ooc_prime_[pid].v]
    set fh [open $f w]
    puts $fh "module ooc_prime(input a, b, output y); assign y = a ^ b; endmodule"
    close $fh
    close_project -quiet
    read_verilog $f
    synth_design -top ooc_prime -part $part -mode out_of_context -quiet
    close_design -quiet
    close_project -quiet
    file delete $f
    set ::ooc_primed 1
}

proc ooc_reset_session {} {
    catch {namespace delete ::optrace}
    close_design -quiet
    close_project -quiet
    # The run scripts set these for a fresh process; undo them for the next.
    catch {reset_msg_config -quiet -id {IP_Flow 19-2162} -default_severity}
    foreach p {project.vivado.isBlockSynthRun synth.vivado.isSynthRun
               chipscope.flow project.singleFileAddWarning.threshold
               project.compositeFile.enableAutoGeneration
               constraints.enableBinaryConstraints ips.enableIPCacheLiteLoad} {
        catch {reset_param $p}
    }
}

proc ooc_run_one {run_dir} {
    set tcl [lindex [glob -nocomplain -directory $run_dir *.tcl] 0]
    set old_pwd [pwd]
    cd $run_dir
    set f [open .vivado.begin.rst w]
    puts $f "<?xml version=\"1.0\"?>\n<ProcessHandle Version=\"1\" Minor=\"0\">\n    <Process Command=\"vivado\" Owner=\"$::env(USER)\" Host=\"[info hostname]\" Pid=\"[pid]\">\n    </Process>\n</ProcessHandle>"
    close $f
    set t0 [clock milliseconds]
    # The script runs at global scope, as `vivado -source` would run it.
    set rc [catch {uplevel #0 [list source $tcl]} msg]
    set dt [expr {([clock milliseconds] - $t0) / 1000.0}]
    if {$rc == 0 || $rc == 2} {
        close [open .vivado.end.rst w]
        set status ok
    } else {
        close [open .vivado.error.rst w]
        set f [open vrc_error.txt w]; puts $f $::errorInfo; close $f
        set status "error: [lindex [split $msg \n] 0]"
    }
    cd $old_pwd
    ooc_reset_session
    return [list $status $dt]
}

proc ooc_mem_available_gb {} {
    set f [open /proc/meminfo]; set t [read $f]; close $f
    regexp {MemAvailable:\s+(\d+)} $t -> kb
    return [expr {$kb / 1048576.0}]
}

# Settings, set by run_pool.sh before calling ooc_worker:
#   ::ooc_session      this session's name; names the watchdog's cancel marker
#   ::ooc_max_runs     return after this many runs, so the caller can restart
#                      the session (each run leaves 40-80 MB behind)
#   ::ooc_min_free_gb  do not claim a run while less memory is available
foreach {v d} {ooc_session "" ooc_max_runs 1000000 ooc_min_free_gb 0} {
    if {![info exists ::$v]} { set ::$v $d }
}

# A run that fails while <runs_dir>/.vrc_cancel_<session> exists was cancelled
# by the memory watchdog in run_pool.sh. It is put back in the queue: markers
# and claim removed, so this or another session runs it again later.
proc ooc_requeue_if_cancelled {runs_dir run_dir} {
    set marker [file join $runs_dir .vrc_cancel_$::ooc_session]
    if {$::ooc_session eq "" || ![file exists $marker]} { return 0 }
    file delete $marker
    foreach f {.vivado.begin.rst .vivado.error.rst __synthesis_is_running__ .vrc_claim} {
        file delete -force [file join $run_dir $f]
    }
    return 1
}

proc ooc_worker {runs_dir args} {
    set out {}
    set n 0
    foreach run $args {
        if {$n >= $::ooc_max_runs} break
        set run_dir [file join $runs_dir $run]
        if {[file exists [file join $run_dir .vrc_claim]]} continue
        while {[ooc_mem_available_gb] < $::ooc_min_free_gb} { after 5000 }
        # Prime before claiming, so that a failed prime leaves the run in the
        # queue instead of claimed by a session that never runs it.
        if {![info exists ::ooc_primed]} {
            set tcl [lindex [glob -directory $run_dir *.tcl] 0]
            set fh [open $tcl]; set text [read $fh]; close $fh
            regexp -- {create_project -in_memory -part (\S+)} $text -> part
            ooc_prime $part
        }
        # O_EXCL create is atomic, so exactly one session gets each run.
        if {[catch {close [open [file join $run_dir .vrc_claim] {WRONLY CREAT EXCL}]}]} { continue }
        file delete [file join $runs_dir .vrc_cancel_$::ooc_session]
        set start [clock seconds]
        # Tells the watchdog this session is inside a run and may be cancelled.
        set current [file join $runs_dir .vrc_current_$::ooc_session]
        set fh [open $current w]; puts $fh $run; close $fh
        foreach {status dt} [ooc_run_one $run_dir] break
        file delete $current
        incr n
        if {$status ne "ok" && [ooc_requeue_if_cancelled $runs_dir $run_dir]} {
            # A cancelled synth_design leaves the session unable to run another
            # ("invalid command name NULL"), so return and let run_pool.sh
            # restart it.
            lappend out [format "%-40s %-8s start=%d %6.1fs" $run requeued $start $dt]
            break
        } elseif {$status ne "ok"} {
            set failed 1
        }
        lappend out [format "%-40s %-8s start=%d %6.1fs" $run $status $start $dt]
    }
    # An error makes `vrc wait` exit 1; the parent sees .vivado.error.rst.
    if {[info exists failed]} { error [join $out \n] }
    return [join $out \n]
}
