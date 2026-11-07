"""Main SonyTV controller.

The wire machinery — framing, pacing, the read loop, reconnect — lives in
:class:`serialkit.SerialLink`, which this class owns. What is here is the Sony
command surface, the device model, and the decoding that turns a reply into a
value.

Every Sony answer frame is caused by something we sent: the TV emits nothing
unsolicited, and a reply carries no identifier saying which command it answers.
Commands therefore run inside :meth:`serialkit.SerialLink.exchange`, which
holds the wire for one send-and-read round so the reply is unambiguous, and
anchors on arrival order so a late reply to a timed-out command cannot be read
as the next one's answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, TypeVar

import serialx
from serialkit import (
    Backoff,
    CommandTimeoutError,
    Pacing,
    ProtocolError,
    SerialLink,
)

from .const import (
    BAUD_RATE,
    COMMAND_TIMEOUT,
    IDLE_WAKE_AFTER,
    IDLE_WAKE_RETRIES,
    INTER_COMMAND_DELAY,
    AdvancedIris,
    AnswerCode,
    CineMotion,
    ClosedCaption,
    Function,
    InputSource,
    Language,
    Mode4_3,
    OffTimer,
    PictureMode,
    PowerState,
    SoundMode,
    WideMode,
)
from .framing import SonyAnswerFramer
from .protocol import (
    Answer,
    SonyProtocolError,
    byte_to_percent,
    encode_control,
    encode_query,
    parse_answer,
    percent_to_byte,
)
from .state import TVState

_LOGGER = logging.getLogger(__name__)


def _redact(port: str) -> str:
    """Drop a port URL's query string: an esphome:// URL can carry an API key."""
    return port.split("?", 1)[0]


_T = TypeVar("_T")


StateCallback = Callable[[TVState | None], None]


def _parse_power(data: bytes) -> PowerState:
    if data == b"\x00":
        return PowerState.OFF
    if data == b"\x01":
        return PowerState.ON
    raise ValueError(f"Unknown power data: {data!r}")


# Function -> (state attribute, decoder mapping reply data to the value). Reply
# data mirrors the Set shape: most values sit at data[1] behind the 0x01
# "Direct" marker; Power/Input/CineMotion/AdvancedIris carry the value(s) at
# data[0], and Treble/Bass at data[2]. refresh() walks these functions.
_QUERY_DECODERS: dict[Function, tuple[str, Callable[[bytes], Any]]] = {
    Function.POWER: ("power", _parse_power),
    Function.INPUT_SELECT: ("input_source", lambda d: InputSource(tuple(d))),
    Function.VOLUME: ("volume", lambda d: byte_to_percent(d[1])),
    Function.AUDIO_MUTE: ("audio_mute", lambda d: d[1] == 0x01),
    Function.PICTURE_MODE: ("picture_mode", lambda d: PictureMode(d[1])),
    Function.PICTURE: ("picture_level", lambda d: byte_to_percent(d[1])),
    Function.BRIGHTNESS: ("brightness", lambda d: byte_to_percent(d[1])),
    Function.COLOR: ("color", lambda d: byte_to_percent(d[1])),
    Function.SHARPNESS: ("sharpness", lambda d: byte_to_percent(d[1])),
    Function.CINE_MOTION: ("cine_motion", lambda d: CineMotion(d[0])),
    Function.ADVANCED_IRIS: ("advanced_iris", lambda d: AdvancedIris(d[0])),
    Function.SOUND_MODE: ("sound_mode", lambda d: SoundMode(d[1])),
    Function.TREBLE: ("treble", lambda d: byte_to_percent(d[2])),
    Function.BASS: ("bass", lambda d: byte_to_percent(d[2])),
    Function.SPEAKER_OFF: ("speaker_off", lambda d: d[1] == 0x01),
    Function.WIDE_MODE: ("wide_mode", lambda d: WideMode(d[1])),
    Function.MODE_4_3: ("mode_4_3", lambda d: Mode4_3(d[1])),
    Function.OFF_TIMER: ("off_timer", lambda d: OffTimer(d[1])),
}

