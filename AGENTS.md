# sony-tv-rs232

Async library controlling Sony Bravia TVs over RS232, built on `serialkit`
(installed from PyPI as the `serial-toolkit` distribution; the import package
is `serialkit`).

## Project structure

```
src/sony_tv_rs232/
  __init__.py    -- public API re-exports (incl. serialkit error types)
  const.py       -- headers, Function/enum codes, baud, delays
  protocol.py    -- encode_control/encode_query/parse_answer/checksum/Answer;
                    SonyCommandError + SonyProtocolError subclass serialkit.ProtocolError
  framing.py     -- SonyAnswerFramer (checksum-discriminated short-ack vs long)
  state.py       -- TVState dataclass
  tv.py          -- SonyTV: owns a SerialLink; the command surface + wiring
  __main__.py    -- CLI

tests/
  conftest.py       -- FakeSonyTV responder + NoHandshakeSonyTV harness
  test_framing.py   -- SonyAnswerFramer byte vectors (incl. garbled-short-ack)
  test_tv.py        -- desync regression, set/query round-trips, handshake, reconnect
```

## Why it's shaped this way

- **`SonyTV` owns a `SerialLink` and implements `DeviceHandler`.** The kit
  handles framing, pacing, the read loop and reconnect; this package holds the
  command surface and the device model (`self.state`).
- **Every command is an `exchange()`.** Sony answers carry no echo of the
  request, so a reply is attributable only because nothing else was
  outstanding. The exchange holds the wire for one send-and-read round and
  anchors on arrival order, so a late reply to a timed-out command cannot be
  read as the next one's answer. This is the headline desync fix.
- **`SonyAnswerFramer`.** Frame length is checksum-disambiguated (a short Set
  ack has no length byte), so no general serialkit framer fits — Sony ships its
  own `Framer`. A checksum-invalid long candidate rescans from `start + 1`
  rather than trusting a corrupt size byte; `max_frame = 32` bounds the resync
  (legitimate frames are ≤ ~8 bytes).
- **`on_connect` owns the handshake.** It arms standby listening and probes
  query support with a power query; the link calls it on every connection, so
  the consumer drives neither reconnect nor state repopulation.
- **Caller-task state updates.** An exchange claims its reply, so the reply
  never reaches `on_frame` — the answer bytes alone do not say what they
  answer. Each `set_*`/`query_*` therefore awaits its round trip and then
  mutates `state` and calls `notify()` on the caller task.
- **Subscriptions live here, not in the kit.** `serialkit` holds no device
  state, so `subscribe()`/`notify()`/`batch()` are this package's own,
  coalescing through `call_soon` so a `refresh()` round delivers one callback.

## Known gaps

- **No liveness.** `FailureCount` is the correct shape for a device that emits
  nothing unsolicited, but a `refresh()` against a set-only TV produces a long
  run of consecutive timeouts that would trip it on every connect. It can be
  enabled once `refresh()` asks only for functions the TV is known to answer.
- **`refresh()` is slow on a real set.** 18 functions × a 2 s timeout, and a
  consumer Bravia answers roughly four of them, so a full round costs tens of
  seconds. `on_connect` runs it when the TV is on, which blocks connection
  setup for that long. Learning the supported set is the fix, and is also the
  prerequisite for liveness above.

## Testing

`pytest` with `pytest-asyncio`, `asyncio_mode = "auto"`; no real hardware.
`FakeSonyTV` (tests/conftest.py) decodes written packets and scripts answers,
and `make_tv()` shrinks pacing, timeouts and backoff through constructor
arguments so the suite runs in milliseconds.

```bash
uv run --python 3.14 pytest
uv run --python 3.14 --with mypy mypy --strict src/
uvx ruff check && uvx ruff format --check
```

Run mypy through `uv run` rather than `uvx` so `serialx` and `serialkit` are
importable — without them every inference downstream of an import degrades to
`Any` and the output is noise.
