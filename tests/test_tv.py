"""SonyTV driver behaviour on serialkit.testing.

Covers the headline desync fix (a garbled/dropped answer must never be read
as the next command's), set/query round-trips, exact Set wire bytes, error
mapping, the idle-wake resend, the connect handshake, and reconnect."""

from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import FakeSonyTV, NoHandshakeSonyTV, make_tv, short_ack
from serialkit import CommandTimeoutError, ConnectionLostError

from sony_tv_rs232 import (
    HEADER_CONTROL,
    HEADER_INQUIRY,
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


def _standby_arms(fake: FakeSonyTV) -> int:
    return sum(
        1
        for p in fake.received
        if p[0] == HEADER_CONTROL and p[2] == Function.STANDBY_COMMAND.value
    )


async def test_handshake_arms_standby_when_tv_is_on(link) -> None:
    fake = FakeSonyTV(link, query_data={Function.POWER.value: b"\x01"})
    tv = make_tv(link, cls=SonyTV, refresh_on_connect=False)
    await tv.connect()
    try:
        assert _standby_arms(fake) == 1
        await tv.query_power()  # still on: already armed this session
        assert _standby_arms(fake) == 1
    finally:
        await tv.disconnect()


async def test_standby_rearmed_when_tv_first_seen_on(link) -> None:
    """An arm sent while the TV is in standby is not honoured, so the session
    arms again the first time a power query sees the TV on (turned on with
    its remote), and only that once."""
    fake = FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"})
    tv = make_tv(link, cls=SonyTV)
    await tv.connect()
    try:
        assert _standby_arms(fake) == 1  # best-effort arm in standby
        fake.query_data[Function.POWER.value] = b"\x01"
        assert await tv.query_power() is PowerState.ON
        assert _standby_arms(fake) == 2
        await tv.query_power()
        assert _standby_arms(fake) == 2
    finally:
        await tv.disconnect()


async def test_set_only_tv_gets_best_effort_arm(link) -> None:
    fake = FakeSonyTV(link, silent_functions={Function.POWER.value})
    tv = make_tv(link, cls=SonyTV)
    await tv.connect()
    try:
        assert tv.supports_queries is False
        assert _standby_arms(fake) == 1
    finally:
        await tv.disconnect()


async def test_refresh_on_connect_false_skips_full_query(link) -> None:
    fake = FakeSonyTV(
        link,
        query_data={
            Function.POWER.value: b"\x01",
            Function.VOLUME.value: b"\x01\x28",
        },
    )
    tv = make_tv(link, cls=SonyTV, refresh_on_connect=False)
    await tv.connect()
    try:
        assert tv.state.power is PowerState.ON
        assert tv.state.volume is None  # no refresh() round
        queries = [p for p in fake.received if p[0] == HEADER_INQUIRY]
        assert len(queries) == 1
    finally:
        await tv.disconnect()


async def test_connect_log_redacts_port_query(link, caplog) -> None:
    """An esphome:// URL carries the proxy's API key in its query string."""
    FakeSonyTV(link)
    tv = NoHandshakeSonyTV(
        "esphome://proxy.local/?port_name=TTL&key=secret-api-key",
        connect_factory=link.connect,
    )
    with caplog.at_level(logging.DEBUG, logger="sony_tv_rs232"):
        await tv.connect()
        await tv.disconnect()
    assert "esphome://proxy.local/" in caplog.text
    assert "secret-api-key" not in caplog.text


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
    """The power probe is the first frame after open, so it is the one a
    sleeping TV NAKs; a NAK on it must not misread the TV as set-only, nor
    skip the standby-listen arm that follows."""
    fake = FakeSonyTV(
        link,
        query_data={Function.POWER.value: b"\x00"},
        parse_error_first=1,
    )
    tv = make_tv(link, cls=SonyTV)  # real on_connect
    await tv.connect()
    try:
        power = [p for p in fake.received if p[2] == Function.POWER.value]
        assert len(power) == 2  # NAKed once, then answered
        assert tv.supports_queries is True  # the power query was not misread
        assert tv.state.power is PowerState.OFF
        assert _standby_arms(fake) == 1
    finally:
        await tv.disconnect()


async def test_unanswered_run_does_not_resend_each_query(link) -> None:
    """Frames sent to the TV keep its receiver awake, so a run of unsupported
    queries costs one timeout each: the idle gate counts sends, not just
    replies. Gating on the last reply turned every timeout after the second
    into a resend, roughly doubling a refresh() against a set that answers
    few queries."""
    fake = FakeSonyTV(
        link,
        query_data={Function.POWER.value: b"\x01"},
        silent_functions={
            Function.VOLUME.value,
            Function.AUDIO_MUTE.value,
            Function.INPUT_SELECT.value,
            Function.SOUND_MODE.value,
        },
    )
    # Two timeouts (0.4 s) outlast the gate, one (0.2 s) does not.
    tv = make_tv(link, idle_wake_after=0.3)
    await tv.connect()
    try:
        await tv.query_power()
        for query in (
            tv.query_volume,
            tv.query_mute,
            tv.query_input_source,
            tv.query_sound_mode,
        ):
            with pytest.raises(CommandTimeoutError):
                await query()
        queries = [p for p in fake.received if p[0] == HEADER_INQUIRY]
        assert len(queries) == 5  # power + four, none resent
    finally:
        await tv.disconnect()


async def test_silent_query_after_idle_is_resent_once(link) -> None:
    """The silent flavour of the idle wake: the TV swallows the first query
    without answering. The query is resent once and the second answer wins."""
    fake = FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"}, silent_first=1)
    tv = make_tv(link)  # first query of a session: the TV counts as idle
    await tv.connect()
    try:
        assert await tv.query_power() is PowerState.OFF
        assert len(fake.received) == 2
        assert fake.received[0] == fake.received[1]  # identical resend
        assert link.connects == 1
    finally:
        await tv.disconnect()


async def test_persistent_silence_after_idle_raises_after_one_resend(
    link,
) -> None:
    fake = FakeSonyTV(link, silent_first=5)
    tv = make_tv(link)
    await tv.connect()
    try:
        with pytest.raises(CommandTimeoutError):
            await tv.query_power()
        assert len(fake.received) == 2  # bounded: one resend, not five
    finally:
        await tv.disconnect()


async def test_silent_query_soon_after_a_reply_is_not_resent(link) -> None:
    """A query the model does not support times out right after another
    reply; resending it would only double the timeout."""
    fake = FakeSonyTV(link, query_data={Function.POWER.value: b"\x00"})
    tv = make_tv(link, idle_wake_after=60.0)
    await tv.connect()
    try:
        await tv.query_power()  # the TV just answered
        fake.silent_first = 5
        with pytest.raises(CommandTimeoutError):
            await tv.query_volume()
        assert len(fake.received) == 2  # power, then volume once
    finally:
        await tv.disconnect()


async def test_silent_set_is_not_resent(link) -> None:
    """Silence after a set is ambiguous (the command may have landed and only
    the ack been lost), and some sets step or toggle, so sets are not
    resent on a timeout."""
    fake = FakeSonyTV(link, silent_first=1)
    tv = make_tv(link)
    await tv.connect()
    try:
        with pytest.raises(CommandTimeoutError):
            await tv.volume_up()
        assert len(fake.received) == 1
    finally:
        await tv.disconnect()


# ---- connect failures --------------------------------------------------


class _ProxyUnavailable(Exception):
    """A transport's open failure that is not an OSError (serialx's is not)."""


async def test_connect_failure_is_a_kit_connection_error() -> None:
    """Whatever the transport raises when the port cannot be opened, connect()
    raises the kit's ConnectionLostError with the original as its cause, so a
    consumer retrying on SerialKitError sees every transport the same way."""

    async def refuse() -> tuple[object, object]:
        raise _ProxyUnavailable("serial proxy unavailable")

    tv = SonyTV("mock://test", connect_factory=refuse)
    with pytest.raises(ConnectionLostError) as excinfo:
        await tv.connect()
    assert isinstance(excinfo.value.__cause__, _ProxyUnavailable)
    assert not tv.connected


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
