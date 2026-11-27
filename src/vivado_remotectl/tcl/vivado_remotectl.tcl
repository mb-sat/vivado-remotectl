# ==============================================================================
# vivado-remotectl :: vivado_remotectl.tcl
# ------------------------------------------------------------------------------
# Listens on a loopback TCP port inside a live Vivado and evaluates Tcl scripts
# sent by the `vrc` CLI, returning the result.
#
# Wire protocol (length-prefixed, see docs/protocol.md):
#     request    "V1 <nbytes> <token>\n"  <nbytes UTF-8 bytes of Tcl>
#     response   "<status> <nbytes>\n"    <nbytes UTF-8 bytes>
#     status in {ok, err, proto}
#
# Constraints this file lives under:
#   * Runs on Tcl 8.5 and 8.6: Vivado 2023.2 and older embed 8.5, 2024.1
#     onwards 8.6, and the tests run both. No `try`, `lmap`, `string cat`,
#     `dict map`, `binary encode`, `tailcall`, `coroutine`, `file tempfile`.
#   * No Vivado-specific command on the core path, so the whole thing runs
#     under a bare tclsh for tests.
#
# Environment (all optional):
#   VRC_NAME       session name              (default vivado-<pid>)
#   VRC_PORT       port to bind, 0 = any     (default 0)
#   VRC_DIR        session registry dir      (default per XDG, see _state_dir)
#   VRC_SERVE      1 -> `vwait` at the end; keeps a -mode tcl Vivado alive
#                  prompt -> define ::vrc::serve and return to Vivado's prompt,
#                  which `vrc start` feeds an endless `::vrc::serve` stream.
#                  Only this mode can be cancelled, see ::vrc::serve.
#   VRC_AUTOSTART  0 -> define procs but do not bind
#   VRC_OWNER      who started the session, recorded in the descriptor
#   VRC_IDLE_STOP  seconds; exit after this long without a request (0 = never)
# ==============================================================================

namespace eval ::vrc {
    # Guard every state variable: Vivado re-runs Vivado_init.tcl on start_gui
    # and some project operations, and we must not clobber a live server.
    if {![info exists sock]}     { variable sock     "" }
    if {![info exists busy]}     { variable busy     0  }
    if {![info exists name]}     { variable name     "" }
    if {![info exists port]}     { variable port     0  }
    if {![info exists token]}    { variable token    "" }
    if {![info exists dir]}      { variable dir      "" }
    if {![info exists sessfile]} { variable sessfile "" }
    if {![info exists forever]}  { variable forever  0  }
    if {![info exists tick]}     { variable tick     0  }
    if {![info exists active]}   { variable active   [clock seconds] }
    if {![info exists idle_stop]} { variable idle_stop 0 }

    # Per-channel byte accumulator, and the set of channels whose peer has
    # closed its write side but which may still hold a complete request.
    if {![info exists buf]}     { variable buf;     array set buf     {} }
    if {![info exists closing]} { variable closing; array set closing {} }

    # Open channels in the order their connections were accepted. This is the
    # order requests are served in, see _pump.
    if {![info exists order]}   { variable order {} }

    variable MAX_HEADER  128
    variable MAX_PAYLOAD [expr {256 * 1024 * 1024}]
    variable VERSION     "V1"

    # Liveness checks the client sends: `vrc list` and `vrc wait` (expr 1),
    # the readiness poll of `vrc start` (info patchlevel) and `vrc ping`.
    # They are not recorded as the last result and do not count as activity,
    # or `vrc list` would overwrite the result it is meant to help recover.
    # `vrc info` asks for the open project (::vrc::project) the same way.
    variable QUIET [list "expr 1" "info patchlevel" "version -short" \
                        "::vrc::project"]
}

# Name of the open project, or empty when there is none. `vrc info` shows it,
# to tell several sessions apart by what they hold.
proc ::vrc::project {} {
    if {[catch {current_project -quiet} name]} { return "" }
    return $name
}

