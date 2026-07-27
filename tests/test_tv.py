"""SonyTV driver behaviour on serialkit.testing.

Covers the headline desync fix (a garbled/dropped answer must never shift
correlation onto the next command), set/query round-trips, error mapping, the
connect handshake, and reconnect."""

from __future__ import annotations

import asyncio

import pytest
from serialkit import CommandTimeoutError

from sony_tv_rs232 import (
    AnswerCode,
    Function,
    PowerState,
    SonyCommandError,
    SonyProtocolError,
)

from conftest import FakeSonyTV, FastSonyTV, make_tv, short_ack


# ---- set / query round-trips -------------------------------------------


async def test_set_updates_state_and_notifies(link) -> None:
    FakeSonyTV(link)  # acks every set
    tv = make_tv(link)
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.start()
    try:
        await asyncio.sleep(0)  # flush the initial snapshot
        await tv.set_volume(42)
        assert tv.state.volume == 42
        await asyncio.sleep(0)  # let the coalesced notify flush
        assert snapshots[-1].volume == 42  # subscriber saw the change
    finally:
        await tv.stop()


async def test_query_returns_value_and_updates_state(link) -> None:
    FakeSonyTV(link, query_data={Function.VOLUME.value: b"\x01\x1e"})  # 0x1e = 30
    tv = make_tv(link)
    await tv.start()
    try:
        assert await tv.query_volume() == 30
        assert tv.state.volume == 30
    finally:
        await tv.stop()


async def test_non_zero_ack_raises_sony_command_error(link) -> None:
    def reject(_packet: bytes) -> None:
        link.rx(short_ack(0x03))  # CANCELED

    link.on_write = reject
    tv = make_tv(link)
    await tv.start()
    try:
        with pytest.raises(SonyCommandError) as excinfo:
            await tv.power_on()
        assert excinfo.value.code is AnswerCode.CANCELED
        assert excinfo.value.function == Function.POWER.value
        assert tv.state.power is None  # rejected set did not update state
    finally:
        await tv.stop()


async def test_query_short_ack_raises_protocol_error_not_indexerror(
    link,
) -> None:
    """A TV that acks a query with a bare (data-less) COMPLETED reply must
    surface a SonyProtocolError, not a raw IndexError, so the coordinator's
    ProtocolError catch handles it."""
    FakeSonyTV(link)  # unscripted queries get a data-less short ack
    tv = make_tv(link)
    await tv.start()
    try:
        with pytest.raises(SonyProtocolError):
            await tv.query_volume()
    finally:
        await tv.stop()


# ---- the headline fix: no correlation shift on a lost answer -----------


async def test_garbled_answer_does_not_shift_correlation(link) -> None:
    """The production desync: a dropped/garbled answer to command A must not
    resolve command B. max_in_flight=1 keeps B off the wire until A completes,
    and the framer rescans the garble away instead of misframing."""
    tv = make_tv(link)
    await tv.start()
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
        await tv.stop()


# ---- connect handshake -------------------------------------------------


async def test_handshake_marks_supports_queries_when_answered(link) -> None:
    FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"})  # power OFF
    tv = make_tv(link, cls=FastSonyTV)  # real on_connect handshake
    await tv.start()
    try:
        assert tv.supports_queries is True
        assert tv.state.power is PowerState.OFF
    finally:
        await tv.stop()


async def test_handshake_set_only_when_queries_unanswered(link) -> None:
    tv = make_tv(link, cls=FastSonyTV)  # nothing answers -> queries time out
    await tv.start()
    try:
        assert tv.supports_queries is False
    finally:
        await tv.stop()


async def test_handshake_power_on_runs_full_query(link) -> None:
    FakeSonyTV(
        link,
        query_data={
            Function.POWER.value: b"\x01",  # ON
            Function.VOLUME.value: b"\x01\x28",  # 40
        },
    )
    tv = make_tv(link, cls=FastSonyTV)
    await tv.start()
    try:
        assert tv.supports_queries is True
        assert tv.state.power is PowerState.ON
        assert tv.state.volume == 40  # full query ran because the TV is on
    finally:
        await tv.stop()


# ---- reconnect ---------------------------------------------------------


async def test_drop_delivers_none_then_reconnects(link) -> None:
    tv = make_tv(link)  # NoHandshake + fast backoff
    snapshots: list = []
    tv.subscribe(snapshots.append)
    await tv.start()
    try:
        await asyncio.sleep(0)
        link.drop()  # EOF
        await asyncio.sleep(0.05)  # tiny backoff + reconnect
        assert None in snapshots  # subscriber saw the disconnect
        assert link.connects == 2  # serialkit reconnected on its own
        assert tv.connected
    finally:
        await tv.stop()
