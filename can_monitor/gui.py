"""Energica Ego battery CAN monitor.

Run:  python3 -m can_monitor.gui [--iface can0 | --replay file.log [--loop]]   (from the project folder)
  or: python3 can_monitor/gui.py ...
"""
import argparse
import collections
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "can_monitor"

from .bus import ReplaySource, SocketCanSource, can_interfaces, format_log_line  # noqa: E402
from .protocol import BY_ID, MESSAGES, RX, TX  # noqa: E402

REFRESH_MS = 100
HISTORY = 6000  # samples kept per signal (5 min at 50 ms)
WINDOWS = {"10 s": 10, "1 min": 60, "5 min": 300}
OTHER = "other"


class MsgState:
    __slots__ = ("count", "first", "last", "period", "data")

    def __init__(self):
        self.count = 0
        self.first = self.last = None
        self.period = None  # smoothed period, s
        self.data = b""

    def update(self, ts, data):
        if self.last is not None:
            dt = ts - self.last
            self.period = dt if self.period is None else self.period + 0.1 * (dt - self.period)
        else:
            self.first = ts
        self.last = ts
        self.count += 1
        self.data = data


class Plot(tk.Canvas):
    PAD_L, PAD_R, PAD_T, PAD_B = 64, 12, 12, 24

    def __init__(self, parent):
        super().__init__(parent, background="white", highlightthickness=0, height=260)

    def draw(self, samples, window, unit, now):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        x0, x1, y0, y1 = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        if x1 - x0 < 20 or y1 - y0 < 20:
            return
        pts = [(t, v) for t, v in samples if t >= now - window]
        self.create_rectangle(x0, y0, x1, y1, outline="#bbb")
        for i, frac in enumerate((0, 0.25, 0.5, 0.75, 1)):
            x = x0 + frac * (x1 - x0)
            self.create_line(x, y0, x, y1, fill="#eee")
            self.create_text(x, y1 + 4, anchor="n", fill="#666", font=("TkDefaultFont", 8),
                             text=f"-{window * (1 - frac):g} s" if frac < 1 else "now")
        if not pts:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="no data", fill="#999")
            return
        lo = min(v for _, v in pts)
        hi = max(v for _, v in pts)
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        span = hi - lo
        lo, hi = lo - 0.05 * span, hi + 0.05 * span
        for frac in (0, 0.25, 0.5, 0.75, 1):
            y = y1 - frac * (y1 - y0)
            self.create_line(x0, y, x1, y, fill="#eee")
            v = lo + frac * (hi - lo)
            self.create_text(x0 - 4, y, anchor="e", fill="#666", font=("TkDefaultFont", 8),
                             text=f"{v:.4g} {unit}".rstrip())

        def xy(t, v):
            return (x0 + (t - (now - window)) / window * (x1 - x0),
                    y1 - (v - lo) / (hi - lo) * (y1 - y0))

        coords = []
        prev_v = None
        for t, v in pts:  # step plot: a value holds until the next frame
            x, y = xy(t, v)
            if prev_v is not None:
                coords += [x, xy(t, prev_v)[1]]
            coords += [x, y]
            prev_v = v
        coords += [x1, coords[-1]]
        if len(coords) >= 4:
            self.create_line(*coords, fill="#1f5fbf", width=2)
        else:
            self.create_oval(coords[0] - 2, coords[1] - 2, coords[0] + 2, coords[1] + 2, fill="#1f5fbf")