# ------------------------------------------------------------------------------
# Diagnostics. Goes to stderr, which the CLI redirects into the session log.
# Wrapped because a GUI Vivado may have no usable stderr.
# ------------------------------------------------------------------------------
proc ::vrc::_log {msg} {
    catch {
        puts stderr "\[vrc\] $msg"
        flush stderr
    }
}

# ------------------------------------------------------------------------------
# Read an environment variable, falling back to a default when unset or empty.
# ------------------------------------------------------------------------------
proc ::vrc::_env {var default} {
    if {[info exists ::env($var)] && $::env($var) ne ""} {
        return $::env($var)
    }
    return $default
}

# ------------------------------------------------------------------------------
# Registry directory. Deliberately under XDG *state*, not cache: a cache
# cleaner removing the token of a live 30-minute synthesis is a real failure.
# ------------------------------------------------------------------------------
proc ::vrc::_state_dir {} {
    set d [::vrc::_env VRC_DIR ""]
    if {$d ne ""} { return $d }
    set d [::vrc::_env VIVADO_REMOTECTL_DIR ""]
    if {$d ne ""} { return $d }
    set xdg [::vrc::_env XDG_STATE_HOME ""]
    if {$xdg ne ""} { return [file join $xdg vivado-remotectl] }
    return [file join [::vrc::_env HOME /tmp] .local state vivado-remotectl]
}

# ------------------------------------------------------------------------------
# 32 hex characters of connection token. Without this, any local process can
# connect to the port and evaluate arbitrary Tcl as the user running Vivado.
# ------------------------------------------------------------------------------
proc ::vrc::_token {} {
    if {![catch {
        set f [open /dev/urandom rb]
        set bytes [read $f 16]
        close $f
        binary scan $bytes H* hex
    }]} {
        if {[info exists hex] && [string length $hex] == 32} { return $hex }
    }
    # Fallback: weaker, but better than refusing to start.
    set s ""
    for {set i 0} {$i < 32} {incr i} {
        append s [format %x [expr {int(rand() * 16)}]]
    }
    return $s
}

# ------------------------------------------------------------------------------
# Return 1 if the given pid is a running process. Linux-only, which is what
# we target. A zombie keeps its /proc entry until the parent reaps it, so the
# process state has to be checked or a session that already exited looks live.
# ------------------------------------------------------------------------------
proc ::vrc::_pid_alive {p} {
    if {![string is integer -strict $p]} { return 0 }
    if {![file exists /proc/$p/stat]} { return 0 }
    if {[catch {
        set f [open /proc/$p/stat r]
        set line [read $f]
        close $f
    }]} {
        return 0
    }
    # "<pid> (comm) <state> ..."; comm can contain spaces and parentheses,
    # so anchor on the last ')'.
    set idx [string last ")" $line]
    if {$idx < 0} { return 1 }
    set rest [string trim [string range $line [expr {$idx + 1}] end]]
    return [expr {[lindex [split $rest " "] 0] ne "Z"}]
}

# ------------------------------------------------------------------------------
# Process start time in clock ticks since boot (field 22 of /proc/<pid>/stat).
#
# Recorded in the descriptor so `vrc stop` can prove a pid still refers to the
# process we started. Pids get recycled, and without this a stale descriptor
# could aim a kill at an unrelated Vivado.
# ------------------------------------------------------------------------------
proc ::vrc::_starttime {p} {
    if {![string is integer -strict $p]} { return "" }
    if {[catch {
        set f [open /proc/$p/stat r]
        set line [read $f]
        close $f
    }]} {
        return ""
    }
    set idx [string last ")" $line]
    if {$idx < 0} { return "" }
    set fields [split [string trim [string range $line [expr {$idx + 1}] end]] " "]
    if {[llength $fields] < 20} { return "" }
    return [lindex $fields 19]
}

