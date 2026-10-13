"""Simulated slave boards and the bus dispatcher (no GUI code).

Register map and value coding follow LithiumBalance_sBMS_RS485_Protocol.md §7.
"""
import random
import threading
import time

from .sbms_protocol import build

UNADDRESSED = 0xF0
MASTER = 0x00
DEV21 = 0x21
N_CELLS = 8
N_TEMPS = 4

REG_BOOT = 0x002
REG_CELLS = 0x7D0
REG_INFO = 0x7E0
REG_TEMPS = 0x7E5

# LMU configuration registers read by the diagnostic software ("Local cell settings" tab),
# forwarded by the BMCU with SRC = FE. Layout: doc §7.5. The values are made up.
REG_CFG_BOOT = 0x000        # uint8
REG_CFG_FW = 0x004          # uint16 BE, uint8, uint8, ASCII char, uint8
REG_CFG_CELL_EN = 0x046     # 8 bytes, non-zero = enabled
REG_CFG_LIMITS = 0x04E      # Max °C, Δ °C, Max mV (uint16 BE), Δ mV
REG_CFG_BATT_EN = 0x054     # Batt 1, Batt 2, non-zero = enabled
REG_CFG_SERIAL = 0x0E0      # 15 ASCII chars, stored reversed
# (address, length) of the blocks the diagnostic software reads; saved in profiles.
CONFIG_BLOCKS = ((0x000, 1), (0x004, 6), (0x046, 13), (0x054, 2), (0x0E0, 15))

# Test pattern: every byte recognisable on screen (used to find the layout above).
CONFIG_PATTERN = {
    0x000: bytes([0x01]),
    0x004: bytes([0x01, 0x02, 0x03, 0x04, 0x58, 0x06]),
    0x046: bytes([1, 0, 1, 0, 1, 0, 1, 0]) + b"IJKLM",
    0x054: bytes([0x03, 0x00]),
    0x0E0: b"abcdefghijklmno",
}

# Value overrides (doc §7.1 / §7.2). None = send the normal value.
CELL_CODES = {"OK": None, "not fitted": 0x2000, "fault": 0x4000, "flagged": 0x8000}
TEMP_CODES = {"OK": None, "none": 0x7A, "high fault": 0x79, "low fault": 0xD7}


class Slave:
    def __init__(self, index):
        self.index = index            # position in the daisy chain, 0-based
        self.address = None           # assigned by 'i' (or pre-addressed)
        self.last = False             # bit 7 of the 'i' data
        self.regs = bytearray(0x1000)
        self.load_default_config()
        self.cells = [3300 + random.randint(-5, 5) for _ in range(N_CELLS)]
        self.cell_codes = ["OK"] * N_CELLS
        self.temps = [25] * N_TEMPS
        self.temp_codes = ["OK"] * (N_TEMPS - 1) + ["none"]   # T4 = Bat 2 (optional NTC), disabled by default
        self.info = [0, 0]            # two int16 LE at 0x7E0 (meaning unknown)
        # behaviour / fault injection
        self.online = True
        self.bad_crc = False
        self.nak = False
        self.delay_ms = 0
        self.silent_in_boot = False
        self.in_boot = False
        # statistics
        self.rx = 0
        self.tx = 0
        self.last_activity = 0.0

    @property
    def responsive(self):
        return self.online and not (self.in_boot and self.silent_in_boot)

    def refresh(self):
        r = self.regs
        for i in range(N_CELLS):
            code = CELL_CODES[self.cell_codes[i]]
            v = max(0, min(0x7FFF, int(self.cells[i])))
            if code == 0x8000:
                v |= 0x8000
            elif code is not None:
                v = code
            r[REG_CELLS + 2 * i] = v >> 8
            r[REG_CELLS + 2 * i + 1] = v & 0xFF
        for i in range(2):
            v = int(self.info[i]) & 0xFFFF
            r[REG_INFO + 2 * i] = v & 0xFF
            r[REG_INFO + 2 * i + 1] = v >> 8
        for i in range(N_TEMPS):
            code = TEMP_CODES[self.temp_codes[i]]
            v = code if code is not None else max(-128, min(127, int(self.temps[i]))) & 0xFF
            r[REG_TEMPS + i] = v
        r[REG_TEMPS + N_TEMPS] = 0

    def load_default_config(self):
        r = self.regs
        r[REG_CFG_BOOT] = 1
        r[REG_CFG_FW:REG_CFG_FW + 6] = bytes([0x00, 0x64, 1, 0, ord("A"), 0])   # 00100 01.00.A.000
        r[REG_CFG_CELL_EN:REG_CFG_CELL_EN + 8] = bytes([1] * N_CELLS)
        r[REG_CFG_LIMITS:REG_CFG_LIMITS + 5] = bytes([55, 10, 4145 >> 8, 4145 & 0xFF, 25])
        r[REG_CFG_BATT_EN:REG_CFG_BATT_EN + 2] = bytes([1, 0])      # Bat 1 on, Bat 2 (optional) off
        serial = f"SIM00{self.index + 1:010d}"                               # SIM00-0000000001
        r[REG_CFG_SERIAL:REG_CFG_SERIAL + 15] = serial[::-1].encode("ascii")

    def load_config_pattern(self):
        for reg, data in CONFIG_PATTERN.items():
            self.regs[reg:reg + len(data)] = data
        self.regs[0x000] = self.index + 1     # boot-version byte = chain position, to tell boards apart

    def to_dict(self):
        d = {k: getattr(self, k) for k in (
            "cells", "cell_codes", "temps", "temp_codes", "info",
            "online", "bad_crc", "nak", "delay_ms", "silent_in_boot")}
        d["config"] = {f"{reg:03X}": self.regs[reg:reg + n].hex().upper() for reg, n in CONFIG_BLOCKS}
        return d

    def from_dict(self, d):
        for k, v in d.items():
            if k == "config":
                for reg, data in v.items():
                    data = bytes.fromhex(data)
                    reg = int(reg, 16)
                    self.regs[reg:reg + len(data)] = data
            elif hasattr(self, k):
                setattr(self, k, list(v) if isinstance(v, list) else v)


