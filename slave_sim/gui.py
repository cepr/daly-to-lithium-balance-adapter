"""s-BMS slave-board simulator GUI.

Run:  python3 -m slave_sim.gui      (from the project folder)
  or: python3 slave_sim/gui.py
"""
import json
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "slave_sim"

from serial.tools import list_ports  # noqa: E402

from .serial_link import SerialLink  # noqa: E402
from .slave_model import CELL_CODES, N_CELLS, N_TEMPS, TEMP_CODES, Bus  # noqa: E402

COLUMNS = 5
LOG_COLORS = {"rx": "#1f5fbf", "tx": "#1a8a2e", "err": "#c62828", "junk": "#9e6a00",
              "note": "#6a1b9a", "status": "#000000", "echo": "#9e9e9e"}


def _int(s, default=None):
    try:
        return int(str(s).strip(), 0)
    except ValueError:
        return default


class SlaveCard(ttk.LabelFrame):
    def __init__(self, parent, app, slave):
        super().__init__(parent, text=f"Board {slave.index + 1}", padding=4)
        self.app = app
        self.s = slave
        self._sync = False

        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=6, sticky="ew")
        self.led = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.led_id = self.led.create_oval(2, 2, 12, 12, fill="grey", outline="")
        self.led.pack(side="left")
        self.hdr = ttk.Label(top, text="", width=18, font=("TkDefaultFont", 9, "bold"))
        self.hdr.pack(side="left", padx=4)
        self.v_online = tk.BooleanVar(value=slave.online)
        ttk.Checkbutton(top, text="online", variable=self.v_online,
                        command=lambda: self._set("online", self.v_online.get())).pack(side="right")

        # Cells: 2 columns x 4 rows of [label spinbox code]
        self.v_cells, self.v_ccodes = [], []
        for i in range(N_CELLS):
            r, c = 1 + i % 4, (i // 4) * 3
            ttk.Label(self, text=f"C{i + 1}").grid(row=r, column=c, sticky="e")
            v = tk.StringVar(value=str(slave.cells[i]))
            v.trace_add("write", lambda *_, i=i: self._cell(i))
            ttk.Spinbox(self, from_=0, to=5000, increment=10, width=5, textvariable=v
                        ).grid(row=r, column=c + 1, padx=1, pady=1)
            cv = tk.StringVar(value=slave.cell_codes[i])
            cv.trace_add("write", lambda *_, i=i: self._ccode(i))
            ttk.Combobox(self, values=list(CELL_CODES), textvariable=cv, width=8,
                         state="readonly").grid(row=r, column=c + 2, padx=(0, 4))
            self.v_cells.append(v)
            self.v_ccodes.append(cv)

        ttk.Separator(self).grid(row=5, column=0, columnspan=6, sticky="ew", pady=3)
        self.v_temps, self.v_tcodes = [], []
        for i in range(N_TEMPS):
            r, c = 6 + i % 2, (i // 2) * 3
            ttk.Label(self, text=f"T{i + 1}").grid(row=r, column=c, sticky="e")
            v = tk.StringVar(value=str(slave.temps[i]))
            v.trace_add("write", lambda *_, i=i: self._temp(i))
            ttk.Spinbox(self, from_=-40, to=125, increment=1, width=5, textvariable=v
                        ).grid(row=r, column=c + 1, padx=1, pady=1)
            cv = tk.StringVar(value=slave.temp_codes[i])
            cv.trace_add("write", lambda *_, i=i: self._tcode(i))
            ttk.Combobox(self, values=list(TEMP_CODES), textvariable=cv, width=8,
                         state="readonly").grid(row=r, column=c + 2, padx=(0, 4))
            self.v_temps.append(v)
            self.v_tcodes.append(cv)

        ttk.Separator(self).grid(row=8, column=0, columnspan=6, sticky="ew", pady=3)
        f = ttk.Frame(self)
        f.grid(row=9, column=0, columnspan=6, sticky="ew")
        self.v_badcrc = tk.BooleanVar(value=slave.bad_crc)
        self.v_nak = tk.BooleanVar(value=slave.nak)
        ttk.Checkbutton(f, text="bad CRC", variable=self.v_badcrc,
                        command=lambda: self._set("bad_crc", self.v_badcrc.get())).pack(side="left")
        ttk.Checkbutton(f, text="NAK", variable=self.v_nak,
                        command=lambda: self._set("nak", self.v_nak.get())).pack(side="left")
        ttk.Label(f, text="delay ms").pack(side="left", padx=(6, 1))
        self.v_delay = tk.StringVar(value=str(slave.delay_ms))
        self.v_delay.trace_add("write", lambda *_: self._set("delay_ms", max(0, _int(self.v_delay.get(), 0))))
        ttk.Spinbox(f, from_=0, to=1000, increment=10, width=5, textvariable=self.v_delay).pack(side="left")

        f2 = ttk.Frame(self)
        f2.grid(row=10, column=0, columnspan=6, sticky="ew")
        ttk.Label(f2, text="7E0:").pack(side="left")
        self.v_info = []
        for k in range(2):
            v = tk.StringVar(value=str(slave.info[k]))
            v.trace_add("write", lambda *_, k=k: self._info(k))
            ttk.Entry(f2, textvariable=v, width=7).pack(side="left", padx=1)
            self.v_info.append(v)
        self.v_silent = tk.BooleanVar(value=slave.silent_in_boot)
        ttk.Checkbutton(f2, text="silent in boot", variable=self.v_silent,
                        command=lambda: self._set("silent_in_boot", self.v_silent.get())).pack(side="right")
        self.cnt = ttk.Label(self, text="", foreground="#555")
        self.cnt.grid(row=11, column=0, columnspan=6, sticky="w")

    # ---- GUI -> model ----
    def _set(self, attr, val):
        if self._sync or val is None:
            return
        with self.app.bus.lock:
            setattr(self.s, attr, val)

    def _setitem(self, attr, i, val):
        if self._sync or val is None:
            return
        with self.app.bus.lock:
            getattr(self.s, attr)[i] = val

    def _cell(self, i):
        self._setitem("cells", i, _int(self.v_cells[i].get()))

    def _ccode(self, i):
        self._setitem("cell_codes", i, self.v_ccodes[i].get())

    def _temp(self, i):
        v = _int(self.v_temps[i].get())
        self._setitem("temps", i, None if v is None else max(-128, min(127, v)))

    def _tcode(self, i):
        self._setitem("temp_codes", i, self.v_tcodes[i].get())

    def _info(self, k):
        self._setitem("info", k, _int(self.v_info[k].get()))

    # ---- model -> GUI ----
    def load_values(self):
        """Copy every editable value from the model into the widgets."""
        self._sync = True
        try:
            s = self.s
            for i in range(N_CELLS):
                self.v_cells[i].set(str(s.cells[i]))
                self.v_ccodes[i].set(s.cell_codes[i])
            for i in range(N_TEMPS):
                self.v_temps[i].set(str(s.temps[i]))
                self.v_tcodes[i].set(s.temp_codes[i])
            for k in range(2):
                self.v_info[k].set(str(s.info[k]))
            self.v_online.set(s.online)
            self.v_badcrc.set(s.bad_crc)
            self.v_nak.set(s.nak)
            self.v_delay.set(str(s.delay_ms))
            self.v_silent.set(s.silent_in_boot)
        finally:
            self._sync = False

    def load_cells(self):
        self._sync = True
        try:
            for i in range(N_CELLS):
                self.v_cells[i].set(str(self.s.cells[i]))
        finally:
            self._sync = False

    def refresh_status(self, now):
        s = self.s
        if s.address is None:
            txt = "unaddressed"
        else:
            txt = f"addr {s.address:02X}" + ("  LAST" if s.last else "")
        if s.in_boot:
            txt += "  BOOT"
        self.hdr.configure(text=txt)
        if not s.responsive:
            col = "#c62828"
        elif now - s.last_activity < 0.25:
            col = "#2ecc40"
        elif s.address is not None:
            col = "#1b5e20"
        else:
            col = "grey"
        self.led.itemconfigure(self.led_id, fill=col)
        self.cnt.configure(text=f"rx {s.rx}   tx {s.tx}")


class Dev21Dialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Device 0x21 registers")
        self.resizable(False, False)
        ttk.Label(self, text="Start reg (hex)").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.v_reg = tk.StringVar(value="7D0")
        ttk.Entry(self, textvariable=self.v_reg, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(self, text="Bytes (hex)").grid(row=1, column=0, sticky="e", padx=4)
        self.v_data = tk.StringVar()
        ttk.Entry(self, textvariable=self.v_data, width=40).grid(row=1, column=1, padx=4)
        b = ttk.Frame(self)
        b.grid(row=2, column=0, columnspan=2, pady=4)
        ttk.Button(b, text="Read 16", command=self.read).pack(side="left", padx=2)
        ttk.Button(b, text="Write", command=self.write).pack(side="left", padx=2)
        ttk.Label(self, text="Polled by the BMCU: 7D0 (8 B), 7D6, 7DA, 7DC (2 B); written: 7DE, 7E0",
                  foreground="#555").grid(row=3, column=0, columnspan=2, padx=4, pady=(0, 4))
        self.read()

    def _reg(self):
        try:
            r = int(self.v_reg.get(), 16)
            if 0 <= r < 0x1000:
                return r
        except ValueError:
            pass
        messagebox.showerror("Device 0x21", "Register must be 000-FFF (hex)", parent=self)
        return None

    def read(self):
        r = self._reg()
        if r is not None:
            with self.app.bus.lock:
                self.v_data.set(self.app.bus.dev21.regs[r:r + 16].hex(" ").upper())

    def write(self):
        r = self._reg()
        if r is None:
            return
        try:
            data = bytes.fromhex(self.v_data.get())
        except ValueError:
            messagebox.showerror("Device 0x21", "Bytes must be hex, e.g. 01 02 A0", parent=self)
            return
        with self.app.bus.lock:
            self.app.bus.dev21.regs[r:r + len(data)] = data[:0x1000 - r]


class App:
    def __init__(self, root):
        self.root = root
        root.title("s-BMS Slave Simulator")
        self.bus = Bus(10)
        self.events = queue.Queue()
        self.link = SerialLink(self.bus, self.events)
        self.cards = []
        self.last_drift = 0.0
        self.rate_t, self.rate_n, self.rate = time.time(), 0, 0.0

        self._build_toolbar()
        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True)
        pw.add(self._build_grid(pw), weight=3)
        pw.add(self._build_log(pw), weight=1)
        self.status = ttk.Label(root, anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.rebuild_cards()
        self.refresh_ports()
        root.update_idletasks()
        w = min(root.winfo_screenwidth() - 40, self.grid_frame.winfo_reqwidth() + 40)
        h = min(root.winfo_screenheight() - 80, self.grid_frame.winfo_reqheight() + 420)
        root.geometry(f"{w}x{h}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.tick)

    # ---- layout ----
    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Port").pack(side="left")
        self.v_port = tk.StringVar()
        self.cb_port = ttk.Combobox(bar, textvariable=self.v_port, width=16)
        self.cb_port.pack(side="left", padx=2)
        ttk.Button(bar, text="⟳", width=2, command=self.refresh_ports).pack(side="left")
        ttk.Label(bar, text="Baud").pack(side="left", padx=(8, 0))
        self.v_baud = tk.StringVar(value="38400")
        ttk.Combobox(bar, textvariable=self.v_baud, width=7,
                     values=["9600", "19200", "38400", "57600", "115200"]).pack(side="left", padx=2)
        self.b_conn = ttk.Button(bar, text="Connect", command=self.toggle_connect)
        self.b_conn.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(bar, text="Boards").pack(side="left")
        self.v_count = tk.StringVar(value="10")
        ttk.Spinbox(bar, from_=1, to=32, width=3, textvariable=self.v_count).pack(side="left", padx=2)
        ttk.Button(bar, text="Apply", command=self.apply_count).pack(side="left")
        ttk.Button(bar, text="Pre-address 1…N", command=self.preaddress).pack(side="left", padx=(6, 2))
        ttk.Button(bar, text="Reset chain", command=self.reset_chain).pack(side="left", padx=2)
        ttk.Button(bar, text="Default config", command=self.default_config).pack(side="left", padx=2)
        ttk.Button(bar, text="Config test pattern", command=self.config_pattern).pack(side="left", padx=2)

        bar = ttk.Frame(self.root, padding=(4, 0, 4, 4))
        bar.pack(fill="x")
        self.v_allc = tk.StringVar(value="3300")
        ttk.Entry(bar, textvariable=self.v_allc, width=5).pack(side="left")
        ttk.Button(bar, text="All cells mV", command=self.set_all_cells).pack(side="left", padx=2)
        self.v_allt = tk.StringVar(value="25")
        ttk.Entry(bar, textvariable=self.v_allt, width=4).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="All temps °C", command=self.set_all_temps).pack(side="left", padx=2)
        self.v_drift = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Drift", variable=self.v_drift).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.v_dev21 = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Device 0x21", variable=self.v_dev21,
                        command=self.toggle_dev21).pack(side="left")
        ttk.Button(bar, text="Regs…", command=lambda: Dev21Dialog(self)).pack(side="left", padx=2)
        self.v_nakcrc = tk.BooleanVar()
        ttk.Checkbutton(bar, text="NAK bad CRC", variable=self.v_nakcrc,
                        command=self.toggle_nakcrc).pack(side="left", padx=4)

        ttk.Button(bar, text="Load…", command=self.load_profile).pack(side="right", padx=2)
        ttk.Button(bar, text="Save…", command=self.save_profile).pack(side="right", padx=2)

    def _build_grid(self, parent):
        outer = ttk.Frame(parent)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        for seq, d in (("<Button-4>", -1), ("<Button-5>", 1)):
            self.canvas.bind_all(seq, lambda e, d=d: self._wheel(e, d))
        self.canvas.bind_all("<MouseWheel>", lambda e: self._wheel(e, -1 if e.delta > 0 else 1))
        return outer

    def _wheel(self, e, d):
        w = e.widget
        if isinstance(w, str) or not str(w).startswith(str(self.canvas)):
            return
        if isinstance(w, (ttk.Spinbox, ttk.Combobox)):
            return
        self.canvas.yview_scroll(d, "units")

    def _build_log(self, parent):
        f = ttk.Frame(parent)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="Traffic", font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=4)
        self.v_pause = tk.BooleanVar()
        self.v_events_only = tk.BooleanVar()
        self.v_echo = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Pause", variable=self.v_pause).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Events/errors only", variable=self.v_events_only).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Show frames to 00 (echo)", variable=self.v_echo).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear", command=lambda: self.log.delete("1.0", "end")).pack(side="right", padx=2)
        ttk.Button(bar, text="Save log…", command=self.save_log).pack(side="right", padx=2)
        self.log = tk.Text(f, height=12, font=("Monospace", 9), wrap="none")
        sb = ttk.Scrollbar(f, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        for k, c in LOG_COLORS.items():
            self.log.tag_configure(k, foreground=c)
        return f

    def rebuild_cards(self):
        for c in self.cards:
            c.destroy()
        self.cards = [SlaveCard(self.grid_frame, self, s) for s in self.bus.slaves]
        for i, c in enumerate(self.cards):
            c.grid(row=i // COLUMNS, column=i % COLUMNS, padx=3, pady=3, sticky="n")

    # ---- actions ----
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports
        if not self.v_port.get() and ports:
            self.v_port.set(ports[0])

    def toggle_connect(self):
        if self.link.connected:
            self.link.close()
            self.b_conn.configure(text="Connect")
            return
        baud = _int(self.v_baud.get())
        if not self.v_port.get() or not baud:
            messagebox.showerror("Connect", "Select a port and a baud rate")
            return
        try:
            self.link.open(self.v_port.get(), baud)
        except Exception as e:
            messagebox.showerror("Connect", str(e))
            return
        self.b_conn.configure(text="Disconnect")

    def apply_count(self):
        n = _int(self.v_count.get())
        if not n or not 1 <= n <= 32:
            messagebox.showerror("Boards", "Number of boards must be 1-32")
            return
        self.bus.set_count(n)
        self.rebuild_cards()

    def preaddress(self):
        self.bus.preaddress()
        self.add_log("status", time.time(), f"boards pre-addressed 01…{len(self.bus.slaves):02X}")

    def default_config(self):
        self.bus.load_default_config()
        self.add_log("status", time.time(), "LMU config registers reset to the defaults")

    def config_pattern(self):
        self.bus.load_config_pattern()
        self.add_log("status", time.time(), "LMU config registers filled with the test pattern")

    def reset_chain(self):
        self.bus.reset_chain()
        self.add_log("status", time.time(), "chain reset: all boards unaddressed")

    def set_all_cells(self):
        v = _int(self.v_allc.get())
        if v is None:
            return
        with self.bus.lock:
            for s in self.bus.slaves:
                s.cells = [v] * N_CELLS
        for c in self.cards:
            c.load_cells()

    def set_all_temps(self):
        v = _int(self.v_allt.get())
        if v is None:
            return
        with self.bus.lock:
            for s in self.bus.slaves:
                s.temps = [max(-128, min(127, v))] * N_TEMPS
        for c in self.cards:
            c.load_values()

    def toggle_dev21(self):
        with self.bus.lock:
            self.bus.dev21.enabled = self.v_dev21.get()

    def toggle_nakcrc(self):
        with self.bus.lock:
            self.bus.ignore_bad_crc = not self.v_nakcrc.get()

    def save_profile(self):
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Profile", "*.json")])
        if fn:
            with open(fn, "w") as f:
                json.dump(self.bus.to_dict(), f, indent=1)

    def load_profile(self):
        fn = filedialog.askopenfilename(filetypes=[("Profile", "*.json")])
        if not fn:
            return
        try:
            with open(fn) as f:
                self.bus.from_dict(json.load(f))
        except (OSError, ValueError, TypeError) as e:
            messagebox.showerror("Load profile", str(e))
            return
        self.v_count.set(str(len(self.bus.slaves)))
        self.v_dev21.set(self.bus.dev21.enabled)
        self.rebuild_cards()

    def save_log(self):
        fn = filedialog.asksaveasfilename(defaultextension=".txt")
        if fn:
            with open(fn, "w") as f:
                f.write(self.log.get("1.0", "end"))

    # ---- periodic ----
    def add_log(self, kind, ts, text):
        t = time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"
        prefix = {"rx": "RX ", "tx": "TX ", "err": "ERR", "junk": "?? ", "note": " · ",
                  "status": "== ", "echo": "<< "}[kind]
        self.log.insert("end", f"{t} {prefix} {text}\n", kind)

    def tick(self):
        now = time.time()
        added = False
        try:
            while True:
                kind, ts, text, _node = self.events.get_nowait()
                if kind == "rx":
                    self.rate_n += 1
                if kind == "status" and not self.link.connected:
                    self.b_conn.configure(text="Connect")
                if self.v_pause.get():
                    continue
                if kind == "echo" and not self.v_echo.get():
                    continue
                if self.v_events_only.get() and kind in ("rx", "tx", "echo"):
                    continue
                self.add_log(kind, ts, text)
                added = True
        except queue.Empty:
            pass
        if added:
            self.log.see("end")
        if self.link.port is not None and not self.link.running:
            # reader thread died (e.g. adapter unplugged)
            self.link.close()
            self.b_conn.configure(text="Connect")

        if self.v_drift.get() and now - self.last_drift > 1.0:
            self.last_drift = now
            self.bus.drift()
            for c in self.cards:
                c.load_cells()
        for c in self.cards:
            c.refresh_status(now)

        if now - self.rate_t >= 1.0:
            self.rate = self.rate_n / (now - self.rate_t)
            self.rate_t, self.rate_n = now, 0
        st = self.bus.stats
        addressed = sum(1 for s in self.bus.slaves if s.address is not None)
        self.status.configure(text=(
            f"  {'CONNECTED ' + self.v_port.get() if self.link.connected else 'not connected'}"
            f"   |   addressed {addressed}/{len(self.bus.slaves)}"
            f"   |   frames {st['frames']}  ({self.rate:.1f}/s)   replies {st['replies']}"
            f"   CRC errors {st['crc_errors']}   ignored {st['ignored']}"))
        self.root.after(50, self.tick)

    def on_close(self):
        self.link.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
