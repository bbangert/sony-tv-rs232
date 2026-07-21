# Sony Bravia RS-232C protocol

Source of truth for this library's command set:

- Pro Bravia Knowledge Center — Serial (RS-232C) control:
  https://pro-bravia.sony.net/remote-display-control/serial-control/
- Function codes and the community query format come from the Sony
  KDL/KDS-XBR5 RS-232C protocol manual.

## Packet formats

```
Set / Control (host -> TV):  [0x8C][0x00][Function][Length][Data...][Checksum]
Query / Get   (host -> TV):  [0x83][0x00][Function][0xFF][0xFF][Checksum]   (6 bytes)
Answer (TV -> host):
    Set ack:      [0x70][Status][Checksum]                    (3 bytes)
    Query reply:  [0x70][Status][Size][Data...][Checksum]
```

`Length`/`Size` = data bytes + 1 (count of bytes after the field, incl. the
trailing checksum). `Checksum` = sum of all preceding bytes mod 256.

Example — power query `83 00 00 FF FF 81`, reply `70 00 02 01 73` (Completed,
data `0x01` = power on).

The full encoder/decoder lives in `src/sony_tv_rs232/protocol.py`; the
checksum-discriminated framing (short ack vs long reply) is in `framing.py`.
