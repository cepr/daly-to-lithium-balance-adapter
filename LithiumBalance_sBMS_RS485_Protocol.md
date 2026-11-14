# Lithium Balance s-BMS — BMCU ↔ Slave-Board RS485 Protocol

*Reverse-engineered from `BMCU 6.18.0.hex` (PIC18F4680 application image, 0x1200–0xFFFF).*

Confidence markers used below:

- **[C]** confirmed directly from the disassembly (byte layout, constants, control flow)
- **[I]** inferred from how the master uses the data (meaning / units) — verify on real hardware
- **[V]** verified on real hardware (BMCU running against the slave simulator)

---

## 1. Physical layer

| Item | Value | |
|---|---|---|
| Medium | RS485, half-duplex, multi-drop | [C] |
| UART | PIC18 EUSART, async, 8 data bits, no parity, 1 stop (8N1) | [C] |
| Baud rate | **38 400 baud** nominal (38 462 actual, +0.16 %). SPBRG = 25, BRGH = 1, BRG16 = 0, Fosc = 16 MHz | [C] |
| Character set | Printable ASCII, except for the 2 raw CRC bytes | [C] |

Clock: 4 MHz crystal with the HSPLL oscillator setting (CONFIG1H = 0x06) gives Fosc = 16 MHz,
i.e. Fcy = 4 MHz. Timer1 interrupts every 2.5 ms, and the firmware's timeout tick is 4 of those
interrupts = 10 ms.

### 1.1 Driver control pins on the BMCU (for reference)

The single EUSART is shared by the slave bus and the diagnostic bus.

| Pin | Behaviour seen in firmware |
|---|---|
| **RC1** | Set high (with a ~50-cycle settle delay) before every transmission. Cleared when the TX-complete interrupt sees the final `CR`, **except** for frames addressed to `0x21` and for `p` frames (in those cases it stays high). → Behaves like the transmit driver enable (DE). |
| **RD3** | Set high at the start of every normal frame and whenever a valid frame from the PC (`0xFE`) arrives. Cleared before and after a `p` (status broadcast) frame. → Behaves like a selector/enable between the two RS485 segments. |

The exact wiring of RC1/RD3 can't be fully determined from firmware alone. Check it on the PCB.

---

## 2. Frame format  [C]

```
+-----+--------+--------+-------------------+-------+-------+----+
| CMD | DEST   | SRC    | PAYLOAD (ASCII)   | CRC_H | CRC_L | CR |
| 1 B | 2 hex  | 2 hex  | 0 … ~90 chars     | raw   | raw   |0x0D|
+-----+--------+--------+-------------------+-------+-------+----+
```

| Field | Size | Encoding |
|---|---|---|
| `CMD` | 1 char | ASCII command letter (see §4) |
| `DEST` | 2 chars | Destination address, upper-case ASCII hex (`"00"`–`"FF"`) |
| `SRC` | 2 chars | Source address, upper-case ASCII hex |
| `PAYLOAD` | variable | Command-specific. Numbers are upper-case ASCII hex. |
| `CRC_H`,`CRC_L` | 2 bytes | **Binary** CRC-16, high byte first (see §3) |
| `CR` | 1 byte | `0x0D`, end of frame |

Notes:

- The BMCU always sends **one lone `CR` (0x0D) just before every frame** to resync receivers.
  A receiver that is hunting for a frame start ignores every byte that isn't a valid command
  letter, so the stray `CR` is harmless. [C]
- When receiving, the BMCU only starts a frame on one of `w q r ! # a`. Other characters
  are ignored until one of those appears. [C]
- The BMCU stores at most 90 payload characters. Its RX ring buffer is 100 bytes. [C]
- The BMCU sends hex digits in upper case (`'A'`–`'F'`). [C] The diagnostic PC sends register
  addresses in **lower case** (e.g. `q01FE0e0E`), and the BMCU forwards them unchanged, so a
  slave must accept both cases. [V]

---

## 3. CRC  [C]

