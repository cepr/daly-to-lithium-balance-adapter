"""Minimal BMCU emulator for testing the slave simulator without hardware.

Usage:
  python3 -m slave_sim.fake_master --selftest          # in-process simulator over a pty pair
  python3 -m slave_sim.fake_master --port /dev/ttyUSB1 # talk to the GUI through a real/virtual link
"""
import argparse
import os
import queue
import select
import sys
import time
import tty

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "slave_sim"

from .sbms_protocol import FrameParser, build, fmt_raw  # noqa: E402

TIMEOUT = 0.14   # 14 ticks of 10 ms (doc §6)


class FdPort:
    def __init__(self, fd):
        self.fd = fd

    def write(self, b):
        os.write(self.fd, b)

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 256) if r else b""


class SerialPort:
    def __init__(self, path, baud):
        import serial
        self.s = serial.serial_for_url(path, baudrate=baud, timeout=0)

    def write(self, b):
        self.s.write(b)

    def read(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            d = self.s.read(256)
            if d:
                return d
            time.sleep(0.002)
        return b""


class Master:
    def __init__(self, port, verbose=False):
        self.port = port
        self.parser = FrameParser()
        self.verbose = verbose

    def transact(self, cmd, dest, payload, ok_cmds, attempts=3):
        frame = build(cmd, dest, 0x00, payload)
        for _ in range(attempts):
            self.port.write(b"\r" + frame)
            if self.verbose:
                print("->", fmt_raw(frame))
            end = time.time() + TIMEOUT
            while time.time() < end:
                for kind, f in self.parser.feed(self.port.read(end - time.time())):
                    if kind != "frame" or f.dest != 0x00:
                        continue
                    if self.verbose:
                        print("<-", fmt_raw(f.raw), "" if f.crc_ok else "BAD CRC")
                    if f.crc_ok and f.cmd in ok_cmds:
                        return f
        return None

    def enumerate(self, n):
        found = 0
        for addr in range(1, n + 1):
            r = self.transact("i", 0xF0, f"{addr | (0x80 if addr == n else 0):02X}", "!", attempts=1)
            if r is None:
                break
            found += 1
        return found

    def read(self, dest, reg, n):
        r = self.transact("q", dest, f"{reg:03X}{n - 1:X}", "r")
        return bytes.fromhex(r.payload[4:]) if r else None

    def write(self, dest, reg, data):
        return self.transact("w", dest, f"{reg:03X}{len(data) - 1:X}{data.hex().upper()}", "!") is not None

    def poll(self, dest):
        c = self.read(dest, 0x7D0, 16)
        t = self.read(dest, 0x7E5, 5)
        cells = [c[2 * i] << 8 | c[2 * i + 1] for i in range(8)] if c else None
        temps = [b - 256 if b > 127 else b for b in t[:4]] if t else None
        return cells, temps


def report(m, n, cycles):
    found = m.enumerate(n)
    print(f"enumeration: {found}/{n} slaves answered")
    for _ in range(cycles):
        for a in range(1, found + 1):
            cells, temps = m.poll(a)
            print(f"slave {a:2d}: cells={cells}  temps={temps}")
    return found


def selftest(n, cycles, verbose):
    from .serial_link import SerialLink
    from .slave_model import Bus
    mfd, sfd = os.openpty()
    tty.setraw(mfd)
    tty.setraw(sfd)
    bus = Bus(n)
    link = SerialLink(bus, queue.Queue())
    link.open(os.ttyname(sfd), 38400)
    try:
        m = Master(FdPort(mfd), verbose)
        ok = report(m, n, cycles) == n
        # fault injection checks
        bus.slaves[2].online = False
        ok &= m.poll(3) == (None, None)
        bus.slaves[2].online = True
        bus.slaves[0].cell_codes[1] = "not fitted"
        ok &= m.poll(1)[0][1] == 0x2000
        ok &= m.write(1, 0x002, b"\xAC") and bus.slaves[0].in_boot
        print("SELFTEST", "PASS" if ok else "FAIL")
        return ok
    finally:
        link.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest(a.n, a.cycles, a.verbose) else 1)
    if not a.port:
        ap.error("--port or --selftest required")
    report(Master(SerialPort(a.port, a.baud), a.verbose), a.n, a.cycles)


if __name__ == "__main__":
    main()
