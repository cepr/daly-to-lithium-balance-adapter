# s-BMS slave-board simulator

Pretends to be up to 32 Lithium Balance s-BMS slave boards (default 10) on the BMCU's slave RS485
bus. The protocol is described in [`../LithiumBalance_sBMS_RS485_Protocol.md`](../LithiumBalance_sBMS_RS485_Protocol.md).

## Run
    cd "Lithium_Balance_sBMS"
    python3 -m slave_sim.gui

Requirements: Python 3, Tkinter, pyserial (no other dependencies).

Wire a USB-RS485 adapter (with automatic DE) to the BMCU slave bus (A/B), pick the port, keep
38400 baud, and click **Connect**. Power up or reset the BMCU: it enumerates the boards
(`i F0 00 01` … `i F0 00 8A`), and the board headers then show their addresses. If the BMCU was
already running, click **Pre-address 1…N** instead.

## Per-board controls
- 8 cell voltages (mV) + code: OK / not fitted (0x2000) / fault (0x4000) / flagged (bit 15)
- 4 temperatures (°C) + code: OK / none (0x7A) / high fault (0x79) / low fault (0xD7). T4 (Bat 2) defaults to none.
- online (off = never answers → BMCU comm fault), bad CRC, NAK, reply delay
- 7E0: the two int16 values read from board 1 at start-up (meaning unknown)
- silent in boot: stop answering after the BMCU writes 0xAC to register 0x002

## LMU configuration (diagnostic software, "Local cell settings" tab)
The BMCU forwards the PC's reads with SRC = FE, and the boards reply to FE (doc §7.5). Each board
starts with made-up values: boot 1, firmware `00100 01.00.A.000`, serial `SIM00-00000000NN`,
Max 55 °C, Δ 10 °C, Max 4145 mV, Δ 25 mV, all cells enabled, Bat 1 enabled, Bat 2 disabled. **Default config**
restores them, **Config test pattern** loads the byte pattern used to work out the layout.
Writes from the PC are kept until one of those buttons is clicked, and saved profiles include
them (older profiles without them load with the defaults).

Enumeration emulates the daisy chain: `i` to F0 is taken by the first unaddressed board in chain
order. An offline board breaks the chain. Address 01 starts a new round, which resets the chain.

## Tests
    python3 -m unittest slave_sim.test_protocol      # CRC vs doc examples, model behaviour
    python3 -m slave_sim.fake_master --selftest      # BMCU emulator <-> simulator over a pty
    python3 -m slave_sim.fake_master --port /dev/ttyUSB1 -v   # against the GUI via a 2nd adapter
