# The wire protocol

`vrc` and the Tcl plugin exchange length-prefixed frames over a loopback TCP
socket. This page is what you need to write another client, or to debug one
with `nc`.

The format is deliberately not JSON. Tcl scripts are multi-line and a
`report_timing_summary -return_string` on a large design runs to megabytes, so
framing by byte count means nothing has to be escaped in either direction —
and the Vivado side needs no JSON encoder at all, which is several hundred
lines of Tcl that does not have to exist.

## The frames

A request:

```
V1 <nbytes> <token>\n
<nbytes bytes of UTF-8 encoded Tcl>
```

A response:

```
<status> <nbytes>\n
<nbytes bytes of UTF-8>
```

`status` is one of:

`ok`
: the script ran; the payload is its result. Tcl result code 0 and 2 both
land here, because a script ending in `return "WNS=..."` is a success.

`err`
: the script raised; the payload is `$::errorInfo`, whose first line is the
message and the rest the Tcl traceback. The session is unharmed and keeps its
state. A request stopped by `vrc cancel` is an `err` whose first line is
`cancelled by vrc cancel`.

`proto`
: the frame was malformed or the token was wrong. The server closes the
connection straight after sending this.

Connections are persistent and requests may be pipelined. The server answers
in order, one at a time.

## Counting bytes, not characters

`nbytes` counts bytes of UTF-8, and both sides convert explicitly. On the Tcl
side the channel is `fconfigure -translation binary`, with
`encoding convertto utf-8` and `encoding convertfrom utf-8` around it.

This is the detail worth getting right first. Tcl's `read $chan $n` counts
*characters*, so a channel configured `-encoding utf-8` desynchronises
permanently the first time a payload contains a non-ASCII byte, and the
failure appears much later as a garbled response to an unrelated request.

A client must also buffer both parts in one place. The body routinely arrives
in the same TCP segment as the header, so reading the header with a line-read
and then `recv(nbytes)` silently loses the bytes that came with the header.
`FrameReader` in `src/vivado_remotectl/protocol.py` is the reference
implementation: one buffer, `read_line()` and `read_exact()` off the same one.

Tcl 8.5 emits CESU-8 for characters outside the BMP and `\xc0\x80` for NUL, so
a client should decode leniently rather than fail. The channel is text; a
`.dcp` or any other binary artifact goes through a file, not through here.

## Limits

A header line is capped at 128 bytes and a payload at 256 MB, checked before
anything is allocated, on both sides. Without that a stray `curl` at the port
could make a 3 GB Vivado ask for more.

## Authentication

Every request carries the session's token, and the server replies `proto` and
hangs up if it does not match. The token is 32 hex characters from
`/dev/urandom`, generated at startup and written into the session descriptor
with mode `0600`.

Binding to loopback is not enough on a multi-user or shared build host: any
local process could otherwise connect and evaluate arbitrary Tcl as the user
running Vivado, which includes `exec`.

## The session descriptor

`<state-dir>/<name>.session`, mode `0600`, written atomically by the plugin
and read by everyone else. Flat `key value` lines, one per line — the format
is this plain so that the Tcl side, which is the only writer, needs no encoder,
and readers ignore keys they do not know:

```
v 1
name dut
host 127.0.0.1
port 41239
pid 2118203
starttime 7554058
token def737ecb7496f5f78f93e415c5bf371
tcl 8.5.14
started 1789698682
cwd /home/you/rtl
cancel sigint
```

`port` is the port actually bound, which matters because the default request
is port 0 and the kernel chooses. `starttime` is field 22 of
`/proc/<pid>/stat`; it exists so a client can prove `pid` still refers to the
process the descriptor describes before signalling it. `token` is why the file
is `0600`.

The name is claimed with an `O_EXCL` create, so two simultaneous `vrc start`s
for the same name cannot both win. The log path is not stored — it is
`<name>.log` beside the descriptor, by convention, which keeps the plugin the
only writer and leaves nothing to drift.

`cancel sigint` is present only in a session running under Vivado's prompt
loop, which is what `vrc start` sets up without `--gui`. It means a SIGINT to
`pid` cancels the running command and the session stays usable. A client must
not send SIGINT to a session without it: in a `vwait` or a GUI the session
stops answering for good. Before the signal, a client creates
`<state-dir>/<name>.cancel`. When the request ends with an error and the file
is no older than the request, the plugin puts the `cancelled by vrc cancel`
line in front of the traceback; it deletes the file after every request either
way.

## Talking to it by hand

```console
$ eval $(sed -n 's/^\(port\|token\) /\1=/p' ~/.local/state/vivado-remotectl/dut.session)
 $ printf 'V1 12 %s\nexpr {6 * 7}' "$token" | nc 127.0.0.1 $port
ok 2
42
```

The byte count has to be right, which is most of why a real client is worth
having.