| Parameter | Value |
|---|---|
| Width | 16 bit |
| Polynomial | **0xAC9A** (MSB-first / non-reflected) |
| Init | 0xFFFF |
| RefIn / RefOut | No / No |
| XorOut | 0x0000 |
| Coverage | Every byte from `CMD` through the last `PAYLOAD` char (i.e. everything before the CRC) |
| Post-processing | **Each CRC byte that equals 0x00, 0x0D or 0x1B is incremented by 1** before it is sent or compared. This keeps NUL, CR and ESC out of the frame. |

The table is stored in flash at 0x1B33 (256 × 16-bit, little-endian).

Reference implementation:

```python
def _table(poly=0xAC9A):
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        t.append(c)
    return t
TABLE = _table()

def sbms_crc(data: bytes) -> bytes:
    c = 0xFFFF
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ TABLE[(c >> 8) ^ b]
    hi, lo = c >> 8, c & 0xFF
    if hi in (0x00, 0x0D, 0x1B): hi += 1
    if lo in (0x00, 0x0D, 0x1B): lo += 1
    return bytes([hi, lo])

def build(cmd: str, dest: int, src: int, payload: str = "") -> bytes:
    body = f"{cmd}{dest:02X}{src:02X}{payload}".encode()
    return body + sbms_crc(body) + b"\r"
```

---

## 4. Addresses  [C]

| Address | Meaning |
|---|---|
| `0x00` | BMCU (master). All master frames use `SRC = 00`. Slaves reply to `DEST = 00`. |
| `0x01`–`0x20` | Slave boards (cell monitors), up to **32**, assigned at start-up (§6) |
| `0x21` | The pack voltage / current measurement unit **on the BMCU board itself**, polled separately (Appendix B). Not a slave board: a replacement for the LMUs must not answer it. |
| `0xF0` | "Unaddressed slave", the address a module answers to before it gets its number |
| `0xFE` | Diagnostic PC tool. The BMCU broadcasts `p` status frames to it. |
| `0xFF` | Broadcast (the BMCU accepts it as addressed to itself) |

How the BMCU handles received frames:

- `DEST = 00`: processed locally.
- `DEST` in `01…21`: forwarded (see the "routing" note in §5).
- Any other `DEST`: ignored until the next `CR`.

---

## 5. Command set

All frames use the layout in §2. `AAA` is a 12-bit register address (3 hex chars). `N` is
**one hex char equal to the byte count minus 1** (so `0` = 1 byte … `F` = 16 bytes). Each data
byte is 2 hex chars.

### 5.1 Master → slave

| CMD | Name | Payload | Expected reply |
|---|---|---|---|
| `q` (0x71) | **Read registers** | `AAA N` | `r` frame |
| `w` (0x77) | **Write registers** | `AAA N` + (N+1)×`HH` | `!` (ACK) |
| `i` (0x69) | **Assign address** (sent to `0xF0`) | `HH` = new address; **bit 7 set = last module in the chain** | `!` (ACK) |
| `:` (0x3A) | Bootloader packet (firmware update) | binary, see §8 | — |
| *(none)* | Lone `CR` | resync / wake-up | — |

### 5.2 Slave → master

| CMD | Name | Payload | Notes |
|---|---|---|---|
| `r` (0x72) | **Read response** | `AAA N` + (N+1)×`HH` | `AAA N` echoes the request. The master stores the (N+1) data bytes. |
| `!` (0x21) | **ACK** | 1 char = the command being acknowledged (e.g. `w`, `i`) | Ends a `w` / `i` transaction successfully |
| `#` (0x23) | **NAK** | 1 char = the rejected command + 1 char error code | The master doesn't treat it as success. It times out and retries. |

Error codes the BMCU itself uses in `#` frames:

| Code | Meaning |
|---|---|
| `'C'` (0x43) | CRC error |
| `'D'` (0x44) | Request can't be served: destination not handled / forwarding disabled |

### 5.3 Other commands on the bus (PC ↔ BMCU, not used by slaves)

