"""Energica Ego battery CAN messages (Lithium Balance BMCU, firmware 6.18.0).

Source: ../Battery_canbus_protocol.html. Bit positions are 0-63 within the 8-byte payload:
  - Intel (little-endian):    bit n is bit n of the payload read as a little-endian uint64.
  - Motorola (big-endian):    bits are counted from the first bit of byte 0 (MSB first), so
                              start 32 / 16 bits = bytes 4-5, MSB in byte 4.

The protocol document gives no scaling or units: every signal starts with scale 1 and no unit,
so the GUI shows the raw integer. Edit `scale`, `offset`, `unit` and `signed` below once a
signal's meaning is confirmed. `signed=True` on currents and temperatures is an assumption.
"""
from dataclasses import dataclass, field

TX = "BMCU → bus"
RX = "bus → BMCU"


@dataclass(frozen=True)
class Signal:
    name: str
    start: int
    length: int
    data_id: int = 0
    intel: bool = False
    constant: bool = False  # "Constant" data type: fixed value, shown in grey
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    flags: bool = False  # bit field: the GUI lists the set bits
    note: str = ""

    def extract(self, data: bytes):
        """Raw unsigned value, or None if the frame is too short to hold the signal."""
        if (self.start + self.length + 7) // 8 > len(data):
            return None
        if self.intel:
            v = int.from_bytes(data.ljust(8, b"\0"), "little") >> self.start
        else:
            v = int.from_bytes(data.ljust(8, b"\0"), "big") >> (64 - self.start - self.length)
        return v & ((1 << self.length) - 1)

    def insert(self, data: bytearray, raw: int):
        """Write a raw value into an 8-byte payload (used by the simulator and tests)."""
        mask = (1 << self.length) - 1
        raw &= mask
        order = "little" if self.intel else "big"
        shift = self.start if self.intel else 64 - self.start - self.length
        v = int.from_bytes(data, order)
        v = (v & ~(mask << shift)) | (raw << shift)
        data[:] = v.to_bytes(8, order)

    def physical(self, raw: int) -> float:
        if self.signed and raw >= 1 << (self.length - 1):
            raw -= 1 << self.length
        return raw * self.scale + self.offset

    def raw_from_physical(self, value: float) -> int:
        return round((value - self.offset) / self.scale) & ((1 << self.length) - 1)

    def format(self, raw: int) -> str:
        v = self.physical(raw)
        if self.flags:
            bits = [str(b) for b in range(self.length) if raw >> b & 1]
            return f"0x{raw:0{(self.length + 3) // 4}X}" + (f"  bits {','.join(bits)}" if bits else "  (none)")
        if self.scale == int(self.scale) and self.offset == int(self.offset):
            s = str(int(v))
        else:
            s = f"{v:.3f}".rstrip("0").rstrip(".")
        return f"{s} {self.unit}".rstrip()


@dataclass(frozen=True)
class Message:
    can_id: int
    name: str
    direction: str
    interval_ms: int | None
    signals: tuple[Signal, ...] = field(default_factory=tuple)
    note: str = ""


