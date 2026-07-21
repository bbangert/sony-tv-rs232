"""Protocol helpers for sony_tv_rs232.

Sony Bravia RS-232C protocol per the Pro Bravia Knowledge Center
(https://pro-bravia.sony.net/remote-display-control/serial-control/).

Set / Control packet (host -> TV)::

    [0x8C][0x00][Function][Length][Data...][Checksum]

where ``Length`` = (number of data bytes) + 1 (i.e. the count of bytes
that follow the Length byte, including the trailing checksum), and
``Checksum`` = sum of all preceding bytes mod 256.

Query / Get packet (host -> TV) — fixed 6 bytes::

    [0x83][0x00][Function][0xFF][0xFF][Checksum]

The header byte distinguishes Set (``0x8C``) from Query (``0x83``).

Answer packet (TV -> host)::

    Set ack:        [0x70][Status][Checksum]                     (3 bytes)
    Query reply:    [0x70][Status][Size][Data...][Checksum]

``Size`` follows the same convention as Set's ``Length``: data bytes + 1.

Example::

    Power query:    83 00 00 FF FF 81
    Power reply:    70 00 02 01 73    (Completed; data=0x01 -> power on)
"""

from __future__ import annotations

from dataclasses import dataclass

from ._kit import ProtocolError
from .const import (
    CATEGORY,
    HEADER_ANSWER,
    HEADER_CONTROL,
    HEADER_INQUIRY,
    AnswerCode,
)


class SonyProtocolError(ProtocolError):
    """Raised when a malformed packet is received from the TV.

    Subclasses serialkit's ``ProtocolError`` so a Home Assistant coordinator
    can catch kit-level types without importing sony's private exceptions.
    """


class SonyCommandError(ProtocolError):
    """Raised when the TV returns a non-zero answer code."""

    def __init__(self, code: AnswerCode, function: int) -> None:
        super().__init__(
            f"TV returned {code.name} (0x{code.value:02x}) for function 0x{function:02x}"
        )
        self.code = code
        self.function = function


@dataclass(frozen=True)
class Answer:
    """Parsed answer packet from the TV."""

    code: AnswerCode
    data: bytes  # empty for Set acks; populated for query replies

    @property
    def ok(self) -> bool:
        return self.code is AnswerCode.COMPLETED

    def raise_for_status(self, function: int) -> None:
        if not self.ok:
            raise SonyCommandError(self.code, function)


def checksum(buf: bytes) -> int:
    """Return the protocol checksum of ``buf`` (sum of bytes mod 256).

    >>> checksum(bytes([0x8C, 0x00, 0x00, 0x02, 0x01]))
    143
    """
    return sum(buf) & 0xFF


def encode_control(function: int, data: bytes) -> bytes:
    """Encode a Set / Control packet for transmission to the TV.

    >>> encode_control(0x00, bytes([0x01]))  # power on
    b'\\x8c\\x00\\x00\\x02\\x01\\x8f'
    """
    if not 0 <= function <= 0xFF:
        raise ValueError(f"function out of byte range: {function}")
    if not data:
        raise ValueError("Set packet must have at least one data byte")
    length = len(data) + 1
    if length > 0xFF:
        raise ValueError(f"Data too long: {len(data)} bytes")
    body = bytes([HEADER_CONTROL, CATEGORY, function, length]) + bytes(data)
    return body + bytes([checksum(body)])


def encode_query(function: int) -> bytes:
    """Encode a Query / Get packet for transmission to the TV.

    >>> encode_query(0x00)  # power status query
    b'\\x83\\x00\\x00\\xff\\xff\\x81'
    """
    if not 0 <= function <= 0xFF:
        raise ValueError(f"function out of byte range: {function}")
    body = bytes([HEADER_INQUIRY, CATEGORY, function, 0xFF, 0xFF])
    return body + bytes([checksum(body)])


def parse_answer(packet: bytes) -> Answer:
    """Parse an answer packet (header byte through checksum byte).

    Set acks are 3 bytes: ``[0x70][Status][Checksum]``.
    Query replies are 4+ bytes: ``[0x70][Status][Size][Data...][Checksum]``
    where ``Size`` = data bytes + 1.

    >>> parse_answer(bytes([0x70, 0x00, 0x70])).ok
    True
    >>> parse_answer(bytes([0x70, 0x00, 0x02, 0x01, 0x73])).data
    b'\\x01'
    """
    if len(packet) < 3:
        raise SonyProtocolError(f"Answer too short: {packet!r}")
    if packet[0] != HEADER_ANSWER:
        raise SonyProtocolError(f"Unexpected answer header: 0x{packet[0]:02x}")
    if packet[-1] != checksum(packet[:-1]):
        raise SonyProtocolError(f"Bad checksum on answer: {packet!r}")

    try:
        code = AnswerCode(packet[1])
    except ValueError as err:
        raise SonyProtocolError(f"Unknown answer code: 0x{packet[1]:02x}") from err

    if len(packet) == 3:
        return Answer(code=code, data=b"")

    # Query reply with data: [0x70][status][size][...data][cs]
    # Size byte = data_bytes + 1 (the count of bytes after Size, incl. cs).
    size = packet[2]
    expected_total = 3 + size
    if len(packet) != expected_total:
        raise SonyProtocolError(
            f"Size mismatch: header says {size} (=> {expected_total} total), "
            f"got {len(packet)} bytes"
        )
    return Answer(code=code, data=bytes(packet[3:-1]))


def percent_to_byte(value: int) -> int:
    """Validate a 0..100 percent value and return it as an int byte.

    >>> percent_to_byte(0)
    0
    >>> percent_to_byte(100)
    100
    """
    if not 0 <= value <= 100:
        raise ValueError(f"value out of range 0..100: {value}")
    return value


def byte_to_percent(value: int) -> int:
    """Validate a 0..100 byte received from the TV and return it as a percent.

    >>> byte_to_percent(0)
    0
    >>> byte_to_percent(100)
    100
    """
    if not 0 <= value <= 100:
        raise ValueError(f"data out of range 0..100: {value}")
    return value
