"""Sony driver test harness on serialkit.testing.

No real hardware: a :class:`FakeLink` is the injected transport, and
:class:`FakeSonyTV` decodes written packets and scripts plausible answers so
the driver can be exercised end-to-end (handshake, queries, sets).
"""

from __future__ import annotations

import pytest
from serialkit import Backoff
from serialkit.testing import FakeLink

from sony_tv_rs232 import SonyTV
from sony_tv_rs232.const import HEADER_CONTROL, HEADER_INQUIRY
from sony_tv_rs232.protocol import checksum


def short_ack(code: int = 0x00) -> bytes:
    """A Set-ack answer: [0x70][code][cs]."""
    body = bytes([0x70, code])
    return body + bytes([checksum(body)])


def long_reply(data: bytes, code: int = 0x00) -> bytes:
    """A query reply: [0x70][code][size][data...][cs], size = len(data)+1."""
    body = bytes([0x70, code, len(data) + 1]) + bytes(data)
    return body + bytes([checksum(body)])


FAST_BACKOFF = Backoff(initial=0.01, factor=1.0, max_delay=0.01)


class NoHandshakeSonyTV(SonyTV):
    """SonyTV that skips the on_connect handshake (for command-level tests)."""

    async def on_connect(self) -> None:
        return


class FakeSonyTV:
    """A protocol-aware responder wired to a FakeLink's on_write.

    Sets are acked (COMPLETED). Queries for functions in ``query_data`` get a
    long reply echoing that data; unscripted queries get a bare COMPLETED ack
    (no data), which the driver treats as "answered but nothing to decode".
    """

    def __init__(
        self,
        link: FakeLink,
        *,
        query_data: dict[int, bytes] | None = None,
    ) -> None:
        self.link = link
        self.query_data = dict(query_data or {})
        self.received: list[bytes] = []
        link.on_write = self._on_write

    def _on_write(self, packet: bytes) -> None:
        self.received.append(packet)
        header = packet[0]
        function = packet[2]
        if header == HEADER_INQUIRY:
            data = self.query_data.get(function)
            self.link.rx(long_reply(data) if data is not None else short_ack())
        elif header == HEADER_CONTROL:
            self.link.rx(short_ack())


@pytest.fixture
def link() -> FakeLink:
    return FakeLink()


def make_tv(link: FakeLink, cls: type[SonyTV] = NoHandshakeSonyTV) -> SonyTV:
    """Build a SonyTV on the fake transport, with pacing and timeouts shrunk
    so the suite runs in milliseconds rather than seconds."""
    return cls(
        "mock://test",
        connect_factory=link.connect,
        command_timeout=0.2,
        inter_command_delay=0.0,
        backoff=FAST_BACKOFF,
    )
