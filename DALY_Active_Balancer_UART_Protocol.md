# DALY Smart Active Balancer (mini, red PCB) — UART Protocol

> **Status:** compiled reference, **partly validated on hardware** (settings registers, see §4.9).
> DALY has not published a protocol document for the active balance module. This file
> brings together DALY's official BMS UART spec, the official balancer manual and
> community reverse-engineering of the DALY "Modbus" (0xD2) protocol. Every table marks
> how much its content can be trusted:
>
> | Tag | Meaning |
> |---|---|
> | **[OFFICIAL]** | Taken from a DALY document |
> | **[COMMUNITY]** | Reverse-engineered on DALY BMS units and used in working open-source code |
> | **[INFERRED]** | Expected to apply to the balancer, but not yet confirmed on one |
> | **[VERIFIED]** | Read back from a real balancer over D2 (see §4.9) |
>
> Before writing to any register, check it against a capture from your own unit (see §8).

---

## 1. Device overview

| Item | Value | Source |
|---|---|---|
| Product | DALY Smart Active Balance Module, "UART/RS485" variant (the single-UART "mini" board) | [OFFICIAL] |
| Cell count | 3S – 24S (the hardware is built for one specific string count) | [OFFICIAL] |
| Balance current | ≤ 1 A (capacitive/inductive charge transfer) | [OFFICIAL] |
| Chemistries | Li-ion (NMC), LiFePO4; LTO on the dual-UART model | [OFFICIAL] |
| Ports | `UART` (the Bluetooth dongle plugs in here), `RS485`, cell-sense harness | [OFFICIAL] |
| Default balance start voltage | 3.3 V (UART/RS485 model) | [OFFICIAL] |
| Default balance start ΔV | 20 mV (UART/RS485 model) | [OFFICIAL] |
| Default sleep time | 3600 s. Set it to **65535** to disable sleep | [OFFICIAL] |
| Disabled below | Pack < 2 V (UART/RS485 model). The manual also says not to use the module below 10 V total | [OFFICIAL] |
| LED | Green on = balancing, off = idle | [OFFICIAL] |

There is also a **dual-UART** variant (UART1 + UART2). UART1 is meant to link to a
DALY K/M/S Smart BMS **UART-1** port. In that setup the BMS reports the balancer's data
and you query the BMS, not the balancer.

---

## 2. Physical layer

| Parameter | Value | Source |
|---|---|---|
| Interface | TTL-level UART (also on RS485 through the RS485 port) | [OFFICIAL] |
| Baud rate | **9600 bps** | [OFFICIAL] (DALY UART/485 spec V1.2) |
| Format | 8 data bits, no parity, 1 stop bit (8N1) | [OFFICIAL] |
| TXD | Open-collector: "0" < 0.5 V, "1" = open (≤ 100 V withstand). **Needs a pull-up** on the host side | [OFFICIAL] |
| RXD | "0" < 0.5 V, "1" > 3 V | [OFFICIAL] |
| Bus role | Master/slave. The device only talks when it is addressed | [OFFICIAL] |

> **Logic level:** community reports indicate 3.3 V works, though some DALY units expect 5 V
> on RX. Because TX is open-collector, add a pull-up (for example 4.7 kΩ to 3.3 V) to your MCU's RX line.

> **Bluetooth dongle:** the DALY BT dongle is a transparent UART↔BLE bridge (GATT service
> `0xFFF0`, notify `0xFFF1`, write `0xFFF2`). Whatever the app sends over BLE therefore
> reaches the balancer's UART unchanged. **[COMMUNITY]**

---

## 3. Which protocol does the balancer speak?

DALY uses two unrelated framings:

| Protocol | Start byte | Integrity check | Used by |
|---|---|---|---|
| **"Classic" A5** | `0xA5` | 8-bit sum | Older DALY Smart BMS (J/T/A/U/W series), DALY V1.2 spec |
| **"Modbus" D2** | `0xD2` (BLE/UART) or `0x80 + addr` (RS485) | CRC-16/Modbus | Newer DALY H/K/M/S BMS, "100 Balance", active balancer |

Evidence for the balancer:

* A DIY Solar Forum user found that **A5 requests got no answer** from the active balancer, and that
  its replies started with **`0xD2`**. **[COMMUNITY]**
* Another project reads balancer cell voltages over RS485 with the `dalybms` Python package,
  which uses A5 framing. Some firmware revisions may therefore answer both. **[COMMUNITY, unconfirmed]**

**Recommendation:** treat **D2 (§4)** as the primary protocol and keep **A5 (§5)** as a fallback probe.

---

## 4. D2 "Modbus-like" protocol (primary)

### 4.1 Request frame (always 8 bytes)