class RawDevice:
    """Device 0x21: unknown semantics, answers from a raw register file."""

    def __init__(self):
        self.enabled = False
        self.regs = bytearray(0x1000)
        self.rx = 0
        self.tx = 0
        self.last_activity = 0.0


class Reply:
    def __init__(self, data=None, delay_s=0.0, notes=(), node=None):
        self.data = data
        self.delay_s = delay_s
        self.notes = list(notes)
        self.node = node


class Bus:
    def __init__(self, count=10):
        self.lock = threading.RLock()
        self.slaves = []
        self.dev21 = RawDevice()
        self.ignore_bad_crc = True
        self.stats = {"frames": 0, "crc_errors": 0, "replies": 0, "ignored": 0}
        self.set_count(count)

    # ---- configuration -------------------------------------------------
    def set_count(self, n):
        with self.lock:
            n = max(1, min(32, n))
            while len(self.slaves) < n:
                self.slaves.append(Slave(len(self.slaves)))
            del self.slaves[n:]

    def reset_chain(self):
        with self.lock:
            for s in self.slaves:
                s.address = None
                s.last = False
                s.in_boot = False

    def preaddress(self):
        with self.lock:
            for i, s in enumerate(self.slaves):
                s.address = i + 1
                s.last = i == len(self.slaves) - 1

    def load_default_config(self):
        with self.lock:
            for s in self.slaves:
                s.load_default_config()

    def load_config_pattern(self):
        with self.lock:
            for s in self.slaves:
                s.load_config_pattern()

    def drift(self, mv=2):
        with self.lock:
            for s in self.slaves:
                s.cells = [max(2500, min(4300, c + random.randint(-mv, mv))) for c in s.cells]

    def find(self, addr):
        for s in self.slaves:
            if s.address == addr:
                return s
        return None

    # ---- dispatch ------------------------------------------------------
    def handle(self, f):
        """Process a parsed Frame. Returns a Reply (data None = stay silent)."""
        with self.lock:
            self.stats["frames"] += 1
            if not f.crc_ok:
                self.stats["crc_errors"] += 1
            if f.dest == UNADDRESSED and f.cmd == "i":
                return self._enumerate(f)
            if f.dest == DEV21 and self.dev21.enabled:
                return self._serve(self.dev21, DEV21, f)
            s = self.find(f.dest)
            if s is None:
                self.stats["ignored"] += 1
                return Reply()
            return self._serve(s, f.dest, f)

    def _finish(self, node, addr, cmd, payload, notes, to=MASTER):
        node.tx += 1
        self.stats["replies"] += 1
        data = build(cmd, to, addr, payload)
        delay = 0.0
        if isinstance(node, Slave):
            if node.bad_crc:
                data = data[:-3] + bytes([data[-3] ^ 0x55, data[-2] ^ 0x55]) + b"\r"
                notes.append("bad CRC injected")
            delay = node.delay_ms / 1000.0
        return Reply(data, delay, notes, node)

    def _enumerate(self, f):
        try:
            val = int(f.payload[:2], 16)
        except ValueError:
            return Reply(notes=["bad 'i' payload"])
        if not f.crc_ok:
            return Reply(notes=["'i' with bad CRC ignored"])
        new = val & 0x7F
        notes = []
        if new == 1:
            self.reset_chain()
            notes.append("new enumeration round: chain reset")
        target = next((s for s in self.slaves if s.address is None), None)
        if target is None:
            notes.append(f"no unaddressed slave left for address {new}")
            return Reply(notes=notes)
        target.rx += 1
        target.last_activity = time.time()
        if not target.responsive:
            notes.append(f"chain position {target.index + 1} offline: no reply")
            return Reply(notes=notes, node=target)
        target.address = new
        target.last = bool(val & 0x80)
        notes.append(f"chain position {target.index + 1} -> address {new:02X}"
                     + (" (LAST)" if target.last else ""))
        return self._finish(target, new, "!", "i", notes)

    def _serve(self, node, addr, f):
        # Replies go back to the requester: SRC is 00 for the BMCU's own polls, and the
        # original SRC when the BMCU forwards a request from the diagnostic PC (doc §5.3).
        node.rx += 1
        node.last_activity = time.time()
        notes = []
        if isinstance(node, Slave) and not node.responsive:
            return Reply(notes=["slave offline: no reply"], node=node)
        if not f.crc_ok:
            if self.ignore_bad_crc:
                return Reply(notes=["bad CRC: ignored"], node=node)
            return self._finish(node, addr, "#", f.cmd + "C", ["bad CRC: NAK"], f.src)
        if isinstance(node, Slave) and node.nak:
            return self._finish(node, addr, "#", f.cmd + "D", ["NAK injected"], f.src)
        p = f.payload
        if f.cmd in "qw":
            try:
                reg, n = int(p[0:3], 16), int(p[3], 16) + 1
            except (ValueError, IndexError):
                return Reply(notes=["malformed request"], node=node)
            if reg + n > 0x1000:
                return self._finish(node, addr, "#", f.cmd + "D", ["register out of range"], f.src)
            if f.cmd == "q":
                if isinstance(node, Slave):
                    node.refresh()
                data = node.regs[reg:reg + n].hex().upper()
                return self._finish(node, addr, "r", p[:4] + data, notes, f.src)
            try:
                data = bytes.fromhex(p[4:4 + 2 * n])
                if len(data) != n:
                    raise ValueError
            except ValueError:
                return Reply(notes=["malformed write data"], node=node)
            node.regs[reg:reg + n] = data
            notes.append(f"wrote {data.hex().upper()} to {reg:03X}")
            if isinstance(node, Slave) and reg <= REG_BOOT < reg + n and node.regs[REG_BOOT] == 0xAC:
                node.in_boot = True
                notes.append("BOOTLOADER ENTRY requested")
            elif node is self.dev21 and reg == REG_BOOT and data[0] == 0xAC:
                notes.append("BOOTLOADER ENTRY requested (device 0x21)")
            return self._finish(node, addr, "!", "w", notes, f.src)
        return Reply(notes=[f"command '{f.cmd}' not handled"], node=node)

    # ---- profiles ------------------------------------------------------
    def to_dict(self):
        with self.lock:
            return {"count": len(self.slaves),
                    "slaves": [s.to_dict() for s in self.slaves],
                    "dev21_enabled": self.dev21.enabled,
                    "dev21_regs": self.dev21.regs.hex()}

    def from_dict(self, d):
        with self.lock:
            self.set_count(d.get("count", len(self.slaves)))
            for s, sd in zip(self.slaves, d.get("slaves", [])):
                s.from_dict(sd)
            self.dev21.enabled = d.get("dev21_enabled", False)
            if "dev21_regs" in d:
                self.dev21.regs[:] = bytes.fromhex(d["dev21_regs"])