# ------------------------------------------------------------------------------
# Take ownership of <dir>/<name>.session, or fail if a live session holds it.
# `CREAT EXCL` is what closes the race between two concurrent `vrc start`s.
# ------------------------------------------------------------------------------
proc ::vrc::_claim_session {} {
    variable dir
    variable name
    variable sessfile

    file mkdir $dir
    set sessfile [file join $dir "$name.session"]

    for {set attempt 0} {$attempt < 2} {incr attempt} {
        if {![catch {set f [open $sessfile {WRONLY CREAT EXCL} 0600]}]} {
            catch {close $f}
            return 1
        }
        # Somebody already owns the name. Alive, or a leftover?
        set owner ""
        if {![catch {set g [open $sessfile r]}]} {
            set data ""
            catch {set data [read $g]}
            catch {close $g}
            foreach line [split $data "\n"] {
                set parts [split [string trim $line] " "]
                if {[lindex $parts 0] eq "pid"} { set owner [lindex $parts 1] }
            }
        }
        if {$owner ne "" && $owner ne [pid] && [::vrc::_pid_alive $owner]} {
            return 0
        }
        catch {file delete -force $sessfile}
    }
    return 0
}

# ------------------------------------------------------------------------------
# Write the session descriptor atomically. Flat `key value` lines, one per
# line: this is the entire reason no JSON writer is needed on the Tcl side.
# Tcl is the only writer of this file.
# ------------------------------------------------------------------------------
proc ::vrc::_write_session {} {
    variable dir
    variable name
    variable port
    variable token
    variable sessfile

    set tmp [file join $dir "$name.session.[pid].tmp"]
    set f [open $tmp {WRONLY CREAT TRUNC} 0600]
    fconfigure $f -translation lf -encoding utf-8
    puts $f "v 1"
    puts $f "name $name"
    puts $f "host 127.0.0.1"
    puts $f "port $port"
    puts $f "pid [pid]"
    puts $f "starttime [::vrc::_starttime [pid]]"
    puts $f "token $token"
    puts $f "tcl [info patchlevel]"
    puts $f "started [clock seconds]"
    puts $f "cwd [pwd]"
    # Who started it, so `vrc stop --mine` and `--all` can tell sessions
    # apart on a shared host. Set by `vrc start`; absent when attached.
    set owner [::vrc::_env VRC_OWNER ""]
    if {$owner ne ""} { puts $f "owner $owner" }
    variable idle_stop
    if {$idle_stop > 0} { puts $f "idle_stop $idle_stop" }
    # Tells the client this plugin writes <name>.running and <name>.result.
    puts $f "results 1"
    if {[::vrc::_env VRC_SERVE 0] eq "prompt"} {
        # Tells `vrc cancel` that a SIGINT here cancels rather than wedges.
        puts $f "cancel sigint"
    }
    close $f
    catch {file attributes $tmp -permissions 0600}
    file rename -force $tmp $sessfile
}

proc ::vrc::_rm_session {} {
    variable sessfile
    if {$sessfile ne ""} {
        catch {file delete -force $sessfile}
        ::vrc::_rm_sidecars
    }
}

# The per-request files beside the descriptor; see _record_start.
proc ::vrc::_sidecar {ext} {
    variable dir
    variable name
    return [file join $dir "$name.$ext"]
}

proc ::vrc::_rm_sidecars {} {
    foreach ext {running result} {
        catch {file delete -force [::vrc::_sidecar $ext]}
    }
}

# Write a file beside the descriptor atomically, mode 0600. `bytes` is
# already bytes; the result of a script may be a multi-megabyte report.
proc ::vrc::_write_sidecar {ext bytes} {
    set path [::vrc::_sidecar $ext]
    set tmp "$path.[pid].tmp"
    if {[catch {
        set f [open $tmp {WRONLY CREAT TRUNC} 0600]
        fconfigure $f -translation binary
        puts -nonewline $f $bytes
        close $f
        file rename -force $tmp $path
    } err]} {
        catch {close $f}
        catch {file delete -force $tmp}
        ::vrc::_log "cannot write $path: $err"
    }
}

