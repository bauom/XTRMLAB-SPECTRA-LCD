"""
app.py -- a desktop GUI for this whole project.

    python app.py

Pick your panel's COM port (or leave it on auto-detect), pick a theme
tab, fill in that theme's settings, hit Start. Stop switches themes or
quits cleanly. Nothing here needs the command line once you're set up.

Closing the window doesn't stop anything running -- it dismisses to a
system tray icon instead (needs `pip install pystray`; right-click it
for Show window / Stop screen / Quit). Whatever's actually streaming
when you Quit for real is remembered and picked back up automatically
the next time you launch the app, on its own, with no need to click
Start again -- whether that's a normal `python app.py`, or the Windows
startup launcher below.

Ships with NO personal video or image baked in on purpose -- the
Dashboard theme's "nothing playing" picture and the Video theme's clip
are both things *you* pick via the file browser buttons below; leave
them unset and you get sensible built-in fallbacks (a plain drawn
placeholder icon for the dashboard, and the video tab just won't start
until you choose a file).

Requires whatever the theme you use requires -- see README.md. The GUI
itself only needs Tkinter, which ships with Python already.

Running automatically at Windows startup: tick "Launch at Windows
startup" near the top of the window. That drops a small hidden-window
launcher script into your Startup folder (Windows only) that runs
`pythonw app.py --autostart` at login -- no console window, no need to
log in yourself. --autostart resumes whatever theme was last actually
streaming (falling back to Dashboard if nothing ever was), and (with
`pystray` installed) hides straight into the tray instead of leaving an
open taskbar entry. You can also run this manually:

    python app.py --autostart                  # resumes last theme, tray/minimized
    python app.py --autostart --theme video     # force a specific tab instead

Note on the Dashboard tab's web mirror: it's off by default for fresh
setups specifically because turning it on opens a network listener,
which makes Windows show a one-time firewall permission prompt the
first time it binds. That's expected -- it's what lets you view the
panel from a phone on the same network -- but if you don't want that
prompt (e.g. at every boot before you've clicked "Allow"), just leave
"Enable live web mirror" unticked.
"""

import argparse
import os
import sys
import threading
import queue
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from .driver import hongtai_screen
from .themes import dashboard_theme
from .themes import video_theme
from .themes import webpage_theme
from .themes import demo_clock

# Everything below used to live in app.py directly; Phase 1 of the v2.0
# rewrite (see ROADMAP.md) pulled out everything that isn't Tkinter into
# its own module, so the same logic can eventually be driven by a
# non-Tkinter UI too. This file is now just the Tkinter layer on top.
from .paths import ICON_PATH, SHOW_TRIGGER_PATH, _write_startup_log
from .config_store import (AUTO_DETECT, THEME_TAB_ORDER, load_config, save_config,
                            migrate_dashboard_elements, migrate_dashboard_weather_element,
                            migrate_strip_redundant_builtin_presets, migrate_unshadow_builtin_presets,
                            migrate_dashboard_preset_shape)
from .startup_registration import is_startup_enabled, enable_startup, disable_startup
from .desktop_shortcut import create_desktop_shortcut
from .single_instance import _ensure_single_instance
from .theme_worker import ThemeWorker
from . import tray_icon
from . import image_store
from . import power_state


