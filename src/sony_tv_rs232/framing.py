"""Sony answer framer.

Sony's answer has two shapes that the general serialkit framers cannot
express, because the frame boundary is decided by the protocol checksum:

    Short (Set ack):    [0x70][status][cs]                 — 3 bytes, no length
    Long (query reply): [0x70][status][size][data...][cs]  — byte 2 is length

There is no length byte in the short form (byte 2 is the checksum), so a
``LengthPrefixedFramer`` would misread every Set ack. The shapes are told apart
by checksum: in a short packet byte 2 must equal ``(0x70 + status) & 0xFF``.

The framer owns the residual buffer and resyncs on a checksum-invalid long
candidate by rescanning from ``start + 1`` rather than trusting the size byte
(which could otherwise swallow the following frames). ``max_frame`` bounds the
resync so a corrupted size byte cannot park it waiting for a phantom frame.
"""

from __future__ import annotations

from .const import HEADER_ANSWER
from .protocol import checksum
from ._kit import ResyncError


class SonyAnswerFramer:
    """serialkit ``Framer`` for Sony Bravia answer packets."""

    def __init__(self, *, max_frame: int = 32) -> None:
        # Sony frames are tiny (3 bytes to ~8); a long run with no valid frame
        # is garbage, so a small max_frame bounds the resync and prevents a
        # corrupted size byte from parking the framer waiting for a phantom
        # long frame.
        self._max_frame = max_frame
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames: list[bytes] = []
        while True:
            # Resync: drop everything before the next answer header.
            idx = self._buffer.find(HEADER_ANSWER)
            if idx < 0:
                # No header at all: keep nothing but guard against an
                # unbounded garbage buffer.
                if len(self._buffer) > self._max_frame:
                    exc = ResyncError(
                        f"{len(self._buffer)} bytes with no answer header",
                        frames=frames,
                    )
                    self._buffer.clear()
                    raise exc
                self._buffer.clear()
                break
            if idx > 0:
                del self._buffer[:idx]
            if len(self._buffer) < 3:
                break  # need at least [0x70][status][b2]

            status = self._buffer[1]
            b2 = self._buffer[2]
            short_cs = (HEADER_ANSWER + status) & 0xFF
            if b2 == short_cs:
                frames.append(bytes(self._buffer[:3]))
                del self._buffer[:3]
                continue

            # Long candidate: byte 2 is the size (data bytes + 1); total is
            # 3 + size and the last byte is the checksum of all preceding.
            total = 3 + b2
            if total > self._max_frame:
                exc = ResyncError(
                    f"long frame of {total} bytes exceeds max_frame={self._max_frame}",
                    frames=frames,
                )
                self._buffer.clear()
                raise exc
            if len(self._buffer) < total:
                break  # wait for the rest of the candidate
            candidate = bytes(self._buffer[:total])
            if checksum(candidate[:-1]) == candidate[-1]:
                frames.append(candidate)
                del self._buffer[:total]
                continue
            # Checksum-invalid long candidate: this 0x70 was not a real frame
            # start (a corrupted short ack, or garbage). Rescan from start+1
            # rather than trusting the bogus size and swallowing real frames.
            del self._buffer[:1]
        return frames

    def reset(self) -> None:
        self._buffer.clear()
