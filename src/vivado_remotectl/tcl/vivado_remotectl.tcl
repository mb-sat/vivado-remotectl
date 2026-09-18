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
#   * Vivado 2023.2 embeds Tcl 8.5.14. No `try`, `lmap`, `string cat`,
#     `dict map`, `binary encode`, `tailcall`, `coroutine`, `file tempfile`.
#   * No Vivado-specific command on the core path, so the whole thing runs
#     under a bare tclsh8.5 for tests.
#
# Environment (all optional):
#   VRC_NAME       session name              (default vivado-<pid>)
#   VRC_PORT       port to bind, 0 = any     (default 0)
#   VRC_DIR        session registry dir      (default per XDG, see _state_dir)
#   VRC_SERVE      1 -> `vwait` at the end; keeps a -mode tcl Vivado alive
#   VRC_AUTOSTART  0 -> define procs but do not bind
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

    # Per-channel byte accumulator, and the set of channels whose peer has
    # closed its write side but which may still hold a complete request.
    if {![info exists buf]}     { variable buf;     array set buf     {} }
    if {![info exists closing]} { variable closing; array set closing {} }

    variable MAX_HEADER  128
    variable MAX_PAYLOAD [expr {256 * 1024 * 1024}]
    variable VERSION     "V1"
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
    close $f
    catch {file attributes $tmp -permissions 0600}
    file rename -force $tmp $sessfile
}

proc ::vrc::_rm_session {} {
    variable sessfile
    if {$sessfile ne ""} {
        catch {file delete -force $sessfile}
    }
}

# ------------------------------------------------------------------------------
# Accept callback. -myaddr already restricts the bind; check the peer anyway.
# ------------------------------------------------------------------------------
proc ::vrc::_accept {chan addr rport} {
    variable buf

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
    if {[catch {read $chan} data]} {
        ::vrc::_drop $chan "read error"
        return
    }
    append buf($chan) $data

    if {[eof $chan]} {
        # Peer closed its write side. Anything already buffered still counts;
        # _pump retires the channel once nothing complete is left.
        set closing($chan) 1
    }
    ::vrc::_pump
}

# ------------------------------------------------------------------------------
# Dispatch at most one request, then return. _exec reschedules us with
# `after idle`, so a pipelined second request is handled on the next turn of
# the event loop rather than recursively.
# ------------------------------------------------------------------------------
proc ::vrc::_pump {} {
    variable busy
    variable buf
    variable closing

    if {$busy} { return }

    foreach chan [array names buf] {
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

    set busy 1
    set rc [catch {uplevel #0 $script} res opts]
    set busy 0

    # rc 2 is TCL_RETURN: a script ending in `return "WNS=..."` is the
    # documented idiom for getting a value out, so it is a success.
    if {$rc == 0 || $rc == 2} {
        ::vrc::_respond $chan ok $res
    } else {
        set detail $res
        catch {set detail [dict get $opts -errorinfo]}
        ::vrc::_respond $chan err [::vrc::_trim_errorinfo $detail]
    }
    after idle ::vrc::_pump
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

    catch {fileevent $chan readable {}}
    catch {close $chan}
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

    if {$sock ne ""} { return $port }

    set name    [::vrc::_env VRC_NAME "vivado-[pid]"]
    set dir     [::vrc::_state_dir]
    set reqport [::vrc::_env VRC_PORT 0]

    if {![::vrc::_claim_session]} {
        return -code error "session '$name' is already live in $dir"
    }
    if {[catch {socket -server ::vrc::_accept -myaddr 127.0.0.1 $reqport} s]} {
        ::vrc::_rm_session
        set sessfile ""
        return -code error "cannot listen on 127.0.0.1:$reqport -> $s"
    }
    set sock  $s
    set port  [lindex [fconfigure $sock -sockname] 2]
    set token [::vrc::_token]
    ::vrc::_write_session
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
    if {[catch {::vrc::start} err]} {
        ::vrc::_log "start failed: $err"
        if {[string is true -strict [::vrc::_env VRC_SERVE 0]]} {
            exit 1
        }
    } elseif {[string is true -strict [::vrc::_env VRC_SERVE 0]]} {
        # Launched by `vrc start`: Vivado's stdin is /dev/null, so without a
        # vwait here -mode tcl would hit EOF and exit immediately. A GUI or
        # interactive Vivado must NOT take this branch -- it already owns an
        # event loop, and blocking here would freeze the UI.
        vwait ::vrc::forever
        ::vrc::stop
    }
}
