import os
import queue
import tempfile
import unittest

from .bus import ReplaySource, format_log_line, parse_log_line
from .protocol import BY_ID, MESSAGES, decode, encode
from .sim import frames


def sig(can_id, name):
    return next(s for s in BY_ID[can_id].signals if s.name == name)


class BitLayoutTest(unittest.TestCase):
    def test_motorola_byte_positions(self):
        data = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
        self.assertEqual(sig(0x200, "Pack_Temp_Low").extract(data), 0x11)         # byte 0
        self.assertEqual(sig(0x200, "Pack_Voltage").extract(data), 0x5566)        # bytes 4-5, MSB first
        self.assertEqual(sig(0x200, "Pack_Current").extract(data), 0x7788)
        self.assertEqual(sig(0x201, "Error_Flags").extract(data), 0x22334455)     # bytes 1-4
        self.assertEqual(sig(0x201, "DI_2017").extract(data), 0x667788)           # bits 40-63
        self.assertEqual(sig(0x204, "Remaining_Charge").extract(data), 0x112233)  # bits 0-23
        self.assertEqual(sig(0x204, "SoC_Set_Echo").extract(data), 0x66)          # bits 40-47
        self.assertEqual(sig(0x10A, "DI_2072").extract(data), 0x88)               # byte 7
        self.assertEqual(sig(0x119, "SoC_Set").extract(data), 0x11)               # byte 0

    def test_intel_byte_positions(self):
        data = bytes([0x01, 0xFF, 0xFE, 0x34, 0x12, 0x78, 0x56, 0x00])
        self.assertEqual(sig(0x2FF, "Charger_Enabled").extract(data), 0x01)
        self.assertEqual(sig(0x2FF, "Post_Processor_1").extract(data), 0x2FF)     # byte 1 + 2 LSBs of byte 2
        self.assertEqual(sig(0x2FF, "Const_bits18").extract(data), 0xFE >> 2)       # rest of byte 2
        self.assertEqual(sig(0x2FF, "Charger_Max_DC_V").extract(data), 0x1234)    # bytes 3-4, LSB first
        self.assertEqual(sig(0x2FF, "Charger_Max_DC_I").extract(data), 0x5678)

    def test_signed(self):
        s = sig(0x200, "Pack_Current")
        self.assertEqual(s.physical(0xFFFF), -1)
        self.assertEqual(s.physical(0x7FFF), 32767)
        self.assertEqual(sig(0x200, "SoC").physical(0xFF), 255)

    def test_short_frame(self):
        self.assertIsNone(sig(0x200, "Pack_Current").extract(bytes(6)))
        self.assertEqual(sig(0x200, "Pack_Temp_Low").extract(bytes([5])), 5)

    def test_signals_fit_and_do_not_overlap(self):
        for m in MESSAGES:
            used = 0
            for s in m.signals:
                self.assertLessEqual(s.start + s.length, 64, (hex(m.can_id), s.name))
                data = bytearray(8)
                s.insert(data, -1)  # all ones: the payload bits this signal really occupies
                mask = int.from_bytes(data, "big")
                self.assertFalse(used & mask, (hex(m.can_id), s.name))
                used |= mask

    def test_encode_decode_round_trip(self):
        for m in MESSAGES:
            vals = {s.name: (i * 37 + 5) % (1 << min(s.length, 7)) for i, s in enumerate(m.signals)}
            data = encode(m.can_id, vals)
            for s, raw in decode(m.can_id, data):
                self.assertEqual(s.physical(raw), vals[s.name], (hex(m.can_id), s.name))


class LogTest(unittest.TestCase):
    def test_line_round_trip(self):
        line = format_log_line(1700000000.5, "can0", 0x2FF, bytes([1, 2, 3]))
        self.assertEqual(line, "(1700000000.500000) can0 2FF#010203\n")
        self.assertEqual(parse_log_line(line), (1700000000.5, "can0", 0x2FF, bytes([1, 2, 3])))
        self.assertIsNone(parse_log_line("garbage"))

    def test_replay_of_sim_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.log")
            with open(path, "w") as f:
                for t, can_id, data in frames(0.2):
                    f.write(format_log_line(1000 + t, "vcan0", can_id, data))
            q = queue.SimpleQueue()
            src = ReplaySource(q, path)
            src.start()
            src._thread.join(2)
            items = []
            while not q.empty():
                items.append(q.get())
            self.assertEqual(items[-1], ("error", "replay finished"))
            ids = {i[1] for i in items[:-1]}
            self.assertEqual(ids, {m.can_id for m in MESSAGES if m.interval_ms})


if __name__ == "__main__":
    unittest.main()
