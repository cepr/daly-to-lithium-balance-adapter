"""Lithium Balance s-BMS slave-bus framing (see LithiumBalance_sBMS_RS485_Protocol.md §2-§3).

Frame: CMD | DEST(2 hex) | SRC(2 hex) | PAYLOAD(ASCII hex) | CRC_H CRC_L (raw) | CR
"""
from dataclasses import dataclass

CR = 0x0D
ESC = 0x1B
# Command letters that may start a frame on the bus.
FRAME_START = set(b"wqri!#apd:")
MAX_FRAME = 120


def _table(poly=0xAC9A):
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        t.append(c)
    return t


TABLE = _table()


def _fix(b):
    return b + 1 if b in (0x00, 0x0D, 0x1B) else b


def sbms_crc(data: bytes) -> bytes:
    c = 0xFFFF
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ TABLE[(c >> 8) ^ b]
    return bytes([_fix(c >> 8), _fix(c & 0xFF)])


def build(cmd: str, dest: int, src: int, payload: str = "") -> bytes:
    body = f"{cmd}{dest:02X}{src:02X}{payload}".encode("ascii")
    return body + sbms_crc(body) + b"\r"


@dataclass
class Frame:
    cmd: str
    dest: int
    src: int
    payload: str
    crc_ok: bool
    raw: bytes

    def describe(self) -> str:
        return f"{self.cmd} {self.dest:02X} {self.src:02X} {self.payload}"


def parse_frame(raw: bytes):
    """raw = bytes from the command letter up to and including CR. Returns Frame or None."""
    if len(raw) < 1 + 4 + 2 + 1 or raw[-1] != CR:
        return None
    body, crc = raw[:-3], raw[-3:-1]
    try:
        dest = int(body[1:3].decode("ascii"), 16)
        src = int(body[3:5].decode("ascii"), 16)
        payload = body[5:].decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None
    return Frame(chr(body[0]), dest, src, payload, sbms_crc(body) == crc, bytes(raw))


class FrameParser:
    """Streaming parser: hunts for a command letter, collects up to CR.

    The CRC bytes can never be 0x0D thanks to the fix-up, so CR always ends a frame.
    Returns a list of events: ("frame", Frame) or ("junk", bytes).
    """

    def __init__(self):
        self.buf = bytearray()
        self.junk = bytearray()

    def feed(self, data: bytes):
        out = []
        for b in data:
            if not self.buf:
                if b in FRAME_START:
                    if self.junk:
                        out.append(("junk", bytes(self.junk)))
                        self.junk.clear()
                    self.buf.append(b)
                else:
                    self.junk.append(b)
                    if b == CR or len(self.junk) > MAX_FRAME:
                        out.append(("junk", bytes(self.junk)))
                        self.junk.clear()
                continue
            self.buf.append(b)
            if b == CR:
                f = parse_frame(bytes(self.buf))
                out.append(("frame", f) if f else ("junk", bytes(self.buf)))
                self.buf.clear()
            elif len(self.buf) > MAX_FRAME:
                out.append(("junk", bytes(self.buf)))
                self.buf.clear()
        return out


def fmt_raw(raw: bytes) -> str:
    """Printable ASCII kept, other bytes shown as <HH>."""
    return "".join(chr(b) if 0x20 <= b < 0x7F else f"<{b:02X}>" for b in raw)
