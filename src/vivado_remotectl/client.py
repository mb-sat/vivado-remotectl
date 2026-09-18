"""Synchronous client for one Vivado session."""

from __future__ import annotations

import socket

from .protocol import (
    STATUS_ERR,
    STATUS_OK,
    FrameReader,
    ProtocolError,
    RemoteTclError,
    SessionDied,
    encode_request,
    read_response,
)
from .registry import SessionInfo


class ConnectFailed(Exception):
    """Could not reach the session's port."""


class Session:
    """One connection, many scripts.

    The connection is persistent so the REPL can hold it open across
    evaluations, which also keeps the session's Tcl state (an open design)
    exactly where it was.
    """

    def __init__(self, info: SessionInfo):
        self.info = info
        self._sock: socket.socket | None = None
        self._reader: FrameReader | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.info.host, self.info.port), timeout=timeout
            )
        except OSError as exc:
            raise ConnectFailed(
                f"cannot reach session '{self.info.name}' on "
                f"{self.info.host}:{self.info.port}: {exc}"
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = FrameReader(sock)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._reader = None

    def __enter__(self) -> Session:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- evaluation --------------------------------------------------------

    def run(self, script: str, timeout: float | None = None) -> str:
        """Evaluate ``script`` and return its result.

        ``timeout=None`` waits forever, which is the right default: an
        out-of-context synthesis runs for half an hour and there is nothing
        useful to do but wait.
        """
        if self._sock is None or self._reader is None:
            self.connect()
        assert self._sock is not None and self._reader is not None

        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(encode_request(script, self.info.token))
            status, text = read_response(self._reader)
        except (socket.timeout, TimeoutError):
            # The script is still running inside Vivado; the connection is no
            # longer in a known state, so do not reuse it.
            self.close()
            raise
        except (OSError, SessionDied):
            self.close()
            raise

        if status == STATUS_OK:
            return text
        if status == STATUS_ERR:
            raise RemoteTclError(text)
        self.close()
        raise ProtocolError(text or "protocol error")

    def ping(self, timeout: float = 2.0) -> str:
        return self.run("info patchlevel", timeout=timeout)