| Byte | Field | Value |
|---|---|---|
| 0 | Device address | `0xD2` over UART/BLE. Over RS485 on H/K/M/S BMS: `0x80 + board_no` (e.g. `0x81`) |
| 1 | Function | `0x03` = read holding registers, `0x06` = write single register |
| 2–3 | Register address | big-endian |
| 4–5 | Read: register count / Write: value | big-endian |
| 6–7 | CRC-16/Modbus | **low byte first** |

### 4.2 Read response (`0x03`)

| Byte | Field |
|---|---|
| 0 | Address: `0xD2` (on RS485: `0x50 + board_no`, e.g. `0x51`) |
| 1 | `0x03` |
| 2 | Byte count N (= 2 × register count) |
| 3 … 3+N−1 | Register data, big-endian 16-bit words |
| last 2 | CRC-16/Modbus, low byte first |

### 4.3 Write response (`0x06`)

The device echoes the 8-byte request: address, `0x06`, register, value, CRC. **[COMMUNITY]**

### 4.4 CRC

Standard Modbus CRC-16: init `0xFFFF`, reflected polynomial `0xA001`, no final XOR. It is
computed over every byte before the CRC and sent **low byte first**.

```c
uint16_t crc16_modbus(const uint8_t *p, size_t n) {
    uint16_t crc = 0xFFFF;
    while (n--) {
        crc ^= *p++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
    }
    return crc;
}
```

### 4.5 Real-time block (`0x0000`, read 62 or 80 registers) — [COMMUNITY] / [INFERRED for balancer]

Values are unsigned 16-bit, big-endian. Fields a balancer cannot measure (current, SOC, MOSFETs)
are expected to read as 0 or a fixed value.

| Reg | Field | Scaling |
|---|---|---|
| `0x0000`–`0x001F` | Cell voltage 1 … 32 | mV |
| `0x0020`–`0x0027` | Temperature 1 … 8 | raw − 40 → °C. **[TESTED]** on a 24-cell balancer: only Temperature 1 (`0x0020`) is used, its PCB sensor (`0x0032` = 1, max/min = `0x0020`); 2–6 read 0 |
| `0x0028` | Total voltage | × 0.1 V |
| `0x0029` | Current | (raw − 30000) × 0.1 A |
| `0x002A` | SOC | × 0.1 % |
| `0x002B` | Max cell voltage | mV |
| `0x002C` | Min cell voltage | mV |
| `0x002D` | Max temperature | raw − 40 → °C |
| `0x002E` | Min temperature | raw − 40 → °C |
| `0x002F` | Charge/discharge state | 0 idle, 1 charge, 2 discharge |
| `0x0030` | Remaining capacity | × 0.1 Ah |
| `0x0031` | **Cell count** | — |
| `0x0032` | Temperature sensor count | — |
| `0x0033` | Cycles | — |
| `0x0034` | **Balancer state** | 0 off, 1 passive, 2 active |
| `0x0035` | Charge MOSFET | bool |
| `0x0036` | Discharge MOSFET | bool |
| `0x0037` | Average cell voltage | mV |
| `0x0038` | **Cell ΔV (max − min)** | mV |
| `0x0039` | Power | W |
| `0x003A`–`0x003D` | Alarm flags (64-bit bitmask) | see §6 |
| `0x003E` | **Balancing bitmask cells 1–16** (only in the 80-register read) | bit n = cell n+1 |
| `0x003F` | **Balancing bitmask cells 17–32** | bit n = cell n+17 |
| `0x0040` | **Balance current** | (raw − 30000) × 0.001 A, signed via offset |
| `0x0041` | unknown | — |
| `0x0042` | MOSFET / power-stage temperature | raw − 40 → °C on a BMS. Reads 65535 on a balancer |
| `0x0043` | Board temperature | raw − 40 → °C on a BMS. **[TESTED]** reads 0 on a balancer: use `0x0020` |

### 4.6 Settings block (`0x0080`, read 41 registers) — [COMMUNITY] / [INFERRED for balancer]

| Reg | Field | Notes |
|---|---|---|
| `0x0080` | Rated capacity | |
| `0x0081` | Cell reference voltage | |
| `0x0082` | Acquisition-board count | |
| `0x0083`–`0x0085` | Cells per board 1–3 | **Set to the real string count** |
| `0x0086`–`0x0088` | Temp sensors per board 1–3 | |
| `0x0089` | Battery type | 0 LFP, 1 NMC, 2 LTO. **[VERIFIED]**: reads 1 on an NMC pack (LFP/LTO codes still unconfirmed) |
| `0x008A` | **Sleep wait time** | s. `65535` = never sleep (matches balancer manual). **[VERIFIED]**: reads 3600, write echoed |
| `0x008B`–`0x009E` | Warning/alarm thresholds (cell/pack V, I, T) | BMS only, not used by balancer |
| `0x00A3` | **Balance start (activation) voltage** | mV (default 3300 on balancer). **[VERIFIED]**: reads 3300 |
| `0x00A4` | **Balance start voltage difference** | mV (default 20 on balancer). **[VERIFIED]**: reads 20 |
| `0x00A5` | Charge MOS switch | BMS only |
| `0x00A6` | Discharge MOS switch | BMS only |
| `0x00A7` | SOC setting | |
| `0x00A8` | MOS over-temperature alarm | Answers on the balancer (raw 87, scaling unknown) but is not shown in the DALY PC software; treat as unused |