class App(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title("Energica Ego — battery CAN monitor")
        self.geometry("1280x760")
        self.q = queue.SimpleQueue()
        self.source = None
        self.source_name = ""
        self.record = None
        self.states = {}
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self.selected = None  # (can_id, signal index)
        self.rate_count, self.rate_t, self.rate = 0, time.time(), 0.0

        self._build()
        self.after(REFRESH_MS, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        if args.replay:
            self._start_replay(args.replay, args.loop)
        elif args.iface:
            self.v_iface.set(args.iface)
            self._connect()

    # ---- layout ---------------------------------------------------------------------------
    def _build(self):
        bar = ttk.Frame(self, padding=(6, 6, 6, 2))
        bar.pack(fill="x")
        ttk.Label(bar, text="Interface").pack(side="left")
        ifaces = can_interfaces()
        self.v_iface = tk.StringVar(value=ifaces[0] if ifaces else "can0")
        self.cb_iface = ttk.Combobox(bar, textvariable=self.v_iface, values=ifaces, width=10,
                                     postcommand=lambda: self.cb_iface.configure(values=can_interfaces()))
        self.cb_iface.pack(side="left", padx=4)
        self.b_conn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.b_conn.pack(side="left")
        ttk.Button(bar, text="Replay log…", command=self._ask_replay).pack(side="left", padx=(12, 0))
        self.v_loop = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="loop", variable=self.v_loop).pack(side="left", padx=4)
        self.v_rec = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Record…", variable=self.v_rec, command=self._toggle_record
                        ).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Clear", command=self._clear).pack(side="left", padx=12)
        self.status = ttk.Label(bar, text="disconnected", anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(pane)
        cols = ("value", "raw", "count", "period", "age")
        self.tree = ttk.Treeview(left, columns=cols, selectmode="browse")
        for c, text, width, anchor in (("#0", "Message / signal", 260, "w"), ("value", "Value", 220, "w"),
                                       ("raw", "Raw", 170, "w"), ("count", "Frames", 70, "e"),
                                       ("period", "Period (ms)", 100, "e"), ("age", "Age", 70, "e")):
            self.tree.heading(c, text=text, anchor=anchor)
            self.tree.column(c, width=width, anchor=anchor, stretch=c in ("value", "#0"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("msg", font=("TkDefaultFont", 10, "bold"))
        self.tree.tag_configure("section", font=("TkDefaultFont", 10, "bold"), background="#e8eef8")
        self.tree.tag_configure("never", foreground="#9e9e9e")
        self.tree.tag_configure("stale", foreground="#c62828")
        self.tree.tag_configure("const", foreground="#9e9e9e")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=(6, 0, 0, 0))
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.sig_title = ttk.Label(top, text="Select a signal to plot it", font=("TkDefaultFont", 11, "bold"))
        self.sig_title.pack(side="left")
        self.v_window = tk.StringVar(value="1 min")
        ttk.Combobox(top, textvariable=self.v_window, values=list(WINDOWS), width=6, state="readonly"
                     ).pack(side="right")
        self.sig_info = ttk.Label(right, text="", justify="left")
        self.sig_info.pack(fill="x", pady=4)
        right.bind("<Configure>", lambda e: self.sig_info.configure(wraplength=max(e.width - 12, 100)))
        self.plot = Plot(right)
        self.plot.pack(fill="both", expand=True)
        self.sig_stats = ttk.Label(right, text="", justify="left", font=("TkFixedFont", 9))
        self.sig_stats.pack(fill="x", pady=4)
        pane.add(right, weight=2)

        self._populate()

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        for direction in (TX, RX):
            sec = self.tree.insert("", "end", iid=direction, text="Sent by the BMCU" if direction == TX
                                   else "Received by the BMCU", open=True, tags=("section",))
            for m in MESSAGES:
                if m.direction != direction:
                    continue
                mid = f"{m.can_id:X}"
                self.tree.insert(sec, "end", iid=mid, text=f"0x{m.can_id:03X}  {m.name}", open=True,
                                 values=("", "", 0, f"— / {m.interval_ms}" if m.interval_ms else "—", ""),
                                 tags=("msg", "never"))
                for i, s in enumerate(m.signals):
                    self.tree.insert(mid, "end", iid=f"{mid}/{i}", text=s.name,
                                     tags=("const",) if s.constant else ("never",))
        self.tree.insert("", "end", iid=OTHER, text="Other IDs", open=True, tags=("section",))

    # ---- sources --------------------------------------------------------------------------
    def _toggle(self):
        if self.source:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        iface = self.v_iface.get().strip()
        try:
            src = SocketCanSource(self.q, iface)
        except OSError as e:
            messagebox.showerror("CAN", f"Can't open {iface}: {e}\n\nIs it up? "
                                 f"sudo ip link set {iface} up type can bitrate 500000")
            return
        self._set_source(src, iface)

    def _ask_replay(self):
        path = filedialog.askopenfilename(title="candump -L log",
                                          filetypes=[("candump log", "*.log *.txt"), ("All", "*")])
        if path:
            self._start_replay(path, self.v_loop.get())

    def _start_replay(self, path, loop):
        try:
            src = ReplaySource(self.q, path, loop)
        except (OSError, ValueError) as e:
            messagebox.showerror("Replay", str(e))
            return
        self._set_source(src, f"replay {os.path.basename(path)}")

    def _set_source(self, src, name):
        self._disconnect()
        self._clear()
        self.source, self.source_name = src, name
        src.start()
        self.b_conn.configure(text="Disconnect")

    def _disconnect(self):
        if self.source:
            self.source.stop()
            self.source = None
        self.b_conn.configure(text="Connect")

    def _toggle_record(self):
        if self.record:
            self.record.close()
            self.record = None
            return
        path = filedialog.asksaveasfilename(title="Record to candump -L file", defaultextension=".log",
                                            initialfile=time.strftime("ego-can-%Y%m%d-%H%M%S.log"))
        if not path:
            self.v_rec.set(False)
            return
        self.record = open(path, "w")

    def _clear(self):
        self.states.clear()
        self.history.clear()
        for iid in self.tree.get_children(OTHER):
            self.tree.delete(iid)
        self._populate_reset()

    def _populate_reset(self):
        for m in MESSAGES:
            mid = f"{m.can_id:X}"
            self.tree.item(mid, values=("", "", 0, f"— / {m.interval_ms}" if m.interval_ms else "—", ""),
                           tags=("msg", "never"))
            for i, s in enumerate(m.signals):
                self.tree.item(f"{mid}/{i}", values=("", ""), tags=("const",) if s.constant else ("never",))

    # ---- update loop ----------------------------------------------------------------------
    def _tick(self):
        try:
            self._drain()
            self._refresh()
        finally:
            self.after(REFRESH_MS, self._tick)

    def _drain(self):
        iface = self.source_name.split()[0] if self.source_name else "can0"
        for _ in range(20000):
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "error":
                self._disconnect()
                self.source_name = item[1]
                continue
            ts, can_id, data = item
            self.rate_count += 1
            st = self.states.get(can_id)
            if st is None:
                st = self.states[can_id] = MsgState()
            st.update(ts, data)
            if self.record:
                self.record.write(format_log_line(ts, iface, can_id, data))
            msg = BY_ID.get(can_id)
            if msg:
                for i, s in enumerate(msg.signals):
                    raw = s.extract(data)
                    if raw is not None and not s.constant:
                        self.history[(can_id, i)].append((ts, s.physical(raw)))

    def _refresh(self):
        now = time.time()
        if now - self.rate_t >= 1:
            self.rate = self.rate_count / (now - self.rate_t)
            self.rate_count, self.rate_t = 0, now
        state = self.source_name if self.source else (self.source_name or "disconnected")
        rec = "  ● REC" if self.record else ""
        self.status.configure(text=f"{state}   {self.rate:.0f} frames/s{rec}")

        for can_id, st in self.states.items():
            msg = BY_ID.get(can_id)
            age = now - st.last
            per = f"{st.period * 1000:.0f}" if st.period else "—"
            if msg is None:
                iid = f"x{can_id:X}"
                if not self.tree.exists(iid):
                    self.tree.insert(OTHER, "end", iid=iid, text=f"0x{can_id:03X}")
                self.tree.item(iid, values=(st.data.hex(" ").upper(), "", st.count, per, f"{age:.1f} s"))
                continue
            mid = f"{can_id:X}"
            expected = msg.interval_ms / 1000 if msg.interval_ms else None
            stale = expected is not None and age > max(3 * expected, 0.5)
            tag = "stale" if stale else ""
            self.tree.item(mid, values=(st.data.hex(" ").upper(), f"DLC {len(st.data)}", st.count,
                                        f"{per} / {msg.interval_ms}" if msg.interval_ms else per,
                                        f"{age:.1f} s"), tags=("msg", tag))
            for i, s in enumerate(msg.signals):
                raw = s.extract(st.data)
                if raw is None:
                    vals, tags = ("(frame too short)", ""), ("stale",)
                else:
                    vals = (s.format(raw), f"0x{raw:0{(s.length + 3) // 4}X}  ({raw})")
                    tags = ("const",) if s.constant else (tag,)
                self.tree.item(f"{mid}/{i}", values=vals, tags=tags)

        self._draw_selected(now)

    # ---- selected signal ------------------------------------------------------------------
    def _on_select(self, _ev=None):
        sel = self.tree.selection()
        self.selected = None
        if sel and "/" in sel[0]:
            mid, idx = sel[0].split("/")
            self.selected = (int(mid, 16), int(idx))
        self._describe()
        self._draw_selected(time.time())

    def _describe(self):
        if not self.selected:
            self.sig_title.configure(text="Select a signal to plot it")
            self.sig_info.configure(text="")
            return
        can_id, idx = self.selected
        m = BY_ID[can_id]
        s = m.signals[idx]
        order = "Intel (little-endian)" if s.intel else "Motorola (big-endian)"
        lines = [f"0x{can_id:03X} {m.name} · start bit {s.start}, {s.length} bits, {order}",
                 f"data identifier {s.data_id}" + (" · constant" if s.constant else "")
                 + (" · signed" if s.signed else "")
                 + (f" · scale {s.scale:g}, offset {s.offset:g}" if (s.scale, s.offset) != (1, 0) else " · raw value")]
        if s.note:
            lines.append(s.note)
        self.sig_title.configure(text=s.name)
        self.sig_info.configure(text="\n".join(lines))

    def _draw_selected(self, now):
        if not self.selected:
            self.plot.delete("all")
            self.sig_stats.configure(text="")
            return
        can_id, idx = self.selected
        s = BY_ID[can_id].signals[idx]
        samples = self.history.get((can_id, idx), ())
        window = WINDOWS[self.v_window.get()]
        self.plot.draw(samples, window, s.unit, now)
        vals = [v for t, v in samples if t >= now - window]
        if vals:
            self.sig_stats.configure(text=f"last {vals[-1]:g}   min {min(vals):g}   max {max(vals):g}   "
                                          f"mean {sum(vals) / len(vals):.4g}   n {len(vals)}")
        else:
            self.sig_stats.configure(text="")

    def _quit(self):
        self._disconnect()
        if self.record:
            self.record.close()
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="Energica Ego battery CAN monitor")
    ap.add_argument("--iface", help="SocketCAN interface to open at start, e.g. can0")
    ap.add_argument("--replay", help="candump -L log file to play back")
    ap.add_argument("--loop", action="store_true", help="loop the replay")
    App(ap.parse_args()).mainloop()


if __name__ == "__main__":
    main()
