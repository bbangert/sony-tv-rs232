"""SonyTV driver behaviour on serialkit.testing.

Covers the headline desync fix (a garbled/dropped answer must never be read
as the next command's), set/query round-trips, exact Set wire bytes, error
mapping, the idle-wake resend, the connect handshake, and reconnect."""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeSonyTV, make_tv, short_ack
from serialkit import CommandTimeoutError

from sony_tv_rs232 import (
    AnswerCode,
    Function,
    InputSource,
    PowerState,
    SonyCommandError,
    SonyProtocolError,
    SonyTV,
)

# ---- set / query round-trips -------------------------------------------


async def test_set_updates_state_and_notifies(link) -> None:
    FakeSonyTV(link)  # acks every set
    tv = make_tv(link)
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.connect()
    try:
        await asyncio.sleep(0)  # flush the initial snapshot
        await tv.set_volume(42)
        assert tv.state.volume == 42
        await asyncio.sleep(0)  # let the coalesced notify flush
        assert snapshots[-1].volume == 42  # subscriber saw the change
    finally:
        await tv.disconnect()


async def test_query_returns_value_and_updates_state(link) -> None:
    FakeSonyTV(link, query_data={Function.VOLUME.value: b"\x01\x1e"})  # 0x1e = 30
    tv = make_tv(link)
    await tv.connect()
    try:
        assert await tv.query_volume() == 30
        assert tv.state.volume == 30
    finally:
        await tv.disconnect()


async def test_non_zero_ack_raises_sony_command_error(link) -> None:
    def reject(_packet: bytes) -> None:
        link.rx(short_ack(0x03))  # CANCELED

    link.on_write = reject
    tv = make_tv(link)
    await tv.connect()
    try:
        with pytest.raises(SonyCommandError) as excinfo:
            await tv.power_on()
        assert excinfo.value.code is AnswerCode.CANCELED
        assert excinfo.value.function == Function.POWER.value
        assert tv.state.power is None  # rejected set did not update state
    finally:
        await tv.disconnect()


async def test_query_short_ack_raises_protocol_error_not_indexerror(
    link,
) -> None:
    """A TV that acks a query with a bare (data-less) COMPLETED reply must
    surface a SonyProtocolError, not a raw IndexError, so the coordinator's
    ProtocolError catch handles it."""
    FakeSonyTV(link)  # unscripted queries get a data-less short ack
    tv = make_tv(link)
    await tv.connect()
    try:
        with pytest.raises(SonyProtocolError):
            await tv.query_volume()
    finally:
        await tv.disconnect()


# ---- the headline fix: no correlation shift on a lost answer -----------


async def test_garbled_answer_does_not_shift_correlation(link) -> None:
    """The production desync: a dropped/garbled answer to command A must not
    resolve command B. The exchange holds the wire, so B stays off it until A
    finishes, and the framer rescans the garble away instead of misframing."""
    tv = make_tv(link)
    await tv.connect()
    try:
        a = asyncio.ensure_future(tv.set_volume(50))
        await asyncio.sleep(0.01)
        b = asyncio.ensure_future(tv.set_brightness(70))
        await asyncio.sleep(0.01)

        # B is gated behind A: only A has hit the wire.
        assert len(link.sent) == 1

        # A's answer is garbled (a corrupt long candidate); the framer rescans
        # it away, nothing resolves A, and A times out.
        link.rx(bytes([0x70, 0x00, 0x05, 0x11, 0x22, 0x33, 0x44, 0x99]))
        with pytest.raises(CommandTimeoutError):
            await a

        # Only now does B reach the wire, and it gets ITS OWN ack.
        await asyncio.sleep(0.01)
        assert len(link.sent) == 2
        link.rx(short_ack())
        await b

        assert tv.state.brightness == 70  # B applied correctly
        assert tv.state.volume is None  # A never misattributed B's answer
        assert link.connects == 1  # no reconnect churn
    finally:
        await tv.disconnect()


# ---- connect handshake -------------------------------------------------


async def test_handshake_marks_supports_queries_when_answered(link) -> None:
    FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"})  # power OFF
    tv = make_tv(link, cls=SonyTV)  # real on_connect handshake
    await tv.connect()
    try:
        assert tv.supports_queries is True
        assert tv.state.power is PowerState.OFF
    finally:
        await tv.disconnect()


async def test_handshake_set_only_when_queries_unanswered(link) -> None:
    tv = make_tv(link, cls=SonyTV)  # nothing answers -> queries time out
    await tv.connect()
    try:
        assert tv.supports_queries is False
    finally:
        await tv.disconnect()


async def test_handshake_power_on_runs_full_query(link) -> None:
    FakeSonyTV(
        link,
        query_data={
            Function.POWER.value: b"\x01",  # ON
            Function.VOLUME.value: b"\x01\x28",  # 40
        },
    )
    tv = make_tv(link, cls=SonyTV)
    await tv.connect()
    try:
        assert tv.supports_queries is True
        assert tv.state.power is PowerState.ON
        assert tv.state.volume == 40  # full query ran because the TV is on
    finally:
        await tv.disconnect()