### 4.7 Other registers — [COMMUNITY]

| Reg | Count | Function |
|---|---|---|
| `0x00A9` | 32 | Firmware / hardware version (ASCII) |
| `0x00C9` | 3 | Password (D2 only) |
| `0x00CF` | 1 | **Balancer enable switch**: R/W, 0 = off, 1 = on |

### 4.8 Ready-made frames (CRC computed)

| Purpose | Frame (hex) |
|---|---|
| Read real-time block, 62 regs | `D2 03 00 00 00 3E D7 B9` |
| Read real-time block, 80 regs | `D2 03 00 00 00 50 56 55` |
| Read settings, 41 regs | `D2 03 00 80 00 29 96 5F` |
| Read version, 32 regs | `D2 03 00 A9 00 20 87 91` |
| Read balancer switch | `D2 03 00 CF 00 01 A7 96` |
| Balancer **ON** | `D2 06 00 CF 00 01 6B 96` |
| Balancer **OFF** | `D2 06 00 CF 00 00 AA 56` |
| Disable sleep (65535) | `D2 06 00 8A FF FF BA 33` |

Example response header for the 62-register read: `D2 03 7C …124 data bytes… CRC_L CRC_H` (129 bytes total).

### 4.9 Hardware validation log

Readings from the connected balancer (NMC pack) over the UART port at 9600 8N1, D2 address `0xD2`,
one register per read (function `0x03`, count 1), 100 ms between requests. Every request was answered
with a valid CRC on the first try.

| Reg | Raw | Meaning |
|---|---|---|
| `0x0089` | 1 | NMC, matches the pack chemistry |
| `0x008A` | 3600 | 3600 s sleep time (factory default) |
| `0x00A3` | 3300 | 3.3 V balance start voltage (factory default) |
| `0x00A4` | 20 | 20 mV balance start difference (factory default) |
| `0x00A8` | 87 | Not shown in the DALY PC software; meaning unconfirmed |

Write (function `0x06`) tested on `0x008A`: the device echoes the 8-byte request, as described in §4.3.
Writes to the other registers have not been tested yet.

---

## 5. Classic A5 protocol (fallback / probe)

DALY UART/485 Communications Protocol V1.2 **[OFFICIAL]**. Every frame is exactly **13 bytes**.

| Byte | Field | Host → device | Device → host |
|---|---|---|---|
| 0 | Start | `0xA5` | `0xA5` |
| 1 | Address | `0x40` (PC), `0x80` (BT app), `0x20` (GPRS) | `0x01` (device) |
| 2 | Data ID | see table | same |
| 3 | Length | `0x08` | `0x08` |
| 4–11 | Data | all `0x00` | payload |
| 12 | Checksum | sum of bytes 0–11, low 8 bits | same |

Multi-byte values are big-endian. One request returns one data item. Multi-frame items such as
`0x95` send several 13-byte responses in sequence.

| ID | Content (response) |
|---|---|
| `0x90` | B0–1 total V (0.1 V), B2–3 gathered V (0.1 V), B4–5 current (30000 offset, 0.1 A), B6–7 SOC (0.1 %) |
| `0x91` | B0–1 max cell mV, B2 max cell #, B3–4 min cell mV, B5 min cell # |
| `0x92` | B0 max T (+40), B1 sensor #, B2 min T (+40), B3 sensor # |
| `0x93` | B0 state (0/1/2), B1 chg MOS, B2 dsg MOS, B3 life, B4–7 remaining mAh |
| `0x94` | B0 cell count, B1 temp count, B2 charger, B3 load, B4 DI/DO bits |
| `0x95` | Cell voltages: B0 frame # (from 1 in practice; spec says 0), B1–6 = 3 cells × mV, B7 reserved |
| `0x96` | Temperatures: B0 frame #, B1–7 = 7 sensors (+40 °C) |
| `0x97` | **Balancing state bitmap**: bit 0 = cell 1 … bit 47 = cell 48 |
| `0x98` | Fault bitmap, 7 bytes + fault code (see V1.2 spec) |

Probe frames:

```
A5 40 90 08 00 00 00 00 00 00 00 00 7D   ; pack summary
A5 40 91 08 00 00 00 00 00 00 00 00 7E   ; min/max cell
A5 40 95 08 00 00 00 00 00 00 00 00 82   ; cell voltages (multi-frame reply)
A5 40 97 08 00 00 00 00 00 00 00 00 84   ; balancing bitmap
```

