"""SonyAnswerFramer byte-vector regression suite.

Includes the sony garbled-short-ack vector (deferred here from serialkit's
Phase 2 because it needs the checksum-discriminated framer the general framers
cannot express)."""

from __future__ import annotations

import pytest
from serialkit import ResyncError

from sony_tv_rs232 import SonyAnswerFramer

from conftest import long_reply, short_ack


def test_short_ack_frame() -> None:
    framer = SonyAnswerFramer()
    ack = short_ack()  # 70 00 70
    assert framer.feed(ack) == [ack]


def test_long_query_reply_frame() -> None:
    framer = SonyAnswerFramer()
    reply = long_reply(b"\x01")  # power reply: 70 00 02 01 73
    assert reply == bytes([0x70, 0x00, 0x02, 0x01, 0x73])
    assert framer.feed(reply) == [reply]


def test_frame_split_across_chunks() -> None:
    framer = SonyAnswerFramer()
    reply = long_reply(b"\x01\x32")  # volume reply shape
    assert framer.feed(reply[:2]) == []  # partial header
    assert framer.feed(reply[2:]) == [reply]


def test_multiple_frames_in_one_chunk() -> None:
    framer = SonyAnswerFramer()
    a = short_ack()
    b = long_reply(b"\x01\x2a")
    assert framer.feed(a + b) == [a, b]


def test_leading_garbage_is_resynced_away() -> None:
    framer = SonyAnswerFramer()
    ack = short_ack()
    # Stray bytes before the header are skipped (scan-to-0x70 resync).
    assert framer.feed(b"\x00\xff\x12" + ack) == [ack]


def test_garbled_short_ack_rescans_and_recovers() -> None:
    """A short ack whose checksum byte is corrupted looks like a long frame;
    the completed candidate fails checksum and is rescanned from start+1 rather
    than trusting the bogus size and swallowing the following real frame."""
    framer = SonyAnswerFramer()
    garbled = bytes([0x70, 0x00, 0x05, 0x11, 0x22, 0x33, 0x44, 0x99])
    assert framer.feed(garbled) == []  # rescanned away, no frame
    ack = short_ack()
    assert framer.feed(ack) == [ack]  # framer not wedged


def test_oversize_long_candidate_resyncs() -> None:
    framer = SonyAnswerFramer()
    # A corrupted size byte that implies a frame larger than max_frame must
    # resync immediately instead of parking forever.
    with pytest.raises(ResyncError):
        framer.feed(bytes([0x70, 0x00, 0xFE]))  # implies 3+254 bytes
    framer.reset()
    ack = short_ack()
    assert framer.feed(ack) == [ack]
