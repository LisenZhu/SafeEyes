#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rest Reminder (Stretchly/SafeEyes-like) for Windows
- No installer, no .exe. Pure Python + tkinter + ctypes (built-in).
- Features:
  * Micro + Long breaks with customizable intervals/durations
  * Fullscreen overlays on ALL monitors, always-on-top
  * Break tips (rotate eyes, blink, stand up, etc.)
  * Strict Mode (cannot finish early)
  * Snooze
  * Smart Pause: detects user idle time via Win32 GetLastInputInfo
  * Counts only ACTIVE time towards the next break

Usage examples:
  python rest_reminder.py
  python rest_reminder.py --micro-interval 10 --micro-duration 20 --long-interval 30 --long-duration 300
  python rest_reminder.py --strict
"""

import argparse
import ctypes
import sys
import time
import threading
import random
from datetime import timedelta
import tkinter as tk
from tkinter import ttk

# -------------------------
# Windows idle detection
# -------------------------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

GetLastInputInfo = ctypes.windll.user32.GetLastInputInfo
GetTickCount = ctypes.windll.kernel32.GetTickCount

def get_idle_seconds() -> int:
    """Return number of seconds since last user input (keyboard/mouse)."""
    liinfo = LASTINPUTINFO()
    liinfo.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not GetLastInputInfo(ctypes.byref(liinfo)):
        return 0
    millis = GetTickCount() - liinfo.dwTime
    return max(0, millis // 1000)

# -------------------------
# Monitor enumeration (all screens)
# -------------------------
MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_long * 4), ctypes.c_double)
user32 = ctypes.windll.user32

def get_all_monitors():
    """
    Returns a list of monitor rectangles [(left, top, right, bottom), ...]
    using EnumDisplayMonitors.
    """
    monitors = []

    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        rc = lprcMonitor.contents
        left, top, right, bottom = rc[0], rc[1], rc[2], rc[3]
        monitors.append((left, top, right, bottom))
        return 1

    cb = MONITORENUMPROC(_callback)
    if not user32.EnumDisplayMonitors(0, 0, cb, 0):
        # Fallback to primary screen if enumeration fails
        root = tk.Tk()
        monitors.append((0, 0, root.winfo_screenwidth(), root.winfo_screenheight()))
        root.destroy()
    return monitors

# -------------------------
# Break suggestions
# -------------------------
MICRO_TIPS = [
    "Blink slowly 10 times",
    "Rotate your eyes clockwise, then counter‑clockwise",
    "Look at an object 20+ ft away for 20 seconds (20‑20‑20 rule)",
    "Roll your shoulders and relax your jaw",
    "Unclench your hands; wiggle your fingers",
    "Adjust your posture and sit tall",
]

LONG_TIPS = [
    "Stand up and stretch your arms, neck, and back",
    "Walk to get water or look outside the window",
    "Gently stretch your wrists and forearms",
    "Do a quick posture reset: shoulders back, chin in",
    "Close your eyes and breathe deeply for a minute",
    "Look far away to relax eye muscles",
]

# -------------------------
# Overlay UI
# -------------------------
class OverlayWindow(tk.Toplevel):
    def __init__(self, master, rect, theme, strict, tip, duration, on_finish, snooze_cb):
        super().__init__(master)
        self.strict = strict
        self.duration = duration
        self.remaining = duration
        self.on_finish = on_finish
        self.snooze_cb = snooze_cb
        self.tip = tip
        self.configure(bg=theme["bg"])
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", theme["alpha"])

        # Place to specific monitor
        left, top, right, bottom = rect
        width = right - left
        height = bottom - top
        self.geometry(f"{width}x{height}+{left}+{top}")

        # Swallow Alt+F4 / close attempts in Strict Mode
        self.protocol("WM_DELETE_WINDOW", self._on_try_close)

        # Layout
        container = tk.Frame(self, bg=theme["bg"])
        container.place(relx=0.5, rely=0.5, anchor="center")

        title = tk.Label(
            container,
            text="Time for a break",
            bg=theme["bg"], fg=theme["fg"],
            font=("Segoe UI", 44, "bold")
        )
        title.pack(pady=(0, 10))

        tip_lbl = tk.Label(
            container,
            text=tip,
            bg=theme["bg"], fg=theme["fg2"],
            font=("Segoe UI", 24),
            wraplength=int(width*0.7),
            justify="center"
        )
        tip_lbl.pack(pady=(0, 20))

        self.timer_lbl = tk.Label(
            container,
            text=self._fmt_time(self.remaining),
            bg=theme["bg"], fg=theme["fg"],
            font=("Segoe UI", 36, "bold")
        )
        self.timer_lbl.pack(pady=(0, 20))

        btns = tk.Frame(container, bg=theme["bg"])
        btns.pack()

        if not strict:
            self.skip_btn = ttk.Button(btns, text="Skip", command=self._finish_now)
            self.skip_btn.pack(side="left", padx=8)
        else:
            # Placeholder to keep layout aligned
            self.skip_btn = None

        self.snooze_btn = ttk.Button(btns, text="Snooze 5 min", command=lambda: self._snooze(5*60))
        self.snooze_btn.pack(side="left", padx=8)

        # Update timer
        self._tick()

    def _fmt_time(self, sec):
        return str(timedelta(seconds=sec))

    def _on_try_close(self):
        if self.strict:
            # Ignore attempts to close in strict mode
            return
        self._finish_now()

    def _finish_now(self):
        self.after_cancel(self._after_id)
        self.destroy()
        self.on_finish(skipped=True)

    def _snooze(self, seconds):
        # End windows, call snooze callback
        self.after_cancel(self._after_id)
        self.destroy()
        self.snooze_cb(seconds)

    def _tick(self):
        self.timer_lbl.config(text=self._fmt_time(self.remaining))
        if self.remaining <= 0:
            self.destroy()
            self.on_finish(skipped=False)
            return
        self.remaining -= 1
        self._after_id = self.after(1000, self._tick)

class OverlayManager:
    def __init__(self, master, strict=False, alpha=0.88):
        self.master = master
        self.strict = strict
        self.theme = {
            "bg": "#0B132B",
            "fg": "#FFFFFF",
            "fg2": "#A3C4F3",
            "alpha": alpha,
        }
        self.active_overlays = []

    def show_break(self, tip, duration, on_finish, snooze_cb):
        # Create overlay per monitor
        rects = get_all_monitors()
        self.active_overlays.clear()
        for rect in rects:
            ow = OverlayWindow(self.master, rect, self.theme, self.strict, tip, duration, on_finish, snooze_cb)
            self.active_overlays.append(ow)

    def close_all(self):
        for ow in self.active_overlays:
            try:
                ow.destroy()
            except Exception:
                pass
        self.active_overlays.clear()

# -------------------------
# Scheduler
# -------------------------
class BreakScheduler:
    def __init__(self, micro_interval, micro_duration, long_interval, long_duration, idle_grace, strict):
        self.micro_interval = micro_interval * 60
        self.micro_duration = micro_duration
        self.long_interval = long_interval * 60
        self.long_duration = long_duration
        self.idle_grace = idle_grace
        self.strict = strict

        # active time trackers
        self.active_seconds_since_micro = 0
        self.active_seconds_since_long = 0

        self.running = True
        self.in_break = False

        self._lock = threading.Lock()
        self.status = "Running"
        self.next_micro_in = self.micro_interval
        self.next_long_in = self.long_interval

    def reset_after_micro(self):
        with self._lock:
            self.active_seconds_since_micro = 0
            self.next_micro_in = self.micro_interval

    def reset_after_long(self):
        with self._lock:
            self.active_seconds_since_long = 0
            self.next_long_in = self.long_interval
            # when a long break occurs we also clear micro counters so that
            # the next cycle starts fresh (avoids an immediate micro break after
            # the long one)
            self.active_seconds_since_micro = 0
            self.next_micro_in = self.micro_interval

    def tick_active_time(self, delta=1):
        with self._lock:
            self.active_seconds_since_micro += delta
            self.active_seconds_since_long += delta
            self.next_micro_in = max(0, self.micro_interval - self.active_seconds_since_micro)
            self.next_long_in = max(0, self.long_interval - self.active_seconds_since_long)

    def need_micro(self):
        return self.active_seconds_since_micro >= self.micro_interval

    def need_long(self):
        # long break is determined solely by the long interval.  the micro counter
        # is reset when a long break happens so micro and long timers no longer
        # coincide.
        return self.active_seconds_since_long >= self.long_interval

# -------------------------
# Controller window
# -------------------------
class ControllerApp:
    def __init__(self, args):
        self.args = args

        # Tk root
        self.root = tk.Tk()
        self.root.title("Rest Reminder (no-install)")
        self.root.geometry("420x220")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Dark-ish theme
        self.root.configure(bg="#101820")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TLabel", background="#101820", foreground="#E6E6E6", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))

        # UI
        ttk.Label(self.root, text="Rest Reminder", style="Header.TLabel").pack(pady=(10, 5))
        self.status_lbl = ttk.Label(self.root, text="Status: starting…")
        self.status_lbl.pack()

        self.micro_lbl = ttk.Label(self.root, text="Next micro-break: —")
        self.micro_lbl.pack(pady=(10, 0))
        self.long_lbl = ttk.Label(self.root, text="Next long break: —")
        self.long_lbl.pack()

        btns = tk.Frame(self.root, bg="#101820")
        btns.pack(pady=(14, 0))

        self.toggle_btn = ttk.Button(btns, text="Pause", command=self._toggle_run)
        self.toggle_btn.pack(side="left", padx=6)

        self.snooze_btn = ttk.Button(btns, text="Snooze 10 min", command=lambda: self._snooze(10*60))
        self.snooze_btn.pack(side="left", padx=6)

        self.strict_var = tk.BooleanVar(value=args.strict)
        self.strict_chk = ttk.Checkbutton(btns, text="Strict Mode", variable=self.strict_var, command=self._apply_strict)
        self.strict_chk.pack(side="left", padx=6)

        self.quit_btn = ttk.Button(btns, text="Quit", command=self._on_close)
        self.quit_btn.pack(side="left", padx=6)

        # Manager
        self.scheduler = BreakScheduler(
            micro_interval=args.micro_interval,
            micro_duration=args.micro_duration,
            long_interval=args.long_interval,
            long_duration=args.long_duration,
            idle_grace=args.idle_grace,
            strict=args.strict,
        )
        self.overlay_mgr = OverlayManager(self.root, strict=args.strict, alpha=args.overlay_alpha)

        # State
        self.snoozed_until = 0  # epoch seconds, not counting active time, but absolute wall clock
        self._update_ui()
        self.root.after(1000, self._main_tick)

    # ------------- UI interactions ---------------
    def _apply_strict(self):
        self.scheduler.strict = self.strict_var.get()
        self.overlay_mgr.strict = self.strict_var.get()

    def _toggle_run(self):
        self.scheduler.running = not self.scheduler.running
        self.toggle_btn.config(text=("Resume" if not self.scheduler.running else "Pause"))
        self.scheduler.status = "Paused" if not self.scheduler.running else "Running"
        self._update_ui()

    def _snooze(self, seconds):
        self.snoozed_until = time.time() + seconds
        self.scheduler.status = f"Snoozed for {seconds//60} min"
        self._update_ui()

    def _on_close(self):
        self.overlay_mgr.close_all()
        self.root.destroy()

    # ------------- Scheduling logic ---------------
    def _main_tick(self):
        try:
            now = time.time()
            idle = get_idle_seconds()

            if not self.scheduler.running:
                self._update_ui()
                self.root.after(1000, self._main_tick)
                return

            if now < self.snoozed_until:
                # Snoozed; do nothing
                self._update_ui()
                self.root.after(1000, self._main_tick)
                return

            # If user is idle longer than grace, we pause "active time"
            if idle <= self.args.idle_grace and not self.scheduler.in_break:
                self.scheduler.tick_active_time(delta=1)

            # Decide which break is due (long has priority)
            if not self.scheduler.in_break:
                if self.scheduler.need_long():
                    self._start_break(kind="long")
                elif self.scheduler.need_micro():
                    self._start_break(kind="micro")

            self._update_ui()
        finally:
            self.root.after(1000, self._main_tick)

    def _start_break(self, kind):
        self.scheduler.in_break = True
        if kind == "long":
            tip = random.choice(LONG_TIPS)
            duration = self.scheduler.long_duration
        else:
            tip = random.choice(MICRO_TIPS)
            duration = self.scheduler.micro_duration

        def on_finish(skipped=False):
            # Reset counters depending on break kind
            if kind == "long":
                self.scheduler.reset_after_long()
            else:
                self.scheduler.reset_after_micro()
            self.scheduler.in_break = False
            self.scheduler.status = "Running"

        def snooze_cb(seconds):
            # If user snoozes during break
            if kind == "long":
                self.scheduler.reset_after_long()
            else:
                self.scheduler.reset_after_micro()
            self.scheduler.in_break = False
            self.snoozed_until = time.time() + seconds
            self.scheduler.status = f"Snoozed for {seconds//60} min"

        # Status
        label = "Long" if kind == "long" else "Micro"
        self.scheduler.status = f"{label} break in progress"

        # Open overlays on each monitor
        self.overlay_mgr.show_break(tip=tip, duration=duration, on_finish=on_finish, snooze_cb=snooze_cb)

    # ------------- UI refresh ---------------
    def _update_ui(self):
        self.status_lbl.config(text=f"Status: {self.scheduler.status}")

        nm = str(timedelta(seconds=self.scheduler.next_micro_in))
        nl = str(timedelta(seconds=self.scheduler.next_long_in))
        if self.snoozed_until > time.time():
            # If snoozed, show remaining snooze time
            remain = int(self.snoozed_until - time.time())
            self.micro_lbl.config(text=f"Snoozed… resuming in: {str(timedelta(seconds=remain))}")
            self.long_lbl.config(text="")
        else:
            self.micro_lbl.config(text=f"Next micro-break in: {nm}")
            self.long_lbl.config(text=f"Next long break in: {nl}")

# -------------------------
# CLI
# -------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Stretchly/SafeEyes-like break reminder (no-install, pure Python).")
    p.add_argument("--micro-interval", type=int, default=15, help="Minutes between micro-breaks (default: 10)")
    p.add_argument("--micro-duration", type=int, default=20, help="Seconds of a micro-break (default: 20)")
    p.add_argument("--long-interval", type=int, default=45, help="Minutes between long breaks (default: 30)")
    p.add_argument("--long-duration", type=int, default=600, help="Seconds of a long break (default: 300 = 5min)")
    p.add_argument("--idle-grace", type=int, default=60, help="Idle seconds that pause the active timer (default: 60)")
    p.add_argument("--strict", action="store_true", help="Strict Mode: you can't skip breaks early.")
    p.add_argument("--overlay-alpha", type=float, default=0.88, help="Overlay opacity 0..1 (default: 0.88)")
    return p.parse_args()

def main():
    args = parse_args()
    app = ControllerApp(args)
    app.root.mainloop()

if __name__ == "__main__":
    if sys.platform != "win32":
        print("Warning: this script targets Windows (idle detection via Win32). On Linux/macOS the overlay works but idle detection is disabled.")
    main()