# ------------------------------------------------------------------------------
# What a session is doing, kept on disk so it survives the client.
#
# A client that dies mid-synthesis -- an agent's shell torn down, a laptop
# lid closed -- used to take the result with it, and the only way back was to
# rerun the half hour. Now <name>.running says what is executing and since
# when, and <name>.result holds the reply to the last request, so `vrc wait`
# and `vrc result` can hand it over later. Both formats are in
# docs/protocol.md.
# ------------------------------------------------------------------------------
proc ::vrc::_record_start {started script} {
    set first ""
    foreach line [split $script "\n"] {
        set line [string trim $line]
        if {$line ne ""} { set first $line; break }
    }
    if {[string length $first] > 200} {
        set first "[string range $first 0 196]..."
    }
    ::vrc::_write_sidecar running \
        [encoding convertto utf-8 "started $started\n$first\n"]
}

proc ::vrc::_record_end {started status text} {
    set payload [encoding convertto utf-8 $text]
    ::vrc::_write_sidecar result \
        "$status [string length $payload] $started [clock seconds]\n$payload"
    catch {file delete -force [::vrc::_sidecar running]}
}

# ------------------------------------------------------------------------------
# Accept callback. -myaddr already restricts the bind; check the peer anyway.
# ------------------------------------------------------------------------------
proc ::vrc::_accept {chan addr rport} {
    variable buf
    variable order

    if {$addr ne "127.0.0.1" && $addr ne "::1"} {
        ::vrc::_log "rejected connection from $addr"
        catch {close $chan}
        return
    }
    # -translation binary implies -encoding binary, so `read $chan` yields
    # bytes and `string length` counts bytes. UTF-8 conversion is explicit
    # in _try_one / _respond; a -encoding utf-8 channel would desynchronise
    # the length prefix on the first non-ASCII byte.
    if {[catch {fconfigure $chan -translation binary -blocking 0} err]} {
        ::vrc::_log "fconfigure failed for $chan: $err"
        catch {close $chan}
        return
    }
    set buf($chan) ""
    lappend order $chan
    fileevent $chan readable [list ::vrc::_readable $chan]
}

# ------------------------------------------------------------------------------
# Readable callback: accumulate bytes, then try to dispatch. Never blocks --
# a client that connects and sends nothing must not freeze the Vivado GUI.
# ------------------------------------------------------------------------------
proc ::vrc::_readable {chan} {
    variable buf
    variable closing

    if {![info exists buf($chan)]} {
        catch {fileevent $chan readable {}}
        catch {close $chan}
        return
    }
    if {![::vrc::_fill $chan]} { return }
    ::vrc::_pump
}

# Move whatever is waiting on a channel into its buffer, without blocking.
# Returns 0 if the channel had to be dropped.
proc ::vrc::_fill {chan} {
    variable buf
    variable closing

    if {[catch {read $chan} data]} {
        ::vrc::_drop $chan "read error"
        return 0
    }
    append buf($chan) $data

    if {[eof $chan]} {
        # Peer closed its write side. Anything already buffered still counts;
        # _pump retires the channel once nothing complete is left.
        set closing($chan) 1
    }
    return 1
}

# ------------------------------------------------------------------------------
# Dispatch at most one request, then return. _exec reschedules us with
# `after idle`, so a pipelined second request is handled on the next turn of
# the event loop rather than recursively.
#
# Ordering: requests are served in the order their connections were accepted,
# and on one connection in the order sent. Connections are accepted in the
# order they were made (listen-backlog order), so this is the order clients
# connected. A connection that has not sent a complete frame yet does not hold
# up later ones: the earliest connection with a complete frame goes first.
#
# While a script runs, no fileevent is serviced unless the script re-enters
# the event loop, so clients that queued meanwhile have their bytes unread in
# the kernel, and whichever channel's event fires first would otherwise be the
# only one with a buffered frame. Hence the drain: before choosing, read what
# is pending on every open channel. It is a non-blocking read and runs only
# here, never while `busy`, so it cannot run inside a script that spins the
# event loop (wait_on_run, vwait); those events reach _readable, whose _pump
# returns at once. No `update` or `after idle` is needed to order accepts: a
# connection is accepted only after all earlier ones, so every connection that
# is still unaccepted is later than every accepted one.
# ------------------------------------------------------------------------------
proc ::vrc::_pump {} {
    variable busy
    variable buf
    variable closing
    variable order

    if {$busy} { return }

    foreach chan $order {
        if {[info exists buf($chan)] && ![info exists closing($chan)]} {
            ::vrc::_fill $chan
        }
    }
    foreach chan $order {
        if {[::vrc::_try_one $chan]} { return }
    }
    # Nothing dispatchable anywhere: retire half-closed channels.
    foreach chan [array names closing] {
        ::vrc::_drop $chan "eof"
    }
}