| CMD | Use |
|---|---|
| `p` (0x70) | Periodic BMCU status broadcast to `0xFE` (pack values as hex fields) |
| `a` (0x61) | PC → BMCU parameter/command message (2-hex sub-type, then data) |
| `d` (0x64) | Debug/echo reply |
| (0x01) | Internal "forward" code. When the PC (or another node) sends a frame to `DEST` 01–21 via the BMCU, the BMCU re-transmits it unchanged (same `CMD`, `DEST`, original `SRC`, payload) with a freshly calculated CRC. The diagnostic software uses `SRC = FE` (§7.5). [V] |

The BMCU answers `q`/`w` addressed to itself from the PC with the same `r`/`!`/`#` format, so a
slave implementation and the BMCU's own server code are interchangeable at frame level.

---

## 6. Transaction rules  [C]

**Single master.** Slaves only ever speak in reply to a frame addressed to them.

**Timing (one tick = 10 ms):**

| Parameter | Value |
|---|---|
| Reply timeout | 14 ticks ≈ **140 ms** after the request is queued |
| Retries | `q`/`w` to slaves: **3 attempts**. `i` during enumeration: **1 attempt**. |
| Pacing | The master doesn't start a new frame until the previous one has fully left the UART and the previous reply timeout has expired (or the reply arrived). |

**What counts as a successful reply:**

- `q` succeeds when any `r` frame with `DEST = 00` and a valid CRC is received.
  The master doesn't check `SRC` or `AAA`, but slaves should echo them correctly.
- `w` and `i` succeed only on `!`. A `#` or a timeout means "retry".

### 6.1 Start-up address assignment (enumeration)

The BMCU runs this sequence (function at 0xE1B6). `N` is the configured number of slave
boards (≤ 32).

```
for addr = 1 … N:
    send lone CR
    short delay (~1500 loop iterations)
    send  i F0 00 <addr | (0x80 if addr == N)>      (1 attempt)
    wait for '!' ACK  (≈140 ms)
    if no ACK: stop the round
```

- If fewer than `N` modules answered, the whole round is repeated up to 3 more times, with a
  pause of about 1 s between rounds. The number of modules found is then compared with the
  configured count. A mismatch raises a BMCU error.
- **[I]** The `0xF0` + "last-module bit" scheme implies a daisy-chain enable: only the next
  un-addressed module listens to `0xF0`. After it takes its address it enables the next module
  in the chain. Bit 7 tells the last module that it is the end of the chain.

Example (address 1 of 4; address 4 = last, so it is sent as `84`):

```
→ 0D
→ "iF00001"  C3 C6  0D        69 46 30 30 30 30 31 C3 C6 0D
← "!0001i"   98 DC  0D        21 30 30 30 31 69 98 DC 0D   (expected ACK)
…
→ "iF00084"  42 A8  0D        (address 4, last module)
```

### 6.2 Cyclic polling

Each pass of the BMCU main loop polls **one slave** (round-robin 1…N), plus the on-board pack
voltage / current unit `0x21` (Appendix B):

| Order | Request | Reply data | On failure |
|---|---|---|---|
| 1 | `q <slave> 00 7D0 F` (16 bytes) | 8 cell voltages | Slave's bit is set in a 32-bit comm-fault mask; error 0x12/sub 1 logged |
| 2 | `q <slave> 00 7E5 4` (5 bytes) | 4 temperatures + 1 spare byte | error 0x12/sub 2 logged |

---

## 7. Slave register map (as used by the BMCU)

All registers are byte-wide. Multi-byte values span consecutive addresses. The configuration
registers that only the diagnostic software reads are in §7.5.

