# sony-tv-rs232

Async Python library to control Sony Bravia TVs over RS232 serial, built on
[serialkit](https://pypi.org/project/serial-toolkit/) (the `serial-toolkit`
distribution on PyPI) and [serialx](https://github.com/puddly/serialx).

## Installation

```bash
pip install sony-tv-rs232

# To talk to a TV over an ESPHome serial proxy:
pip install 'sony-tv-rs232[esphome]'
```

Requires Python 3.14+.

## Quick start

```python
import asyncio
from sony_tv_rs232 import SonyTV, InputSource


async def main():
    tv = SonyTV("/dev/ttyUSB0")
    await tv.connect()

    await tv.enable_standby_listening()  # so power_on works from standby
    await tv.power_on()
    await tv.set_volume(20)
    await tv.select_input_source(InputSource.HDMI1)

    await tv.disconnect()


asyncio.run(main())
```

## CLI

```bash
python -m sony_tv_rs232 /dev/ttyUSB0                 # query + print status
python -m sony_tv_rs232 socket://192.168.1.29:5000   # raw TCP bridge
python -m sony_tv_rs232 'esphome://192.168.1.29/?port_name=TTL'
python -m sony_tv_rs232 /dev/ttyUSB0 --power on
python -m sony_tv_rs232 /dev/ttyUSB0 --input HDMI1
python -m sony_tv_rs232 /dev/ttyUSB0 --diagnose      # raw wiring diagnosis
```

## Protocol notes

Sony's consumer RS-232C protocol is effectively **set-only**: the TV
acknowledges every Set command but most models ignore query packets, and the
answer frame carries no echo of which command it answers. The library:

- updates `state` optimistically when a Set is acknowledged, and from query
  replies on the Pro Bravia displays that answer them;
- runs every command as an exclusive exchange, because answers are
  unaddressed — holding the wire for one send-and-read round, anchored on
  arrival order, is what keeps a dropped or garbled answer from being read as
  the next command's;
- paces commands ≥ 500 ms apart, as the Sony spec requires;
- resends the first command after an idle gap once: a TV that has slept
  loses the leading bytes of the frame that wakes it and answers
  `PARSE_ERROR`, so that reply is treated as "send it again". When the wake
  is silent instead, a query (never a set) that goes unanswered after the
  TV has been quiet for a few seconds is resent once as well;
- reconnects automatically when the serial link drops.

All of that machinery lives in `serialkit`; this library is the Sony command
surface plus a checksum-discriminated answer framer.

## Connection handling

```python
from sony_tv_rs232 import CommandTimeoutError, ConnectionLostError, SonyCommandError

try:
    await tv.set_volume(50)
except SonyCommandError as err:  # TV returned a non-zero answer code
    ...
except CommandTimeoutError:  # no answer in time (common on set-only TVs)
    ...
except ConnectionLostError:  # serial link dropped
    ...
```

Subscribe to state changes; callbacks receive a `TVState` snapshot, or `None`
when the connection is lost:

```python
unsub = tv.subscribe(lambda state: print("state:", state))
```

## Serial connection

Sony Bravia sets use **9600 baud, 8N1**. The library accepts any
serialx-compatible URL (`/dev/ttyUSB0`, `socket://host:port`,
`esphome://host/?port_name=TTL`).

## Development

```bash
uv run --python 3.14 pytest
uv run --python 3.14 --with mypy mypy --strict src/
uvx ruff check && uvx ruff format --check
```

The RS-232C protocol reference is in [docs/](docs/).

## License

MIT