# ------------------------------------------------------------------------------
# Parse one frame off a channel's buffer and execute it.
# Returns 1 if the channel was acted on (dispatched or dropped), else 0.
# ------------------------------------------------------------------------------
proc ::vrc::_try_one {chan} {
    variable buf
    variable token
    variable MAX_HEADER
    variable MAX_PAYLOAD
    variable VERSION

    if {![info exists buf($chan)]} { return 0 }
    set data $buf($chan)

    set nl [string first "\n" $data]
    if {$nl < 0} {
        if {[string length $data] > $MAX_HEADER} {
            ::vrc::_respond $chan proto "header exceeds $MAX_HEADER bytes"
            ::vrc::_drop $chan "oversized header"
            return 1
        }
        return 0
    }
    if {$nl > $MAX_HEADER} {
        ::vrc::_respond $chan proto "header exceeds $MAX_HEADER bytes"
        ::vrc::_drop $chan "oversized header"
        return 1
    }

    # split, never llength: the header may be arbitrary bytes and must not be
    # parsed as a Tcl list.
    set parts [split [string range $data 0 [expr {$nl - 1}]] " "]
    if {[llength $parts] != 3 || [lindex $parts 0] ne $VERSION} {
        ::vrc::_respond $chan proto "bad header, expected '$VERSION <nbytes> <token>'"
        ::vrc::_drop $chan "bad header"
        return 1
    }
    set nbytes [lindex $parts 1]
    set got    [lindex $parts 2]
    if {![string is integer -strict $nbytes] || $nbytes < 0 || $nbytes > $MAX_PAYLOAD} {
        ::vrc::_respond $chan proto "bad payload length: $nbytes"
        ::vrc::_drop $chan "bad length"
        return 1
    }

    set need [expr {$nl + 1 + $nbytes}]
    if {[string length $data] < $need} { return 0 }

    set body [string range $data [expr {$nl + 1}] [expr {$need - 1}]]
    set buf($chan) [string range $data $need end]

    if {$got ne $token} {
        ::vrc::_respond $chan proto "authentication failed"
        ::vrc::_drop $chan "bad token"
        return 1
    }

    ::vrc::_exec $chan [encoding convertfrom utf-8 $body]
    return 1
}

# ------------------------------------------------------------------------------
# Strip the trailing frames that describe our own `uplevel #0 $script`
# plumbing. They appear in every traceback and tell the reader nothing about
# their script. `string last`/end-anchoring means a caller's own `uplevel`
# frames further up are left alone.
# ------------------------------------------------------------------------------
proc ::vrc::_trim_errorinfo {info} {
    set info [regsub {\n    invoked from within\n"uplevel #0 \$script"\s*$} $info ""]
    set info [regsub {\n    \("uplevel" body line \d+\)\s*$} $info ""]
    return $info
}

