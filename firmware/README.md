# Interface board firmware

Rust firmware for the interface board (STM32G0B1KBT6), built on [Embassy](https://embassy.dev).
It emulates the 10 Lithium Balance LMUs on the BMCU slave bus, using the cell voltages of the 4
DALY balancers and the 4 module NTCs. See the [main README](../README.md) for the system.

> **Status:** compiles, protocol logic unit-tested on the PC. Not yet run on hardware: there is
> no board yet, and the DALY protocol is still unvalidated.

## Layout

| Path | Contents |
|---|---|
| [`core/`](core/) | `bridge-core`: hardware-independent `no_std` logic, tested on the PC with `cargo test`. |
| `core/src/sbms.rs` | s-BMS framing, CRC-16 (0xAC9A), streaming receiver, frame builder. |
| `core/src/lmu.rs` | The 10 emulated LMUs: enumeration, register reads, settings registers. |
| `core/src/daly.rs` | DALY D2 requests, CRC-16/Modbus, real-time reply parser. |
| `core/src/cells.rs` | Balancer channels → LMU cells, overlap plausibility check. |
| `core/src/temps.rs` | NTC conversion and the LMU temperature mapping (balancer PCB + module NTC). |
| `core/src/storage.rs` | Flash record of the LMU settings. |
| [`board/`](board/) | `interface-board`: the STM32 binary (tasks, pins, peripherals). |
| `justfile` | `just test`, `just build`, `just run`, `just build-nucleo`, `just run-nucleo`, `just lint`, `just size`. |
| `rust-toolchain.toml` | Stable Rust with the `thumbv6m-none-eabi` target (Cortex-M0+). |

The two crates are separate Cargo projects (not a workspace) so that `core` builds for the
PC and `board` for the STM32 without passing `--target` by hand.

## Install

1. **Rust** (already installed here): `rustup` with stable Rust. The first build in `firmware/`
   adds the `thumbv6m-none-eabi` target and `llvm-tools` automatically from `rust-toolchain.toml`.
2. **probe-rs**, to flash and to read the defmt log through an ST-LINK:
   ```sh
   curl --proto '=https' --tlsv1.2 -LsSf https://github.com/probe-rs/probe-rs/releases/latest/download/probe-rs-tools-installer.sh | sh
   ```
   (or `cargo install probe-rs-tools --locked`, which needs `libudev-dev` and `pkg-config`).
3. **udev rules**, so the ST-LINK works without root:
   ```sh
   curl -LO https://probe.rs/files/69-probe-rs.rules
   sudo mv 69-probe-rs.rules /etc/udev/rules.d/
   sudo udevadm control --reload && sudo udevadm trigger
   ```
4. Optional: `cargo install cargo-binutils` for `just size`; in VS Code, the
   *rust-analyzer* and *probe-rs* (debugger) extensions.

`just` is already installed. The interface board's programming header J11 takes an
**ST-LINK/V2 clone USB dongle** and its 10-wire ribbon cable. See [Programming header](../README.md#programming-header)
in the main README for the pinout. A Nucleo's on-board ST-LINK also works, through jumper wires (see
[below](#using-a-nucleos-st-link-for-the-interface-board)).

Clone dongles often run old ST-LINK firmware. If probe-rs doesn't connect, update it with ST's
STSW-LINK007 updater (some clones refuse the update).

## Use

```sh
just test          # protocol and mapping tests on the PC
just build         # firmware → board/target/thumbv6m-none-eabi/release/interface-board
just run           # flash the interface board and show the log (chip STM32G0B1KBTx)
just build-nucleo  # same firmware for the NUCLEO-G0B1RE dev board
just run-nucleo    # flash the Nucleo and show the log (chip STM32G0B1RETx)
```

The log level is set by `DEFMT_LOG` in `board/.cargo/config.toml` (`info` by default).

### Build targets

The target is a Cargo feature of `board/`. Exactly one must be enabled.

| Feature | Hardware | Chip (Embassy / probe-rs) | Differences |
|---|---|---|---|
| `board` (default) | Interface board | `stm32g0b1kb` / `STM32G0B1KBTx` | — |
| `nucleo` | NUCLEO-G0B1RE | `stm32g0b1re` / `STM32G0B1RETx` | Balancer #4 on PC0 (TX) / PC1 (RX) instead of PA4/PA5; status LED on the green user LED (PA5) instead of PB5; clock from the internal HSI16 (16 MHz) instead of the 8 MHz oscillator |

Without `just`: `cargo build --release --no-default-features --features nucleo` in `board/`.
For `cargo run`, also override the probe-rs chip, as the `run-nucleo` recipe does.

## Testing on the NUCLEO-G0B1RE

The NUCLEO-G0B1RE carries the same chip in a 64-pin package, so the firmware runs on it
before the interface board exists. Its on-board ST-LINK flashes it, shows the defmt log, and
provides a virtual COM port wired to USART2 (PA2/PA3): the BMCU bus. Over that port the
BMCU emulator talks to the firmware directly, with no RS485 adapter.

### What you need

- NUCLEO-G0B1RE and a USB cable.
- Up to 4 **3.3 V** USB-TTL adapters (CP2102, FT232 or similar) to emulate the DALY balancers. One is
  enough to start.
- For the temperature inputs: 10 kΩ resistors, or the real NTCs.
- Python 3 with pyserial, for the emulators.

### Wiring

Pins are given by name. The header positions are in the board's user manual (UM2324) and
are printed on the board.

| Function | Nucleo pins | Connect |
|---|---|---|
| BMCU bus (USART2) | PA2 / PA3 | Nothing: already on the ST-LINK virtual COM port (`/dev/ttyACM0`). PA1 (DE) toggles but is unused. |
| Balancer #1 (USART1) | PB6 TX, PB7 RX | Adapter RX ← PB6, adapter TX → PB7, GND ↔ GND |
| Balancer #2 (USART5) | PB3 TX, PB4 RX | the same way |
| Balancer #3 (USART3) | PB2 TX, PB0 RX | the same way |
| Balancer #4 (USART6) | PC0 TX, PC1 RX | the same way (on the interface board this is PA4/PA5) |
| T1…T4 | PA0, PA6, PA7, PB1 | Each: 10 kΩ to 3V3 and 10 kΩ (or the NTC) to GND. Two 10 kΩ resistors read 25 °C. |

An unconnected NTC input floats, so it reads a random temperature. Leave it open
on purpose only to test the "open sensor" code. Power the adapters from USB only, and don't
connect their 5 V or 3.3 V pins to the Nucleo.

### Steps

1. **Flash and watch the log:**
   ```sh
   cd firmware && just run-nucleo
   ```
   Expect `interface board start`. The green LED blinks fast (5 Hz) while any balancer is missing.
2. **Start one emulator per connected adapter** (from the repository root, one terminal each):
   ```sh
   python3 daly/fake_balancer.py --port /dev/ttyUSB0 --cells 24 --base 3600   # balancer #1
   python3 daly/fake_balancer.py --port /dev/ttyUSB1 --cells 24 --base 3600   # balancer #2
   python3 daly/fake_balancer.py --port /dev/ttyUSB2 --cells 24 --base 3600   # balancer #3
   python3 daly/fake_balancer.py --port /dev/ttyUSB3 --cells 20 --base 3600   # balancer #4
   ```
   The log shows `balancer #n: online, 24 cells, state 2`. With all 4 online, the LED blinks
   slowly (1 Hz). Cell n of an emulator reads `base + n − 1` mV.
3. **Run the BMCU emulator** against the virtual COM port:
   ```sh
   python3 -m slave_sim.fake_master --port /dev/ttyACM0 -v --cycles 3
   ```
   Expect `enumeration: 10/10 slaves answered`, then 8 cells and 4 temperatures per LMU.
   Cells of a missing balancer read `16384` (`0x4000`, cell fault). The 4 temperatures are the
   balancer PCB temperature (`30` from the emulator, `215` = `0xD7` open while that balancer is missing), `122`
   (`0x7A`, not fitted), the NTC value, and `122` again.

### Checks

| Test | How | Expected |
|---|---|---|
| Cell mapping | All 4 emulators, base 3600 | LMU 3 = #1 ch 17–20 (3616…3619) + #2 ch 1–4 (3600…3603), and so on as in the README table |
| Balancer lost | Stop one emulator | After 2 s its cells read `16384`; the LED blinks fast |
| Overlap check | Start #1 with `--offset 21=80` | LMU 3, cell 5 (global cell 21) reads `36368` (bit 15 set + 3600 mV) |
| Wrong string count | Start #4 with `--cells 24` | Log warns `reports 24 cells, expected 20`; its cells read `16384` |
| Fans | Swap one NTC input's lower 10 kΩ for 4.7 kΩ (about 43 °C) or 3.3 kΩ (about 52 °C) | Log shows `fans: 86%`, then `fans: 100%`. Check PA8 with a scope or LED: 25 kHz |
| Temperatures | Change a resistor, or open or short an input | Value follows; open reads `-41` (`0xD7`), shorted reads `121` (`0x79`); LMUs 3 and 8 show the fault if either of their sensors has one |

### Using a Nucleo's ST-LINK for the interface board

The usual programmer is the ST-LINK/V2 clone dongle on J11 (see above). The ST-LINK half of
the Nucleo can be used instead. Set its jumpers for an external target as described in the
user manual, and wire its SWD header (CN4) to J11:

| Nucleo CN4 | J11 pin |
|---|---|
| 2 SWCLK | 2 |
| 3 GND | 5 or 6 |
| 4 SWDIO | 4 |
| 5 NRST | 1 |
| 1 VDD_TARGET (voltage sense) | not on J11: take 3.3 V from the board, or leave it open if the ST-LINK runs without it |

Power the interface board from its own 12 V input, and use `just run`.

## Clock

| Target | Source | SYSCLK |
|---|---|---|
| Interface board | 8 MHz oscillator X1 (ASE-8.000MHZ-LC-T, ±50 ppm) on PC14, HSE bypass mode, PLL ×16 / 2 | 64 MHz |
| NUCLEO-G0B1RE | Internal HSI16 (±1 %) | 16 MHz |

On the interface board the firmware waits for the oscillator at start-up. If X1 is missing,
unpowered or not connected to PC14, it stops before printing `interface board start`, and the
LED stays dark. Check X1 first if that happens.

## What it does

| Task | Peripheral | Behaviour |
|---|---|---|
| `bmcu` | USART2, PA2/PA3, hardware DE on PA1, 38400 8N1 | Parses every frame on the bus and answers `i` (enumeration), `q` and `w` for addresses `01`…`0A`, from the BMCU (`SRC 00`) or forwarded from the diagnostic PC (`SRC FE`); replies go to the sender. Bad CRC → silent (the BMCU retries). 1 ms turnaround before replying. |
| `balancer` ×4 | USART1/3/5/6, 9600 8N1 | Every 500 ms sends `D2 03 0000 0050` and parses the 165-byte reply (cells, string count, PCB temperature at `0x0020`). Checks the reported string count (24/24/24/20). |
| `temperatures` | ADC1 IN0/6/7/9 | Every 500 ms, 8 samples per input, converted with a 10 kΩ B3950 NTC table. |
| `fans` | TIM1_CH1 on PA8, 25 kHz | Every 1 s sets the fan speed from the hottest module temperature (see [Fans](#fans)). |
| `store_settings` | Flash, page at `0x0801F800` | Saves the LMU settings 2 s after the diagnostic software's last write. |
| `status` | PB5 LED | Blinks at 1 Hz when all 4 balancers answer, 5 Hz otherwise. |

Behaviour towards the BMCU:

- **Missing data:** a balancer that hasn't answered for 2 s makes all cells it owns read
  `0x4000` (cell fault) and its PCB temperature read "open" (`0xD7`). The NTC temperatures
  read "open" until the first ADC pass.
- **Overlap check:** when the two readings of an overlap cell differ by more than 50 mV, the cell
  is reported with bit 15 set (BMCU fault, value still shown).
- **Addresses:** 1…10 are answered even before enumeration, so a reset of the interface board
  while the bike is on doesn't drop the LMUs from the BMCU.
- **Settings (diagnostic software, Local cell settings tab):** registers `0x046`–`0x055` (cell
  and Batt enables, Max/Δ °C, Max/Δ mV) are stored per LMU and can be changed with the tab's
  **Write** button. They are saved in flash, in the last 2 KB page of the first 128 KB
  (`memory.x` keeps the program out of it). A Write click is saved 2 s after the last write,
  with one page erase, and only if a value changed. The CPU stalls for the erase (about 40 ms),
  so the BMCU may retry one request. A blank or corrupt page loads the defaults:
  all cells enabled, Max 55 °C, Δ 10 °C, Max 4145 mV, Δ 25 mV, Batt 1 on, Batt 2 off.
  The settings don't change what the board reports to the BMCU.
- **Identity (read-only):** boot version 1, firmware version `00000 00.01.A.000`, serial number
  `IFB00-00000000nn` (nn = LMU address).
- **Other writes:** acknowledged and ignored (the BMCU writes the bootloader entry at `0x002`,
  and `0x7E0` on slave 1 every 4 s).
- **Register `0x7E0`** (unknown meaning, read once from slave 1): reads as 0.

## Fans

`core/src/fan.rs` sets the duty cycle, which is the fan speed (the board hardware stops the fans
while the MCU is in reset; see the main README, [Fans](../README.md#fans)):

| Hottest module | Duty |
|---|---|
| below 35 °C (fans off), or below 32 °C (fans running) | 0 %, stopped |
| 35 °C | 30 % (lowest duty used: most fans don't start reliably below 20–30 %) |
| 35…45 °C | linear 30 → 100 % |
| 45 °C and above | 100 % |
| any NTC open or shorted | 100 % |

The fans stay stopped for the first second after start-up, until the first temperature pass. The
thresholds are constants at the top of `core/src/fan.rs`.

If the firmware panics, the timer keeps running at the last duty cycle until a reset. The
watchdog (open point below) will make a panic reset the MCU and so stop the fans.

## Open points

- [ ] Check the DALY frames on a real balancer (DALY doc §8), then enable `DISABLE_SLEEP` in
      `board/src/main.rs`. Until then the balancers stop answering after 1 h of default sleep timeout.
- [ ] Fill in the real NTC type (`core/src/temps.rs`, `NTC_TABLE`).
- [ ] Run the test procedure above on a NUCLEO-G0B1RE, then test through a USB–RS485
      adapter and against the real BMCU.
- [ ] Watchdog (IWDG), with the counter frozen while the debugger halts the core.
- [ ] CAN (FDCAN1): not implemented. Clock it from the 8 MHz oscillator (`fdcansel = HSE`,
      commented out in `clock_config` in `board/src/main.rs`).
- [ ] Choose the fan temperature thresholds for the real modules (`core/src/fan.rs`).
- [ ] LPUART2 debug output (PC6): pin reserved, no code.
- [x] Confirm the balancer PCB temperature register (`0x0020`; `0x0043` reads 0) and that the
      balancer answers the 80-register read (DALY doc §8).
