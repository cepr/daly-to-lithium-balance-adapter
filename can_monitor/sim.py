"""Fake BMCU traffic for testing the monitor without the bike.

  python3 -m can_monitor.sim --iface vcan0              # send live on a (virtual) CAN interface
  python3 -m can_monitor.sim --out demo.log --seconds 60  # write a candump -L file to replay

Values are made up (raw integers in plausible ranges); they don't mean the real scaling.
"""
import argparse
import math
import os
import socket
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "can_monitor"

from .bus import CAN_FRAME, format_log_line  # noqa: E402
from .protocol import MESSAGES, encode  # noqa: E402


def values(t: float) -> dict:
    w = math.sin(t / 5)
    cur = round(60 * math.sin(t / 3))
    return {
        "Pack_Temp_Low": 22, "Pack_Temp_High": 27 + round(2 * w), "SoC": 80 - int(t / 30) % 50,
        "SoH": 97, "Pack_Voltage": 3200 + round(40 * w), "Pack_Current": cur,
        "System_State": 3, "Error_Flags": (1 << 4) if int(t / 10) % 3 == 0 else 0, "DI_2017": 0,
        "Const_2006": 3, "Const_2016": 1, "Allowed_disch_current": 300, "Allowed_regen_current": 60,
        "Average_Cell_Voltage": 4000 + round(50 * w), "Cell_with_lowest_voltage": 17,
        "Cell_with_highest_voltage": 62, "Cell_Voltage_Min": 3985 + round(50 * w),
        "Cell_Voltage_Max": 4012 + round(50 * w),
        "Remaining_Charge": 5000, "Pack_C_Rate": abs(cur) // 10, "SoC_Set_Echo": 0, "Charger_Max_DC_I": 0,
        "Remaining_Energy": 1600, "Deviation_Voltage": 27, "Amp_H_Sum": 1234, "Cells_Connected": 80,
        "Pack_resistance": 85, "LMU_Comm_Warnings": 0, "IO_State": 0b101,
        "DI_2053": 0, "DI_2055": 0, "DI_2057": 0, "Cell_Voltage_Sum": 3200 + round(40 * w),
        "Charger_Enabled": 0, "Post_Processor_1": 0, "Charger_Max_DC_V": 3360,
    }


def frames(duration: float):
    """Yields (t, can_id, data) for the periodic BMCU messages, in time order."""
    periodic = [(m.can_id, m.interval_ms / 1000) for m in MESSAGES if m.interval_ms]
    tick = 0.05
    for n in range(int(duration / tick)):
        t = n * tick
        v = values(t)
        for can_id, period in periodic:
            if n % round(period / tick) == 0:
                yield t, can_id, encode(can_id, v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--iface", help="CAN interface to send on, e.g. vcan0")
    g.add_argument("--out", help="candump -L file to write")
    ap.add_argument("--seconds", type=float, default=3600)
    a = ap.parse_args()

    if a.out:
        t0 = time.time()
        with open(a.out, "w") as f:
            for t, can_id, data in frames(a.seconds):
                f.write(format_log_line(t0 + t, "vcan0", can_id, data))
        return

    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((a.iface,))
    t0 = time.monotonic()
    try:
        for t, can_id, data in frames(a.seconds):
            delay = t0 + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sock.send(CAN_FRAME.pack(can_id, len(data), data))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
