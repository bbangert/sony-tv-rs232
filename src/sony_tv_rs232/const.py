"""Constants and enums shared across the sony_tv_rs232 package."""

from enum import Enum

BAUD_RATE = 9600
COMMAND_TIMEOUT = 2.0  # seconds to wait for a response
INTER_COMMAND_DELAY = 0.5  # Sony spec mandates >= 500ms between commands

# Packet header bytes
HEADER_CONTROL = 0x8C  # host -> TV: Set / Write request
HEADER_INQUIRY = 0x83  # host -> TV: Query / Get request
HEADER_ANSWER = 0x70  # TV -> host: response

# Category byte (fixed at 0x00 for the documented protocol)
CATEGORY = 0x00

# Volume range (TV percent: 0..100)
MIN_VOLUME = 0
MAX_VOLUME = 100

# Picture-related percent ranges
MIN_PERCENT = 0
MAX_PERCENT = 100


class PowerState(Enum):
    """TV chassis power state."""

    OFF = "OFF"
    ON = "ON"


class Function(Enum):
    """Sony Bravia RS-232C function codes (the ``Function`` byte).

    Values come from the Sony KDL/KDS-XBR5 RS-232C protocol manual.
    """

    POWER = 0x00
    STANDBY_COMMAND = 0x01  # arms the TV to listen for power-on while in standby
    INPUT_SELECT = 0x02
    PROGRAM_SELECT = 0x04
    VOLUME = 0x05
    AUDIO_MUTE = 0x06
    LANGUAGE = 0x07
    OFF_TIMER = 0x0C  # sleep timer
    DISPLAY = 0x0F  # toggles the on-screen "info" display
    CLOSED_CAPTION = 0x10
    PICTURE_MODE = 0x20
    PICTURE = 0x23  # contrast (called "Picture" on Sony menus)
    BRIGHTNESS = 0x24
    COLOR = 0x25
    HUE = 0x26
    SHARPNESS = 0x28
    CINE_MOTION = 0x2A
    ADVANCED_IRIS = 0x2B  # SXRD-only
    SOUND_MODE = 0x30
    TREBLE = 0x32
    BASS = 0x33
    SPEAKER_OFF = 0x36
    H_SIZE = 0x40
    H_SHIFT = 0x41
    V_SIZE = 0x42
    V_SHIFT = 0x43
    WIDE_MODE = 0x44
    MODE_4_3 = 0x46


class AnswerCode(Enum):
    """Answer status codes returned by the TV."""

    COMPLETED = 0x00  # normal end
    LIMIT_OVER_MAX = 0x01  # value exceeded upper limit
    LIMIT_OVER_MIN = 0x02  # value below lower limit
    CANCELED = 0x03  # value not permitted in current state
    PARSE_ERROR = 0x04  # malformed packet / bad checksum


class InputSource(Enum):
    """TV input sources for the Input Select command (0x02).

    Each value is the data tuple sent over the wire after the function byte.
    """

    TOGGLE = (0x00,)  # cycle to next input (same as the remote's INPUT key)
    TV = (0x01,)  # select last TV channel
    VIDEO1 = (0x02, 0x01)
    VIDEO2 = (0x02, 0x02)
    VIDEO3 = (0x02, 0x03)
    COMPONENT1 = (0x03, 0x01)
    COMPONENT2 = (0x03, 0x02)
    HDMI1 = (0x04, 0x01)
    HDMI2 = (0x04, 0x02)
    HDMI3 = (0x04, 0x03)
    HDMI4 = (0x04, 0x04)  # SXRD models only
    PC = (0x05, 0x01)


class PictureMode(Enum):
    """Picture mode preset (function 0x20)."""

    VIVID = 0x00
    STANDARD = 0x01
    CINEMA = 0x02
    CUSTOM = 0x03


class SoundMode(Enum):
    """Sound mode preset (function 0x30)."""

    DYNAMIC = 0x00
    STANDARD = 0x01
    CUSTOM = 0x02


class WideMode(Enum):
    """Wide / aspect mode (function 0x44)."""

    WIDE_ZOOM = 0x00
    FULL = 0x01
    ZOOM = 0x02
    NORMAL = 0x03
    PC_NORMAL = 0x05
    PC_FULL_1 = 0x06
    PC_FULL_2 = 0x07
    PC_ZOOM = 0x08  # SXRD models only


class Mode4_3(Enum):
    """4:3 default mode (function 0x46)."""

    OFF = 0x00
    ZOOM = 0x01
    FULL = 0x02
    WIDE_ZOOM = 0x03
    NORMAL = 0x04


class CineMotion(Enum):
    """Cine Motion / cinema drive (function 0x2A)."""

    OFF = 0x00
    AUTO_1 = 0x02
    AUTO_2 = 0x03


class AdvancedIris(Enum):
    """Advanced Iris (function 0x2B) — SXRD models only."""

    MIN = 0x00
    LOW = 0x01
    MID = 0x02
    HIGH = 0x03
    MAX = 0x04
    AUTO_1 = 0x05
    AUTO_2 = 0x06


class ClosedCaption(Enum):
    """Closed caption direct mode (function 0x10, sub-mode 0x02 / 0x01).

    The first byte of the data tuple selects the caption family
    (analogue=0x00 / digital=0x01); the second is the channel.
    """

    OFF = (0x01, 0x00)  # Direct Display Off
    ON = (0x01, 0x01)  # Direct Display On

    ANALOG_CC1 = (0x02, 0x00, 0x01)
    ANALOG_CC2 = (0x02, 0x00, 0x02)
    ANALOG_CC3 = (0x02, 0x00, 0x03)
    ANALOG_CC4 = (0x02, 0x00, 0x04)
    ANALOG_TEXT1 = (0x02, 0x00, 0x05)
    ANALOG_TEXT2 = (0x02, 0x00, 0x06)
    ANALOG_TEXT3 = (0x02, 0x00, 0x07)
    ANALOG_TEXT4 = (0x02, 0x00, 0x08)

    DIGITAL_SERVICE1 = (0x02, 0x01, 0x01)
    DIGITAL_SERVICE2 = (0x02, 0x01, 0x02)
    DIGITAL_SERVICE3 = (0x02, 0x01, 0x03)
    DIGITAL_SERVICE4 = (0x02, 0x01, 0x04)
    DIGITAL_SERVICE5 = (0x02, 0x01, 0x05)
    DIGITAL_SERVICE6 = (0x02, 0x01, 0x06)
    DIGITAL_CC1 = (0x02, 0x01, 0x07)
    DIGITAL_CC2 = (0x02, 0x01, 0x08)
    DIGITAL_CC3 = (0x02, 0x01, 0x09)
    DIGITAL_CC4 = (0x02, 0x01, 0x0A)


class OffTimer(Enum):
    """Sleep timer presets (function 0x0C)."""

    OFF = 0x00
    MIN_15 = 0x0F
    MIN_30 = 0x1E
    MIN_45 = 0x2D
    MIN_60 = 0x3C
    MIN_90 = 0x5A
    MIN_120 = 0x78


class Language(Enum):
    """Menu language (function 0x07). Data is a 3-byte ASCII tag."""

    ENGLISH = b"eng"
    SPANISH = b"spa"
    FRENCH = b"fre"
