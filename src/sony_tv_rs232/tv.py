"""Main SonyTV controller, built on serialkit.

The robustness machinery (framing, request/response correlation, pacing,
reconnect, the read loop) lives in :class:`serialkit.SerialDevice`. This module
is just the Sony command surface plus the wiring that configures the runtime.

Migration note (fixes the production desync): correlation is no longer the
positional ``pending.pop(0)`` FIFO — Sony answers carry no identifying content,
so the driver serializes with ``max_in_flight = 1`` (one command owed a reply
at a time) and lets serialkit's slot gate + write-abandon guarantee that a
dropped or garbled answer can never shift correlation onto the next command.
The hand-rolled 500 ms sleeps, read loop, teardown, and pending list are gone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import serialx

from ._kit import (
    CommandTimeoutError,
    Pacing,
    ProtocolError,
    SerialDevice,
)
from .const import (
    BAUD_RATE,
    COMMAND_TIMEOUT,
    HEADER_ANSWER,
    INTER_COMMAND_DELAY,
    AdvancedIris,
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
    byte_to_percent,
    encode_control,
    encode_query,
    parse_answer,
    percent_to_byte,
)
from .state import TVState

_LOGGER = logging.getLogger(__name__)


StateCallback = Callable[[TVState | None], None]

# Functions worth polling in query_state(); consumer Bravias ignore them all
# and each simply times out (best effort).
_QUERYABLE: tuple[Function, ...] = (
    Function.POWER,
    Function.INPUT_SELECT,
    Function.VOLUME,
    Function.AUDIO_MUTE,
    Function.PICTURE_MODE,
    Function.PICTURE,
    Function.BRIGHTNESS,
    Function.COLOR,
    Function.SHARPNESS,
    Function.CINE_MOTION,
    Function.ADVANCED_IRIS,
    Function.SOUND_MODE,
    Function.TREBLE,
    Function.BASS,
    Function.SPEAKER_OFF,
    Function.WIDE_MODE,
    Function.MODE_4_3,
    Function.OFF_TIMER,
)


def _is_answer(frame: bytes) -> bool:
    """Match any answer frame.

    Sony answers carry no echo of the request, so there is nothing to
    correlate on by content — ``max_in_flight = 1`` guarantees there is only
    ever one pending, so the sole answer belongs to it.
    """
    return frame.startswith(bytes([HEADER_ANSWER]))


class SonyTV(SerialDevice[TVState]):
    """Async controller for a Sony Bravia TV over RS232.

    Speaks the Sony Bravia RS-232C protocol over any serialx-supported URL
    (``/dev/ttyUSB0``, ``socket://host:port``, ``esphome://host/?port_name=TTL``).

    Sony's documented protocol is set-only: every Set command is acknowledged
    and ``state`` is updated optimistically when an ack arrives. A
    community-discovered query format is also supported for the models that
    honour it; ``query_*`` methods time out on sets that don't.
    """

    framer_factory = SonyAnswerFramer
    max_in_flight = 1  # Sony answers are unaddressed: serialize (see _is_answer)
    pacing = Pacing(min_interval=INTER_COMMAND_DELAY)  # >= 500 ms between sends
    probe = None  # explicit: consumer Bravias ignore queries, so no watchdog
    request_timeout = COMMAND_TIMEOUT

    def __init__(self, port: str) -> None:
        self._port = port
        super().__init__(self._open_connection)
        # True once the TV answers any query, False once a probe goes
        # unanswered (consumer sets are set-only). None until first probed.
        self._supports_queries: bool | None = None

    async def _open_connection(
        self,
    ) -> tuple[object, object]:
        return await serialx.open_serial_connection(self._port, baudrate=BAUD_RATE)

    @property
    def supports_queries(self) -> bool | None:
        """Whether the TV has answered a query since connecting."""
        return self._supports_queries

    # -- serialkit lifecycle callbacks --------------------------------------

    def make_state(self) -> TVState:
        return TVState()

    def copy_state(self, state: TVState) -> TVState:
        # TVState is all immutable-valued fields; a shallow replace() snapshot
        # is correct and far cheaper than deepcopy.
        return state.copy()

    async def on_connect(self) -> None:
        """Arm standby listening, then probe query support (frames flow here).

        serialkit calls this on every (re)connection, so state repopulation is
        owned by the driver, not the HA coordinator.
        """
        try:
            await self.enable_standby_listening()
        except (CommandTimeoutError, ProtocolError) as err:
            _LOGGER.debug("Could not enable standby listening: %s", err)
        try:
            power = await self.query_power()
        except (CommandTimeoutError, ProtocolError) as err:
            self._supports_queries = False
            _LOGGER.debug(
                "TV did not answer a power query (%s); set-only mode", err
            )
            return
        self._supports_queries = True
        if power is PowerState.ON:
            await self.query_state()

    def on_frame(self, frame: bytes) -> None:
        # Sony emits nothing unsolicited; every frame answers the sole pending.
        if not self.pending.feed(frame):
            _LOGGER.debug("Unsolicited answer dropped: %s", frame.hex(" "))

    # -- Connection lifecycle (aliases over serialkit start/stop) -----------

    async def connect(self) -> None:
        """Open the serial connection and run the handshake."""
        await self.start()
        _LOGGER.info("Connected to Sony TV on %s", self._port)

    async def disconnect(self) -> None:
        """Close the serial connection (no reconnect)."""
        await self.stop()
        _LOGGER.info("Disconnected from Sony TV")

    # -- Status queries (compound) ------------------------------------------

    async def query_state(self) -> None:
        """Query every supported attribute and populate ``state``.

        Each query is serialized (``max_in_flight = 1``); sets that do not
        honour queries simply time out and the loop moves on. Notifications are
        batched so a full round delivers one subscriber update, not one per
        answered function.
        """
        with self.batch():
            for function in _QUERYABLE:
                try:
                    await self._query(function)
                except (CommandTimeoutError, ProtocolError) as err:
                    _LOGGER.debug(
                        "Skipping query for %s: %s", function.name, err
                    )

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
        answer = await self._query(Function.POWER)
        return self._parse_power(answer.data)

    async def enable_standby_listening(self) -> None:
        """Allow the TV to accept Power ON commands while in standby.

        Send this once after the TV is powered on; it persists until the TV is
        reset or the standby command is disabled.
        """
        await self._set(Function.STANDBY_COMMAND, bytes([0x01]))

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
        answer = await self._query(Function.INPUT_SELECT)
        return InputSource(tuple(answer.data))

    # -- Volume / mute -------------------------------------------------------

    async def set_volume(self, percent: int) -> None:
        """Set volume to a 0..100 percent."""
        await self._set(Function.VOLUME, bytes([0x01, percent_to_byte(percent)]))
        self._apply("volume", percent)

    async def volume_up(self) -> None:
        await self._set(Function.VOLUME, bytes([0x00, 0x00]))

    async def volume_down(self) -> None:
        await self._set(Function.VOLUME, bytes([0x00, 0x01]))

    async def query_volume(self) -> int:
        answer = await self._query(Function.VOLUME)
        # Reply data echoes the Set shape: [Direct=0x01, value]
        return byte_to_percent(answer.data[1])

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
        answer = await self._query(Function.AUDIO_MUTE)
        # Reply data echoes the Set shape: [Direct=0x01, mute_flag]
        return answer.data[1] == 0x01

    # -- Picture controls (all 0..100) --------------------------------------

    async def set_picture_level(self, percent: int) -> None:
        """Set "Picture" (contrast on most Sony menus) to 0..100."""
        await self._set(Function.PICTURE, bytes([0x01, percent_to_byte(percent)]))
        self._apply("picture_level", percent)

    async def query_picture_level(self) -> int:
        return byte_to_percent((await self._query(Function.PICTURE)).data[1])

    async def set_brightness(self, percent: int) -> None:
        await self._set(Function.BRIGHTNESS, bytes([0x01, percent_to_byte(percent)]))
        self._apply("brightness", percent)

    async def query_brightness(self) -> int:
        return byte_to_percent((await self._query(Function.BRIGHTNESS)).data[1])

    async def set_color(self, percent: int) -> None:
        await self._set(Function.COLOR, bytes([0x01, percent_to_byte(percent)]))
        self._apply("color", percent)

    async def query_color(self) -> int:
        return byte_to_percent((await self._query(Function.COLOR)).data[1])

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
        return byte_to_percent((await self._query(Function.SHARPNESS)).data[1])

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
        return PictureMode((await self._query(Function.PICTURE_MODE)).data[1])

    async def set_sound_mode(self, mode: SoundMode) -> None:
        await self._set(Function.SOUND_MODE, bytes([0x01, mode.value]))
        self._apply("sound_mode", mode)

    async def query_sound_mode(self) -> SoundMode:
        return SoundMode((await self._query(Function.SOUND_MODE)).data[1])

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
        return WideMode((await self._query(Function.WIDE_MODE)).data[1])

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

    async def _set(self, function: Function, data: bytes) -> Answer:
        """Send a Set/Control packet and wait for the (validated) ack."""
        frame = await self.request(encode_control(function.value, data), _is_answer)
        answer = parse_answer(frame)
        answer.raise_for_status(function.value)
        return answer

    async def _query(self, function: Function) -> Answer:
        """Send a Query packet, wait for the reply, and update state from it."""
        frame = await self.request(encode_query(function.value), _is_answer)
        answer = parse_answer(frame)
        answer.raise_for_status(function.value)
        if answer.data:
            self._apply_query(function, answer)
        return answer

    def _apply(self, attr: str, value: object) -> None:
        """Mutate one state field on the caller task and notify on change."""
        if getattr(self.state, attr) == value:
            return
        setattr(self.state, attr, value)
        self.notify()

    def _apply_query(self, function: Function, answer: Answer) -> None:
        """Update ``state`` from a successful query reply.

        Reply data mirrors the corresponding Set command: functions whose Set
        begins with the Direct marker ``0x01`` echo it back (value at
        ``data[1]``); Power/Input/CineMotion carry the value(s) at ``data[0]``.
        """
        data = answer.data
        try:
            if function is Function.POWER:
                self._apply("power", self._parse_power(data))
            elif function is Function.INPUT_SELECT:
                self._apply("input_source", InputSource(tuple(data)))
            elif function is Function.VOLUME:
                self._apply("volume", byte_to_percent(data[1]))
            elif function is Function.AUDIO_MUTE:
                self._apply("audio_mute", data[1] == 0x01)
            elif function is Function.PICTURE_MODE:
                self._apply("picture_mode", PictureMode(data[1]))
            elif function is Function.PICTURE:
                self._apply("picture_level", byte_to_percent(data[1]))
            elif function is Function.BRIGHTNESS:
                self._apply("brightness", byte_to_percent(data[1]))
            elif function is Function.COLOR:
                self._apply("color", byte_to_percent(data[1]))
            elif function is Function.SHARPNESS:
                self._apply("sharpness", byte_to_percent(data[1]))
            elif function is Function.CINE_MOTION:
                self._apply("cine_motion", CineMotion(data[0]))
            elif function is Function.ADVANCED_IRIS:
                self._apply("advanced_iris", AdvancedIris(data[0]))
            elif function is Function.SOUND_MODE:
                self._apply("sound_mode", SoundMode(data[1]))
            elif function is Function.TREBLE:
                self._apply("treble", byte_to_percent(data[2]))
            elif function is Function.BASS:
                self._apply("bass", byte_to_percent(data[2]))
            elif function is Function.SPEAKER_OFF:
                self._apply("speaker_off", data[1] == 0x01)
            elif function is Function.WIDE_MODE:
                self._apply("wide_mode", WideMode(data[1]))
            elif function is Function.MODE_4_3:
                self._apply("mode_4_3", Mode4_3(data[1]))
            elif function is Function.OFF_TIMER:
                self._apply("off_timer", OffTimer(data[1]))
        except (ValueError, KeyError, IndexError) as err:
            _LOGGER.debug(
                "Could not parse query data %s for %s: %s",
                data.hex(" "),
                function.name,
                err,
            )

    @staticmethod
    def _parse_power(data: bytes) -> PowerState:
        if data == b"\x00":
            return PowerState.OFF
        if data == b"\x01":
            return PowerState.ON
        raise ValueError(f"Unknown power data: {data!r}")
