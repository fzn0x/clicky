"""
Auto Clicker with Multi-Locations Recorder
========================
Clicking multiple locations in sequence (Point 1, 2, 3, ... as many as needed), which can then be set to repeat automatically. Example:
Point 1 = a box in the application
Point 2 = the "Yes" button in the first pop-up
Point 3 = the "Save" button in the next pop-up, and so on.

Preparation:
    pip install pynput pillow
"""

import base64
import ctypes
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

APP_TITLE = "Multi-Point Auto Clicker"


def enable_dpi_awareness():
    """Windows: use physical pixels so click positions stay accurate at
    125%/150% display scaling. Must be called before any window is created."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def fatal(message):
    """Show an error (also when the file is opened by double-click), then exit."""
    print(message, file=sys.stderr)
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_TITLE, message)
        r.destroy()
    except tk.TclError:
        pass
    sys.exit(1)


enable_dpi_awareness()

try:
    from pynput import keyboard, mouse
except ImportError as exc:
    if importlib.util.find_spec("pynput") is None:
        fatal("The 'pynput' library is not installed.\n\n"
              "Open Command Prompt / Terminal and run:\n\n"
              "    pip install pynput pillow\n\n"
              "Then start this program again.")
    fatal(f"pynput cannot run on this system:\n\n{exc}")

try:
    from PIL import ImageGrab  # optional, used by the "Wait to appear" option
except ImportError:
    ImageGrab = None


# ========================= Advanced settings =========================
HOTKEY_ADD = keyboard.Key.f6      # add a point at the mouse position
HOTKEY_UNDO = keyboard.Key.f7     # remove the last point
HOTKEY_TOGGLE = keyboard.Key.f8   # start / stop
HOTKEY_STOP = keyboard.Key.esc    # stop

CLICK_TYPES = ("Left click", "Double click", "Right click")
MAX_POINTS = 50
HOVER_PAUSE = 0.05     # pause after moving the cursor, before pressing the button
CORNER = 3             # emergency zone: top-left corner (x <= 3 and y <= 3)

# "Wait to appear": compares a small 17x17 pixel box around the point with
# its snapshot (taken when the point is added, or via the "Capture" button).
WATCH_RADIUS = 8
PIXEL_DIFF = 35        # minimum color difference for a pixel to count as "different"
MATCH_RATIO = 0.08     # <= 8% of pixels differ from the snapshot = target is visible
POLL_SECONDS = 0.08
SETTLE_SECONDS = 0.10  # short pause after the target appears, before clicking
# =====================================================================

SNAP_BYTES = (2 * WATCH_RADIUS + 1) ** 2 * 3


def fmt_num(x, spec="g"):
    """Format a number for display: 2.0 -> '2', 0.5 -> '0.5'."""
    if spec == "g" and float(x).is_integer():
        return str(int(x))
    return format(x, spec)


def config_path():
    if getattr(sys, "frozen", False):  # bundled into an .exe (PyInstaller)
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "auto_clicker_config.json")


# ------------------------------- Mouse --------------------------------
_thread_local = threading.local()


def _mouse():
    """One controller per thread (an X11 connection is not safe to share)."""
    ctl = getattr(_thread_local, "mouse", None)
    if ctl is None:
        ctl = _thread_local.mouse = mouse.Controller()
    return ctl


def mouse_pos():
    pos = _mouse().position
    if pos is None:  # e.g. the screen is locked / a UAC prompt is showing
        return None
    return int(round(pos[0])), int(round(pos[1]))


def move_to(point):
    _mouse().position = point


def press_click(kind):
    button = mouse.Button.right if kind == "Right click" else mouse.Button.left
    count = 2 if kind == "Double click" else 1
    ctl = _mouse()
    for i in range(count):
        ctl.press(button)
        time.sleep(0.02)
        ctl.release(button)
        if i + 1 < count:
            time.sleep(0.06)


def in_emergency_corner():
    pos = mouse_pos()
    return pos is not None and pos[0] <= CORNER and pos[1] <= CORNER


# ------------------------------- Screen -------------------------------
def grab_area(point, radius=WATCH_RADIUS):
    """A small screen crop around the point, as RGB bytes."""
    x, y = point
    bbox = (x - radius, y - radius, x + radius + 1, y + radius + 1)
    if sys.platform == "win32":
        img = ImageGrab.grab(bbox=bbox, all_screens=True)  # supports multiple monitors
    else:
        img = ImageGrab.grab(bbox=bbox)
    return img.convert("RGB").tobytes()


def try_grab(point):
    """Like grab_area, but returns None if the screen cannot be read."""
    if ImageGrab is None or point is None:
        return None
    try:
        return grab_area(point)
    except Exception:
        return None


def diff_ratio(a, b):
    """Fraction of pixels (0..1) whose color clearly differs between two crops."""
    if not a or len(a) != len(b):
        return 1.0
    changed = 0
    for i in range(0, len(a), 3):
        if (abs(a[i] - b[i]) > PIXEL_DIFF
                or abs(a[i + 1] - b[i + 1]) > PIXEL_DIFF
                or abs(a[i + 2] - b[i + 2]) > PIXEL_DIFF):
            changed += 1
    return changed / (len(a) // 3)


# ------------------------------- Points -------------------------------
class Point:
    """A single click point. Its values live in Tk variables so they are
    bound directly to the input fields in the window."""

    def __init__(self, x="", y="", kind=CLICK_TYPES[0], delay="1", wait=False):
        self.x = tk.StringVar(value=str(x))
        self.y = tk.StringVar(value=str(y))
        self.kind = tk.StringVar(value=kind if kind in CLICK_TYPES else CLICK_TYPES[0])
        self.delay = tk.StringVar(value=str(delay))
        self.wait = tk.BooleanVar(value=bool(wait) and ImageGrab is not None)
        self.snap = None      # 17x17 pixel snapshot of the area around the point
        self.snap_pos = None  # position where the snapshot was taken
        self.widgets = {}

    def set_position(self, pos, snap):
        self.x.set(str(pos[0]))
        self.y.set(str(pos[1]))
        self.snap = snap
        self.snap_pos = tuple(pos) if snap else None

    def to_json(self):
        return {
            "x": self.x.get(), "y": self.y.get(), "kind": self.kind.get(),
            "delay": self.delay.get(), "wait": bool(self.wait.get()),
            "snap": base64.b64encode(self.snap).decode("ascii") if self.snap else None,
            "snap_pos": list(self.snap_pos) if self.snap_pos else None,
        }

    @classmethod
    def from_json(cls, d):
        pt = cls(d.get("x", ""), d.get("y", ""), d.get("kind"), d.get("delay", "1"),
                 d.get("wait", False))
        try:
            snap = base64.b64decode(d.get("snap") or "")
            pos = tuple(int(v) for v in d.get("snap_pos") or ())
            if len(snap) == SNAP_BYTES and len(pos) == 2:
                pt.snap, pt.snap_pos = snap, pos
        except (TypeError, ValueError):
            pass
        return pt


# --------------------------- Click runner -----------------------------
class ClickWorker(threading.Thread):
    """Runs the click sequence on a separate thread so the window stays responsive."""

    def __init__(self, cfg, events, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.stop_event = stop_event

    def emit(self, kind, value):
        self.events.put((kind, value))

    def sleep(self, seconds):
        """Interruptible sleep. True = a stop was requested."""
        return self.stop_event.wait(max(0.0, seconds))

    def run(self):
        try:
            reason = self.loop()
        except Exception as exc:  # never let the thread die silently
            reason = f"Stopped due to an error: {exc}"
        self.emit("done", reason)

    def loop(self):
        c = self.cfg
        steps = c["steps"]

        for left in range(c["start_delay"], 0, -1):
            self.emit("status", f"Starting in {left} s...  (Esc = cancel)")
            if self.sleep(1):
                return "Cancelled before start."

        total = c["repeat"]  # 0 = run forever
        done = 0

        def stopped():
            return f"Stopped. {done} cycles completed."

        def emergency():
            return f"Emergency stop (mouse in the top-left corner). {done} cycles completed."

        while total == 0 or done < total:
            tag = f"{done + 1}/{total}" if total else str(done + 1)
            for i, st in enumerate(steps):
                name = f"Point {i + 1}"

                # 1) Delay before the click (Point 1 runs at once in the first cycle)
                if st["delay"] > 0 and not (i == 0 and done == 0):
                    self.emit("status", f"Cycle {tag} · {name}: delay {fmt_num(st['delay'])} s")
                    if self.sleep(st["delay"]):
                        return stopped()

                # 2) Wait until the target looks the same as when recorded
                if st["wait"]:
                    self.emit("status", f"Cycle {tag} · waiting for {name} to appear...")
                    t0 = time.monotonic()
                    result = self.wait_for_target(st["pos"], st["snap"], c["timeout"])
                    if result == "stopped":
                        return stopped()
                    if result == "corner":
                        return emergency()
                    if result == "timeout":
                        return (f"Stopped: {name} did not appear within {fmt_num(c['timeout'])} s "
                                f"(to avoid a wrong click). {done} cycles completed.")
                    self.emit("log", f"[{tag}] {name} appeared "
                                     f"({fmt_num(time.monotonic() - t0, '.2f')} s)")
                    if self.sleep(SETTLE_SECONDS):
                        return stopped()

                # 3) Click
                if self.stop_event.is_set():
                    return stopped()
                if in_emergency_corner():
                    return emergency()
                self.emit("status", f"Cycle {tag} · clicking {name}")
                move_to(st["pos"])
                time.sleep(HOVER_PAUSE)
                press_click(st["kind"])
                self.emit("log", f"[{tag}] {name} clicked at {st['pos']}")

            done += 1
            self.emit("cycle", done)
        return f"Done: {done} cycles."

    def wait_for_target(self, pos, reference, timeout):
        """Wait until the small box around the point matches its snapshot.
        Returns "found", "timeout", "stopped", or "corner"."""
        move_to(pos)  # move the cursor there first: same hover effect as when recording
        deadline = time.monotonic() + timeout
        hits = 0
        while True:
            if self.sleep(POLL_SECONDS):
                return "stopped"
            if in_emergency_corner():
                return "corner"
            try:
                same = diff_ratio(reference, grab_area(pos)) <= MATCH_RATIO
            except Exception:
                same = False
            hits = hits + 1 if same else 0
            if hits >= 2:  # 2 matches in a row = fully shown and settled
                return "found"
            if time.monotonic() >= deadline:
                return "timeout"


# -------------------------------- GUI ---------------------------------
def derive_font(delta=0, weight="normal"):
    """A copy of the system default font with a different size/weight."""
    f = tkfont.nametofont("TkDefaultFont").copy()
    size = f.cget("size")
    if delta:
        # negative size = pixels (Linux), positive = points (Windows/macOS)
        f.configure(size=size + delta if size > 0 else size - delta)
    f.configure(weight=weight)
    return f


class AutoClickerApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()  # messages from other threads -> GUI
        self.stop_event = threading.Event()
        self.worker = None
        self.running = False
        self.capture_job = None
        self.points = []
        self.scroll_needed = False

        root.title(APP_TITLE)
        root.resizable(False, False)
        self._setup_style()
        self._make_vars()
        self._build_ui()
        self._load_config()
        self._apply_topmost()
        self._render_points()
        self._place_window()

        self.listener = keyboard.Listener(on_press=self._on_key_press)
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(40, self._poll_events)

        self._log("Ready. Point the mouse at a target and press F6 to add a point.")
        if ImageGrab is None:
            self._log("Info: the 'Wait to appear' option needs Pillow  ->  pip install pillow")

    # ---------- layout ----------
    def _setup_style(self):
        style = ttk.Style(self.root)
        if sys.platform.startswith("linux") and "clam" in style.theme_names():
            style.theme_use("clam")
        self.f_title = derive_font(3, "bold")
        self.f_bold = derive_font(0, "bold")
        self.f_small = derive_font(-1)
        style.configure("Hint.TLabel", foreground="#6B6B6B", font=self.f_small)
        style.configure("Head.TLabel", foreground="#555555", font=self.f_small)
        style.configure("Status.TLabel", font=self.f_bold)
        style.configure("Big.TButton", font=self.f_bold, padding=(12, 5))
        style.configure("Small.TButton", padding=(3, 1))
        self.bg = style.lookup("TFrame", "background") or self.root.cget("background")
        self.px = self.root.winfo_fpixels("1i") / 96.0  # screen DPI scale

    def _make_vars(self):
        self.timeout = tk.StringVar(value="5")
        self.start_delay = tk.StringVar(value="2")
        self.repeat_mode = tk.StringVar(value="count")
        self.repeat = tk.StringVar(value="10")
        self.topmost = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready.")
        self.progress = tk.StringVar(value="Cycles done: 0")

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text=APP_TITLE, font=self.f_title).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Emergency stop: press Esc, or move the mouse to the top-left corner.",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))

        # --- Point list (scrollable when long) ---
        box = ttk.LabelFrame(outer, text=" Click points (run in order) ", padding=(8, 2, 8, 6))
        box.grid(row=2, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self.points_box = box
        holder = ttk.Frame(box)
        holder.grid(row=0, column=0, sticky="w")
        self.points_head = ttk.Frame(holder)  # column headers (do not scroll)
        self.points_head.grid(row=0, column=0, sticky="w")
        self.head_labels = []
        for c, text in enumerate(("#", "X", "Y", "Click type", "Delay\n(sec)", "Wait to\nappear")):
            lbl = ttk.Label(self.points_head, text=text, style="Head.TLabel",
                            justify="center", anchor="center")
            lbl.grid(row=0, column=c, sticky="sew")
            self.head_labels.append(lbl)
        self.points_canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0,
                                       background=self.bg, height=40)
        self.points_scroll = ttk.Scrollbar(holder, orient="vertical",
                                           command=self.points_canvas.yview)
        self.points_canvas.configure(yscrollcommand=self.points_scroll.set)
        self.points_canvas.grid(row=1, column=0, sticky="nw")
        self.points_scroll.grid(row=1, column=1, sticky="ns")
        holder.columnconfigure(1, minsize=self.points_scroll.winfo_reqwidth())
        self.points_inner = ttk.Frame(self.points_canvas)
        self.points_canvas.create_window((0, 0), window=self.points_inner, anchor="nw")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.unbind_class("TCombobox", seq)  # mouse wheel must not change the click type
            self.root.bind_all(seq, self._on_wheel, add="+")

        btns = ttk.Frame(box)
        btns.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.add_btn = ttk.Button(btns, text="+ Add point (F6)",
                                  command=lambda: self._start_capture("new"))
        self.add_btn.pack(side="left")
        self.undo_btn = ttk.Button(btns, text="Remove last (F7)", command=self._remove_last)
        self.undo_btn.pack(side="left", padx=(6, 0))
        self.clear_btn = ttk.Button(btns, text="Clear all", command=self._clear_points)
        self.clear_btn.pack(side="left", padx=(6, 0))
        ttk.Label(box, style="Hint.TLabel", justify="left", text=(
            "Delay = wait before that point is clicked (Point 1 delay = gap between cycles).\n"
            "Wait to appear = click only once that spot looks the same as when recorded."
        )).grid(row=2, column=0, sticky="w", pady=(6, 0))

        # --- Repeat & timing ---
        st = ttk.LabelFrame(outer, text=" Repeat & timing ", padding=(10, 2, 10, 6))
        st.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.rb_count = ttk.Radiobutton(st, text="Repeat", value="count",
                                        variable=self.repeat_mode, command=self._refresh_states)
        self.rb_count.grid(row=0, column=0, sticky="w")
        self.repeat_entry = ttk.Entry(st, textvariable=self.repeat, width=7, justify="center")
        self.repeat_entry.grid(row=0, column=1, padx=6, pady=3)
        ttk.Label(st, text="times").grid(row=0, column=2, sticky="w", padx=(0, 16))
        self.rb_inf = ttk.Radiobutton(st, text="Until stopped", value="infinite",
                                      variable=self.repeat_mode, command=self._refresh_states)
        self.rb_inf.grid(row=0, column=3, columnspan=3, sticky="w")

        ttk.Label(st, text="Start after").grid(row=1, column=0, sticky="w")
        self.start_spin = ttk.Spinbox(st, from_=0, to=60, textvariable=self.start_delay,
                                      width=5, justify="center")
        self.start_spin.grid(row=1, column=1, padx=6, pady=3)
        ttk.Label(st, text="seconds").grid(row=1, column=2, sticky="w", padx=(0, 16))
        ttk.Label(st, text="Wait timeout").grid(row=1, column=3, sticky="w")
        self.timeout_entry = ttk.Entry(st, textvariable=self.timeout, width=6, justify="center")
        self.timeout_entry.grid(row=1, column=4, padx=6)
        ttk.Label(st, text="seconds").grid(row=1, column=5, sticky="w")

        # --- Controls ---
        ctl = ttk.Frame(outer)
        ctl.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.start_btn = ttk.Button(ctl, text="▶  Start (F8)", style="Big.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctl, text="■  Stop (Esc)", style="Big.TButton",
                                   command=self.stop)
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(ctl, text="Always on top", variable=self.topmost,
                        command=self._apply_topmost).pack(side="right")

        sf = ttk.Frame(outer)
        sf.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        sf.columnconfigure(0, weight=1)
        ttk.Label(sf, textvariable=self.status, style="Status.TLabel",
                  wraplength=int(400 * self.px)).grid(row=0, column=0, sticky="w")
        ttk.Label(sf, textvariable=self.progress).grid(row=0, column=1, sticky="ne", padx=(10, 0))

        # --- Log ---
        lf = ttk.LabelFrame(outer, text=" Log ", padding=(6, 2, 6, 6))
        lf.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        lf.columnconfigure(0, weight=1)
        self.log_text = tk.Text(lf, height=5, width=56, wrap="word", font=self.f_small,
                                state="disabled", relief="flat", borderwidth=0)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="ew")
        sb.grid(row=0, column=1, sticky="ns")

    def _render_points(self):
        """Rebuild the point rows (called whenever the list changes)."""
        inner = self.points_inner
        for w in inner.winfo_children():
            w.destroy()
        for c in range(10):
            inner.columnconfigure(c, minsize=0)
        if not self.points:
            self.points_head.grid_remove()
            ttk.Label(inner, style="Hint.TLabel", justify="left", text=(
                "No points yet. Point the mouse at the first target and press F6,\n"
                "or click “+ Add point” and move the mouse there within 3 seconds."
            )).grid(row=0, column=0, columnspan=10, sticky="w", padx=4, pady=8)
        else:
            self.points_head.grid()
            for i, pt in enumerate(self.points):
                self._render_row(inner, i, pt)
        self._update_points_view()
        self._refresh_states()

    def _render_row(self, inner, i, pt):
        r = i
        ttk.Label(inner, text=str(i + 1), font=self.f_bold).grid(row=r, column=0, padx=(2, 4))
        ex = ttk.Entry(inner, textvariable=pt.x, width=6, justify="center")
        ey = ttk.Entry(inner, textvariable=pt.y, width=6, justify="center")
        cb = ttk.Combobox(inner, textvariable=pt.kind, values=CLICK_TYPES,
                          state="readonly", width=12)
        ed = ttk.Entry(inner, textvariable=pt.delay, width=5, justify="center")
        cw = ttk.Checkbutton(inner, variable=pt.wait)
        ba = ttk.Button(inner, text="Capture", style="Small.TButton", width=8,
                        command=lambda p=pt: self._start_capture(p))
        bu = ttk.Button(inner, text="▲", style="Small.TButton", width=2,
                        command=lambda k=i: self._move_point(k, -1))
        bd = ttk.Button(inner, text="▼", style="Small.TButton", width=2,
                        command=lambda k=i: self._move_point(k, 1))
        bx = ttk.Button(inner, text="✕", style="Small.TButton", width=2,
                        command=lambda k=i: self._delete_point(k))
        for c, w in enumerate((ex, ey, cb, ed, cw, ba, bu, bd, bx), start=1):
            w.grid(row=r, column=c, padx=2, pady=2)
        pt.widgets = {"entries": (ex, ey, ed), "combo": cb, "wait": cw,
                      "capture": ba, "up": bu, "down": bd, "delete": bx}

    def _update_points_view(self):
        """Set the list height; with too many points the list becomes scrollable."""
        inner, canvas = self.points_inner, self.points_canvas
        inner.update_idletasks()
        limit = None
        if self.points:
            # match the header column widths to the input columns below
            last = len(self.points) - 1
            for c, lbl in enumerate(self.head_labels):
                cw = max(inner.grid_bbox(c, 0, c, last)[2], lbl.winfo_reqwidth())
                inner.columnconfigure(c, minsize=cw)
                self.points_head.columnconfigure(c, minsize=cw)
            inner.update_idletasks()
            row_h = max(1, inner.grid_bbox(0, 0, 9, 0)[3])
            canvas.configure(yscrollincrement=row_h)
            limit = self._visible_rows(row_h) * row_h
        w, h = inner.winfo_reqwidth(), inner.winfo_reqheight()
        if limit is None:
            limit = h
        self.scroll_needed = h > limit
        canvas.configure(width=w, height=min(h, limit), scrollregion=(0, 0, w, h))
        if self.scroll_needed:
            self.points_scroll.grid()
        else:
            self.points_scroll.grid_remove()
            canvas.yview_moveto(0)
        self._keep_on_screen()

    def _visible_rows(self, row_h):
        """Number of point rows that fit without the window outgrowing the screen."""
        self.root.update_idletasks()
        others = self.root.winfo_reqheight() - self.points_canvas.winfo_reqheight()
        free = self.root.winfo_screenheight() - others - int(110 * self.px)
        return max(3, min(12, free // row_h))

    def _on_wheel(self, event):
        if not self.scroll_needed:
            return
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None and w is not self.points_box:
            w = w.master
        if w is None:
            return  # mouse wheel is not over the point list
        up = event.num == 4 or getattr(event, "delta", 0) > 0
        self.points_canvas.yview_scroll(-1 if up else 1, "units")

    def _place_window(self):
        """Place at the top-right of the screen so it does not cover the target app."""
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        margin = int(40 * self.px)
        x = max(0, self.root.winfo_screenwidth() - w - margin)
        y = max(0, min(margin, self.root.winfo_screenheight() - h - int(90 * self.px)))
        self.root.geometry(f"+{x}+{y}")

    def _keep_on_screen(self):
        """If the window grows (more points) past the screen edge, move it back in."""
        if not self.root.winfo_ismapped():
            return
        self.root.update_idletasks()
        m = re.match(r"^\d+x\d+\+(-?\d+)\+(-?\d+)$", self.root.geometry())
        if not m:
            return
        x, y = int(m.group(1)), int(m.group(2))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        over_x = self.root.winfo_rootx() + self.root.winfo_reqwidth() - (sw - int(10 * self.px))
        over_y = self.root.winfo_rooty() + self.root.winfo_reqheight() - (sh - int(50 * self.px))
        # only for windows on the main screen (never pull one off another monitor)
        nx = max(0, x - over_x) if over_x > 0 and 0 < x < sw else x
        ny = max(0, y - over_y) if over_y > 0 and 0 < y < sh else y
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")

    def _refresh_states(self):
        idle = not self.running
        on = "normal" if idle else "disabled"
        can_wait = ImageGrab is not None
        n = len(self.points)
        for i, pt in enumerate(self.points):
            w = pt.widgets
            if not w:
                continue
            for e in w["entries"]:
                e.configure(state=on)
            w["combo"].configure(state="readonly" if idle else "disabled")
            w["wait"].configure(state="normal" if idle and can_wait else "disabled")
            w["capture"].configure(state=on)
            w["delete"].configure(state=on)
            w["up"].configure(state="normal" if idle and i > 0 else "disabled")
            w["down"].configure(state="normal" if idle and i < n - 1 else "disabled")
        self.add_btn.configure(state=on)
        for b in (self.undo_btn, self.clear_btn):
            b.configure(state="normal" if idle and n else "disabled")
        for w in (self.start_spin, self.rb_count, self.rb_inf, self.timeout_entry):
            w.configure(state=on)
        counting = self.repeat_mode.get() == "count"
        self.repeat_entry.configure(state="normal" if idle and counting else "disabled")
        self.start_btn.configure(state=on)
        self.stop_btn.configure(state="disabled" if idle else "normal")

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.topmost.get()))
        except tk.TclError:
            pass

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0]) - 1
        if lines > 500:
            self.log_text.delete("1.0", f"{lines - 500 + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _error(self, message):
        self.status.set(message.split("\n")[0])
        self._log("Could not start: " + " ".join(message.split()))
        self.root.deiconify()
        self.root.lift()
        messagebox.showerror(APP_TITLE, message, parent=self.root)

    # ---------- managing points ----------
    def _add_point(self, pos, snap):
        if self.running:
            self._log("Points cannot be changed while the auto clicker is running.")
            return
        if pos is None:
            self._log("Could not read the mouse position, try again.")
            return
        if len(self.points) >= MAX_POINTS:
            self._log(f"The limit is {MAX_POINTS} points.")
            return
        prev = self.points[-1] if self.points else None
        # a new point copies the delay & wait settings of the previous one
        pt = Point(delay=prev.delay.get() if prev else "1",
                   wait=prev.wait.get() if prev else False)
        pt.set_position(pos, snap)
        self.points.append(pt)
        self._render_points()
        self.points_canvas.yview_moveto(1.0)
        n = len(self.points)
        self.status.set(f"Point {n} added at ({pos[0]}, {pos[1]}).")
        self._log(f"Point {n} added: ({pos[0]}, {pos[1]})")
        self._save_config()

    def _remove_last(self):
        if not self.running and self.points:
            self._delete_point(len(self.points) - 1)

    def _delete_point(self, k):
        if self.running or not 0 <= k < len(self.points):
            return
        self._cancel_capture()
        del self.points[k]
        self._render_points()
        self.status.set(f"Point {k + 1} removed. {len(self.points)} left.")
        self._log(f"Point {k + 1} removed. {len(self.points)} left.")
        self._save_config()

    def _move_point(self, k, step):
        j = k + step
        if self.running or not (0 <= k < len(self.points) and 0 <= j < len(self.points)):
            return
        self.points[k], self.points[j] = self.points[j], self.points[k]
        self._render_points()
        self._log(f"Point {k + 1} moved to position {j + 1}.")
        self._save_config()

    def _clear_points(self):
        if self.running or not self.points:
            return
        if not messagebox.askyesno(APP_TITLE, f"Remove all {len(self.points)} points?",
                                   parent=self.root):
            return
        self._cancel_capture()
        self.points.clear()
        self._render_points()
        self.status.set("All points removed.")
        self._log("All points removed.")
        self._save_config()

    # ---------- capture position with countdown ----------
    def _start_capture(self, target):
        """'+ Add point' / 'Capture': count down 3 seconds, then read the mouse position."""
        if self.running:
            return
        self._cancel_capture()
        self._capture_tick(target, 3)

    def _capture_tick(self, target, left):
        is_new = target == "new"
        if not is_new and target not in self.points:  # the point was already deleted
            self.capture_job = None
            return
        name = "the new point" if is_new else f"Point {self.points.index(target) + 1}"
        if left > 0:
            self.status.set(f"Move the mouse to {name}...  {left}")
            self.capture_job = self.root.after(1000, self._capture_tick, target, left - 1)
            return
        self.capture_job = None
        pos = mouse_pos()
        snap = try_grab(pos)
        if is_new:
            self._add_point(pos, snap)
        elif pos is None:
            self._log("Could not read the mouse position, try again.")
        else:
            target.set_position(pos, snap)
            self.status.set(f"{name} updated to ({pos[0]}, {pos[1]}).")
            self._log(f"{name} updated: ({pos[0]}, {pos[1]})")
            self._save_config()

    def _cancel_capture(self):
        if self.capture_job is not None:
            self.root.after_cancel(self.capture_job)
            self.capture_job = None

    # ---------- hotkeys ----------
    def _on_key_press(self, key):
        """Runs on the pynput thread: just post a message to the GUI queue."""
        try:
            if key == HOTKEY_ADD:
                pos = mouse_pos()
                # snapshot the screen now (before the target app reacts), on another thread
                threading.Thread(target=self._snap_and_post, args=(pos,), daemon=True).start()
            elif key == HOTKEY_UNDO:
                self.events.put(("hotkey", ("undo", None, None)))
            elif key == HOTKEY_TOGGLE:
                self.events.put(("hotkey", ("toggle", None, None)))
            elif key == HOTKEY_STOP:
                self.events.put(("hotkey", ("stop", None, None)))
        except Exception:
            pass  # the listener must not die from a minor error

    def _snap_and_post(self, pos):
        snap = None if self.running else try_grab(pos)
        self.events.put(("hotkey", ("add", pos, snap)))

    def _handle_hotkey(self, action, pos, snap):
        if action == "add":
            self._cancel_capture()
            self._add_point(pos, snap)
        elif action == "undo":
            if self.running:
                self._log("Points cannot be changed while the auto clicker is running.")
                return
            self._cancel_capture()
            self._remove_last()
        elif action == "toggle":
            if self.running:
                self.stop()
            else:
                self.start()
        elif action == "stop":
            if self.running:
                self.stop()
            elif self.capture_job is not None:
                self._cancel_capture()
                self.status.set("Point capture cancelled.")

    def _poll_events(self):
        """Process messages from the worker thread & hotkeys (thread-safe)."""
        try:
            while True:
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    self._log(value)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "cycle":
                    self.progress.set(f"Cycles done: {value}")
                elif kind == "done":
                    self._on_worker_done(value)
                elif kind == "hotkey":
                    self._handle_hotkey(*value)
        finally:
            self.root.after(40, self._poll_events)

    # ---------- start / stop ----------
    def _collect_settings(self):
        """Read & validate the inputs. Returns a settings dict, or None if invalid."""
        def number(text, name, lo, hi, integer=False):
            try:
                value = float(text.strip().replace(",", "."))
            except ValueError:
                raise ValueError(f"{name} must be a number (e.g. 1 or 0.5).") from None
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be between {fmt_num(lo)} and {fmt_num(hi)}.")
            return int(value) if integer else value

        if not self.points:
            self._error("No points yet.\n\n"
                        "Point the mouse at a target and press F6 to add a point.")
            return None
        steps = []
        try:
            lim = 100000
            for i, pt in enumerate(self.points, start=1):
                name = f"Point {i}"
                pos = (number(pt.x.get(), f"X of {name}", -lim, lim, True),
                       number(pt.y.get(), f"Y of {name}", -lim, lim, True))
                delay = number(pt.delay.get(), f"Delay of {name}", 0, 86400)
                wait = bool(pt.wait.get())
                if wait:
                    if ImageGrab is None:
                        raise ValueError("The 'Wait to appear' option needs Pillow:  pip install pillow")
                    if pt.snap is None or pt.snap_pos != pos:
                        raise ValueError(
                            f"{name} uses 'Wait to appear' but has no screen snapshot yet "
                            "(or its position was edited by hand).\n\n"
                            f"Show the target on screen, click 'Capture' in the {name} row, "
                            "then move the mouse to the target within 3 seconds.")
                steps.append({"pos": pos, "kind": pt.kind.get(), "delay": delay,
                              "wait": wait, "snap": pt.snap})
            any_wait = any(s["wait"] for s in steps)
            cfg = {
                "steps": steps,
                "timeout": (number(self.timeout.get(), "Wait timeout", 0.5, 600)
                            if any_wait else 0.0),
                "start_delay": number(self.start_delay.get(), "Start after", 0, 60, True),
                "repeat": (0 if self.repeat_mode.get() == "infinite"
                           else number(self.repeat.get(), "Repeat count", 1, 10 ** 9, True)),
            }
        except ValueError as exc:
            self._error(str(exc))
            return None

        for i, s in enumerate(steps, start=1):
            p = s["pos"]
            if p[0] <= CORNER and p[1] <= CORNER:
                self._error(f"Point {i} is in the top-left corner of the screen (emergency stop zone).\n"
                            "Pick another position.")
                return None
            if self._over_own_window(p):
                self._error(f"Point {i} {p} is covered by this Auto Clicker window.\n\n"
                            "Move this window so it does not get clicked.")
                return None
        return cfg

    def _over_own_window(self, p):
        self.root.update_idletasks()
        x0, y0 = self.root.winfo_rootx(), self.root.winfo_rooty()
        x1, y1 = x0 + self.root.winfo_width(), y0 + self.root.winfo_height()
        title_bar = int(40 * self.px)
        return x0 <= p[0] < x1 and y0 - title_bar <= p[1] < y1

    def start(self):
        if self.running:
            return
        self._cancel_capture()
        cfg = self._collect_settings()
        if cfg is None:
            return
        waiting = [s for s in cfg["steps"] if s["wait"]]
        if waiting:
            try:
                grab_area(waiting[0]["pos"])
            except Exception as exc:
                self._error("The 'Wait to appear' option cannot read the screen on this system:\n"
                            f"{exc}\n\nTurn that option off and use delays only.")
                return
        self._save_config()
        self.stop_event = threading.Event()
        self.worker = ClickWorker(cfg, self.events, self.stop_event)
        self.running = True
        self._refresh_states()
        self.progress.set("Cycles done: 0")
        amount = "until stopped" if cfg["repeat"] == 0 else f"{cfg['repeat']}x"
        self._log(f"Started: {len(cfg['steps'])} points, {amount}.")
        self.worker.start()

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.status.set("Stopping...")

    def _on_worker_done(self, reason):
        self.running = False
        self.worker = None
        self._refresh_states()
        self.status.set(reason)
        self._log(reason)

    # ---------- save / load settings ----------
    def _save_config(self):
        data = {
            "version": 2,
            "points": [p.to_json() for p in self.points],
            "timeout": self.timeout.get(),
            "start_delay": self.start_delay.get(),
            "repeat_mode": self.repeat_mode.get(),
            "repeat": self.repeat.get(),
            "topmost": bool(self.topmost.get()),
        }
        try:
            with open(config_path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
        except OSError:
            pass  # folder is not writable: ignore

    def _load_config(self):
        try:
            with open(config_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("points"), list):
            for d in data["points"][:MAX_POINTS]:
                if isinstance(d, dict):
                    self.points.append(Point.from_json(d))
        elif "x1" in data:  # settings from the old version (2 points)
            old = (
                (data.get("x1", ""), data.get("y1", ""), data.get("kind1"),
                 data.get("interval", "1"), False),
                (data.get("x2", ""), data.get("y2", ""), data.get("kind2"),
                 data.get("delay", "1"), data.get("wait_popup", False)),
            )
            for x, y, kind, delay, wait in old:
                if str(x).strip() and str(y).strip():
                    self.points.append(Point(x, y, kind, delay, wait))
        for key, var in (("timeout", self.timeout), ("start_delay", self.start_delay),
                         ("repeat", self.repeat)):
            if key in data:
                var.set(str(data[key]))
        if data.get("repeat_mode") in ("count", "infinite"):
            self.repeat_mode.set(data["repeat_mode"])
        self.topmost.set(bool(data.get("topmost", False)))

    def _on_close(self):
        self.stop_event.set()
        self._cancel_capture()
        try:
            self.listener.stop()
        except Exception:
            pass
        self._save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    AutoClickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