| Addr | Len | Access | Content | |
|---|---|---|---|---|
| `0x002` | 1 | W | **Bootloader entry.** Writing `0xAC` makes the module jump to its bootloader (used before a firmware update) | [C] |
| `0x7D0`–`0x7DF` | 16 | R | **Cell voltages 1–8**, 8 × uint16 **big-endian** (byte at the lower address = MSB) | [C] layout / [V] unit = mV |
| `0x7E0`–`0x7E3` | 4 | R/W | **The BMCU's charge counter**, slave `01` only: `7E0` remaining charge, `7E2` coulomb-counter remainder, both int16 **little-endian**. **Read** once at start-up, right after a successful enumeration, to restore the SoC. **Written** about every 4 s. The slave must keep what was written and return it, also after a power cycle. See 7.6. | [C] |
| `0x7E5`–`0x7E8` | 4 | R | **Temperatures T1–T4**, int8, °C: `7E5` = T1 Pcb 1, `7E6` = T2 Pcb 2, `7E7` = T3 Bat 1, `7E8` = T4 Bat 2 (see 7.2) | [C] layout / [V] unit |
| `0x7E9` | 1 | R | Read as part of the 5-byte `7E5` request but not used | [C] |

### 7.1 Cell-voltage value coding  [C]

| Raw value | BMCU interpretation |
|---|---|
| `0x2000` | Cell input not fitted / unused. Skipped, not included in min/max/sum. |
| `0x4000` | Cell measurement fault. A fault is flagged and an event logged with the cell number. |
| bit 15 set (≥ 0x8000) | Flagged/invalid reading. A fault is raised and the lower 15 bits are used as the value. |
| anything else | Cell voltage. Used for pack min/max/sum. Global cell number = (slave−1)×8 + index + 1. |

### 7.2 Temperature sensors

Each slave (LMU) reports four temperatures:

| Register | Sensor | Name | Location |
|---|---|---|---|
| `0x7E5` | T1 | **Pcb 1** | LMU board sensor |
| `0x7E6` | T2 | **Pcb 2** | LMU board sensor |
| `0x7E7` | T3 | **Bat 1** | NTC on LMU port **L-J2** (battery / cell temperature) |
| `0x7E8` | T4 | **Bat 2** | Optional NTC on LMU port **L-J6**. Reads `0x7A` (not fitted) when absent. |

### 7.3 Temperature value coding  [C]

| Raw byte | BMCU interpretation | Diagnostic software shows [V] |
|---|---|---|
| `0x7A` (+122) | Sensor not fitted. Ignored. | `-` |
| `0x79` (+121) | Sensor fault, high side (short / over-range). Error 0x10 logged. | `SC` (short circuit) |
| `0xD7` (−41) | Sensor fault, low side (open / under-range). Error 0x09 logged. | `OC` (open circuit) |
| other | Signed °C. Used for pack min/max/average. The BMCU initialises min to +120 and max to −50. | the temperature |

The last column is what the Windows diagnostic software displays when the slave
simulator sends each code to a real BMCU.

Order processed by the BMCU: bytes 2, 3, 0, 1 (i.e. registers `7E7`, `7E8`, `7E5`, `7E6`, so Bat 1, Bat 2, Pcb 1, Pcb 2).

### 7.4 Example: reading slave 1

```
→ q 01 00 7D0 F            "q01007D0F" DB 1C 0D
← r 00 01 7D0 F + 16 bytes "r00017D0F0CE40CE60CE10CE80CE20CE30CE50CE7" 1C 38 0D
                           = 3300, 3302, 3297, 3304, 3298, 3299, 3301, 3303 (mV)

→ q 01 00 7E5 4            "q01007E54" 42 54 0D
← r 00 01 7E5 4 + 5 bytes  "r00017E541716181A7A" DE 5A 0D
                           = T1 Pcb 1 23 °C, T2 Pcb 2 22 °C, T3 Bat 1 24 °C, T4 Bat 2 26 °C, (spare)
```

(Data values in the examples are made up. The CRCs are correct for the frames shown.)

### 7.5 LMU configuration registers (diagnostic software)  [V]

The BMCU never reads these registers itself. They feed the **Local cell settings** tab of the
Windows diagnostic software. When **Read** is clicked, the PC reads five blocks from each LMU
in turn. The BMCU forwards each request onto the slave bus unchanged, with `SRC = FE` (§5.3).
The slave must address its reply to `FE`, not `00`, or it never reaches the PC.