# Functions worth polling in refresh(); consumer Bravias honour only a few and
# the rest simply time out (best effort).
_QUERYABLE: tuple[Function, ...] = tuple(_QUERY_DECODERS)


class SonyTV:
    """Async controller for a Sony Bravia TV over RS232.

    Speaks the Sony Bravia RS-232C protocol over any serialx-supported URL
    (``/dev/ttyUSB0``, ``socket://host:port``, ``esphome://host/?port_name=TTL``).

    Sony's documented protocol is set-only: every Set command is acknowledged
    and ``state`` is updated when an ack arrives. A community-discovered query
    format is also supported for the models that honour it; ``query_*`` methods
    time out on sets that don't.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = BAUD_RATE,
        command_timeout: float = COMMAND_TIMEOUT,
        inter_command_delay: float = INTER_COMMAND_DELAY,
        backoff: Backoff | None = None,
        connect_factory: Callable[[], Any] | None = None,
        idle_wake_after: float = IDLE_WAKE_AFTER,
        refresh_on_connect: bool = True,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._command_timeout = command_timeout
        self._idle_wake_after = idle_wake_after
        self._refresh_on_connect = refresh_on_connect
        # Loop time of the last frame on the wire this session, in either
        # direction; None until the first send.
        self._last_wire_at: float | None = None
        # Whether standby listening was armed while the TV was known to be on
        # this session. Sony only honours the arm when it is sent to a TV that
        # is on, so an arm sent in standby (or to a set-only TV whose power is
        # unknown) is best-effort and is repeated once the TV is seen on.
        self._standby_armed = False
        self.state = TVState()
        self.link = SerialLink(
            connect=connect_factory or self._open_connection,
            framer=SonyAnswerFramer(),
            handler=self,
            pacing=Pacing(min_interval=inter_command_delay),
            # Liveness is deliberately unset for now. FailureCount is the right
            # shape for a device that emits nothing unsolicited, but a refresh
            # against a set-only TV produces a long run of consecutive
            # timeouts, which would trip it on every connect. It can be enabled
            # once refresh() only asks for functions the TV is known to answer.
            liveness=None,
            backoff=backoff or Backoff(),
        )
        # True once the TV answers any query, False once a probe goes
        # unanswered (consumer sets are set-only). None until first probed.
        self._supports_queries: bool | None = None
        self._subscribers: list[StateCallback] = []
        self._notify_dirty = False
        self._notify_scheduled = False
        self._batch_depth = 0

    async def _open_connection(self) -> tuple[Any, Any]:
        return await serialx.open_serial_connection(self._port, baudrate=self._baudrate)

    @property
    def supports_queries(self) -> bool | None:
        """Whether the TV has answered a query since connecting."""
        return self._supports_queries

    @property
    def connected(self) -> bool:
        return self.link.connected

    # -- Subscribers ---------------------------------------------------------

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """Register ``callback`` for state updates; returns an unsubscribe fn.

        The callback receives the live :class:`TVState` when something changes,
        and ``None`` when the connection drops.
        """
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._subscribers.remove(callback)

        return unsubscribe

    def notify(self) -> None:
        """Request a coalesced state update to subscribers.

        Any number of calls within one event-loop turn (or one :meth:`batch`
        block) deliver a single callback.
        """
        self._notify_dirty = True
        if not self._notify_scheduled:
            self._notify_scheduled = True
            asyncio.get_running_loop().call_soon(self._flush_notify)

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Hold notifications for the block, then deliver one.

        A refresh round updates many fields; without this each one would reach
        subscribers separately.
        """
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self._flush_notify()

    def _flush_notify(self) -> None:
        self._notify_scheduled = False
        if self._batch_depth or not self._notify_dirty:
            return
        self._notify_dirty = False
        self._deliver(self.state)

    def _deliver(self, state: TVState | None) -> None:
        for callback in list(self._subscribers):
            try:
                callback(state)
            except Exception:
                _LOGGER.exception("State subscriber raised")

    # -- serialkit DeviceHandler callbacks ----------------------------------

    def on_frame(self, frame: bytes) -> None:
        """Frames outside an exchange.

        Sony emits nothing unsolicited and every command claims its own reply,
        so anything arriving here is a late reply to a command that already
        timed out.
        """
        _LOGGER.debug("Unsolicited answer dropped: %s", frame.hex(" "))

    async def on_connect(self) -> None:
        """Probe query support, arm standby listening, and repopulate state.

        The link calls this on every connection with frames already flowing, so
        repopulating state is owned by the driver rather than the consumer.
        Subscribers always get a state at the end, even when nothing changed:
        a reconnect to an unchanged TV must still cancel the ``None`` that the
        disconnect delivered.

        A TV that answers the power query as on is armed by ``query_power``.
        Otherwise (in standby, or set-only) the arm is sent best-effort: it is
        the only chance a set-only TV gets, and a TV in standby is armed again
        once a later power query sees it on.
        """
        try:
            power = await self.query_power()
        except (CommandTimeoutError, ProtocolError) as err:
            power = None
            self._supports_queries = False
            _LOGGER.debug("TV did not answer a power query (%s); set-only mode", err)
        else:
            self._supports_queries = True
        if power is not PowerState.ON:
            await self._try_enable_standby_listening()
        elif self._refresh_on_connect:
            await self.refresh()
        self.notify()

    def on_disconnect(self, exc: Exception | None) -> None:  # noqa: ARG002
        self._last_wire_at = None  # a new session starts with the TV idle
        self._standby_armed = False
        self._notify_dirty = False  # a pending update must not outrun None
        self._deliver(None)

    # -- Connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        """Open the serial connection and run the handshake."""
        await self.link.start()
        _LOGGER.info("Connected to Sony TV on %s", _redact(self._port))

    async def disconnect(self) -> None:
        """Close the serial connection (no reconnect)."""
        await self.link.stop()
        _LOGGER.info("Disconnected from Sony TV")

    # -- Status queries (compound) ------------------------------------------

    async def refresh(self) -> None:
        """Query every supported attribute and populate ``state``.

        Each query holds the wire for its own round; a TV that does not honour
        a query simply times out and the round moves on. Updates are batched,
        so a full round delivers one subscriber callback.
        """
        with self.batch():
            for function in _QUERYABLE:
                try:
                    await self._query(function)
                except (CommandTimeoutError, ProtocolError) as err:
                    _LOGGER.debug("Skipping query for %s: %s", function.name, err)

    # -- Power ---------------------------------------------------------------

    async def power_on(self) -> None:
        """Turn the TV on. Requires that Standby Command was previously
        enabled — otherwise the TV will not accept Power ON while in
        standby. See ``enable_standby_listening``."""
        await self._set(Function.POWER, bytes([0x01]))
        self._apply("power", PowerState.ON)

    async def power_off(self) -> None:
        """Turn the TV off (into standby)."""
        await self._set(Function.POWER, bytes([0x00]))
        self._apply("power", PowerState.OFF)

    async def query_power(self) -> PowerState:
        """Query the TV's power state (community query format)."""
        power: PowerState = await self._query(Function.POWER)
        if power is PowerState.ON and not self._standby_armed:
            # Sony honours the arm only while the TV is on; this is the first
            # moment this session it is known to be.
            self._standby_armed = await self._try_enable_standby_listening()
        return power

    async def enable_standby_listening(self) -> None:
        """Allow the TV to accept Power ON commands while in standby.

        Send this once after the TV is powered on; it persists until the TV is
        reset or the standby command is disabled.
        """
        await self._set(Function.STANDBY_COMMAND, bytes([0x01]))

    async def _try_enable_standby_listening(self) -> bool:
        """Arm standby listening, reporting instead of raising a failure."""
        try:
            await self.enable_standby_listening()
        except (CommandTimeoutError, ProtocolError) as err:
            _LOGGER.debug("Could not enable standby listening: %s", err)
            return False
        return True

    async def disable_standby_listening(self) -> None:
        """Stop the TV from listening for Power ON while in standby."""
        await self._set(Function.STANDBY_COMMAND, bytes([0x00]))

    # -- Input ---------------------------------------------------------------

    async def select_input_source(self, source: InputSource) -> None:
        """Select an input source."""
        await self._set(Function.INPUT_SELECT, bytes(source.value))
        self._apply("input_source", source)

    async def select_next_input_source(self) -> None:
        """Cycle to the next input (same effect as the remote's INPUT key)."""
        await self._set(Function.INPUT_SELECT, bytes(InputSource.TOGGLE.value))

    async def query_input_source(self) -> InputSource:
        source: InputSource = await self._query(Function.INPUT_SELECT)
        return source

    # -- Volume / mute -------------------------------------------------------

    async def set_volume(self, percent: int) -> None:
        """Set volume to a 0..100 percent."""
        await self._set(Function.VOLUME, bytes([0x01, percent_to_byte(percent)]))
        self._apply("volume", percent)

    async def volume_up(self) -> None:
        """Step the volume up one unit.

        The acked step does not say where it landed (it may have clamped, or
        the remote may have moved the volume since the last set), so the
        cached ``volume`` is cleared rather than guessed.
        """
        await self._set(Function.VOLUME, bytes([0x00, 0x00]))
        self._apply("volume", None)

    async def volume_down(self) -> None:
        """Step the volume down one unit; clears ``volume`` like :meth:`volume_up`."""
        await self._set(Function.VOLUME, bytes([0x00, 0x01]))
        self._apply("volume", None)

    async def query_volume(self) -> int:
        volume: int = await self._query(Function.VOLUME)
        return volume

    async def mute_on(self) -> None:
        await self._set(Function.AUDIO_MUTE, bytes([0x01, 0x01]))
        self._apply("audio_mute", True)

    async def mute_off(self) -> None:
        await self._set(Function.AUDIO_MUTE, bytes([0x01, 0x00]))
        self._apply("audio_mute", False)

    async def mute_toggle(self) -> None:
        await self._set(Function.AUDIO_MUTE, bytes([0x00]))

    async def query_mute(self) -> bool:
        """Query mute. Returns True when audio is muted."""
        muted: bool = await self._query(Function.AUDIO_MUTE)
        return muted

    # -- Picture controls (all 0..100) --------------------------------------

    async def set_picture_level(self, percent: int) -> None:
        """Set "Picture" (contrast on most Sony menus) to 0..100."""
        await self._set(Function.PICTURE, bytes([0x01, percent_to_byte(percent)]))
        self._apply("picture_level", percent)

    async def query_picture_level(self) -> int:
        level: int = await self._query(Function.PICTURE)
        return level

    async def set_brightness(self, percent: int) -> None:
        await self._set(Function.BRIGHTNESS, bytes([0x01, percent_to_byte(percent)]))
        self._apply("brightness", percent)

    async def query_brightness(self) -> int:
        brightness: int = await self._query(Function.BRIGHTNESS)
        return brightness

    async def set_color(self, percent: int) -> None:
        await self._set(Function.COLOR, bytes([0x01, percent_to_byte(percent)]))
        self._apply("color", percent)

    async def query_color(self) -> int:
        color: int = await self._query(Function.COLOR)
        return color

    async def set_hue(self, red: int, green: int) -> None:
        """Set hue. Sony exposes both red-bias and green-bias on a 0..100 scale."""
        await self._set(
            Function.HUE,
            bytes([0x01, 0x00, percent_to_byte(red), 0x01, percent_to_byte(green)]),
        )

    async def set_sharpness(self, percent: int) -> None:
        await self._set(Function.SHARPNESS, bytes([0x01, percent_to_byte(percent)]))
        self._apply("sharpness", percent)

    async def query_sharpness(self) -> int:
        sharpness: int = await self._query(Function.SHARPNESS)
        return sharpness

    # -- Audio controls ------------------------------------------------------

    async def set_treble(self, percent: int) -> None:
        await self._set(Function.TREBLE, bytes([0x01, 0x00, percent_to_byte(percent)]))
        self._apply("treble", percent)

    async def set_bass(self, percent: int) -> None:
        await self._set(Function.BASS, bytes([0x01, 0x00, percent_to_byte(percent)]))
        self._apply("bass", percent)

    async def speaker_on(self) -> None:
        """Re-enable the TV's internal speakers."""
        await self._set(Function.SPEAKER_OFF, bytes([0x01, 0x00]))
        self._apply("speaker_off", False)

    async def speaker_off(self) -> None:
        """Mute the TV's internal speakers (e.g. for external audio)."""
        await self._set(Function.SPEAKER_OFF, bytes([0x01, 0x01]))
        self._apply("speaker_off", True)

    # -- Modes ---------------------------------------------------------------

    async def set_picture_mode(self, mode: PictureMode) -> None:
        await self._set(Function.PICTURE_MODE, bytes([0x01, mode.value]))
        self._apply("picture_mode", mode)

    async def query_picture_mode(self) -> PictureMode:
        mode: PictureMode = await self._query(Function.PICTURE_MODE)
        return mode

    async def set_sound_mode(self, mode: SoundMode) -> None:
        await self._set(Function.SOUND_MODE, bytes([0x01, mode.value]))
        self._apply("sound_mode", mode)

    async def query_sound_mode(self) -> SoundMode:
        mode: SoundMode = await self._query(Function.SOUND_MODE)
        return mode

    async def set_cine_motion(self, mode: CineMotion) -> None:
        await self._set(Function.CINE_MOTION, bytes([mode.value]))
        self._apply("cine_motion", mode)

    async def set_advanced_iris(self, mode: AdvancedIris) -> None:
        """SXRD models only."""
        await self._set(Function.ADVANCED_IRIS, bytes([mode.value]))
        self._apply("advanced_iris", mode)

    async def set_wide_mode(self, mode: WideMode) -> None:
        await self._set(Function.WIDE_MODE, bytes([0x01, mode.value]))
        self._apply("wide_mode", mode)

    async def query_wide_mode(self) -> WideMode:
        mode: WideMode = await self._query(Function.WIDE_MODE)
        return mode

    async def set_4_3_mode(self, mode: Mode4_3) -> None:
        await self._set(Function.MODE_4_3, bytes([0x01, mode.value]))
        self._apply("mode_4_3", mode)

    # -- Misc ---------------------------------------------------------------

    async def toggle_display(self) -> None:
        """Toggle the on-screen info display (same as the remote's "info" key)."""
        await self._set(Function.DISPLAY, bytes([0x00]))

    async def set_off_timer(self, timer: OffTimer) -> None:
        """Set the sleep timer."""
        await self._set(Function.OFF_TIMER, bytes([0x01, timer.value]))
        self._apply("off_timer", timer)

    async def set_language(self, language: Language) -> None:
        """Set the menu language."""
        await self._set(Function.LANGUAGE, bytes([0x00]) + language.value)
        self._apply("language", language)

    async def set_closed_caption(self, caption: ClosedCaption) -> None:
        await self._set(Function.CLOSED_CAPTION, bytes(caption.value))

    # -- Internals ----------------------------------------------------------

    async def _round_trip(
        self, packet: bytes, *, resend_on_silence: bool = False
    ) -> Answer:
        """Send one packet and read its decoded reply, holding the wire for both.

        Exclusivity is what makes the reply attributable at all: Sony answers
        carry no identifier, so the only thing that makes this frame *this*
        command's answer is that nothing else was outstanding.

        A TV that has gone idle loses the first frame waking up. Usually it
        says so, answering PARSE_ERROR; sometimes it answers nothing. Either
        is the signal to resend once (``IDLE_WAKE_RETRIES``), inside the same
        exchange so the retry is still the only thing on the wire. The resend
        re-anchors on arrival order, so a reply to the first send that arrives
        before the resend is discarded, and one arriving after it answers the
        same packet.

        Silence is only resent when ``resend_on_silence`` is set (queries,
        which have no side effect) and the line has been quiet for
        ``idle_wake_after`` seconds: a query the model does not support times
        out right after another frame and must not cost a second timeout. A
        persistent PARSE_ERROR (a genuinely malformed packet) or silence still
        surfaces after the one resend.
        """
        async with self.link.exchange() as exchange:
            resends = 0
            while True:
                after_idle = self._idle()
                await exchange.send(packet)
                self._mark_wire()
                try:
                    frame = await exchange.next(timeout=self._command_timeout)
                except CommandTimeoutError:
                    if resend_on_silence and after_idle and resends < IDLE_WAKE_RETRIES:
                        resends += 1
                        _LOGGER.debug(
                            "No reply to the first frame after idle; resending"
                        )
                        continue
                    raise
                self._mark_wire()
                answer = parse_answer(frame)
                if (
                    answer.code is not AnswerCode.PARSE_ERROR
                    or resends >= IDLE_WAKE_RETRIES
                ):
                    return answer
                resends += 1
                _LOGGER.debug(
                    "TV reported PARSE_ERROR (first frame after idle); resending"
                )

    def _mark_wire(self) -> None:
        """Record a frame on the wire (either direction) for the idle gate."""
        self._last_wire_at = asyncio.get_running_loop().time()

    def _idle(self) -> bool:
        """Whether the line has been quiet long enough for the TV to sleep.

        The receiver sleeps when it hears nothing, so a frame sent to it counts
        as much as one it sends back: a run of unanswered queries keeps it
        awake, and must not turn every timeout after the first into a resend.
        """
        if self._last_wire_at is None:
            return True
        quiet = asyncio.get_running_loop().time() - self._last_wire_at
        return quiet >= self._idle_wake_after

    async def _set(self, function: Function, data: bytes) -> Answer:
        """Send a Set/Control packet and wait for the (validated) ack."""
        answer = await self._round_trip(encode_control(function.value, data))
        answer.raise_for_status(function.value)
        return answer

    async def _query(self, function: Function) -> Any:
        """Query the TV, decode the reply via the table, update state, return it.

        Raises SonyProtocolError if the reply can't be decoded (e.g. a
        data-less ack from a set-only TV), which refresh() and the connect
        handshake treat as "unanswered".
        """
        answer = await self._round_trip(
            encode_query(function.value), resend_on_silence=True
        )
        answer.raise_for_status(function.value)
        attr, decoder = _QUERY_DECODERS[function]
        value = self._decode(answer, decoder)
        self._apply(attr, value)
        return value

    def _apply(self, attr: str, value: object) -> None:
        """Mutate one state field on the caller task and notify on change."""
        if getattr(self.state, attr) == value:
            return
        setattr(self.state, attr, value)
        self.notify()

    @staticmethod
    def _decode(answer: Answer, decoder: Callable[[bytes], _T]) -> _T:
        """Run a query-reply decoder, mapping malformed data to a protocol
        error so a short/garbled reply raises SonyProtocolError (a kit
        ProtocolError the coordinator catches) rather than a bare IndexError
        or ValueError."""
        try:
            return decoder(answer.data)
        except (ValueError, IndexError, KeyError, TypeError) as err:
            raise SonyProtocolError(
                f"could not decode query reply {answer.data.hex(' ')!r}: {err}"
            ) from err
