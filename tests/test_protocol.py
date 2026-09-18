"""FrameReader and framing, with no session involved."""

from __future__ import annotations

import pytest

from vivado_remotectl.protocol import (
    MAX_PAYLOAD,
    FrameReader,
    ProtocolError,
    SessionDied,
    encode_request,
    read_response,
)


class ChunkedSocket:
    """A socket stub that hands back pre-arranged chunks."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, _n):
        return self.chunks.pop(0) if self.chunks else b""


def reader_for(*chunks) -> FrameReader:
    return FrameReader(ChunkedSocket(chunks))


def test_encode_request_counts_utf8_bytes_not_characters():
    frame = encode_request("set x €", "deadbeef")
    header, _, body = frame.partition(b"\n")
    assert header == b"V1 %d deadbeef" % len(body)
    assert len(body) == 9  # 6 ASCII chars + 3 bytes of euro


def test_encode_request_rejects_whitespace_in_token():
    with pytest.raises(ValueError):
        encode_request("x", "bad token")


def test_read_response_when_body_shares_the_header_segment():
    # The case that breaks a naive readline()-then-recv(n) client.
    reader = reader_for(b"ok 5\nhello")
    assert read_response(reader) == ("ok", "hello")


def test_read_response_one_byte_at_a_time():
    raw = b"ok 5\nhello"
    reader = reader_for(*[raw[i : i + 1] for i in range(len(raw))])
    assert read_response(reader) == ("ok", "hello")


def test_read_response_handles_several_frames_on_one_connection():
    reader = reader_for(b"ok 1\na", b"ok 1\nb", b"err 3\nboo")
    assert read_response(reader) == ("ok", "a")
    assert read_response(reader) == ("ok", "b")
    assert read_response(reader) == ("err", "boo")


def test_read_response_decodes_utf8():
    body = "€".encode()
    reader = reader_for(b"ok %d\n" % len(body) + body)
    assert read_response(reader) == ("ok", "€")


def test_read_response_replaces_undecodable_bytes():
    reader = reader_for(b"ok 2\n\xc0\x80")
    status, text = read_response(reader)
    assert status == "ok"
    assert text  # replaced, not raised


def test_eof_mid_body_is_session_died():
    reader = reader_for(b"ok 100\nshort")
    with pytest.raises(SessionDied):
        read_response(reader)


def test_eof_before_header_is_session_died():
    with pytest.raises(SessionDied):
        read_response(reader_for())


def test_unknown_status_is_protocol_error():
    with pytest.raises(ProtocolError):
        read_response(reader_for(b"weird 0\n"))


def test_malformed_header_is_protocol_error():
    with pytest.raises(ProtocolError):
        read_response(reader_for(b"ok\n"))


def test_non_numeric_length_is_protocol_error():
    with pytest.raises(ProtocolError):
        read_response(reader_for(b"ok abc\n"))


def test_oversized_length_is_rejected_before_allocating():
    reader = reader_for(b"ok %d\n" % (MAX_PAYLOAD + 1))
    with pytest.raises(ProtocolError):
        read_response(reader)


def test_endless_header_is_rejected():
    reader = reader_for(*[b"x" * 64 for _ in range(10)])
    with pytest.raises(ProtocolError):
        read_response(reader)
