"""Serial reader thread: bytes -> frames -> Bus -> replies. Talks to the GUI via a queue."""
import queue
import threading
import time

import serial

from .sbms_protocol import FrameParser, fmt_raw


class SerialLink:
    def __init__(self, bus, events: queue.Queue):
        self.bus = bus
        self.events = events
        self.port = None
        self.thread = None
        self.running = False
        self.wlock = threading.Lock()

    def emit(self, kind, text, node=None):
        self.events.put((kind, time.time(), text, node))

    def open(self, port, baud=38400):
        self.close()
        self.port = serial.serial_for_url(port, baudrate=baud, bytesize=8, parity="N",
                                          stopbits=1, timeout=0.02)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.emit("status", f"connected to {port} @ {baud} 8N1")

    def close(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
            self.thread = None
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None
            self.emit("status", "disconnected")

    @property
    def connected(self):
        return self.port is not None and self.running

    def _write(self, data):
        with self.wlock:
            if self.port:
                self.port.write(data)
                self.port.flush()

    def _run(self):
        parser = FrameParser()
        while self.running:
            try:
                data = self.port.read(256)
            except Exception as e:
                self.emit("err", f"serial error: {e}")
                self.running = False
                break
            if not data:
                continue
            for kind, item in parser.feed(data):
                if kind == "junk":
                    if item != b"\r":           # lone CR before every master frame
                        self.emit("junk", fmt_raw(item))
                    continue
                f = item
                tag = "" if f.crc_ok else "  [BAD CRC]"
                if f.dest == 0x00:
                    # Frames to the master: our own echo, or another node's reply.
                    self.emit("echo", f"{f.describe()}{tag}")
                    continue
                self.emit("rx" if f.crc_ok else "err", f"{f.describe()}{tag}   {fmt_raw(f.raw)}")
                rep = self.bus.handle(f)
                for n in rep.notes:
                    self.emit("note", n, rep.node)
                if rep.data:
                    self._send(rep)

    def _send(self, rep):
        def go():
            try:
                self._write(rep.data)
            except Exception as e:
                self.emit("err", f"write failed: {e}")
                return
            self.emit("tx", fmt_raw(rep.data), rep.node)
        if rep.delay_s > 0:
            threading.Timer(rep.delay_s, go).start()
        else:
            go()