class App(tk.Tk):
    def __init__(self, autostart=False, autostart_theme=None):
        self._autostart = autostart  # read by _log() -- see STARTUP_LOG_PATH
        if autostart:
            _write_startup_log(
                f"App.__init__ starting (autostart_theme={autostart_theme!r})")

        # Windows groups a window's taskbar entry (and picks its taskbar
        # icon) by "AppUserModelID" -- without explicitly setting one, a
        # python.exe/pythonw.exe-hosted app inherits Python's own AppID,
        # which is why the taskbar showed a generic python icon instead
        # of this app's. This has to happen before any window exists
        # (including Tkinter's own implicit root), so it's here, before
        # super().__init__(), not after.
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "HongtaiScreen.DesktopApp")
            except Exception:  # noqa: BLE001 -- cosmetic only, never fatal
                pass

        super().__init__()
        self.title("Hongtai Screen Control")
        # Real geometry is set once by _autosize_window(), after every
        # widget exists and Tk knows how much room they actually need --
        # see there for why a hardcoded size used to hide the log.

        # The window/taskbar icon itself -- separate from AppUserModelID
        # above (that's what picks the taskbar *grouping identity*; this
        # is the actual picture). iconbitmap needs a real .ico on Windows
        # (iconphoto's PNG/GIF route doesn't drive the taskbar icon the
        # same way), so this is a no-op with a quiet log line if
        # assets/icon.ico is missing for some reason -- never fatal.
        try:
            self.iconbitmap(ICON_PATH)
        except Exception as e:  # noqa: BLE001 -- cosmetic only, never fatal
            print(f"(couldn't set the window icon from {ICON_PATH}: {e})")

        self.cfg = load_config()
        migrated = migrate_dashboard_elements(self.cfg)
        migrated = migrate_dashboard_weather_element(self.cfg) or migrated
        # Presets are a web-canvas-only feature (this Tkinter GUI has no
        # preset UI of its own), but this one-time cleanup still runs
        # here too, same reasoning as the other migrations -- a config
        # launched via app.py first (e.g. `python app.py` for manual/
        # headless use) should get it exactly once, not just on
        # installs that happened to open the web UI first. See
        # config_store.py's own docstrings for why each is safe to run
        # unconditionally on every launch. (The built-ins themselves
        # are no longer copied into app_config.json at all -- see
        # config_store.resolve_dashboard_presets() -- so there's
        # nothing else to seed here.)
        migrated = migrate_strip_redundant_builtin_presets(self.cfg) or migrated
        migrated = migrate_unshadow_builtin_presets(self.cfg) or migrated
        migrated = migrate_dashboard_preset_shape(self.cfg) or migrated
        if migrated:
            save_config(self.cfg)
        self.log_queue = queue.Queue()
        self.worker = None
        self.stop_event = None
        self.running_theme = None
        self.running_tab_index = None
        self.active_screen = None  # set once connect() succeeds; lets a few
                                    # settings (brightness always, Dashboard's
                                    # art/web-mirror) apply live instead of
                                    # only taking effect on the next Start
        self._pending_restart = False  # set by _on_apply(); consumed by
                                        # _on_theme_finished() once the
                                        # worker it told to Stop actually
                                        # winds down -- see _on_apply()
        self._tray_icon = None  # a running pystray.Icon, once _start_tray()
                                 # succeeds -- see there for when that is
        self._poll_after_id = None  # the pending _poll_log_queue() timer,
                                     # cancelled before destroy() so Tk
                                     # doesn't try to fire it into a
                                     # destroyed window (harmless, but a
                                     # noisy "invalid command name" on exit)
        # Baseline reading, not None -- see _poll_show_trigger(). Reading
        # whatever's already on disk (rather than starting from "never
        # seen it") means a trigger left over from BEFORE this instance
        # even started (e.g. it crashed right after being touched, or
        # this app_config.json directory is shared some other way) never
        # causes a spurious self-show the moment this instance's own
        # polling starts -- only a touch that happens *after* this line
        # runs counts as "someone just asked to be shown".
        try:
            self._show_trigger_mtime = os.path.getmtime(SHOW_TRIGGER_PATH)
        except OSError:
            self._show_trigger_mtime = None
        # Which tab, if any, was actually streaming last -- set on a
        # successful Start, cleared on an explicit Stop or when a theme
        # ends on its own. Persisted to app_config.json so it survives a
        # restart; that's what lets the *next* launch resume it
        # automatically. See _save_current_config()/_on_start()/_on_stop().
        self._auto_resume_tab = self.cfg.get("auto_resume_tab")

        power_state.set_keep_active_when_locked(self.cfg.get("keep_active_when_locked", True))
        power_state.start_polling()

        self._build_widgets()
        self._autosize_window()
        self._refresh_ports()
        self._poll_log_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start the tray icon on every launch, not just --autostart --
        # this is what makes the window's X button dismiss to tray
        # (_on_close() checks self._tray_icon) instead of quitting and
        # stopping whatever theme is running, whether you started the app
        # by hand or it came up at boot. Manual launches still just show
        # the window normally; only --autostart also starts hidden.
        have_tray = self._start_tray()

        # Figure out what to auto-start, if anything: an explicit --theme
        # always wins; otherwise fall back to whatever was last actually
        # streaming (persisted across restarts); --autostart with neither
        # falls back to Dashboard. A plain launch with no --autostart
        # flag still resumes automatically if something was left running
        # -- that's the whole point of remembering it.
        if autostart_theme in THEME_TAB_ORDER:
            resume_theme = autostart_theme
        elif self._auto_resume_tab is not None and 0 <= self._auto_resume_tab < len(THEME_TAB_ORDER):
            resume_theme = THEME_TAB_ORDER[self._auto_resume_tab]
        elif autostart:
            resume_theme = "dashboard"
        else:
            resume_theme = None  # nothing to resume, and not explicitly asked to autostart

        if autostart:
            _write_startup_log(
                f"resume_theme={resume_theme!r}, have_tray={have_tray}, "
                f"auto_resume_tab(from config)={self._auto_resume_tab!r}")

        if resume_theme is not None:
            self.notebook.select(THEME_TAB_ORDER.index(resume_theme))
            if autostart:
                # The boot-time / explicit-flag path also hides the
                # window -- a plain "python app.py" that happens to
                # resume a remembered session still just shows normally.
                if have_tray:
                    # Tray icon is running and will bring the window
                    # back on request -- no need for a taskbar entry.
                    self.withdraw()
                else:
                    # No tray available (pystray not installed, or not
                    # Windows) -- fall back to a minimized-but-still-in-
                    # the-taskbar window rather than disappearing with
                    # no way back short of Task Manager.
                    self.iconify()
            self.after(300, self._on_start)

    def _autosize_window(self):
        """Size the window to what its widgets actually need instead of
        a hardcoded guess. The old fixed "760x620" was shorter than the
        Dashboard tab's content once it grew past the original 4 gauge
        dropdowns to 8 plus a background picker -- the window just
        stayed at 620px, silently cropping the log at the bottom off the
        visible window with nothing to indicate it was even there (no
        scrollbar; you had to know to manually resize/maximize).
        ttk.Notebook sizes itself to the largest of its tabs even when a
        smaller one is selected, so asking Tk for the required size here
        (after every widget exists, but before anything is drawn) gets
        the true size the Dashboard tab needs, not whatever tab happens
        to be showing.

        Capped to 90% of the screen in case that's smaller than the
        content (e.g. a small laptop panel) -- resizable() stays on
        (Tk's default), so a window that got capped can still be
        dragged bigger, and one that didn't can still be shrunk."""
        self.update_idletasks()
        req_w = max(self.winfo_reqwidth(), 760)
        req_h = max(self.winfo_reqheight(), 520)
        max_w = int(self.winfo_screenwidth() * 0.9)
        max_h = int(self.winfo_screenheight() * 0.9)
        w, h = min(req_w, max_w), min(req_h, max_h)
        self.geometry(f"{w}x{h}")
        self.minsize(min(640, w), min(480, h))

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def _build_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Panel port:").pack(side="left")
        self.port_var = tk.StringVar(value=self.cfg.get("port", AUTO_DETECT))
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=42, state="readonly")
        self.port_combo.pack(side="left", padx=(6, 6))
        ttk.Button(top, text="Refresh", command=self._refresh_ports).pack(side="left")

        ttk.Label(top, text="   Brightness:").pack(side="left", padx=(14, 0))
        self.brightness_var = tk.IntVar(value=self.cfg.get("brightness", 90))
        # A trace on the variable (not Scale's command=) -- ttk.Scale's
        # command only fires on a live mouse drag, not on the variable
        # being set programmatically, which made this silently do nothing.
        # A variable trace fires uniformly either way.
        self.brightness_var.trace_add("write", self._on_brightness_change)
        ttk.Scale(top, from_=10, to=100, orient="horizontal", variable=self.brightness_var,
                  length=120).pack(side="left", padx=(4, 0))
        # Numeric readout next to the slider -- width fixed so the layout
        # doesn't shift as the value goes from 1 to 3 digits.
        self.brightness_label = ttk.Label(top, text=str(self.brightness_var.get()), width=4)
        self.brightness_label.pack(side="left", padx=(4, 0))

        startup_row = ttk.Frame(self, padding=(10, 0, 10, 0))
        startup_row.pack(fill="x")
        self.startup_var = tk.BooleanVar(value=is_startup_enabled())
        startup_cb = ttk.Checkbutton(startup_row, text="Launch at Windows startup (resumes whatever was last running)",
                                      variable=self.startup_var, command=self._on_toggle_startup)
        startup_cb.pack(side="left")
        if sys.platform != "win32":
            # The VBS-launcher trick is Windows-only; show it disabled
            # with an explanation rather than hiding it, so it's obvious
            # why it can't be ticked on other platforms.
            startup_cb.configure(state="disabled")
            ttk.Label(startup_row, text="(Windows only)", foreground="#666").pack(side="left", padx=(6, 0))

        shortcut_btn = ttk.Button(startup_row, text="Create Desktop Shortcut",
                                   command=self._on_create_desktop_shortcut)
        shortcut_btn.pack(side="left", padx=(14, 0))
        if sys.platform != "win32":
            shortcut_btn.configure(state="disabled")

        # Mirrors the official XTRM Lab app's "Keep playing when screen
        # is off" setting -- see power_state.py's docstring for the full
        # reasoning. Global/system-level like "Launch at Windows
        # startup" above (not per-theme), so it lives in this same row.
        power_row = ttk.Frame(self, padding=(10, 4, 10, 0))
        power_row.pack(fill="x")
        self.keep_active_var = tk.BooleanVar(value=self.cfg.get("keep_active_when_locked", True))
        keep_active_cb = ttk.Checkbutton(
            power_row, text="Keep the panel updating while Windows is locked",
            variable=self.keep_active_var, command=self._on_toggle_keep_active)
        keep_active_cb.pack(side="left")
        if not power_state.IS_WINDOWS:
            keep_active_cb.configure(state="disabled")
            ttk.Label(power_row, text="(Windows only)", foreground="#666").pack(side="left", padx=(6, 0))
        else:
            ttk.Label(power_row, text="-- unticking freezes the panel on whatever it last showed until you unlock",
                       foreground="#666").pack(side="left", padx=(6, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.dashboard_tab = self._build_dashboard_tab()
        self.video_tab = self._build_video_tab()
        self.webpage_tab = self._build_webpage_tab()
        self.clock_tab = self._build_clock_tab()
        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.video_tab, text="Video")
        self.notebook.add(self.webpage_tab, text="Webpage Mirror")
        self.notebook.add(self.clock_tab, text="Clock")
        self.notebook.select(self.cfg.get("active_tab", 0))

        controls = ttk.Frame(self, padding=(10, 0, 10, 10))
        controls.pack(fill="x")
        self.start_btn = ttk.Button(controls, text="Start", command=self._on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        # A one-click restart for the settings that need Stop+Start to
        # apply (gauge layout, background, a new video file, ...) -- see
        # _on_apply()'s docstring. Only meaningful while something's
        # actually running, so it tracks stop_btn's enabled state.
        self.apply_btn = ttk.Button(controls, text="Apply (restart)", command=self._on_apply, state="disabled")
        self.apply_btn.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(controls, textvariable=self.status_var).pack(side="left", padx=(14, 0))

        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=False)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled",
                                                    font=("Consolas", 9) if os.name == "nt" else ("Menlo", 10))
        self.log_text.pack(fill="both", expand=True)

    def _build_dashboard_tab(self):
        f = ttk.Frame(self.notebook, padding=12)
        d = self.cfg.get("dashboard", {})
        row = 0

        # Off by default for a fresh config -- opening this listens for
        # network connections, which makes Windows show a one-time
        # firewall permission prompt the first time it binds. That's
        # expected if you actually want to view the panel from a phone
        # on the same network, but it's a surprise otherwise (e.g. at
        # every boot via "Launch at Windows startup" before you've
        # clicked Allow), so it's opt-in rather than on by default.
        self.dash_web_enable = tk.BooleanVar(value=d.get("enable_web", False))
        ttk.Checkbutton(f, text="Enable live web mirror (view from your phone -- opens a network port,"
                                 "\ntriggers a one-time Windows firewall prompt)",
                        variable=self.dash_web_enable,
                        command=self._apply_dash_web_settings).grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        ttk.Label(f, text="Web mirror port:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.dash_web_port = tk.StringVar(value=str(d.get("web_port", 8765)))
        ttk.Entry(f, textvariable=self.dash_web_port, width=10).grid(row=row, column=1, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Apply", command=self._apply_dash_web_settings).grid(
            row=row, column=2, sticky="w", padx=(6, 0))
        ttk.Button(f, text="Open in browser", command=self._open_dash_web_mirror).grid(
            row=row, column=3, sticky="w", padx=(6, 0))
        row += 1

        ttk.Label(f, text="Weather and the now-playing display are both movable/resizable\n"
                          "elements now, added (and configured -- location, units, which\n"
                          "pieces to show) from the web design canvas rather than from here --\n"
                          "click \"+ Add weather\" or \"+ Add now-playing\" there.",
                  foreground="#666").grid(row=row, column=0, columnspan=4, sticky="w", pady=(16, 4))
        row += 1

        ttk.Label(f, text="\"Nothing playing\" image:").grid(row=row, column=0, sticky="w", pady=(12, 0))
        row += 1
        self.dash_art_path = tk.StringVar(value=d.get("default_art_path", "") or "")
        self.dash_art_path.trace_add("write", self._on_dash_art_change)
        ttk.Entry(f, textvariable=self.dash_art_path, width=44).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Button(f, text="Browse...", command=self._pick_dash_art).grid(row=row, column=2, sticky="w", padx=(6, 0))
        row += 1
        ttk.Button(f, text="Clear (use plain placeholder)", command=lambda: self.dash_art_path.set("")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        row += 1

        ttk.Label(f, text="Nothing is bundled here -- pick your own image, or leave this blank\n"
                          "and a plain drawn placeholder is used instead. Applies live -- no\n"
                          "need to Stop/Start.",
                  foreground="#666").grid(row=row, column=0, columnspan=4, sticky="w", pady=(10, 0))
        row += 1

        ttk.Label(f, text="\"Nothing playing\" message:").grid(row=row, column=0, sticky="w", pady=(12, 0))
        row += 1
        self.dash_not_playing_message = tk.StringVar(value=d.get("not_playing_message", "") or "")
        self.dash_not_playing_message.trace_add("write", self._on_dash_message_change)
        ttk.Entry(f, textvariable=self.dash_not_playing_message, width=54).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        row += 1
        ttk.Label(f, text="Shown in place of the track title when nothing is playing. Leave\n"
                          "blank for the default line. Applies live -- no need to Stop/Start.",
                  foreground="#666").grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        row += 1

        # --- background -----------------------------------------------
        ttk.Label(f, text="Background:", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", pady=(16, 0))
        row += 1

        bg_cfg = dict(dashboard_theme.DEFAULT_BACKGROUND, **d.get("background", {}))
        bg_labels = list(dashboard_theme.BACKGROUND_PRESETS.values())
        self._bg_label_to_key = {v: k for k, v in dashboard_theme.BACKGROUND_PRESETS.items()}
        self.dash_bg_mode = tk.StringVar(
            value=dashboard_theme.BACKGROUND_PRESETS.get(bg_cfg["mode"], bg_labels[0]))
        ttk.Label(f, text="Style:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(f, textvariable=self.dash_bg_mode, values=bg_labels, state="readonly", width=26).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        row += 1

        scheme_labels = [v["label"] for v in dashboard_theme.BACKGROUND_COLOR_SCHEMES.values()]
        self._bg_scheme_label_to_key = {v["label"]: k for k, v in dashboard_theme.BACKGROUND_COLOR_SCHEMES.items()}
        scheme_key = bg_cfg.get("scheme", dashboard_theme.DEFAULT_SCHEME)
        self.dash_bg_scheme = tk.StringVar(
            value=dashboard_theme.BACKGROUND_COLOR_SCHEMES.get(
                scheme_key, dashboard_theme.BACKGROUND_COLOR_SCHEMES[dashboard_theme.DEFAULT_SCHEME])["label"])
        ttk.Label(f, text="Color scheme:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(f, textvariable=self.dash_bg_scheme, values=scheme_labels, state="readonly", width=26).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(f, text="Custom image:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.dash_bg_image_path = tk.StringVar(value=bg_cfg.get("image_path") or "")
        ttk.Entry(f, textvariable=self.dash_bg_image_path, width=34).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Browse...", command=self._pick_dash_bg_image).grid(
            row=row, column=3, sticky="w", padx=(6, 0))
        row += 1

        ttk.Label(f, text="Color scheme is ignored for \"Custom image\" and the built-in\n"
                          "picture styles (Aurora Glow, Deep Nebula, Synthwave Sunset, Bokeh\n"
                          "Night) -- those are photos, not tinted. \"Custom image\" itself is\n"
                          "only used when Style is \"Custom image\"; either way, a photo is\n"
                          "cropped to fit and darkened a bit so the gauges stay readable.",
                  foreground="#666").grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
        row += 1

        # --- gauge layout -----------------------------------------------
        # Per-slot stat pickers. Labels are what the user sees in the
        # dropdown; _dashboard_kwargs() translates the selection back to
        # a dashboard_theme.STAT_DEFS key before handing it to the theme.
        # Laid out as 2 columns (left-column slots, right-column slots)
        # side by side rather than 8 rows stacked -- same information,
        # about half the vertical space, and it mirrors how the slots
        # actually sit on the panel itself (left/right of the album art).
        ttk.Label(f, text="Gauge layout:", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", pady=(16, 0))
        row += 1

        stat_labels = [v["label"] for v in dashboard_theme.STAT_DEFS.values()]
        self._stat_label_to_key = {v["label"]: k for k, v in dashboard_theme.STAT_DEFS.items()}
        saved_slots = dict(dashboard_theme.DEFAULT_SLOTS, **d.get("slots", {}))

        self.dash_slot_vars = {}
        slot_pairs = [
            ("top_left", "Top-left (CPU col):", "top_right", "Top-right (GPU col):"),
            ("bottom_left", "Bottom-left (CPU col):", "bottom_right", "Bottom-right (GPU col):"),
            ("left_secondary", "Left secondary (by art):", "right_secondary", "Right secondary (by art):"),
            ("left_mini", "Left mini (by clock):", "right_mini", "Right mini (by clock):"),
        ]

        def add_slot_picker(slot_key, label, col):
            ttk.Label(f, text=label).grid(row=row, column=col, sticky="w", pady=(6, 0),
                                            padx=(20, 0) if col else 0)
            stat_key = saved_slots.get(slot_key, dashboard_theme.DEFAULT_SLOTS[slot_key])
            var = tk.StringVar(value=dashboard_theme.STAT_DEFS[stat_key]["label"])
            self.dash_slot_vars[slot_key] = var
            ttk.Combobox(f, textvariable=var, values=stat_labels, state="readonly", width=14).grid(
                row=row, column=col + 1, sticky="w", pady=(6, 0))

        for left_key, left_label, right_key, right_label in slot_pairs:
            add_slot_picker(left_key, left_label, 0)
            add_slot_picker(right_key, right_label, 2)
            row += 1

        ttk.Label(f, text="Gauge layout and background are both baked into the panel image --\n"
                          "Stop then Start (or just hit Apply below) to see changes. Every\n"
                          "gauge can show any stat -- pick whatever's most useful to you.",
                  foreground="#666").grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))
        return f

    def _build_video_tab(self):
        f = ttk.Frame(self.notebook, padding=12)
        v = self.cfg.get("video", {})

        ttk.Label(f, text="Video file:").grid(row=0, column=0, sticky="w")
        self.video_path = tk.StringVar(value=v.get("path", "") or "")
        ttk.Entry(f, textvariable=self.video_path, width=48).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Browse...", command=self._pick_video).grid(row=1, column=2, sticky="w", padx=(6, 0))

        self.video_loop = tk.BooleanVar(value=v.get("loop", True))
        ttk.Checkbutton(f, text="Loop when it ends", variable=self.video_loop).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.video_bw = tk.BooleanVar(value=v.get("bw", False))
        ttk.Checkbutton(f, text="Force black & white", variable=self.video_bw).grid(
            row=3, column=0, columnspan=3, sticky="w")
        self.video_audio = tk.BooleanVar(value=v.get("audio", False))
        ttk.Checkbutton(f, text="Also play audio (needs ffmpeg + pygame)", variable=self.video_audio).grid(
            row=4, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="FPS override (blank = use the video's own rate):").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.video_fps = tk.StringVar(value=str(v.get("fps", "")) if v.get("fps") else "")
        ttk.Entry(f, textvariable=self.video_fps, width=10).grid(row=6, column=0, sticky="w")

        ttk.Label(f, text="No video is bundled with this app -- point it at any file you have.",
                  foreground="#666").grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(f, text="Changing these while running needs Stop then Start again (or just\n"
                          "hit Apply below) -- a different file/rate means reopening the video.",
                  foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return f

    def _build_webpage_tab(self):
        f = ttk.Frame(self.notebook, padding=12)
        w = self.cfg.get("webpage", {})

        ttk.Label(f, text="URL:").grid(row=0, column=0, sticky="w")
        self.webpage_url = tk.StringVar(value=w.get("url", ""))
        ttk.Entry(f, textvariable=self.webpage_url, width=52).grid(row=1, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="Screenshot interval (seconds):").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.webpage_interval = tk.StringVar(value=str(w.get("interval", 0.1)))
        ttk.Entry(f, textvariable=self.webpage_interval, width=10).grid(row=3, column=0, sticky="w")

        ttk.Label(f, text="Force a full reload every N seconds (blank = never):").grid(
            row=4, column=0, sticky="w", pady=(10, 0))
        self.webpage_reload = tk.StringVar(
            value=str(w.get("reload_every", "")) if w.get("reload_every") else "")
        ttk.Entry(f, textvariable=self.webpage_reload, width=10).grid(row=5, column=0, sticky="w")

        ttk.Label(f, text="Needs Playwright (pip install playwright, then\n"
                          "playwright install chromium) -- a real page, best kept simple\n"
                          "and landscape (a status page, clock, weather widget, your own .html file).",
                  foreground="#666").grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(f, text="Changing these while running needs Stop then Start again (or just\n"
                          "hit Apply below).",
                  foreground="#666").grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return f

    def _build_clock_tab(self):
        f = ttk.Frame(self.notebook, padding=12)
        ttk.Label(f, text="A live clock with CPU/RAM bars -- no settings beyond the\n"
                          "port and brightness above.", foreground="#666").pack(anchor="w")
        return f

    # ------------------------------------------------------------------ #
    # file pickers
    # ------------------------------------------------------------------ #
    def _pick_dash_art(self):
        path = filedialog.askopenfilename(
            title="Choose an image for \"nothing playing\"",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")])
        if not path:
            return
        # Copy it into this app's own managed image folder rather than
        # remembering the browsed path verbatim -- so moving, renaming,
        # or deleting the original afterward doesn't quietly break the
        # placeholder (see image_store.py).
        try:
            stored_path = image_store.store_image_file(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Couldn't use that image", str(e))
            return
        self.dash_art_path.set(stored_path)

    def _pick_dash_bg_image(self):
        path = filedialog.askopenfilename(
            title="Choose a background image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")])
        if not path:
            return
        try:
            stored_path = image_store.store_image_file(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Couldn't use that image", str(e))
            return
        self.dash_bg_image_path.set(stored_path)
        # Picking a file implies you want it used -- flip Style over
        # to "Custom image" too instead of leaving it silently ignored
        # because Style was still set to something else.
        self.dash_bg_mode.set(dashboard_theme.BACKGROUND_PRESETS["image"])

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="Choose a video file",
            filetypes=[("Videos", "*.mp4 *.mkv *.avi *.mov *.webm"), ("All files", "*.*")])
        if path:
            self.video_path.set(path)

    # ------------------------------------------------------------------ #
    # ports
    # ------------------------------------------------------------------ #
    def _refresh_ports(self):
        try:
            candidates = hongtai_screen.find_hongtai_ports()
        except Exception as e:  # noqa: BLE001
            candidates = []
            self._log(f"(couldn't scan serial ports: {e})")
        values = [AUTO_DETECT] + [c.label for c in candidates]
        self._port_devices = {c.label: c.device for c in candidates}
        self.port_combo["values"] = values
        if self.port_var.get() not in values:
            self.port_var.set(AUTO_DETECT)

    def _selected_port(self):
        label = self.port_var.get()
        if label == AUTO_DETECT:
            return None
        return self._port_devices.get(label, None)

    # ------------------------------------------------------------------ #
    # logging (thread-safe: workers only ever put() onto the queue)
    # ------------------------------------------------------------------ #
    def _log(self, msg):
        self.log_queue.put(str(msg))
        if self._autostart:
            # See STARTUP_LOG_PATH's comment -- the in-memory queue above
            # is all a normally-launched window needs, but an autostart
            # launch is usually hidden/minimized with nobody watching it,
            # so mirror everything to disk too.
            _write_startup_log(str(msg))

    def _poll_log_queue(self):
        drained = False
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            drained = True
        if drained and self.worker is not None and not self.worker.is_alive():
            # the background thread finished on its own (video ended
            # without --loop, a connection error, etc.) -- reset the UI
            self._on_theme_finished()
        self._poll_show_trigger()
        try:
            self._poll_after_id = self.after(100, self._poll_log_queue)
        except tk.TclError:
            # The window was destroyed (e.g. Quit from the tray menu)
            # while this was scheduled -- nothing left to poll into.
            pass

    def _poll_show_trigger(self):
        """Piggybacks on this same 100ms timer to check whether another
        launch touched SHOW_TRIGGER_PATH -- see single_instance.py's
        _bring_existing_window_to_front(). Double-clicking the desktop
        icon (or the .lnk) while this instance is already running is
        the single most common way that happens: rather than a second
        copy either failing silently or popping up a "already running"
        message box (what this used to do), this instance just shows
        itself -- the way double-clicking the icon for any normal
        single-window app behaves. mtime (not existence) is the signal,
        since the file is created once and then only ever touched again
        -- comparing against the last-seen mtime is what lets this fire
        again on a THIRD launch, a fourth, and so on, not just the
        first one after this instance started."""
        try:
            mtime = os.path.getmtime(SHOW_TRIGGER_PATH)
        except OSError:
            return
        if mtime != self._show_trigger_mtime:
            self._show_trigger_mtime = mtime
            self._show_window()

    # ------------------------------------------------------------------ #
    # start / stop
    # ------------------------------------------------------------------ #
    def _on_start(self):
        if self.worker is not None and self.worker.is_alive():
            return

        tab_index = self.notebook.index(self.notebook.select())
        port = self._selected_port()
        brightness = self.brightness_var.get()

        try:
            if tab_index == 0:
                theme_name, target, kwargs = self._dashboard_kwargs(port, brightness)
            elif tab_index == 1:
                theme_name, target, kwargs = self._video_kwargs(port, brightness)
            elif tab_index == 2:
                theme_name, target, kwargs = self._webpage_kwargs(port, brightness)
            else:
                theme_name, target, kwargs = self._clock_kwargs(port, brightness)
        except ValueError as e:
            messagebox.showerror("Can't start", str(e))
            return

        self.stop_event = threading.Event()
        kwargs["stop_event"] = self.stop_event
        kwargs["log"] = self._log
        kwargs["on_connected"] = self._on_screen_connected

        # Remember this as "what to resume next launch" -- cleared again
        # on an explicit Stop or if this theme ends on its own (see
        # _on_stop() / _on_theme_finished()), so it only carries over
        # into the next launch if the app was closed/quit while this was
        # still actually running.
        self._auto_resume_tab = tab_index
        self._save_current_config()
        self.running_theme = theme_name
        self.running_tab_index = tab_index
        self.status_var.set(f"Running: {theme_name}")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.apply_btn.configure(state="normal")
        self.notebook.tab(0, state="disabled" if tab_index != 0 else "normal")

        # ThemeWorker (theme_worker.py) owns the actual thread + the
        # retry/blind_restart recovery loop -- see README's "If the
        # panel stops responding" section for why a plain reconnect
        # isn't enough on its own for a mid-stream wedge.
        self.worker = ThemeWorker(target, kwargs, self._log)
        self.worker.start()

    def _on_screen_connected(self, screen):
        """Called from the worker thread right after connect() succeeds
        (see the on_connected= kwarg each theme's run() takes). Just
        stashes the reference -- plain attribute assignment is safe from
        another thread under the GIL, and this is all a GUI callback
        needs to start applying settings live via screen.set_brightness()
        etc. instead of only on the next Start."""
        self.active_screen = screen

    def _on_brightness_change(self, *_args):
        """Fires on every change to the slider's variable, including every
        intermediate value during a drag.

        Brightness is applied by the screen driver as a per-frame software
        dim (a black overlay blended over each rendered frame before it's
        sent) rather than relying on the panel's hardware brightness
        command, which -- per the official app's own source -- doesn't
        appear to do much visibly on this panel by itself. Because it's
        just a number the render loop reads on its next frame, this call
        is instant and non-blocking: no reconnect, no interruption, so no
        debouncing/threading is needed here either."""
        value = self.brightness_var.get()
        self.brightness_label.configure(text=str(value))
        if self.active_screen is None:
            return  # nothing running yet -- takes effect on the next Start instead
        try:
            self.active_screen.set_brightness(value)
        except Exception as e:  # noqa: BLE001
            self._log(f"(brightness change failed: {e})")

    def _on_dash_art_change(self, *_args):
        # Fires on every edit to the art-path field (typing, Browse,
        # Clear) -- dashboard_theme re-checks this path every frame, so
        # this genuinely takes effect on the very next frame, no restart.
        dashboard_theme.set_default_art_path(self.dash_art_path.get().strip() or None)

    def _on_dash_message_change(self, *_args):
        # Same "applies live, no restart" reasoning as _on_dash_art_
        # change() above -- render_frame() calls get_not_playing_
        # message() fresh every frame rather than baking it into the
        # static background.
        dashboard_theme.set_not_playing_message(self.dash_not_playing_message.get())

    def _apply_dash_web_settings(self):
        if not (self.running_tab_index == 0 and self.active_screen is not None):
            return  # nothing running yet -- takes effect on the next Start instead
        try:
            port = self._parse_int(self.dash_web_port.get(), "Web mirror port", default=8765)
        except ValueError as e:
            self._log(f"(web mirror: {e})")
            return
        # Always disable first -- enable_web_mirror() is a no-op if a
        # mirror is already running, which is exactly what would silently
        # swallow a port change otherwise. Both calls route their status/
        # error lines through self._log so they land in the Log panel --
        # see enable_web_mirror()'s docstring for why that matters more
        # than it looks like it should.
        self.active_screen.disable_web_mirror(log=self._log)
        if self.dash_web_enable.get():
            self.active_screen.enable_web_mirror(port=port, log=self._log)

    def _open_dash_web_mirror(self):
        """Opens the mirror page in the system's default browser --
        localhost, not the LAN IP enable_web_mirror() logs, since this
        machine can always reach itself there regardless of network
        setup. Checks the *actual* live state (HongtaiScreen.web_mirror_
        enabled) rather than just the checkbox, since the checkbox can
        say "on" while nothing's actually listening yet (Dashboard not
        started, or a bind failure already logged above)."""
        is_live = self.active_screen is not None and self.active_screen.web_mirror_enabled
        if not is_live:
            messagebox.showinfo(
                "Web mirror",
                "The web mirror isn't running right now.\n\n"
                "Make sure the Dashboard is started and \"Enable live web "
                "mirror\" is checked -- check the Log for a port error if "
                "you've already done both.")
            return
        try:
            port = self._parse_int(self.dash_web_port.get(), "Web mirror port", default=8765)
        except ValueError as e:
            self._log(f"(web mirror: {e})")
            return
        webbrowser.open(f"http://localhost:{port}/")

    def _on_toggle_startup(self):
        """Ticking this writes (or removes) a small hidden-window launcher
        script in the Windows Startup folder -- see enable_startup()'s
        docstring for why a .vbs rather than a .bat or a shortcut."""
        try:
            if self.startup_var.get():
                enable_startup(log=self._log)
                self._log("Launch at Windows startup: enabled "
                          "(resumes whatever theme was last running, or Dashboard if nothing was).")
            else:
                disable_startup(log=self._log)
                self._log("Launch at Windows startup: disabled.")
        except Exception as e:  # noqa: BLE001
            self._log(f"(couldn't update Windows startup launcher: {e})")
            messagebox.showerror("Launch at startup", str(e))
            # Reflect what's actually on disk rather than the failed click.
            self.startup_var.set(is_startup_enabled(log=self._log))

    def _on_toggle_keep_active(self):
        # Applies immediately, same as brightness -- power_state.should_
        # pause() is checked fresh by every theme's render loop on every
        # frame, so there's nothing to restart. Persisted here too (not
        # just applied live) so it survives to the next launch, same as
        # every other Dashboard/global setting _save_current_config()
        # writes.
        value = self.keep_active_var.get()
        power_state.set_keep_active_when_locked(value)
        self._log(f"Keep panel updating while locked: {'enabled' if value else 'disabled'}.")
        self._save_current_config()

    def _on_create_desktop_shortcut(self):
        """"Create Desktop Shortcut" button next to the startup checkbox
        -- see create_desktop_shortcut()'s docstring for what it actually
        points at (the exe itself in a frozen build, the hidden .vbs
        launcher when running from source)."""
        try:
            shortcut_path = create_desktop_shortcut()
        except Exception as e:  # noqa: BLE001 -- shown to the user either way
            self._log(f"(couldn't create desktop shortcut: {e})")
            messagebox.showerror("Create Desktop Shortcut", str(e))
            return
        self._log(f"Desktop shortcut created: {shortcut_path}")
        messagebox.showinfo(
            "Create Desktop Shortcut",
            f'"Hongtai Screen" shortcut added to your Desktop.\n\n{shortcut_path}')

    def _on_apply(self):
        """One click instead of Stop-then-notice-it-stopped-then-Start,
        for settings that only take effect on a fresh Start -- gauge
        layout, background, a changed video file/URL, etc. Stops
        whatever's running; once _on_theme_finished() sees the worker
        thread has actually wound down (not just been asked to), it
        starts things back up again with whatever the tabs currently
        say -- exactly what a manual Stop then Start would do, just
        without you having to notice the log settle and click Start
        yourself."""
        if self.worker is None or not self.worker.is_alive():
            return  # nothing running to restart -- Start does this already
        self._pending_restart = True
        self._on_stop()

    def _on_stop(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.status_var.set("Stopping...")
        self.stop_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        # A deliberate Stop means "don't resume this automatically next
        # launch" -- persisted right away rather than waiting for
        # _on_theme_finished() (which only fires once the worker thread
        # actually winds down), so it's not lost if the app is closed or
        # crashes before that happens.
        self._auto_resume_tab = None
        self._save_current_config()

    def _on_theme_finished(self):
        self.worker = None
        self.stop_event = None
        self.running_theme = None
        self.running_tab_index = None
        self.active_screen = None
        self.status_var.set("Idle")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        for i in range(4):
            self.notebook.tab(i, state="normal")
        if self._auto_resume_tab is not None:
            # The theme ended on its own (video finished without --loop,
            # a connection error, etc.) rather than via Stop -- nothing
            # is actually running any more, so there's nothing to resume.
            self._auto_resume_tab = None
            self._save_current_config()
        if self._pending_restart:
            # _on_apply() stopped this on purpose so it could come back
            # up with the current settings -- now that the worker thread
            # has actually wound down (port closed and all, not just
            # asked to), it's safe to start it again. The short delay
            # gives the OS a moment to fully release the serial port
            # before reopening it.
            self._pending_restart = False
            self.status_var.set("Restarting...")
            self.after(300, self._on_start)

    # ------------------------------------------------------------------ #
    # per-theme kwarg building
    # ------------------------------------------------------------------ #
    def _dashboard_kwargs(self, port, brightness):
        web_port = self._parse_int(self.dash_web_port.get(), "Web mirror port", default=8765)
        art_path = self.dash_art_path.get().strip() or None
        not_playing_message = self.dash_not_playing_message.get().strip() or None
        slots = {slot_key: self._stat_label_to_key.get(var.get(), dashboard_theme.DEFAULT_SLOTS[slot_key])
                  for slot_key, var in self.dash_slot_vars.items()}
        background = self._dash_background_dict()
        return "Dashboard", dashboard_theme.run, dict(
            port=port, web_port=web_port, enable_web=self.dash_web_enable.get(),
            default_art_path=art_path, not_playing_message=not_playing_message,
            brightness=brightness, slots=slots, background=background,
        )

    def _dash_background_dict(self):
        return {
            "mode": self._bg_label_to_key.get(self.dash_bg_mode.get(), "default"),
            "scheme": self._bg_scheme_label_to_key.get(self.dash_bg_scheme.get(), dashboard_theme.DEFAULT_SCHEME),
            "image_path": self.dash_bg_image_path.get().strip() or None,
        }

    def _video_kwargs(self, port, brightness):
        path = self.video_path.get().strip()
        if not path:
            raise ValueError("Pick a video file first.")
        if not os.path.isfile(path):
            raise ValueError(f"Video file not found:\n{path}")
        fps = self._parse_float(self.video_fps.get(), "FPS", default=None, allow_blank=True)
        return "Video", video_theme.run, dict(
            video_path=path, port=port, fps=fps, bw=self.video_bw.get(),
            audio=self.video_audio.get(), loop=self.video_loop.get(), brightness=brightness,
        )

    def _webpage_kwargs(self, port, brightness):
        url = self.webpage_url.get().strip()
        if not url:
            raise ValueError("Enter a URL first.")
        interval = self._parse_float(self.webpage_interval.get(), "Interval", default=0.1)
        reload_every = self._parse_float(self.webpage_reload.get(), "Reload interval",
                                          default=None, allow_blank=True)
        return "Webpage Mirror", webpage_theme.run, dict(
            url=url, port=port, interval=interval, reload_every=reload_every, brightness=brightness,
        )

    def _clock_kwargs(self, port, brightness):
        return "Clock", demo_clock.run, dict(port=port, brightness=brightness)

    @staticmethod
    def _parse_int(text, label, default=None):
        text = (text or "").strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{label} must be a whole number.")

    @staticmethod
    def _parse_float(text, label, default=None, allow_blank=False):
        text = (text or "").strip()
        if not text:
            if allow_blank or default is not None:
                return default
            raise ValueError(f"{label} is required.")
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{label} must be a number.")

    # ------------------------------------------------------------------ #
    # config persistence
    # ------------------------------------------------------------------ #
    def _save_current_config(self):
        # Merge into the existing "dashboard" dict rather than replacing
        # it wholesale -- this tab only has controls for a subset of its
        # keys (enable_web/web_port/default_art_path/not_playing_message/
        # slots/background). "elements" and "presets" are owned by the
        # web UI's design canvas (see controller.py's
        # save_dashboard_elements()/save_dashboard_preset()) and never
        # have Tkinter controls at all -- overwriting the dict from
        # scratch here would silently wipe out any layout/presets saved
        # from the web canvas the moment this Tkinter window saves
        # anything (Stop, closing to tray, changing any other tab...).
        dashboard_cfg = dict(self.cfg.get("dashboard") or {})
        dashboard_cfg.update({
            "enable_web": self.dash_web_enable.get(),
            "web_port": self.dash_web_port.get(),
            "default_art_path": self.dash_art_path.get().strip() or None,
            "not_playing_message": self.dash_not_playing_message.get().strip() or None,
            "slots": {slot_key: self._stat_label_to_key.get(
                          var.get(), dashboard_theme.DEFAULT_SLOTS[slot_key])
                      for slot_key, var in self.dash_slot_vars.items()},
            "background": self._dash_background_dict(),
        })
        self.cfg.update({
            "port": self.port_var.get(),
            "brightness": self.brightness_var.get(),
            "active_tab": self.notebook.index(self.notebook.select()),
            "auto_resume_tab": self._auto_resume_tab,
            "keep_active_when_locked": self.keep_active_var.get(),
            "dashboard": dashboard_cfg,
            "video": {
                "path": self.video_path.get().strip() or None,
                "loop": self.video_loop.get(),
                "bw": self.video_bw.get(),
                "audio": self.video_audio.get(),
                "fps": self.video_fps.get().strip() or None,
            },
            "webpage": {
                "url": self.webpage_url.get().strip(),
                "interval": self.webpage_interval.get().strip(),
                "reload_every": self.webpage_reload.get().strip() or None,
            },
        })
        save_config(self.cfg)

    def _cancel_poll_timer(self):
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:  # noqa: BLE001
                pass
            self._poll_after_id = None

    def _on_close(self):
        if self._tray_icon is not None:
            # A tray icon is running -- the window's X button hides to
            # tray rather than quitting (matching normal tray-app
            # behavior); the tray menu's "Quit" is what actually exits.
            self._save_current_config()
            self.withdraw()
            return
        self._save_current_config()
        if self.stop_event is not None:
            self.stop_event.set()
        self._cancel_poll_timer()
        self.destroy()

    # ------------------------------------------------------------------ #
    # system tray (Windows + pystray only -- see tray_icon.py)
    # ------------------------------------------------------------------ #
    def _start_tray(self):
        """Starts a system tray icon so the app can run with no taskbar
        presence at all -- right-click it for Show/Stop screen/Quit.
        Windows-only (matches the "Launch at Windows startup" feature
        this exists for) and needs the optional `pystray` package
        (`pip install pystray`); returns False without one, so callers
        know to fall back to a plain minimized window instead of the app
        disappearing with no way back."""
        if not tray_icon.available():
            return False
        try:
            icon = tray_icon.TrayIcon(
                on_show=self._tray_show,
                on_stop_screen=self._tray_stop_screen,
                on_quit=self._tray_quit,
            )
            icon.start()
            self._tray_icon = icon
            return True
        except Exception as e:  # noqa: BLE001 -- a tray icon is a nice-to-have, never fatal
            self._log(f"(couldn't start the system tray icon: {e})")
            return False

    # Tray menu callbacks run on pystray's own background thread, not the
    # Tk main thread -- each hops back onto the Tk thread via after(0, ...)
    # rather than touching widgets directly.
    def _tray_show(self):
        self.after(0, self._show_window)

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_stop_screen(self):
        self.after(0, self._on_stop)

    def _tray_quit(self):
        self.after(0, self._quit_for_real)

    def _quit_for_real(self):
        self._tray_icon = None
        self._save_current_config()
        if self.stop_event is not None:
            self.stop_event.set()
        self._cancel_poll_timer()
        self.destroy()


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--autostart", action="store_true",
                     help="select a theme tab and start it immediately (minimized), "
                          "instead of waiting for you to click Start. Used by the "
                          "Windows startup launcher; also handy to run by hand.")
    ap.add_argument("--theme", choices=THEME_TAB_ORDER, default=None,
                     help="force a specific tab for --autostart. Default: resume "
                          "whatever theme was last actually running, or dashboard "
                          "if nothing ever was.")
    return ap.parse_args()


def main():
    """The actual entry point -- called by root app.py (a thin launcher
    that puts src/ on sys.path; see its own docstring) via
    `from hongtai_screen_app.app import main`. Split into its own
    function, rather than living directly under `if __name__ ==
    "__main__":`, purely so it has something importable to be called
    from -- this module still runs standalone too (`python -m
    hongtai_screen_app.app` from src/), unchanged behavior either way.
    """
    args = parse_args()
    if args.autostart:
        # First thing that happens in the whole process -- if this line
        # never shows up in startup_debug.log after a reboot, Windows
        # never actually ran the Startup-folder .vbs at all (check Task
        # Manager's Startup Apps tab -- it can show a script as
        # "Disabled" even though the file itself is still sitting
        # there), rather than anything in this script failing.
        _write_startup_log(f"=== process started, argv={sys.argv!r} ===")
    try:
        if _ensure_single_instance():
            if args.autostart:
                _write_startup_log("single-instance check passed -- constructing App")
            App(autostart=args.autostart, autostart_theme=args.theme).mainloop()
            if args.autostart:
                _write_startup_log("mainloop() returned -- app closed normally")
        elif args.autostart:
            _write_startup_log(
                "single-instance check FAILED -- another copy's mutex is "
                "already held (a previous autostart attempt still running "
                "or stuck, most likely), so this launch exited without "
                "ever opening a window")
    except Exception:
        if args.autostart:
            import traceback
            _write_startup_log("UNCAUGHT EXCEPTION during startup:\n"
                                + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
