# sony-tv-rs232

Async library controlling Sony Bravia TVs over RS232, built on `serialkit`.
This is the first driver migrated onto serialkit and the reason it exists: the
old hand-rolled driver desynced in production (positional FIFO correlation + no
residual buffer + no resync + no watchdog). serialkit owns all of that now.

## Project structure

```
src/sony_tv_rs232/
  __init__.py    -- public API re-exports (incl. serialkit error types)
  _kit.py        -- the single serialkit import seam (rewritten when vendored)
  const.py       -- headers, Function/enum codes, baud, delays
  protocol.py    -- encode_control/encode_query/parse_answer/checksum/Answer;
                    SonyCommandError + SonyProtocolError subclass serialkit.ProtocolError
  framing.py     -- SonyAnswerFramer (checksum-discriminated short-ack vs long)
  state.py       -- TVState dataclass
  tv.py          -- SonyTV(SerialDevice[TVState]): the command surface + wiring
  __main__.py    -- CLI

tests/
  conftest.py       -- FakeSonyTV responder + FastSonyTV/NoHandshakeSonyTV harness
  test_framing.py   -- SonyAnswerFramer byte vectors (incl. garbled-short-ack)
  test_tv.py        -- desync regression, set/query round-trips, handshake, reconnect
```

## Why it's shaped this way

- **`max_in_flight = 1`.** Sony answers carry no echo of the request, so
  content-based correlation is impossible; the matcher (`_is_answer`) accepts
  any answer, and serialization guarantees the sole answer belongs to the sole
  pending. This is the headline desync fix.
- **`SonyAnswerFramer`.** Frame length is checksum-disambiguated (short Set ack
  has no length byte), so no general serialkit framer fits — Sony ships its own
  `Framer`. A checksum-invalid long candidate rescans from `start + 1` rather
  than trusting a corrupt size byte; `max_frame = 32` bounds the resync (legit
  frames are ≤ ~8 bytes).
- **`probe = None`.** Consumer Bravias ignore queries, so a liveness watchdog
  would false-positive-disconnect a healthy TV. Explicitly no watchdog.
- **`on_connect` owns the handshake.** It arms standby listening and probes
  query support (a power query); serialkit calls it on every reconnect, so the
  HA coordinator no longer drives reconnect or state repopulation.
- **Caller-task state updates.** Each `set_*`/`query_*` awaits `request()` then
  mutates `state` and calls `notify()` on the caller task — Sony state is only
  meaningful in request context (the answer bytes alone don't say what they
  answer).

## The `_kit.py` seam

Every serialkit import goes through `_kit.py`. When this package is vendored
into the Home Assistant integration alongside a vendored `serialkit`, only that
one file changes (`from serialkit import ...` → `from ..serialkit import ...`).

## Testing

`pytest` with `pytest-asyncio`, `asyncio_mode = "auto"`; no real hardware.
`FakeSonyTV` (tests/conftest.py) decodes written packets and scripts answers.
Run under Python 3.14 (`uv run --python 3.14 pytest`); serialkit is resolved
from PyPI, where it is published as the `serial-toolkit` distribution (the
import package stays `serialkit`).
