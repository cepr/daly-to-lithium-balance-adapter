"""Minimal DALY active balancer emulator (D2 protocol), for testing the interface board firmware.

Answers the requests the firmware sends (DALY_Active_Balancer_UART_Protocol.md §4):
  D2 03 <reg> <count> CRC   -> D2 03 <2*count> <registers> CRC
  D2 06 <reg> <value> CRC   -> echo

Usage (one process per USB-TTL adapter, 9600 8N1):
  python3 daly/fake_balancer.py --port /dev/ttyUSB0 --cells 24 --base 3600
  python3 daly/fake_balancer.py --selftest

Cell n (1-based) reads base + n - 1 mV, so every value shows which channel it came from and the
overlap readings of two emulators with the same base stay within the firmware's 50 mV tolerance.
"""
import argparse
import struct
import sys
import time

REG_CELL_COUNT = 0x31
REG_BALANCER_STATE = 0x34
REG_CELL_DELTA = 0x38
REG_BOARD_TEMP = 0x20  # Temperature 1, raw - 40 = degC (0x43 reads 0 on a real balancer)


def crc16(data: bytes) -> int:
    c = 0xFFFF
    for x in data:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def with_crc(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16(body))


class Balancer:
    def __init__(self, cells, base, offsets=None):
        self.regs = [0] * 0x100
        volts = [base + i for i in range(cells)]
        for ch, mv in (offsets or {}).items():
            volts[ch - 1] += mv
        self.regs[:cells] = volts
        self.regs[REG_CELL_COUNT] = cells
        self.regs[REG_BALANCER_STATE] = 2
        self.regs[REG_CELL_DELTA] = max(volts) - min(volts)
        self.regs[REG_BOARD_TEMP] = 40 + 30  # PCB 30 degC
        self.regs[0x8A] = 3600  # sleep time, s

    def handle(self, req: bytes):
        """8-byte request -> reply bytes, or None if the request is invalid."""
        if len(req) != 8 or req[0] != 0xD2 or crc16(req[:6]) != struct.unpack("<H", req[6:])[0]:
            return None
        func, reg, val = req[1], *struct.unpack(">HH", req[2:6])
        if func == 0x03 and reg + val <= len(self.regs):
            data = b"".join(struct.pack(">H", r) for r in self.regs[reg:reg + val])
            return with_crc(bytes([0xD2, 0x03, len(data)]) + data)
        if func == 0x06 and reg < len(self.regs):
            self.regs[reg] = val
            return req
        return None


def serve(port, bal, verbose):
    import serial
    s = serial.Serial(port, 9600, timeout=0.05)
    buf = b""
    print(f"{port}: emulating a {bal.regs[REG_CELL_COUNT]}S balancer, Ctrl-C to stop")
    while True:
        buf += s.read(64)
        # Resync on the address byte, then take 8-byte requests.
        while buf and buf[0] != 0xD2:
            buf = buf[1:]
        if len(buf) < 8:
            continue
        req, buf = buf[:8], buf[8:]
        reply = bal.handle(req)
        if verbose:
            print(time.strftime("%H:%M:%S"), "->", req.hex(" "), "| reply", len(reply) if reply else "none")
        if reply:
            s.write(reply)


def selftest():
    b = Balancer(24, 3600)
    r = b.handle(bytes.fromhex("D2 03 00 00 00 3E D7 B9"))
    ok = r is not None and len(r) == 129 and r[:3] == bytes([0xD2, 0x03, 0x7C])
    ok &= crc16(r[:-2]) == struct.unpack("<H", r[-2:])[0]
    ok &= struct.unpack(">H", r[3:5])[0] == 3600 and struct.unpack(">H", r[3 + 2 * 0x31:5 + 2 * 0x31])[0] == 24
    w = bytes.fromhex("D2 06 00 8A FF FF BA 33")
    ok &= b.handle(w) == w and b.regs[0x8A] == 0xFFFF
    ok &= b.handle(bytes.fromhex("D2 03 00 00 00 3E D7 B8")) is None
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--cells", type=int, default=24, help="string count reported (24 for #1-#3, 20 for #4)")
    ap.add_argument("--base", type=int, default=3600, help="cell 1 voltage in mV")
    ap.add_argument("--offset", action="append", default=[], metavar="CH=MV",
                    help="add MV to channel CH, e.g. 21=80 to tell the two readings of an overlap cell apart")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if not a.port:
        ap.error("--port or --selftest required")
    offsets = {int(c): int(v) for c, v in (o.split("=") for o in a.offset)}
    try:
        serve(a.port, Balancer(a.cells, a.base, offsets), a.verbose)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