| Request | Registers | Bytes |
|---|---|---|
| `q xx FE 0000` | `0x000` | 1 |
| `q xx FE 0045` | `0x004`–`0x009` | 6 |
| `q xx FE 046C` | `0x046`–`0x052` | 13 |
| `q xx FE 0541` | `0x054`–`0x055` | 2 |
| `q xx FE 0e0E` | `0x0E0`–`0x0EE` | 15 |

Field layout. It was found by sending test patterns from the slave simulator and reading
the values the diagnostic software displays.

| Addr | Len | Tab column | Coding |
|---|---|---|---|
| `0x000` | 1 | Boot Ver. | uint8, decimal |
| `0x004`–`0x005` | 2 | Firmware version, 1st field | uint16 big-endian, 5 digits |
| `0x006` | 1 | Firmware version, 2nd field | uint8, 2 digits |
| `0x007` | 1 | Firmware version, 3rd field | uint8, 2 digits |
| `0x008` | 1 | Firmware version, 4th field | **ASCII character** |
| `0x009` | 1 | Firmware version, 5th field | uint8, 3 digits |
| `0x046`–`0x04D` | 8 | Cell 1 … Cell 8 | One byte per cell. Non-zero = ticked, `00` = unticked. |
| `0x04E` | 1 | Max °C | 8-bit, decimal (signedness not tested) |
| `0x04F` | 1 | Δ °C | 8-bit, decimal |
| `0x050`–`0x051` | 2 | Max mV | uint16 big-endian |
| `0x052` | 1 | Δ mV | 8-bit, decimal |
| `0x053` | 1 | — | Not read |
| `0x054` | 1 | Batt 1 | Non-zero = ticked. Probably enables the NTC on L-J2 (§7.2). [I] |
| `0x055` | 1 | Batt 2 | Non-zero = ticked. Probably enables the optional NTC on L-J6. [I] |
| `0x0E0`–`0x0EE` | 15 | Serial number | ASCII, stored **in reverse**. Shown as `0EE`…`0EA` + `-` + `0E9`…`0E0` (5 + 10 characters). All zeros shows as `-`. |

The firmware version is shown as `PPPPP MM.mm.C.BBB`. Each field is one row of the table above:
`0x004`–`0x005`, `0x006`, `0x007`, `0x008` (the character), `0x009`.
The software highlighted it in yellow for every value tested; the reason is unknown
(possibly a comparison with an expected version).

Example: reading the serial number `12345-67890ABCDE` from slave 1:

```
→ q 01 FE 0e0E          "q01FE0e0E" 7D 2C 0D
← r FE 01 0e0E + 15 B   "rFE010e0E454443424130393837363534333231" DD 8C 0D
                        = "EDCBA0987654321", reversed = "12345" "67890ABCDE"
```

**Write.** The **Write** button on this tab sends two writes to every LMU in turn, whether or
not anything changed on that board. Only the editable fields are written: the cell
enables + limits block and the Batt enables. Boot version, firmware version and serial number
are read-only. The PC then re-reads all five blocks from every board.

| Request | Registers | Bytes | Reply |
|---|---|---|---|
| `w xx FE 046c` + 13 bytes | `0x046`–`0x052` (Cell 1–8, Max °C, Δ °C, Max mV, Δ mV) | 13 | `!` to `FE` |
| `w xx FE 0541` + 2 bytes | `0x054`–`0x055` (Batt 1, Batt 2) | 2 | `!` to `FE` |

The byte-count digit is lower case too (`046c`). Example: Cell 8 unticked on slave 1, other
fields Max 60 °C, Δ 10 °C, Max 4200 mV, Δ 50 mV:

```
→ w 01 FE 046c + 13 B   "w01FE046c01010101010101003C0A106832" 6C 52 0D
← ! FE 01 w             "!FE01w" 30 18 0D
→ w 01 FE 0541 + 2 B    "w01FE05410100" 46 86 0D
← ! FE 01 w             "!FE01w" 30 18 0D
```

Just before the LMU writes, the PC also writes to device `0x21` (Appendix B).

### 7.6 Charge counter stored in slave 1  [C]

The BMCU has no non-volatile memory for its coulomb counter. It keeps it in registers
`0x7E0`–`0x7E3` of slave `01`, rewrites it there every few seconds, and reads it back at
start-up. A slave that returns zeros (or drops the writes) makes the BMCU start every power-up
at 0 remaining charge, i.e. **SoC 0 %** on CAN and on the PC.

| Register | Content | BMCU RAM |
|---|---|---|
| `7E0`–`7E1` | Remaining charge, int16 LE. Full = 10 × the capacity parameter (EEPROM `0xEC`), so probably 0.1 Ah if that parameter is in Ah. | `0x1CF` |
| `7E2`–`7E3` | Coulomb-counter remainder (below one unit of remaining charge), int16 LE. One unit of remaining charge = 3600 counts. | `0x100` (32-bit accumulator, sign-extended) |

**Start-up** (`0xCE62`, from the start-up sequence at `0x2542`):

1. `q 01 00 7E0 3` (3 attempts).
2. Remaining charge, clamped to 0 … 10 × capacity, is stored in identifier **2022**
   (`Amp_H_Sum` on CAN). SoC = remaining ÷ capacity is stored in identifier **2021** (SoC).
3. No reply: error `0x12` sub 3 for slave 1, remaining charge and SoC set to 0, and the read is
   retried from the main loop (`0x3BAA`) until it succeeds.

**Running** (`0x8B6E`–`0x8D00`): the current is added to the accumulator. Each time it passes
±3600 the whole units move to the remaining charge and the rest stays in the accumulator. SoC
is recalculated (capped at 99 %, 100 % once a full-charge flag is set) and stored in 2021, the
remaining charge in 2022, and then the BMCU writes
`w 01 00 7E0 3 <remaining LE> <remainder LE>`.

**SoC set** (`0xEF98`): when the SoC is set from outside (0…100 %), the remaining charge becomes
capacity × SoC and is written to slave 1 with the remainder 0.

The captures with the slave simulator (which returned zeros) show writes `0000 xxFF`: remaining
charge 0, remainder slightly negative from the discharge current. In those captures the value
written to `7E2` is the negative of the value the BMCU writes to device `0x21` register `7E0`
just before. [V]

---

## 8. Firmware-update / bridge mode (for completeness)  [C]

Selected by a BMCU mode variable. Not part of normal operation.

1. **Slaves (mode 3).** For each address 1–32, the BMCU sends a lone `CR`, then
   `i F0 00 <addr>`.
   - If the `i` is ACKed, it sends `w <addr> 00 002 0 AC` (bootloader entry, up to 3 tries).
   - If the `i` gets no ACK (the module is probably already in its bootloader), it sends a `:`
     packet instead: 17 × `0x00`, then the bytes `: 00 00 00 02 FE 00 00 CR`, with the CRC
     written over the two `00` bytes just before the `CR`.
2. **Device 0x21 (mode 2).** The BMCU sends `w 21 00 002 0 AC`, followed by 4 × `ESC` (0x1B).
3. The BMCU then turns into a **transparent byte bridge**: every received byte is echoed to TX.
   The diagnostic tool talks straight to the bootloaders. **Three consecutive `ESC` bytes**
   end bridge mode and reset the BMCU.

---

## Appendix A — BMCU-side implementation references