# ---- reconnect ---------------------------------------------------------


async def test_drop_delivers_none_then_reconnects(link) -> None:
    tv = make_tv(link)  # NoHandshake + fast backoff
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.connect()
    try:
        await asyncio.sleep(0)
        link.drop()  # EOF
        await asyncio.sleep(0.05)  # tiny backoff + reconnect
        assert None in snapshots  # subscriber saw the disconnect
        assert link.connects == 2  # serialkit reconnected on its own
        assert tv.connected
    finally:
        await tv.disconnect()


# ---- exact Set wire bytes ----------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda tv: tv.power_on(), "8c 00 00 02 01 8f"),
        (lambda tv: tv.power_off(), "8c 00 00 02 00 8e"),
        (lambda tv: tv.set_volume(30), "8c 00 05 03 01 1e b3"),
        (lambda tv: tv.select_input_source(InputSource.HDMI1), "8c 00 02 03 04 01 96"),
        (lambda tv: tv.select_input_source(InputSource.TV), "8c 00 02 02 01 91"),
    ],
    ids=["power_on", "power_off", "volume_30", "hdmi1", "tv"],
)
async def test_set_sends_exact_wire_bytes(link, call, expected) -> None:
    """Pin the Set packets against the Sony KDL/KDS-XBR5 manual's examples."""
    fake = FakeSonyTV(link)
    tv = make_tv(link)
    await tv.connect()
    try:
        await call(tv)
        assert fake.received == [bytes.fromhex(expected)]
    finally:
        await tv.disconnect()


# ---- idle-wake resend --------------------------------------------------


async def test_parse_error_after_idle_is_resent_once(link) -> None:
    """A TV that slept through an idle gap NAKs the first frame; the driver
    resends it inside the same exchange and the second answer wins."""
    fake = FakeSonyTV(link, parse_error_first=1)
    tv = make_tv(link)
    await tv.connect()
    try:
        await tv.set_volume(42)  # no exception
        assert tv.state.volume == 42
        assert len(fake.received) == 2
        assert fake.received[0] == fake.received[1]  # identical resend
        assert link.connects == 1  # no reconnect churn
    finally:
        await tv.disconnect()


async def test_persistent_parse_error_raises_after_one_retry(link) -> None:
    """A genuinely malformed packet still surfaces as PARSE_ERROR; the retry
    is bounded so it cannot mask a driver bug, only delay its report."""
    fake = FakeSonyTV(link, parse_error_first=5)
    tv = make_tv(link)
    await tv.connect()
    try:
        with pytest.raises(SonyCommandError) as excinfo:
            await tv.set_volume(42)
        assert excinfo.value.code is AnswerCode.PARSE_ERROR
        assert len(fake.received) == 2  # bounded: one retry, not five
        assert tv.state.volume is None
    finally:
        await tv.disconnect()


async def test_handshake_survives_parse_error_on_first_frame(link) -> None:
    """The standby-listen enable is the first frame after open, so it is the
    one a sleeping TV NAKs; a NAK on it must not leave standby listening
    silently disabled, nor the next probe misread as set-only."""
    fake = FakeSonyTV(
        link,
        query_data={Function.POWER.value: b"\x00"},
        parse_error_first=1,
    )
    tv = make_tv(link, cls=SonyTV)  # real on_connect
    await tv.connect()
    try:
        standby = [p for p in fake.received if p[2] == Function.STANDBY_COMMAND.value]
        assert len(standby) == 2  # NAKed once, then acked
        assert tv.supports_queries is True  # the power query was not misread
        assert tv.state.power is PowerState.OFF
    finally:
        await tv.disconnect()


# ---- lifecycle notifications ------------------------------------------


async def test_disconnect_delivers_none(link) -> None:
    FakeSonyTV(link)
    tv = make_tv(link)
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.connect()
    await tv.disconnect()
    assert snapshots[-1] is None
    assert not tv.connected


async def test_reconnect_notifies_even_when_state_unchanged(link) -> None:
    """A reconnect to a TV whose state did not change must still deliver a
    state after the ``None``, or a consumer stays "unavailable" until the
    next poll."""
    FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"})
    tv = make_tv(link, cls=SonyTV)  # real on_connect, fast backoff
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.connect()
    try:
        await asyncio.sleep(0)
        assert snapshots[-1] is not None  # the connect handshake notified
        before = len(snapshots)
        link.drop()
        await asyncio.sleep(0.05)  # tiny backoff + reconnect + handshake
        assert link.connects == 2
        after = snapshots[before:]
        assert after[0] is None
        assert after[-1] is not None  # state re-delivered on reconnect
        assert after[-1].power is PowerState.OFF  # ...and equal to before
    finally:
        await tv.disconnect()
