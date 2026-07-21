"""CLI to test a Sony Bravia TV over RS232.

Usage:
    python -m sony_tv_rs232 /dev/ttyUSB0
    python -m sony_tv_rs232 socket://192.168.1.29:5000
    python -m sony_tv_rs232 'esphome://192.168.1.29/?port_name=TTL'
    python -m sony_tv_rs232 /dev/ttyUSB0 --power on
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import (
    CommandTimeoutError,
    ConnectionLostError,
    InputSource,
    PictureMode,
    PowerState,
    ProtocolError,
    SonyTV,
    TVState,
    WideMode,
)


def _format_enum(val: object | None) -> str:
    if val is None:
        return "?"
    if hasattr(val, "name"):
        return val.name
    return str(val)


def _format_bool(val: bool | None, on: str = "ON", off: str = "OFF") -> str:
    if val is None:
        return "?"
    return on if val else off


def _format_percent(val: int | None) -> str:
    if val is None:
        return "?"
    return f"{val}%"


def _print_state(state: TVState) -> None:
    print()
    print("=== Sony Bravia Status ===")
    print()
    print(f"  Power:           {_format_enum(state.power)}")
    print(f"  Input source:    {_format_enum(state.input_source)}")
    print(f"  Wide mode:       {_format_enum(state.wide_mode)}")
    print(f"  4:3 mode:        {_format_enum(state.mode_4_3)}")
    print(f"  Audio mute:      {_format_bool(state.audio_mute)}")
    print(f"  Volume:          {_format_percent(state.volume)}")
    print(f"  Speaker:         {_format_bool(state.speaker_off, on='OFF', off='ON')}")
    print()
    print("  Picture:")
    print(f"    Picture mode:  {_format_enum(state.picture_mode)}")
    print(f"    Cine motion:   {_format_enum(state.cine_motion)}")
    print(f"    Adv. iris:     {_format_enum(state.advanced_iris)}")
    print(f"    Picture (con): {_format_percent(state.picture_level)}")
    print(f"    Brightness:    {_format_percent(state.brightness)}")
    print(f"    Color:         {_format_percent(state.color)}")
    print(f"    Sharpness:     {_format_percent(state.sharpness)}")
    print()
    print("  Audio:")
    print(f"    Sound mode:    {_format_enum(state.sound_mode)}")
    print(f"    Treble:        {_format_percent(state.treble)}")
    print(f"    Bass:          {_format_percent(state.bass)}")
    print()
    print("  System:")
    print(f"    Off timer:     {_format_enum(state.off_timer)}")
    print(f"    Language:      {_format_enum(state.language)}")
    print()


async def _diagnose(port: str) -> int:
    """Open the port, send a power query, and dump everything received.

    Useful when the TV's RS-232 wiring or the command interval is suspect.
    """
    import serialx

    from . import BAUD_RATE, encode_query, Function

    print(f"[diag] Opening {port} at {BAUD_RATE} baud (raw)...")
    reader, writer = await serialx.open_serial_connection(port, baudrate=BAUD_RATE)
    received: list[bytes] = []

    async def reader_task() -> None:
        while True:
            chunk = await reader.read(256)
            if not chunk:
                return
            print(f"[diag] RX: {chunk.hex(' ')}")
            received.append(chunk)

    task = asyncio.create_task(reader_task())
    try:
        packet = encode_query(Function.POWER.value)
        print(f"[diag] TX: {packet.hex(' ')}  (power query)")
        writer.write(packet)
        await writer.drain()
        print("[diag] Listening 5 seconds...")
        await asyncio.sleep(5)
    finally:
        task.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    if received:
        print(f"[diag] Got {len(received)} chunks; total {sum(len(c) for c in received)} bytes")
        return 0
    print(
        "[diag] Nothing received. Most likely the TV is fully off, the RX line is "
        "not connected (some TVs invert TX/RX vs. PC; try a null-modem cable), "
        "or this model does not honour query packets. Try sending a Set "
        "command (e.g. --power on/off) to confirm the TV is at least listening."
    )
    return 1


async def _run(args: argparse.Namespace) -> int:
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.diagnose:
        return await _diagnose(args.port)

    tv = SonyTV(args.port)
    print(f"Connecting to {args.port}...")
    try:
        await tv.connect()
    except (ConnectionLostError, OSError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    try:
        if args.power == "on":
            print("Sending power on...")
            await tv.power_on()
            return 0
        if args.power == "off":
            print("Sending power off...")
            await tv.power_off()
            return 0
        if args.standby_listen == "on":
            print("Enabling standby listening (allows future power-on)...")
            await tv.enable_standby_listening()
            return 0
        if args.standby_listen == "off":
            print("Disabling standby listening...")
            await tv.disable_standby_listening()
            return 0
        if args.input is not None:
            try:
                source = InputSource[args.input.upper()]
            except KeyError:
                print(
                    f"Unknown input source: {args.input!r}. "
                    f"Choices: {', '.join(s.name for s in InputSource)}",
                    file=sys.stderr,
                )
                return 1
            print(f"Selecting input {source.name}...")
            await tv.select_input_source(source)
            return 0
        if args.volume is not None:
            print(f"Setting volume to {args.volume}%...")
            await tv.set_volume(args.volume)
            return 0
        if args.mute == "on":
            print("Muting...")
            await tv.mute_on()
            return 0
        if args.mute == "off":
            print("Unmuting...")
            await tv.mute_off()
            return 0
        if args.wide is not None:
            try:
                mode = WideMode[args.wide.upper()]
            except KeyError:
                print(
                    f"Unknown wide mode: {args.wide!r}. "
                    f"Choices: {', '.join(m.name for m in WideMode)}",
                    file=sys.stderr,
                )
                return 1
            print(f"Setting wide mode {mode.name}...")
            await tv.set_wide_mode(mode)
            return 0
        if args.picture_mode is not None:
            try:
                mode = PictureMode[args.picture_mode.upper()]
            except KeyError:
                print(
                    f"Unknown picture mode: {args.picture_mode!r}. "
                    f"Choices: {', '.join(m.name for m in PictureMode)}",
                    file=sys.stderr,
                )
                return 1
            print(f"Setting picture mode {mode.name}...")
            await tv.set_picture_mode(mode)
            return 0
        if args.display:
            print("Toggling on-screen info display...")
            await tv.toggle_display()
            return 0

        # Default: query everything and print
        try:
            power = await tv.query_power()
        except (CommandTimeoutError, ProtocolError) as err:
            print(
                f"Power query failed ({err}). This Sony model may not honour "
                "queries — only Set commands. Try --power on/off / --input HDMI1.",
                file=sys.stderr,
            )
            return 1

        if power is PowerState.OFF:
            print()
            print("TV is OFF — most queries will be skipped.")
            _print_state(tv.state)
            return 0

        print("Querying TV state...")
        await tv.query_state()
        _print_state(tv.state)
        return 0
    finally:
        await tv.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test a Sony Bravia TV over RS232",
    )
    parser.add_argument(
        "port",
        help=(
            "Serial port URL. Examples: /dev/ttyUSB0, "
            "socket://192.168.1.29:5000, "
            "esphome://192.168.1.29/?port_name=TTL"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Diagnostic mode: send a single raw power query and dump everything "
        "received. Useful when wiring or model compatibility is unknown.",
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--power", choices=["on", "off"], help="Set power state")
    action.add_argument(
        "--standby-listen",
        choices=["on", "off"],
        help="Enable/disable the TV's listening for Power ON while in standby",
    )
    action.add_argument(
        "--input",
        help="Select input source (e.g. HDMI1, HDMI2, COMPONENT1, VIDEO1, PC, TV)",
    )
    action.add_argument("--volume", type=int, help="Set volume 0..100")
    action.add_argument("--mute", choices=["on", "off"], help="Set mute")
    action.add_argument(
        "--wide",
        help="Set wide mode (e.g. NORMAL, FULL, ZOOM, WIDE_ZOOM, PC_NORMAL)",
    )
    action.add_argument(
        "--picture-mode",
        help="Set picture mode (VIVID, STANDARD, CINEMA, CUSTOM)",
    )
    action.add_argument(
        "--display",
        action="store_true",
        help="Toggle the on-screen info display (same as the remote's INFO key)",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
