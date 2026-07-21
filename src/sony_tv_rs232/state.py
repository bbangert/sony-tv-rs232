"""Runtime state dataclass for sony_tv_rs232."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .const import (
    AdvancedIris,
    CineMotion,
    InputSource,
    Language,
    Mode4_3,
    OffTimer,
    PictureMode,
    PowerState,
    SoundMode,
    WideMode,
)


@dataclass
class TVState:
    """Snapshot of the Sony TV's current state.

    Consumer Sony Bravia TVs do not support inquiry commands over RS232:
    the protocol is set-only. Fields are populated optimistically when a
    matching set command receives a successful (Completed) ack. Until then
    they remain ``None``.
    """

    power: PowerState | None = None
    input_source: InputSource | None = None

    # 0..100 percent
    volume: int | None = None
    audio_mute: bool | None = None  # True = muted

    picture_mode: PictureMode | None = None
    picture_level: int | None = None  # contrast (Sony "Picture")
    brightness: int | None = None
    color: int | None = None
    sharpness: int | None = None
    cine_motion: CineMotion | None = None
    advanced_iris: AdvancedIris | None = None

    sound_mode: SoundMode | None = None
    treble: int | None = None
    bass: int | None = None
    speaker_off: bool | None = None

    wide_mode: WideMode | None = None
    mode_4_3: Mode4_3 | None = None

    off_timer: OffTimer | None = None
    language: Language | None = None

    def copy(self) -> TVState:
        return replace(self)
