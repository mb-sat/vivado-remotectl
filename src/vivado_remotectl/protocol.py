"""Wire protocol for vivado-remotectl.

Length-prefixed frames, deliberately not NDJSON::

    request    "V1 <nbytes> <token>\\n"  <nbytes UTF-8 bytes of Tcl>
    response   "<status> <nbytes>\\n"    <nbytes UTF-8 bytes>

``status`` is one of ``ok``, ``err`` (payload is Tcl's ``errorInfo``) or
``proto`` (framing or authentication failure; the peer closes afterwards).

Framing by byte count means multi-line Tcl and multi-megabyte
``report_* -return_string`` payloads need no escaping, and the Vivado side
needs no JSON codec at all.
"""

from __future__ import annotations

import socket

VERSION = "V1"

#: Longest header line we will buffer before giving up.
MAX_HEADER = 128
#: Largest payload we will allocate for, in either direction.
MAX_PAYLOAD = 256 * 1024 * 1024

STATUS_OK = "ok"
STATUS_ERR = "err"
STATUS_PROTO = "proto"


class ProtocolError(Exception):
    """The peer sent something that is not a well-formed frame."""


class RemoteTclError(Exception):
    """The script ran and raised. ``errorInfo`` carries the Tcl traceback."""

    def __init__(self, errorinfo: str):
        self.errorinfo = errorinfo
        super().__init__(errorinfo.splitlines()[0] if errorinfo else "Tcl error")


class SessionDied(Exception):
    """The connection closed before a complete response arrived."""


def encode_request(script: str, token: str) -> bytes:
    """Frame one script for the wire."""
    if " " in token or "\n" in token:
        raise ValueError("token must not contain whitespace")
    payload = script.encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"script is {len(payload)} bytes, limit is {MAX_PAYLOAD}")
    header = f"{VERSION} {len(payload)} {token}\n".encode("ascii")
    return header + payload


class FrameReader:
    """Buffered reader over a socket.

    A single buffer for both the header line and the sized body is the point:
    the body routinely arrives in the same TCP segment as the header, so
    reading the line with one call and then ``recv(n)`` for the body would
    silently drop bytes.
    """

    def __init__(self, sock: socket.socket, chunk: int = 65536):
        self._sock = sock
        self._buf = bytearray()
        self._chunk = chunk
        self._eof = False

    def _fill(self) -> None:
        if self._eof:
            raise SessionDied("connection closed by the Vivado session")
        data = self._sock.recv(self._chunk)
        if not data:
            self._eof = True
            raise SessionDied("connection closed by the Vivado session")
        self._buf += data

    def read_line(self, limit: int = MAX_HEADER) -> str:
        """Read up to and including the next newline; return it without."""
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                return line.decode("ascii", errors="replace")
            if len(self._buf) > limit:
                raise ProtocolError(f"header exceeds {limit} bytes with no newline")
            self._fill()

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def read_response(reader: FrameReader) -> tuple[str, str]:
    """Read one response frame. Returns ``(status, text)``."""
    header = reader.read_line()
    parts = header.split(" ")
    if len(parts) != 2:
        raise ProtocolError(f"malformed response header: {header!r}")
    status, raw_len = parts
    if status not in (STATUS_OK, STATUS_ERR, STATUS_PROTO):
        raise ProtocolError(f"unknown response status: {status!r}")
    try:
        nbytes = int(raw_len)
    except ValueError:
        raise ProtocolError(f"non-numeric payload length: {raw_len!r}") from None
    if nbytes < 0 or nbytes > MAX_PAYLOAD:
        raise ProtocolError(f"payload length out of range: {nbytes}")
    body = reader.read_exact(nbytes)
    # Tcl 8.5 emits CESU-8 for astral characters and \xc0\x80 for NUL, so be
    # forgiving here rather than failing a 30-minute synthesis on one byte.
    return status, body.decode("utf-8", errors="replace")