S = Signal
MESSAGES: tuple[Message, ...] = (
    # ---- transmitted by the BMCU -------------------------------------------------------------
    Message(0x200, "Pack status", TX, 50, (
        S("Pack_Temp_Low", 0, 8, 2005, signed=True),
        S("SoC", 8, 8, 2021),
        S("SoH", 16, 8, 2020),
        S("Pack_Temp_High", 24, 8, 2004, signed=True),
        S("Pack_Voltage", 32, 16, 2008),
        S("Pack_Current", 48, 16, 2010, signed=True),
    )),
    Message(0x201, "System state", TX, 100, (
        S("System_State", 0, 8, 2006),
        S("Error_Flags", 8, 32, 2012, flags=True),
        S("DI_2017", 40, 24, 2017, note="No_Description"),
    )),
    Message(0x202, "Current limits", TX, 100, (
        S("Const_2006", 0, 8, 2006, constant=True),
        S("Const_2012", 8, 8, 2012, constant=True),
        S("Const_0", 16, 8, 0, constant=True),
        S("Const_2016", 24, 8, 2016, constant=True),
        S("Allowed_disch_current", 32, 16, 2030),
        S("Allowed_regen_current", 48, 16, 2032),
    )),
    Message(0x203, "Cell voltages", TX, 50, (
        S("Average_Cell_Voltage", 0, 16, 2061),
        S("Cell_with_lowest_voltage", 16, 8, 2025),
        S("Cell_with_highest_voltage", 24, 8, 2024),
        S("Cell_Voltage_Min", 32, 16, 2002),
        S("Cell_Voltage_Max", 48, 16, 2000),
    )),
    Message(0x204, "Charge", TX, 1000, (
        S("Remaining_Charge", 0, 24, 2067),
        S("Pack_C_Rate", 24, 16, 2028),
        S("SoC_Set_Echo", 40, 8, 2043, note="last SoC set command received in 0x119"),
        S("Charger_Max_DC_I", 48, 16, 2117),
    )),
    Message(0x205, "Energy", TX, 1000, (
        S("Remaining_Energy", 0, 16, 2065),
        S("Deviation_Voltage", 16, 16, 2063),
        S("Amp_H_Sum", 32, 16, 2022),
        S("Cells_Connected", 48, 16, 2034),
    )),
    Message(0x206, "Diagnostics", TX, 1000, (
        S("Pack_resistance", 0, 16, 2036),
        S("LMU_Comm_Warnings", 16, 32, 2046, flags=True),
        S("IO_State", 48, 8, 2007, flags=True),
        S("Pack_Temp_Low", 56, 8, 2005, signed=True),
    )),
    Message(0x207, "Cell voltage sum", TX, 100, (
        S("DI_2053", 0, 16, 2053, intel=True, note="No_Description"),
        S("DI_2055", 16, 16, 2055, intel=True, note="No_Description"),
        S("DI_2057", 32, 16, 2057, intel=True, note="No_Description"),
        S("Cell_Voltage_Sum", 48, 16, 2110, intel=True),
    )),
    Message(0x2FF, "Charger", TX, 100, (
        S("Charger_Enabled", 0, 8, 2119, intel=True),
        S("Post_Processor_1", 8, 10, 2070, intel=True, note="10 × byte 7 of 0x10A"),
        S("Const_bits18", 18, 6, 0, intel=True, constant=True,
          note="doc says Motorola, but read that way it overlaps Post_Processor_1; "
               "it pads byte 2 after the 10-bit Intel field"),
        S("Charger_Max_DC_V", 24, 16, 2112, intel=True),
        S("Charger_Max_DC_I", 40, 16, 2117, intel=True),
        S("Const_byte7", 56, 8, 0, constant=True),
    )),
    # ---- received by the BMCU ----------------------------------------------------------------
    Message(0x10A, "Post-processor input", RX, None, (
        S("DI_2072", 56, 8, 2072, note="× 10 is forwarded in 0x2FF"),
    )),
    Message(0x119, "SoC set command", RX, None, (
        S("SoC_Set", 0, 8, 2043, unit="%", note="< 100 overwrites the SoC; then ignored for ~25 s"),
    )),
    Message(0x605, "Contactor-open request", RX, None, (
        S("Contactor_Open_Req", 56, 8, 2122, note="non-zero opens both contactors; must be repeated"),
    )),
)
del S

BY_ID = {m.can_id: m for m in MESSAGES}


def decode(can_id: int, data: bytes):
    """[(signal, raw or None)] for a known ID, else None."""
    msg = BY_ID.get(can_id)
    if msg is None:
        return None
    return [(s, s.extract(data)) for s in msg.signals]


def encode(can_id: int, values: dict) -> bytes:
    """Build an 8-byte payload from {signal name: physical value} (missing signals = 0)."""
    data = bytearray(8)
    for s in BY_ID[can_id].signals:
        if s.name in values:
            s.insert(data, s.raw_from_physical(values[s.name]))
    return bytes(data)
