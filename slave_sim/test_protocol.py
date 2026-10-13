import unittest

from .sbms_protocol import FrameParser, build, parse_frame, sbms_crc
from .slave_model import Bus


class CrcTest(unittest.TestCase):
    # Example frames from LithiumBalance_sBMS_RS485_Protocol.md §6.1 and §7.3
    CASES = {
        "iF00001": "C3C6",
        "!0001i": "98DC",
        "iF00084": "42A8",
        "q01007D0F": "DB1C",
        "r00017D0F0CE40CE60CE10CE80CE20CE30CE50CE7": "1C38",
        "q01007E54": "4254",
        "r00017E541716181A7A": "DE5A",
    }

    def test_doc_examples(self):
        for body, crc in self.CASES.items():
            self.assertEqual(sbms_crc(body.encode()).hex().upper(), crc, body)

    def test_crc_never_contains_reserved_bytes(self):
        for i in range(5000):
            c = sbms_crc(f"q{i:04X}".encode())
            self.assertFalse(set(c) & {0x00, 0x0D, 0x1B})


class ParserTest(unittest.TestCase):
    def test_round_trip_with_noise(self):
        p = FrameParser()
        stream = b"\x00zz\r" + b"\r" + build("q", 1, 0, "7D0F") + b"\r" + build("w", 2, 0, "0020AC")
        frames = [x for k, x in p.feed(stream) if k == "frame"]
        self.assertEqual([(f.cmd, f.dest, f.payload, f.crc_ok) for f in frames],
                         [("q", 1, "7D0F", True), ("w", 2, "0020AC", True)])

    def test_bad_crc(self):
        raw = bytearray(build("q", 1, 0, "7D0F"))
        raw[-2] ^= 1
        self.assertFalse(parse_frame(bytes(raw)).crc_ok)


def ask(bus, cmd, dest, payload):
    rep = bus.handle(parse_frame(build(cmd, dest, 0, payload)))
    return parse_frame(rep.data) if rep.data else None


class BusTest(unittest.TestCase):
    def test_enumeration_and_poll(self):
        bus = Bus(3)
        self.assertEqual(ask(bus, "i", 0xF0, "01").describe(), "! 00 01 i")
        self.assertEqual(ask(bus, "i", 0xF0, "02").src, 2)
        self.assertEqual(ask(bus, "i", 0xF0, "83").src, 3)
        self.assertTrue(bus.slaves[2].last)
        self.assertIsNone(ask(bus, "i", 0xF0, "04"))
        bus.slaves[1].cells = [3300, 3302, 3297, 3304, 3298, 3299, 3301, 3303]
        r = ask(bus, "q", 2, "7D0F")
        self.assertEqual(r.payload, "7D0F0CE40CE60CE10CE80CE20CE30CE50CE7")
        bus.slaves[1].temps = [23, 22, 24, -5]
        self.assertEqual(ask(bus, "q", 2, "7E54").payload, "7E541716187A00")   # T4 defaults to none
        bus.slaves[1].temp_codes[1] = "none"
        bus.slaves[1].temp_codes[3] = "OK"
        self.assertEqual(ask(bus, "q", 2, "7E54").payload, "7E54177A18FB00")

    def test_round_restart_resets_chain(self):
        bus = Bus(2)
        bus.preaddress()
        self.assertEqual(ask(bus, "i", 0xF0, "01").src, 1)
        self.assertIsNone(bus.slaves[1].address)

    def test_offline_breaks_chain(self):
        bus = Bus(3)
        bus.slaves[1].online = False
        self.assertIsNotNone(ask(bus, "i", 0xF0, "01"))
        self.assertIsNone(ask(bus, "i", 0xF0, "02"))

    def test_write_and_codes(self):
        bus = Bus(1)
        bus.preaddress()
        self.assertEqual(ask(bus, "w", 1, "0020AC").describe(), "! 00 01 w")
        self.assertTrue(bus.slaves[0].in_boot)
        s = bus.slaves[0]
        s.cells = [3000] * 8
        s.cell_codes[0:4] = ["not fitted", "fault", "flagged", "OK"]
        self.assertTrue(ask(bus, "q", 1, "7D07").payload.endswith("200040008BB80BB8"))
        s.nak = True
        self.assertEqual(ask(bus, "q", 1, "7D0F").describe(), "# 00 01 qD")

    def test_dev21(self):
        bus = Bus(1)
        self.assertIsNone(ask(bus, "q", 0x21, "7D00"))
        bus.dev21.enabled = True
        bus.dev21.regs[0x7D0] = 0x5A
        self.assertEqual(ask(bus, "q", 0x21, "7D00").payload, "7D005A")

    def test_forwarded_request_from_pc(self):
        # Diagnostic PC request forwarded by the BMCU (SRC = FE, lower-case hex address).
        bus = Bus(2)
        bus.preaddress()
        bus.load_config_pattern()
        r = parse_frame(bus.handle(parse_frame(build("q", 2, 0xFE, "0e0E"))).data)
        self.assertEqual(r.describe(), "r FE 02 0e0E" + b"abcdefghijklmno".hex().upper())
        r = parse_frame(bus.handle(parse_frame(build("q", 2, 0xFE, "0000"))).data)
        self.assertEqual(r.payload, "000002")
        bus.load_default_config()
        r = parse_frame(bus.handle(parse_frame(build("q", 2, 0xFE, "0e0E"))).data)
        self.assertEqual(bytes.fromhex(r.payload[4:])[::-1], b"SIM000000000002")

    def test_profile_keeps_config(self):
        bus = Bus(2)
        bus.preaddress()
        ask_fe = lambda cmd, payload: bus.handle(parse_frame(build(cmd, 1, 0xFE, payload)))
        ask_fe("w", "046c01010101010101003C0A106832")             # Cell 8 off, as written by the PC
        other = Bus(2)
        other.from_dict(bus.to_dict())
        self.assertEqual(other.slaves[0].regs[0x046:0x053], bytes.fromhex("01010101010101003C0A106832"))
        self.assertEqual(other.slaves[1].regs[0x04E:0x053], bytes([55, 10, 0x10, 0x31, 25]))
        self.assertEqual(other.slaves[1].regs[0x0E0:0x0EF][::-1], b"SIM000000000002")


if __name__ == "__main__":
    unittest.main()
