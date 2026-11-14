"""Frame sources: a live SocketCAN interface, or a candump log file played back in real time.

Both run a reader thread that puts (timestamp, can_id, data) or ("error", text) on a queue.
Log format is `candump -L`: `(1700000000.123456) can0 200#0102030405060708`.
"""
import os
import socket
import struct
import threading
import time

CAN_FRAME = struct.Struct("=IB3x8s")
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000


def can_interfaces():
    """Names of the CAN network interfaces (ARPHRD_CAN = 280)."""
    names = []
    try:
        for n in sorted(os.listdir("/sys/class/net")):
            try:
                with open(f"/sys/class/net/{n}/type") as f:
                    if f.read().strip() == "280":
                        names.append(n)
            except OSError:
                pass
    except OSError:
        pass
    return names


def format_log_line(ts: float, iface: str, can_id: int, data: bytes) -> str:
    ident = f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"
    return f"({ts:.6f}) {iface} {ident}#{data.hex().upper()}\n"


def parse_log_line(line: str):
    """(ts, iface, can_id, data) or None for lines that are not CAN frames."""
    parts = line.split()
    if len(parts) < 3 or not parts[0].startswith("("):
        return None
    try:
        ts = float(parts[0].strip("()"))
        ident, _, payload = parts[2].partition("#")
        if payload.startswith("R"):
            payload = ""
        return ts, parts[1], int(ident, 16), bytes.fromhex(payload)
    except ValueError:
        return None


class _Source:
    def __init__(self, out_queue):
        self.q = out_queue
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


class SocketCanSource(_Source):
    def __init__(self, out_queue, iface: str):
        super().__init__(out_queue)
        self.iface = iface
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.bind((iface,))  # raises OSError if the interface doesn't exist
        self.sock.settimeout(0.2)

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    raw = self.sock.recv(CAN_FRAME.size)
                except socket.timeout:
                    continue
                ts = time.time()
                can_id, dlc, data = CAN_FRAME.unpack(raw)
                if can_id & (CAN_ERR_FLAG | CAN_RTR_FLAG):
                    continue
                can_id &= 0x1FFFFFFF if can_id & CAN_EFF_FLAG else 0x7FF
                self.q.put((ts, can_id, data[:dlc]))
        except OSError as e:
            self.q.put(("error", f"{self.iface}: {e}"))
        finally:
            self.sock.close()


class ReplaySource(_Source):
    """Plays a candump -L file back with its original timing (frames get the current time)."""

    def __init__(self, out_queue, path: str, loop: bool = False):
        super().__init__(out_queue)
        with open(path) as f:
            self.frames = [p for p in map(parse_log_line, f) if p]
        if not self.frames:
            raise ValueError(f"{path}: no candump -L frames found")
        self.loop = loop

    def _run(self):
        while not self._stop.is_set():
            t0_log = self.frames[0][0]
            t0 = time.time()
            for ts, _iface, can_id, data in self.frames:
                delay = (ts - t0_log) - (time.time() - t0)
                if delay > 0 and self._stop.wait(delay):
                    return
                self.q.put((time.time(), can_id, data))
            if not self.loop:
                self.q.put(("error", "replay finished"))
                return
