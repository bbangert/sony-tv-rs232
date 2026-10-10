"""Protocol helper vectors: checksum, Set/Query encoding, answer parsing."""

from __future__ import annotations

import pytest

from sony_tv_rs232 import (
    Answer,
    AnswerCode,
    SonyCommandError,
    SonyProtocolError,
    byte_to_percent,
    checksum,
    encode_control,
    encode_query,
    parse_answer,
    percent_to_byte,
)


def test_checksum() -> None:
    assert checksum(b"\x8c\x00\x00\x02\x01") == 0x8F
    assert checksum(b"\x83\x00\x00\xff\xff") == 0x81
    assert checksum(b"\x70\x00") == 0x70


def test_encode_control_power_on() -> None:
    # 8C 00 00 02 01 8F (per Sony XBR5 spec)
    assert encode_control(0x00, b"\x01") == bytes.fromhex("8c 00 00 02 01 8f")


def test_encode_control_power_off() -> None:
    # 8C 00 00 02 00 8E
    assert encode_control(0x00, b"\x00") == bytes.fromhex("8c 00 00 02 00 8e")


def test_encode_control_volume_30() -> None:
    # 8C 00 05 03 01 1E B3
    assert encode_control(0x05, b"\x01\x1e") == bytes.fromhex("8c 00 05 03 01 1e b3")


def test_encode_control_invalid() -> None:
    with pytest.raises(ValueError):
        encode_control(0x00, b"")
    with pytest.raises(ValueError):
        encode_control(0x100, b"\x01")


def test_encode_query_power() -> None:
    # 83 00 00 FF FF 81 (per Pro Bravia spec)
    assert encode_query(0x00) == bytes.fromhex("83 00 00 ff ff 81")


def test_encode_query_volume() -> None:
    # 83 00 05 FF FF 86
    assert encode_query(0x05) == bytes.fromhex("83 00 05 ff ff 86")


def test_parse_answer_set_ack() -> None:
    # 70 00 70 -> Completed
    answer = parse_answer(b"\x70\x00\x70")
    assert answer == Answer(code=AnswerCode.COMPLETED, data=b"")
    assert answer.ok


def test_parse_answer_query_reply() -> None:
    # 70 00 02 01 73 -> power query reply: power on (size=2: 1 data + 1 cs)
    answer = parse_answer(b"\x70\x00\x02\x01\x73")
    assert answer.code is AnswerCode.COMPLETED
    assert answer.data == b"\x01"


def test_parse_answer_error() -> None:
    # 70 03 73 -> Cancelled
    body = bytes([0x70, AnswerCode.CANCELED.value])
    packet = body + bytes([checksum(body)])
    answer = parse_answer(packet)
    assert not answer.ok
    with pytest.raises(SonyCommandError) as excinfo:
        answer.raise_for_status(0x05)
    assert excinfo.value.code is AnswerCode.CANCELED
    assert excinfo.value.function == 0x05


def test_parse_answer_bad_header() -> None:
    with pytest.raises(SonyProtocolError):
        parse_answer(b"\x80\x00\x80")


def test_parse_answer_bad_checksum() -> None:
    with pytest.raises(SonyProtocolError):
        parse_answer(b"\x70\x00\x99")  # wrong checksum


def test_parse_answer_too_short() -> None:
    with pytest.raises(SonyProtocolError):
        parse_answer(b"\x70\x00")


def test_parse_answer_unknown_code() -> None:
    body = bytes([0x70, 0x99])
    packet = body + bytes([checksum(body)])
    with pytest.raises(SonyProtocolError):
        parse_answer(packet)


def test_parse_answer_size_mismatch() -> None:
    # Size says 2 (=> 5 bytes total) but 6 bytes arrived.
    body = bytes([0x70, 0x00, 0x02, 0x01, 0x02])
    packet = body + bytes([checksum(body)])
    with pytest.raises(SonyProtocolError):
        parse_answer(packet)


def test_percent_roundtrip() -> None:
    for p in (0, 25, 50, 75, 100):
        assert byte_to_percent(percent_to_byte(p)) == p


def test_percent_examples() -> None:
    assert percent_to_byte(0) == 0
    assert percent_to_byte(50) == 50
    assert percent_to_byte(100) == 100


def test_percent_out_of_range() -> None:
    with pytest.raises(ValueError):
        percent_to_byte(101)
    with pytest.raises(ValueError):
        percent_to_byte(-1)
    with pytest.raises(ValueError):
        byte_to_percent(255)