The V1.2 spec does not document any write commands. Community-found A5 writes (e.g. `0x1F`/`0x5F`
for balance thresholds) were observed on BMS units only.

---

## 6. Alarm bitmask (D2 regs `0x003A`–`0x003D`)

This maps to the same bit groups as A5 ID `0x98`: cell/pack over/under-voltage levels 1–2,
temperature levels, ΔV/ΔT, AFE/EEPROM/RTC/communication faults. On a balancer, expect only the
cell-voltage, ΔV, temperature-sensor and AFE bits to be meaningful. **[INFERRED]**

---

## 7. Timing and behaviour notes

* Leave **≥ 100 ms** between requests. At 9600 bps a 129-byte reply takes about 135 ms, so wait for it to finish before sending again.
* The module **sleeps** after the sleep timeout (default 3600 s) and then stops answering UART.
  Disable sleep (`0x008A = 65535`) or wake it by power-cycling or pressing the BT dongle button. Community reports describe the same behaviour on DALY BMS.
* Balancing only runs when **both** conditions hold: a cell is above the start voltage **and** cell ΔV is above the start difference.
* After you change the string count, the harness must still match it. DALY warns that wrong counts and mixed cables cause mis-measurement.

---

## 8. Validation procedure (do this before trusting §4.5–4.7)

1. Connect a USB-TTL adapter (9600 8N1, pull-up on RX) to the balancer `UART` port. Use GND, TX and RX only; do not power the board from the adapter.
2. **Passive sniff:** put a second adapter on the TX/RX lines of the DALY BT dongle while the
   Smart BMS app is open. Record which frames the app sends and which register addresses it uses.
3. **Probe D2:** send `D2 03 00 00 00 3E D7 B9` and check the reply starts with `D2 03 7C` and the CRC is valid.
   Compare registers `0x0000…` with a multimeter on each cell.
4. **Probe A5:** send `A5 40 90 08 00…00 7D` and note whether any reply comes back.
5. Read the settings block, change **one** value in the app (e.g. ΔV 20 → 30 mV) and re-read
   to confirm the register address before you write it from your own code.

Minimal Python probe:

```python
import serial, struct

def crc16(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def d2_read(port, reg, count):
    f = bytes([0xD2, 0x03]) + struct.pack(">HH", reg, count)
    f += struct.pack("<H", crc16(f))
    port.reset_input_buffer()
    port.write(f)
    hdr = port.read(3)
    if len(hdr) < 3 or hdr[0] != 0xD2 or hdr[1] != 0x03:
        raise IOError(f"bad header {hdr.hex()}")
    body = port.read(hdr[2] + 2)
    frame = hdr + body
    if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
        raise IOError("CRC mismatch")
    return struct.unpack(f">{hdr[2]//2}H", body[:-2])

with serial.Serial("/dev/ttyUSB0", 9600, timeout=1) as s:
    r = d2_read(s, 0x0000, 62)
    n = r[0x31]
    print("cells:", [v / 1000 for v in r[:n]])
    print("dV mV:", r[0x38], "balancer state:", r[0x34])
```

---

## 9. Sources

* DALY, *UART/485 Communications Protocol V1.2*: [robu.in mirror](http://robu.in/wp-content/uploads/2021/10/Daly-UART_485-Communications-Protocol-V1.21-1.pdf), also in [acpiccolo/Daly-BMS](https://github.com/acpiccolo/Daly-BMS)
* DALY, *Smart Active Balance Module (1A) Instruction*: [dalyelec.com PDF](https://www.dalyelec.com/images/ZDJHR/%E8%8B%B1%E6%96%87/Smart%20Active%20Balance%20Module%C2%A0(1A).pdf)
* syssi, [esphome-daly-bms](https://github.com/syssi/esphome-daly-bms): D2 frame format, CRC and register map (`docs/protocol-register-map.md`)
* patagonaa, [esphome-daly-hkms-bms](https://github.com/patagonaa/esphome-daly-hkms-bms): RS485 address offsets (`0x80`/`0x50`)
* maland16, [daly-bms-uart](https://github.com/maland16/daly-bms-uart): A5 implementation, logic-level notes
* florianuhlemann, [daly-bms-reader](https://github.com/florianuhlemann/daly-bms-reader): reads a Smart Active Balancer over RS485 with `dalybms`
* fl4p, [batmon-ha](https://github.com/fl4p/batmon-ha): BLE GATT layout, A5 vs D2 detection
* DIY Solar Forum, [Intelligent balance system by Daly](https://diysolarforum.com/threads/intelligent-balance-system-by-daily.98570/): balancer ignores A5 and replies with `0xD2`
