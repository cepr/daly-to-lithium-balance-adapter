# Battery CAN monitor

Linux GUI that decodes the Energica Ego battery CAN messages (Lithium Balance BMCU) described
in [`../Battery_canbus_protocol.html`](../Battery_canbus_protocol.html).

## Run
    cd "Lithium_Balance_sBMS"
    sudo ip link set can0 up type can bitrate 500000     # adapter-dependent; check the bike's bitrate
    python3 -m can_monitor.gui --iface can0

Requirements: Linux, Python 3.10+, Tkinter. No other dependencies: frames are read from SocketCAN
with Python's own `socket` module, so any adapter with a SocketCAN driver works (PEAK, Kvaser,
candleLight/gs_usb, MCP2515…). An slcan adapter needs `slcand` first.

Options: `--iface can0` connects at start, `--replay file.log [--loop]` plays back a log.
Without options, pick the interface in the toolbar and click **Connect**.

## Window
- **Tree**: every message from the protocol document, grouped as sent by / received by the BMCU.
  Message rows show the payload bytes, frame count, measured period vs. expected interval, and
  time since the last frame. Signal rows show the decoded value and the raw value in hex and decimal.
  Grey = never received, or a `Constant` field. Red = stale (no frame for 3 × the interval).
  IDs not in the document appear under **Other IDs** with their raw bytes.
- **Plot**: click a signal to plot its last 10 s / 1 min / 5 min, with last, min, max and mean.
- Bit-field signals (`Error_Flags`, `LMU_Comm_Warnings`, `IO_State`) list the set bits.
- **Record…** writes every received frame to a `candump -L` file; **Replay log…** plays such a
  file (from this tool or from `candump -L can0 > file.log`) back with its original timing.

The monitor only listens. It never transmits, so it can't send a SoC set (0x119) or open the
contactors (0x605).

## Scaling
The protocol document gives no scaling or units, so all values are **raw integers**. When a
signal's meaning is confirmed, set `scale`, `offset`, `unit` and `signed` on its line in
[`protocol.py`](protocol.py). For now only `Pack_Current` and the pack temperatures are decoded
as signed (assumed, not confirmed).

Bit numbering: "Intel" fields are numbered from the LSB of the payload read as little-endian;
Motorola fields from the first bit of byte 0 (so start 32 / 16 bits = bytes 4–5, MSB first).
The 6 constant bits at 18–23 in 0x2FF are marked Motorola in the document, but read that way
they would overlap `Post_Processor_1`. They pad byte 2 after that 10-bit Intel field, so they
are decoded as Intel.

## Testing without the bike
    python3 -m can_monitor.sim --out demo.log --seconds 120   # made-up BMCU traffic
    python3 -m can_monitor.gui --replay demo.log --loop

or live on a virtual interface:

    sudo modprobe vcan && sudo ip link add vcan0 type vcan && sudo ip link set vcan0 up
    python3 -m can_monitor.sim --iface vcan0 &
    python3 -m can_monitor.gui --iface vcan0

## Tests
    python3 -m unittest can_monitor.test_protocol