| Function | Address |
|---|---|
| UART init (38.4 kbaud, RX interrupt) | `0xF5A2` |
| RX ISR → ring buffer `0x578` (100 B) | `0xDB76` |
| TX ISR, stops after `CR`, releases RC1 | `0x1A6E` |
| Frame builder (`q w i r ! # d p : 0x01`) | `0x9012` |
| Frame parser state machine | `0x9ED8`–`0xA462` |
| CRC-16 / CRC byte fix-up | `0xF1DE` / `0xF474` |
| Hex encoder / decoder | `0xF184` / `0xDD30` |
| Generic master transaction (retries, timeout) | `0xE320` (request block at RAM `0x200`: retries, addr H/L, N, cmd, dest, data…) |
| Enumeration | `0xE1B6` |
| Per-slave poll | `0xC40E` |
| Charge counter: start-up read of slave 1 `7E0` / periodic update and write / SoC set (§7.6) | `0xCE62` / `0x8B6E` / `0xEF98` |
| Device `0x21` (on-board pack voltage / current): cyclic read / charge counters / output and status (Appendix B) | `0xBE5C` / `0x8894` / `0x5DE2` |

## Appendix B — Device 0x21: pack voltage and current unit on the BMCU board

Address `0x21` is the pack voltage and current measurement of the BMCU board itself (a separate
Lithium Balance module on the same protocol and bootloader as the LMUs). It is not on the slave
harness, so a replacement for the LMUs must stay silent on `0x21`. The BMCU takes pack voltage,
pack current and the input of its charge counter from it, not from the cells. [C] unless marked.

**Every main-loop pass** (`0xBE5C`, phase 0 of the main loop): `q 21 00 7D0 7`, 8 bytes.

| Register | Content | Use in the BMCU |
|---|---|---|
| `7D0`–`7D1` | **Pack voltage**, uint16 big-endian | × gain in % (EEPROM `0xB6`, applied if 1…200 and ≠ 100), stored in identifier **2008** (`Pack_Voltage` on CAN) |
| `7D2`–`7D5` | **Pack current**, int32 big-endian, signed | ÷ 100, stored in identifier **2010** (`Pack_Current` on CAN). Checked against the charge / discharge current limits (EEPROM `0x17C` / `0x17E`), and used for the C-rate (identifier 2028) |
| `7D6` | Status byte (see the start-up sequence below) | Not used by this read |
| `7D7` | ? | Copied to RAM `0x2B4` (use not traced) |

No reply after 3 attempts: error `0x11` sub 3, fault flag `0x29F`.

**Charge counters, about every 4 s** (`0x8894`–`0x8B6E`, the routine that updates the charge
counter of §7.6):

1. `q 21 00 7DC 1` (2 bytes), then the same 2 bytes are written back with `w 21 00 7E0 1`.
2. `q 21 00 7DA 1` (2 bytes), then written back with `w 21 00 7DE 1`.
3. The `7DA` value, scaled, is added to the coulomb-counter accumulator, whose whole units go
   to the remaining charge kept in slave 1 (§7.6).

[I] `7DA` / `7DC` are charge counters accumulated by the unit, and the write-back to `7DE` /
`7E0` acknowledges what was read, so no charge is counted twice. Units not verified.

**Start-up / output sequence** (`0x5DE2`–`0x5F30`): `w 21 00 7E9 1 <EEPROM 0xDA> <00 or 64>`
(an output switched off / on at 100 %, depending on the state and configuration), then
`q 21 00 7D6 0` until the status byte reads 0. No reply: retried, error `0x11` sub 4.

**Firmware update:** `w 21 00 002 0 AC` (bootloader entry, §8).

**Diagnostic PC** (`SRC = FE`, through the BMCU). [V] traffic only, the replies were not seen
on the slave harness:

| When | Requests |
|---|---|
| Shortly after BMCU start-up, PC connected | `q 21 FE` reads at `000`, `006`, `064`, `068`, `069`, `06e`, `072`, `07a` (1–4 bytes each) |
| Write on Local cell settings | `w 21 FE 0643 00000000`, `w 21 FE 0693 00000000`, `w 21 FE 06e3 00FA0000`, `w 21 FE 07a0 00` |

After sending to `0x21`, the BMCU keeps RC1 asserted: the unit sits on its own segment of the
bus on the BMCU board (§1.1).