# ------------------------------------------------------------------------------
# Evaluate one script at global scope.
#
# `busy` is load-bearing, not an optimisation: wait_on_run, `after ... vwait`
# and several IP-integrator commands re-enter the event loop from inside the
# script, so without it a second client's readable callback dispatches
# mid-synthesis and the two responses interleave.
# ------------------------------------------------------------------------------
proc ::vrc::_exec {chan script} {
    variable busy
    variable QUIET

    set busy 1
    set started [clock seconds]
    set record [expr {[lsearch -exact $QUIET [string trim $script]] < 0}]
    if {$record} { ::vrc::_record_start $started $script }
    set rc [catch {uplevel #0 $script} res opts]
    set busy 0
    if {$record} { set ::vrc::active [clock seconds] }

    # `vrc cancel` drops this marker before its SIGINT. Vivado's own message
    # for a cancelled command is an empty one, so the marker is the only way
    # to tell the waiting client what happened.
    set cancelled [::vrc::_take_cancel_marker $started]

    # rc 2 is TCL_RETURN: a script ending in `return "WNS=..."` is the
    # documented idiom for getting a value out, so it is a success.
    if {$rc == 0 || $rc == 2} {
        set status ok
        set detail $res
    } else {
        set status err
        set detail $res
        catch {set detail [dict get $opts -errorinfo]}
        set detail [::vrc::_trim_errorinfo $detail]
        if {$cancelled} {
            set detail "cancelled by vrc cancel\n[string trimleft $detail \n]"
        }
    }
    # Record before answering, so a client that reads the result file as
    # soon as its reply arrives never sees the previous one.
    if {$record} { ::vrc::_record_end $started $status $detail }
    ::vrc::_respond $chan $status $detail
    after idle ::vrc::_pump
}

proc ::vrc::_cancel_marker {} {
    variable dir
    variable name
    return [file join $dir "$name.cancel"]
}

# Delete the marker and return whether it was written during this request.
# Called after every request, and the mtime check covers the race where the
# command finished just before the SIGINT: the marker is then older than the
# next request and must not label that one's own error.
proc ::vrc::_take_cancel_marker {started} {
    set marker [::vrc::_cancel_marker]
    if {[catch {file mtime $marker} mtime]} { return 0 }
    catch {file delete -force $marker}
    return [expr {$mtime >= $started}]
}

# ------------------------------------------------------------------------------
# One slice of event loop, then back to Vivado's prompt.
#
# This is what makes a session cancellable. Vivado answers SIGINT by
# cancelling the running synth_design / place_design / route_design, but the
# interrupt only clears once control is back at its top-level prompt. Under
# the `vwait` at the bottom of this file it never gets there: the cancelled
# command's error escapes our `catch`, no response is sent, `busy` stays set
# and every later callback fails the same way. So `vrc start` pipes
# `yes ::vrc::serve` into Vivado instead, and the prompt runs this over and
# over; the socket is only serviced inside it. The slice length only bounds
# how long an idle SIGINT stays pending, which is harmless either way.
# ------------------------------------------------------------------------------
proc ::vrc::serve {} {
    set id [after 500 {set ::vrc::tick 1}]
    vwait ::vrc::tick
    after cancel $id
}

# ------------------------------------------------------------------------------
# Exit once nobody has sent a request for `idle_stop` seconds.
#
# Opt-in through `vrc start --idle-stop`, for the session that gets forgotten
# and holds 5 GB overnight. Liveness checks do not count as use (see QUIET),
# or a `vrc list` would keep every session alive. The timer also fires when a
# running script re-enters the event loop, as wait_on_run does; `busy` keeps
# that from ending a session in the middle of its work.
# ------------------------------------------------------------------------------
proc ::vrc::_idle_check {} {
    variable busy
    variable active
    variable idle_stop

    set idle [expr {[clock seconds] - $active}]
    if {!$busy && $idle >= $idle_stop} {
        ::vrc::_log "no request for ${idle}s (--idle-stop $idle_stop); exiting"
        exit 0
    }
    set left [expr {$busy ? $idle_stop : $idle_stop - $idle}]
    after [expr {1000 * ($left < 60 ? $left : 60) + 100}] ::vrc::_idle_check
}

# ------------------------------------------------------------------------------
# Send one response frame. Blocking for the duration of the write, which is
# far simpler to reason about than a background flush of a 50 MB report.
# Wrapped in catch: a client that hit Ctrl-C gives us EPIPE, and that must
# never take Vivado down.
# ------------------------------------------------------------------------------
proc ::vrc::_respond {chan status text} {
    if {[catch {
        set payload [encoding convertto utf-8 $text]
        fconfigure $chan -blocking 1
        puts -nonewline $chan "$status [string length $payload]\n"
        puts -nonewline $chan $payload
        flush $chan
        fconfigure $chan -blocking 0
    } err]} {
        ::vrc::_log "response failed on $chan: $err"
        return 0
    }
    return 1
}

proc ::vrc::_drop {chan why} {
    variable buf
    variable closing
    variable order

    catch {fileevent $chan readable {}}
    catch {close $chan}
    set i [lsearch -exact $order $chan]
    if {$i >= 0} { set order [lreplace $order $i $i] }
    unset -nocomplain buf($chan)
    unset -nocomplain closing($chan)
}

# ------------------------------------------------------------------------------
# Start listening. Returns the bound port. Idempotent.
# ------------------------------------------------------------------------------
proc ::vrc::start {} {
    variable sock
    variable name
    variable port
    variable token
    variable dir
    variable sessfile
    variable idle_stop

    if {$sock ne ""} { return $port }

    set name    [::vrc::_env VRC_NAME "vivado-[pid]"]
    set dir     [::vrc::_state_dir]
    set reqport [::vrc::_env VRC_PORT 0]

    if {![::vrc::_claim_session]} {
        return -code error "session '$name' is already live in $dir"
    }
    # A session of this name that died hard left its last result behind;
    # this one must not hand that out as its own.
    ::vrc::_rm_sidecars
    if {[catch {socket -server ::vrc::_accept -myaddr 127.0.0.1 $reqport} s]} {
        ::vrc::_rm_session
        set sessfile ""
        return -code error "cannot listen on 127.0.0.1:$reqport -> $s"
    }
    set sock  $s
    set port  [lindex [fconfigure $sock -sockname] 2]
    set token [::vrc::_token]
    set idle_stop [::vrc::_env VRC_IDLE_STOP 0]
    if {![string is integer -strict $idle_stop] || $idle_stop < 0} { set idle_stop 0 }
    ::vrc::_write_session
    if {$idle_stop > 0} { after 1000 ::vrc::_idle_check }
    ::vrc::_log "session '$name' listening on 127.0.0.1:$port (pid [pid])"
    return $port
}

# ------------------------------------------------------------------------------
# Stop listening, drop every client, remove the session descriptor.
# ------------------------------------------------------------------------------
proc ::vrc::stop {} {
    variable sock
    variable buf

    foreach chan [array names buf] {
        ::vrc::_drop $chan "server stopping"
    }
    if {$sock ne ""} {
        catch {close $sock}
        set sock ""
    }
    ::vrc::_rm_session
    ::vrc::_log "session stopped"
}

# ==============================================================================
# BOOTSTRAP
# ==============================================================================

# Re-source guard. Vivado re-runs Vivado_init.tcl on start_gui; rebinding the
# socket there would fail and tear down a working session.
if {$::vrc::sock ne ""} {
    return
}

# Remove the descriptor on a clean exit, including `vrc -e exit`.
if {[info commands ::vrc::_orig_exit] eq ""} {
    catch {
        rename exit ::vrc::_orig_exit
        proc exit {{code 0}} {
            catch {::vrc::_rm_session}
            ::vrc::_orig_exit $code
        }
    }
}

if {[string is true -strict [::vrc::_env VRC_AUTOSTART 1]]} {
    set ::vrc::serve_mode [::vrc::_env VRC_SERVE 0]
    if {[catch {::vrc::start} err]} {
        ::vrc::_log "start failed: $err"
        if {[string is true -strict $::vrc::serve_mode] || $::vrc::serve_mode eq "prompt"} {
            exit 1
        }
    } elseif {[string is true -strict $::vrc::serve_mode]} {
        # Stdin is /dev/null, so without a vwait here -mode tcl would hit EOF
        # and exit immediately. A GUI or interactive Vivado must NOT take
        # this branch -- it already owns an event loop, and blocking here
        # would freeze the UI. In prompt mode we return instead, and
        # Vivado's prompt keeps calling ::vrc::serve.
        vwait ::vrc::forever
        ::vrc::stop
    }
}
