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
  test_protocol.py  -- checksum / encode / parse_answer vectors
  test_tv.py        -- desync regression, set/query round-trips, exact Set bytes,
                       idle-wake resend, handshake, reconnect
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
- **`on_connect` owns the handshake.** It probes query support with a power
  query, arms standby listening, and runs `refresh()` when the TV is on
  (`refresh_on_connect=False` skips that, for a caller that only needs the port
  checked); the link calls it on every connection, so the consumer drives
  neither reconnect nor state repopulation.
- **Standby listening is armed while the TV is on.** Sony only honours the arm
  when it reaches a TV that is on, so `query_power` arms it the first time a
  session sees the TV on (`_standby_armed`, reset per session). A TV that is
  off or set-only at connect gets a best-effort arm from `on_connect`; for a
  set-only TV that is the only chance, since its power is never observed.
- **Caller-task state updates.** An exchange claims its reply, so the reply
  never reaches `on_frame` — the answer bytes alone do not say what they
  answer. Each `set_*`/`query_*` therefore awaits its round trip and then
  mutates `state` and calls `notify()` on the caller task.
- **The first frame after idle is resent once.** A TV whose receiver has
  slept (> ~10 s since its last reply) loses the leading bytes of the next
  frame and answers `PARSE_ERROR` after its ~550 ms receive timeout; frames
  sent within ~400 ms of a reply parse fine. `_round_trip` decodes the answer
  and, on `PARSE_ERROR`, resends inside the *same* held exchange, bounded by
  `IDLE_WAKE_RETRIES`, with no added delay so the resend lands in the wake
  window. This matters most for `on_connect`, whose power probe is by
  construction the first frame after a gap. The division of labour: the kit
  retries on timeouts it can see (`confirm(retries=)`); the driver retries on
  reply content only it can decode.
- **Sometimes the wake is silent.** The TV swallows the first frame and
  answers nothing (seen on a standby power poll about once a day). A *query*
  that times out is resent once in the same exchange, but only when the line
  has been quiet for `IDLE_WAKE_AFTER` seconds (`_last_wire_at`: the last frame
  sent *or* received, reset per session). The receiver sleeps when it hears
  nothing, so a run of unanswered queries keeps it awake; measuring from the
  last reply instead turned every timeout after the second in a `refresh()`
  into a resend. Without the gate every unsupported query, which times out
  right after another frame, would cost two timeouts. Sets are never resent on
  silence: the command may have landed with only the ack lost, and some sets
  step or toggle.
- **`on_connect` always ends with `notify()`.** A reconnect to a TV whose
  state did not change would otherwise deliver nothing after the disconnect's
  `None`, leaving a consumer unavailable until its next poll.
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
  setup for that long (a probe-only caller passes `refresh_on_connect=False`).
  Learning the supported set is the fix, and is also the prerequisite for
  liveness above.

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
