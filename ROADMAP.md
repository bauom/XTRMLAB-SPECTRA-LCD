# Roadmap — v2.0 UI rewrite

Plan for replacing the Tkinter desktop app with a webview + Python
backend architecture, and turning the Dashboard's fixed 8-slot layout
into a free-form design canvas.

Status: **Phase 0 done, validated on real hardware/OS.** Design updated
to a two-process split based on real measurements (see below). v1.0.0
(the current Tkinter app) stays the shipped, working version throughout
the rest of this work.

## Why

Three of the four wanted features are the same underlying problem:

- **Free-form dashboard layout** — drag gauges anywhere, not 8 fixed slots.
- **More design options** — per-element colors, fonts, sizes, rotation,
  opacity, instead of a handful of global dropdowns.
- **Live preview inside the app** — no browser tab needed.

All three are satisfied by the same thing: a real canvas editor where
each element is a selectable object with its own properties. Tkinter
can't get there without hand-rolling hit-testing, resize handles,
rotation math and snapping from scratch, and it would still look dated.

RGB fan control is **out of scope** — that's the motherboard's domain
(Gigabyte GCC), not this case's panel, and GCC already handles it fine.
If it's ever wanted, Gigabyte boards are well supported by OpenRGB, so
it'd be an SDK integration rather than a reverse-engineering project.

There's a hard requirement driving the architecture below: **background
(no window open) RAM must stay under 100MB.** Not everyone running this
has 32GB+ to spare.

## Architecture

Two separate OS processes, not one app with a UI bolted on:

```
┌──────────────────────────────────────────────┐   spawned on open,
│ UI process (pywebview / WebView2)             │   KILLED outright
│   React frontend, served over localhost       │   on close --
└───────────────┬──────────────────────────────┘   costs ~390MB while
                │ HTTP / WebSocket on 127.0.0.1      open, ~0MB when not
┌───────────────┴──────────────────────────────┐
│ Backend process (always running)              │   never imports
│  • local HTTP server: frontend + control API  │   pywebview/WebView2
│    + /frame.jpg live feed                     │   at all -- expected
│  • config, tray icon, single-instance,        │   to cost about what
│    startup registration                       │   today's app costs
│  • theme render loop  (unchanged)             │   (~84MB), comfortably
│  • hongtai_screen.py driver  (unchanged)      │   under the 100MB cap
└──────────────────────────────────────────────┘
```

The backend has to keep running regardless of whether any UI is open --
it's the thing driving the physical panel, not something that exists
for the UI's benefit. The UI existing only while it's actually open is
what keeps background RAM under budget; see "Phase 0 results" below for
why this ended up as two processes rather than one process that
destroys its own window.

### Key decisions

**The live preview is the existing web mirror.** `hongtai_screen.py`
already serves `/frame.jpg` from the frame being sent to the panel, and
that code is already debugged (including the HTTP/1.0 keep-alive fix).
The in-app preview is that same feed, displayed in our own window
instead of a browser tab. Nearly free, and impossible to drift out of
sync with the panel because it *is* the panel's frame.

**Frontend is served over HTTP, not `file://`.** Avoids CORS problems,
and means the whole UI is also reachable from a phone/browser for free
— the same trick the mirror already pulls.

**The control API binds to 127.0.0.1 only.** The read-only LAN mirror
stays what it is today: separate, opt-in, and read-only. Nothing that
can change settings or drive the panel is ever exposed to the network.

**No duplicate renderer.** The designer does *not* re-implement gauge
drawing in JS. It draws transparent drag/resize handles positioned over
the real rendered frame. While dragging, the handle moves instantly in
the browser (60fps, feels native); coordinates are pushed to Python,
which re-renders, and the image underneath catches up within ~100ms at
10Hz. Pixel-accurate by construction, one renderer to maintain.

**The UI is a separate process, killed outright on close.** Originally
planned as "destroy the window, keep the same process" -- Phase 0's
measurements ruled that out (see below). The UI process is spawned
fresh each time the window is opened and terminated (its own PID is
enough; WebView2 cleans up its own helper processes) when it closes.
Cost: opening the window takes ~1-2.5s (spawning a process + WebView2
init) instead of being instant. This requires all real state to live
in the backend, with the page treated as stateless UI — good practice
anyway, and now a hard requirement rather than just a nice one, since
the UI process can vanish and be recreated at any time.

**Low lock-in.** Because the UI is an HTTP app, pywebview is just a
window shell. If it ever disappoints, the shell can be swapped without
touching the UI, which still runs in any browser.

## Phases

### Phase 0 — Spike (throwaway) — ✅ DONE

Ran on real hardware. Full scripts and logs in
`experiments/phase0_webview_spike/` (kept for reference, safe to delete
once this is fully absorbed elsewhere).

**Round 1 — does destroying a window reclaim its process's memory?
No.** Across three independent runs (an interactive spike, 12 automated
open/destroy cycles, and a control run that never destroyed anything),
"this process" RSS jumped once on first WebView2 use (~65-90MB) and
never came back down regardless of window state. A small (~1MB/cycle)
creep across repeated cycles flattened out rather than accelerating —
not a leak, just allocator noise. Conclusion: loading WebView2 into a
process is a one-time, per-process cost, not a per-window one — so
destroying and recreating a window in the *same* process buys nothing.

Combined with the 100MB background hard cap, this ruled out the
original single-process design entirely: ~90MB of permanent WebView2
overhead alone, sharing a process with the always-on backend, would
already be most of the budget before counting any real work. Redesigned
as the two-process split described above.

**Round 2 — does killing a separate UI process actually clean up
everything?** Yes, cleanly. 4 automated spawn/kill cycles (alternating
"kill just the child's PID" vs. "kill its whole process tree
explicitly") all showed **zero surviving processes**, checked
immediately and 5s after each kill — killing just the PID was enough
in every case; WebView2's own process-lifetime management tears down
its helpers on its own. The backend process itself barely moved across
all 4 cycles (16.1MB → 17.7MB), confirming the spawn/kill orchestration
itself has no real cost.

**Numbers to build against:** UI process costs ~390-395MB while open
(the real, honest cost of a modern browser-engine UI — irrelevant to
the background budget since it only exists while visible), effectively
0MB within ~1s of the window closing. Backend alone should land close
to today's ~84MB once it's doing real work, comfortably under the
100MB cap.

### Phase 1 — Extract the backend (no visible change) — ✅ DONE

Pulled everything that isn't Tkinter out of `app.py` into its own
modules:

- `paths.py` — base dir / resource path resolution, `startup_debug.log` writer
- `config_store.py` — `app_config.json` load/save, `AUTO_DETECT`/`THEME_TAB_ORDER`
- `startup_registration.py` — Windows Startup-folder VBS launcher
- `desktop_shortcut.py` — Desktop `.lnk` creation
- `single_instance.py` — named-mutex single-instance check
- `theme_worker.py` — `ThemeWorker`: background thread + blind-restart
  recovery loop, talking back only through a plain `log` callback
- `tray_icon.py` — `TrayIcon`: pystray wrapper driven by three plain
  callbacks (`on_show`/`on_stop_screen`/`on_quit`)

`app.py` is now a thin Tkinter layer that calls into these. No
intentional behavior change — verified headlessly (python3.12 +
Tkinter under Xvfb, since the sandbox's default Python has no Tk) that
the `App` class still constructs, builds its widgets, and that
`ThemeWorker` runs/reports through its log callback exactly like the
old inline thread did. One real fix rode along: `enable_startup()` and
`create_desktop_shortcut()` used to resolve the running script via
their own `__file__`; now that this logic lives in a different file
than `app.py`, they resolve it via `sys.modules["__main__"].__file__`
instead, which is what actually still points at `app.py` regardless of
which module the code was called from.

Tray icon, startup registration and desktop shortcut creation are all
Windows-only and untestable from the sandbox — confirmed working
(Start/Stop, tray Show/Stop/Quit, "Launch at Windows startup", "Create
Desktop Shortcut") on the real machine, so this phase is fully closed
out.

### Phase 1.5 — src/ layout restructure (no visible change) — ✅ DONE (pending on-hardware confirmation)

Not one of the originally planned phases -- inserted here because the
project root had grown to ~20 loose `.py` files by the end of Phase 1
and needed a real directory structure before Phase 2 adds a `frontend/`
tree on top of it.

```
app.py                      -- thin launcher (unchanged path -- see below)
src/hongtai_screen_app/      -- the actual package
    app.py                   -- the Tkinter GUI + main()
    paths.py, config_store.py, startup_registration.py,
    desktop_shortcut.py, single_instance.py, theme_worker.py,
    tray_icon.py             -- Phase 1's app-shell modules, unchanged
                                 in substance, just moved + import paths
                                 switched to relative (`from .paths import ...`)
    driver/hongtai_screen.py -- the protocol driver
    themes/                   -- dashboard_theme.py, video_theme.py,
                                  webpage_theme.py, demo_clock.py
scripts/                     -- list_screens.py, test_connection.py,
                                 blind_draw.py, diag2_lines.py,
                                 make_launcher.py, and a thin CLI shim
                                 per theme (so `python scripts/dashboard_theme.py`
                                 etc. keep working standalone)
assets/icon.ico
packaging/hongtai_screen.spec
```

**Root `app.py` deliberately did not move.** It's now a ~15-line
launcher that puts `src/` on `sys.path` and calls
`hongtai_screen_app.app.main()` -- kept at this exact path so every
existing Windows integration that already points at it (a "Launch at
Windows startup" Startup-folder entry, a Desktop shortcut, `Launch
Hongtai Screen.vbs`) keeps working with zero user action, rather than
silently breaking because the real file moved. No install step is
needed to run any of this -- `python app.py` and everything under
`scripts/` use the same `sys.path.insert(0, ".../src")` trick, so
`pip install -r requirements.txt && python app.py` from **Quick
start** still just works. An optional `pyproject.toml` was added purely
for editor/IDE import resolution and an optional `pip install -e .` --
never required.

Two real fixes rode along with the move (both are exactly the kind of
bug a flat "everything imports everything by bare name" layout hides):
`_app_base_dir()` (in `paths.py`) used to resolve via its own
`__file__`, which put `app_config.json`/`startup_debug.log` inside
`src/hongtai_screen_app/` instead of next to the real entry point once
that function moved into its own file -- fixed to resolve via
`sys.modules["__main__"].__file__` instead (same approach
`startup_registration.py`/`desktop_shortcut.py` already used). And
`desktop_shortcut.py`'s dependency on `make_launcher.py` was inverted:
the "write the hidden .vbs launcher" logic now lives once, as
`desktop_shortcut.write_run_vbs()`, and `scripts/make_launcher.py`
(a standalone convenience script) imports *it*, rather than package
code reaching out to a loose top-level script that could be deleted or
moved independently.

Verified headlessly: `python app.py` (plain and `--autostart --theme
clock`) runs end to end under Xvfb with the same behavior as before the
move -- `app_config.json`/`startup_debug.log` land next to root
`app.py` as expected, not under `src/`; every `scripts/*.py` resolves
its import correctly at runtime (checked `list_screens.py` actually
enumerating ports, not just parsing). Windows-only things (Start/Stop
against real hardware, tray, "Launch at Windows startup", "Create
Desktop Shortcut", and a real PyInstaller build against the new
`packaging/hongtai_screen.spec`) still need one real-machine pass
before this is fully closed out -- same caveat as Phase 1 had.

### Phase 2 — Control API + UI process

Split into sub-steps rather than landing all at once, since the
frontend/process-spawn parts can't be verified from the sandbox at all
(no display, no Node runtime tested here, no WebView2) -- each one
lands and gets reviewed before the next starts.

#### Phase 2a — Backend control API — ✅ DONE

New, additive, fully headless -- does **not** touch the Tkinter app.
Three new modules in `src/hongtai_screen_app/`:

- `theme_kwargs.py` — the one thing Phase 1 didn't extract: pure
  functions that turn `app_config.json`'s saved settings into the
  `(theme_name, target, kwargs)` tuple `ThemeWorker` needs, reading
  straight from the config dict instead of Tk widgets (every field is
  already stored in canonical form -- `dashboard.slots` values are
  `STAT_DEFS` keys, not display labels -- so no translation layer is
  needed). The Tkinter app's own inline kwarg-builders are **not**
  switched to call these yet -- see the module's docstring for why
  (it would mean reordering when `self.cfg` gets synced from widget
  state, a GUI behavior change outside this pass's scope).
- `controller.py` — `AppController`: the same start/stop/apply/config
  logic `app.py`'s `App` class drives, minus every Tk widget touch.
  Owns a `ThemeWorker`, a log ring-buffer + pub/sub (so a new SSE
  client catches up on recent lines and then keeps receiving new
  ones), and a background watcher thread standing in for what
  `_poll_log_queue()` does in the GUI (notice the worker died, reset
  state, or restart if `apply()` asked for it). Thread-safe -- callable
  concurrently from multiple HTTP handler threads.
- `control_server.py` — the actual HTTP API, `ThreadingHTTPServer`
  bound to **127.0.0.1 only** (never the LAN, unlike the opt-in web
  mirror): `GET /api/state`, `GET`/`POST /api/config`,
  `POST /api/start` (optional `{"theme": ...}` body), `POST /api/stop`,
  `POST /api/apply`, `GET /api/logs/stream` (SSE, plain HTTP/1.0 with
  no Content-Length -- the connection just stays open), and
  `GET /frame.jpg`. No extra dependencies -- same `http.server`
  approach the driver's own mirror already uses.
- `scripts/run_backend.py` — runs the control API standalone, no
  Tkinter at all, so it can actually be tried with curl before any
  frontend exists: `python scripts/run_backend.py`.

One small, additive change to the driver itself:
`hongtai_screen.enable_frame_capture()`/`disable_frame_capture()`/
`get_mirror_frame_jpeg()` -- keeps the latest frame as JPEG bytes in
memory for a same-process caller (the control API's `/frame.jpg`)
without opening a second network listener the way
`enable_web_mirror()` does (that stays LAN-facing and opt-in, for the
phone-mirror use case specifically). Both share the same underlying
buffer; either one being on is enough to populate it.

Verified headlessly end to end (no hardware needed for any of this):
state/config get and set, start (theme name via the request body),
double-start correctly rejected (400), config patch + apply actually
picking up the new value (started Clock, patched brightness, called
apply, confirmed the restarted worker's state reflected the new
brightness), stop, and an unknown theme name rejected (400) --  all via
real HTTP requests against a running server, not mocked. The SSE log
stream was verified separately: a client connected before `start()` was
called received the worker's live error/recovery log lines in real
time, matching exactly what the Tkinter app's Log panel would show for
the same run. `scripts/run_backend.py` itself was run as a real
subprocess, queried over HTTP, and shut down cleanly via SIGINT.
Confirmed on real hardware: `POST /api/start` connects to the panel,
`connected`/`screen_info` populate correctly, and `/frame.jpg` returns
real image bytes once a theme is running.

One bug surfaced by that real-machine pass, fixed as part of closing
out this phase: `_app_base_dir()` (`paths.py`) resolved via
`sys.modules["__main__"].__file__`, which pointed at `scripts/` (not
the repo root) when the entry point was `scripts/run_backend.py`
rather than root `app.py` -- so running the backend standalone wrote
its own separate `scripts/app_config.json` instead of sharing the real
one next to `app.py`, contradicting `run_backend.py`'s own docstring
promise ("loads the same app_config.json the GUI uses"). Fixed by
anchoring `_app_base_dir()` on `paths.py`'s own file location (three
parents up is always the repo root) instead of on whichever script
Python was run as -- `startup_registration.py`/`desktop_shortcut.py`
keep using `sys.modules["__main__"].__file__`, deliberately, since
they're solving a different problem (pointing a Windows launcher at
the real running script). Verified headlessly: `scripts/run_backend.py`
run from a fresh checkout now writes `app_config.json` at the repo
root regardless of entry point, and a stray `scripts/app_config.json`
is no longer created.

#### Phase 2b — Frontend scaffold — ✅ DONE

A minimal Vite + React shell (`frontend/`) that talks to the Phase 2a
API: connection status, the live preview (`/frame.jpg`, polled at
~2.5Hz -- the API serves one JPEG per request, not a multipart
stream, matching how the old web-mirror page already worked),
theme/port/brightness/start/stop/apply controls, and a live log panel
(consumes `/api/logs/stream` via `EventSource`, which handles SSE
reconnects on its own). `frontend/src/api.js` is the only file that
knows the backend's actual endpoint shapes -- everything else just
calls its functions.

**Served by the backend itself, same origin, on purpose.** `npm run
build`'s output (`frontend/dist/`) is served directly by
`control_server.py`: any GET that isn't an API route now falls
through to a static-file handler that serves `frontend/dist/`
(path-traversal-checked -- verified headlessly that `/../../<anything
outside dist>` 404s rather than leaking a repo file). This means
there's no cross-origin request to configure in production at all --
open `http://127.0.0.1:8899/` with a theme running and the whole UI
loads from the same process already driving the panel. `npm run dev`
(a separate Vite dev server on its own port) instead uses
`vite.config.js`'s proxy for `/api`/`/frame.jpg`, for a fast edit
loop while working on the frontend itself.

`frontend/dist/` is committed to the repo (see `.gitignore`) so
`pip install -r requirements.txt && python app.py` keeps working with
zero Node toolchain required -- only touching `frontend/src/` requires
Node, and only to rebuild the committed bundle afterward.

Verified headlessly: `npm run build` succeeds cleanly; the built
bundle's `index.html` and its hashed JS asset are served correctly by
`control_server.py` with the right content types; `/api/state` and the
rest of the API keep working unchanged alongside the static handler;
a raw-socket path-traversal attempt (`GET /../pyproject.toml`, past
`urlparse` which doesn't normalize `..` itself) correctly 404s instead
of returning a repo file. Confirmed on the real machine: the page loads at
`http://127.0.0.1:8899/`, and start/stop/apply/theme/port/brightness
all work from the browser the same way they do from `curl`/the
Tkinter app. One real bug surfaced by that pass and fixed as part of
closing out this phase (see Phase 2a's writeup above, which this
inherited): `controller.py`'s `_selected_port()` was passing
`app_config.json`'s saved port *label* (a whole descriptive string)
straight to the driver as if it were an openable device path, instead
of translating it back to the real `COM3`-style path the way `app.py`
already does -- fixed to do the same rescan-and-match `app.py` uses.

#### Phase 2c — UI process spawn/kill wiring — ✅ DONE

The actual two-process design Phase 0 validated, wired up end to end
for the first time: `backend_app.py` (`scripts/run_v2_app.py`) is a
new, parallel entry point -- **not yet what `app.py`/Desktop shortcuts/
Startup point at; that switch is Phase 7's job** -- that starts the
control API, a tray icon, and resumes whatever theme was last running
(same logic as `app.py`'s own launch-time resume). Its tray's "Show"
spawns `scripts/run_ui.py` (a tiny, dependency-free pywebview window
pointed at the backend's own URL) as a genuinely separate OS process,
tracked by its `Popen` handle; "Show" again while one is already open
is a no-op rather than opening a second window; "Quit" `terminate()`s
it along with shutting down the server and controller. `run_ui.py` is
deliberately importless from the app's own package -- pywebview (and,
transitively, WebView2) only ever gets loaded in that one process,
never in the always-on backend, which is the entire point of Phase 0's
two-process split: destroying a pywebview window doesn't release its
~90MB of overhead within the same process, so ending the whole process
is the only way to actually reclaim it.

Verified everything headlessly that doesn't require an actual
WebView2/tray/display: `BackendApp` resumes the last-running theme on
a plain launch (checked with a saved `auto_resume_tab`, matching
`app.py`'s behavior exactly); the control server starts and stops
cleanly under its orchestration; `_on_show()` spawns exactly one
subprocess and a second call while the first is still alive is
correctly a no-op (verified with `subprocess.Popen` stubbed, since
there's no `pywebview`/display here to actually open a window against);
`shutdown()` actually terminates the tracked UI subprocess and stops
the server (confirmed the API stops responding afterward). Tray icon
creation itself is a no-op here (`pystray`'s `available()` correctly
reports `False` off Windows) -- that path, `run_ui.py` actually
opening a window, WebView2 availability, and the real spawn/kill
memory numbers all still need one real-machine pass, same caveat every
other phase has had.

**Confirmed on the real machine, and the two-process design works as
designed:** tray icon (Show/Stop screen/Quit) all work; the window
opens as a genuinely separate process, and that process fully exits
when the window is closed. Memory: backend **~80MB**, UI process
**~40MB** while open -- both comfortably under Phase 0's 100MB
background cap, and the UI process in particular came in far under
Phase 0's ~390MB estimate (that spike's throwaway window loaded a
blank/trivial page; this one's a small React app talking to a live
API, evidently not enough extra weight to move the needle much against
WebView2's own baseline). One real gap the first test caught: starting
via `scripts/run_backend.py` instead of `scripts/run_v2_app.py` looks
almost identical from the browser (same control API, same frontend)
but has no tray icon, no startup/shortcut endpoints, and no
live-brightness wiring at all -- worth remembering these are two
different entry points for two different purposes (`run_backend.py`:
curl-testing the API alone; `run_v2_app.py`: the actual app), not
interchangeable.

One known, accepted limitation: the popup window's title-bar/taskbar
icon still shows Python's own default rather than `icon.ico`.
pywebview's `icon=` on `webview.start()` is GTK/Qt (Linux) only by its
own design -- on Windows "icon is set during freezing" (i.e. baked
into a PyInstaller `.exe`'s resources, which `packaging/hongtai_screen.spec`
already does, and which is what Phase 7's cutover ships). Worked
around in the meantime with the same `FindWindowW`-by-title trick
`single_instance.py` already uses elsewhere in this codebase: once the
window is shown, `run_ui.py` pushes `icon.ico` onto its `HWND`
directly via `WM_SETICON` (both `ICON_SMALL`/`ICON_BIG`), which is
what the title bar and taskbar button actually read. Best-effort,
Windows-only, and unverified from here (no Windows/WebView2 in the
sandbox) -- next real-machine pass should confirm whether it actually
fixes the icon or the real fix has to wait for Phase 7's frozen build.

**Fix (real-usage bug in auto-resume):** quitting `backend_app.py`
while a theme was running never resumed it on the next launch, even
though this section's own verification above claims resume "matches
`app.py`'s behavior exactly" -- that check only covered a *plain
launch* reading a pre-seeded `auto_resume_tab`, not a full
start-then-quit round trip, so it missed that quitting itself was
wiping the marker first. Root cause: `ScreenEngine.close()` (process
shutdown, releases the serial port) and `ScreenEngine.stop()` (an
explicit Stop) both funneled through the same `_teardown()` ->
`on_disconnected()` callback, and `AppController._on_screen_
disconnected()` clears `auto_resume_tab` on every call -- correct for
an explicit Stop or a theme dying/finishing on its own, wrong for a
plain quit while something was still running. `_teardown()` now skips
`on_disconnected()` specifically when `close()` triggered it (it still
always physically closes the port), so the marker set by the last
`start()`/`switch()` survives a clean quit. Verified headlessly with a
fake screen/target standing in for real hardware: quit-while-running
now preserves and persists `auto_resume_tab` to disk; an explicit
`stop()` still clears it as before; switching themes still reuses the
one connection without a spurious disconnect in between.

### Phase 3 — Port the simple themes — ✅ DONE (pending a real-machine pass)

Video, Webpage and Clock settings forms, added to the frontend
(`App.jsx`) as a "Theme settings" panel that swaps its fields based on
whichever theme is selected -- mirroring `app.py`'s own per-tab
settings fields and hint text exactly (Video: file path, loop/B&W/
audio checkboxes, FPS override; Webpage: URL, screenshot interval,
full-reload interval; Clock: nothing beyond port/brightness, just an
info line; Dashboard: an explicit "not in the web UI yet, Phase 4"
note rather than silently doing nothing).

**No backend changes needed at all.** `update_config()`'s shallow
per-key merge (Phase 2a) and `theme_kwargs.py`'s parsing straight out
of `app_config.json` (also Phase 2a) already handled arbitrary
`video`/`webpage` settings from day one -- this phase is purely
frontend, POSTing `{"video": {...}}` / `{"webpage": {...}}` patches in
exactly the shape `app.py`'s own `_save_current_config()` already
persists (path/fps as a string or null, url/interval as strings,
reload_every as a string or null), so a config file edited by either
UI stays fully compatible with the other's expectations.

One deliberate deviation from `app.py`: no "Browse..." file picker for
the video path. A browser's native file input can't hand back a full
filesystem path (a sandboxed security restriction, not a bug), and the
backend needs an absolute path to open with OpenCV -- so this is a
plain text field the same way the port field already is, with a
placeholder showing the expected format.

Verified headlessly against a running `run_backend.py`: saved video
settings (path/loop/bw/audio/fps) and webpage settings (url/interval/
reload_every) via `POST /api/config` in the exact shape the frontend
sends, then confirmed `POST /api/start` for both `video` and `webpage`
successfully builds their kwargs and starts a worker (no
theme_kwargs.py error) -- i.e. the whole settings-form-to-running-theme
path works end to end. Not yet checked by eye in a real browser against
a real video file/URL and real hardware.

### Phase 3.5 — Live theme switching (no reconnect) — ✅ DONE, confirmed on hardware

Not on the original plan -- added after testing Phase 2c/3 on real
hardware surfaced a design smell: every theme module (`dashboard_theme.py`,
`video_theme.py`, `webpage_theme.py`, `demo_clock.py`) connected its own
`HongtaiScreen`, ran its own render loop, and closed its own connection,
so switching themes meant fully disconnecting one and reconnecting the
other from scratch even though the panel doesn't care which theme is
driving it -- it just wants a stream of frames on an open serial port.
Producing a frame is a theme's job; owning the connection never needed
to be, and each theme's own `screen_factory` docstring already said as
much (aspirationally -- nothing actually let a caller hand in a
pre-connected screen until now).

**Theme modules (`demo_clock.py`, `video_theme.py`, `webpage_theme.py`,
`dashboard_theme.py`):** `run()` now takes an optional `screen=`. When
given an already-connected `HongtaiScreen`, a theme skips
`screen_factory(port)`/`.connect()`/`on_connected()` entirely, reads
`info` straight off it, and -- critically -- never closes it in
`finally`. `port`/`screen_factory`/`on_connected` are ignored in that
case. Plain CLI/GUI use (`screen=None`, the default, what `app.py` and
the CLI entry points still do) is unaffected: connects and disconnects
its own screen exactly like before. `dashboard_theme.py`'s
`start_media_polling()`/`start_systeminfos()` were both confirmed
already idempotent (guarded by module-level flags), so switching away
from and back to Dashboard repeatedly doesn't spawn duplicate
subprocess/polling threads.

**New `screen_engine.py` (`ScreenEngine`), replacing `ThemeWorker` for
`controller.py` only** (`app.py`'s Tkinter UI keeps `ThemeWorker`
untouched -- this is additive, not a breaking change to the existing
GUI). One dedicated background thread owns the connection for the
controller's whole lifetime: `switch(label, target, kwargs, port)`
interrupts whatever's running (via its `stop_event`) and starts the new
target against the *same* connection, connecting only if nothing was
connected yet; `stop()` interrupts and fully disconnects. A theme
ending on its own (a non-looping video finishing, a page load
failure) still disconnects, same as the old per-theme behavior. Errors
that aren't from an intentional stop/switch get `ThemeWorker`'s same
recovery treatment -- `blind_restart()` (a real firmware restart, not
just a reconnect), then reconnect and retry, up to `RECOVERY_ATTEMPTS`
(3) -- and give up with a logged message if the panel never comes back.

**`controller.py`:** now builds one `ScreenEngine` at construction
instead of a `ThemeWorker` per `start()`. `start()` no longer raises
"already running" -- calling it while a different theme is active *is*
a live switch, handled by `ScreenEngine.switch()`. `apply()` no longer
stops-then-waits-then-restarts (the old `_pending_restart` +
`_watch_loop` dance, now deleted entirely); it just re-switches to the
currently-active theme with freshly-read config, which reuses the
connection like any other switch. `/api/start`, `/api/stop`,
`/api/apply` on `control_server.py` needed zero changes -- the new
behavior falls straight out of what `controller.py` already called.

**Frontend (`App.jsx`):** the theme picker and Start button are no
longer locked while a theme is running -- Start's label becomes
"Switch" once something's active, and picking a different theme + Start
now switches live instead of requiring Stop first.

Verified headlessly with fake/mocked hardware (no real panel needed for
any of this): a switch reuses the same connection with zero reconnects
and fires `on_connected` only once across multiple switches; a natural
finish (theme's `run()` returning on its own) disconnects and fires
`on_finished`; a simulated failure not caused by an intentional
stop/switch retries with `blind_restart()` between attempts and gives
up after `RECOVERY_ATTEMPTS`; an error that races with an intentional
stop is treated as "stopped", not as a fault needing recovery; and
`controller.py`'s `start()`/`apply()`/`stop()` drive all of the above
correctly end to end (switching live, Apply without disconnecting, Stop
actually disconnecting), plus a full `control_server.py` boot-and-query
smoke test with no hardware attached. **Confirmed working on real
hardware**: live switching between real theme streams with no
disconnect/reconnect flicker.

### Phase 4 — Layout model: slots → elements (backend) — ✅ DONE (pending a real-machine pass)

The core data model change that actually unblocks the last three
phases: `build_static_background()`/`render_frame()` no longer compute
8 fixed gauge positions from a formula keyed by a slot name (`top_left`,
`left_secondary`, ...); they walk an arbitrary list of gauge elements
instead, each with its own `x`/`y`/`radius` (fractions of width/height/
min(width, height), so the same list scales correctly to any panel
resolution), `color` (an explicit override, or `None` to keep the old
left-column-cyan/right-column-magenta split), `opacity`, `z` (paint
order), `stat` (a `STAT_DEFS` key, same as before), and a `rotation`
field that's stored/migrated/round-tripped through config but not
rendered yet -- correct rotation needs the per-frame needle/value to
rotate in lockstep with the static ring, and nothing can actually set a
non-zero rotation until Phase 5's canvas exists to offer it, so
implementing that now would be untested, unreachable code. The old
"big"/"secondary"/"mini" `SLOT_KINDS` enum is gone too: whether a gauge
gets full tick labels + an inside title (the old "big" look) or a
compact title-above-the-ring look is now derived from the element's
actual baked radius (`BIG_GAUGE_RADIUS_FRACTION`), and the small-style
label gap is a continuous function of radius instead of two hardcoded
constants -- calibrated so the default 8-gauge layout's look doesn't
change (see below).

- **Migration, done at read time, not as a stored schema version.**
  `slots_to_elements(slots)` runs the exact same geometry formula
  `build_static_background()` used to compute inline (now pulled out
  into `_slot_geometry()`) at a fixed `REFERENCE_WIDTH`/`REFERENCE_HEIGHT`
  matching this panel's real resolution, and expresses each gauge's
  resulting center/radius as a fraction of that reference size.
  `DEFAULT_ELEMENTS` is this applied to `DEFAULT_SLOTS`. `theme_kwargs.py`
  reads `dashboard.elements` from config if present, otherwise derives
  one from `dashboard.slots` (or the defaults) the same way -- so an
  existing `app_config.json` with only `slots` (or nothing dashboard-
  related at all) keeps rendering exactly as before, with nothing
  needing to be written back or bumped. Only a future design canvas
  actually saving custom elements changes what's stored.
- **`app.py`'s Tkinter Dashboard tab is completely unaffected.** It
  still reads/writes `dashboard.slots` directly and still calls
  `dashboard_theme.run(slots=..., ...)` — that parameter still exists
  and still works exactly as before. `run()` only derives `elements`
  from it internally (via `slots_to_elements()`) when no `elements=` was
  given, which is the *only* thing `app.py`'s call site does not pass.
- Kept the static-bake optimization exactly as it was — the whole
  per-element loop (position resolution, static tile draw, tick/title
  labels) runs once per `build_static_background()` call, and only the
  live needle/value redraw every frame in `render_frame()`, same as
  before this phase. Still invalidated by a Stop/Start or the GUI's
  Apply button, same as any other "needs a restart" dashboard setting.

Verified headlessly: pixel-diffed a full rendered frame from the new
element-based path against a reconstruction of the exact pre-Phase-4
formula-based path for the default 8-gauge layout -- 460,800 pixels
compared, 0.065% differing (a sub-pixel label-position shift from the
new continuous label-gap formula replacing the old hardcoded 13px/16px
constants, exactly as expected and documented in code); custom `slots`
overrides migrate to the right `stat` bindings; a hand-built custom
`elements` list (arbitrary position/size/explicit color/opacity, plus
an unrecognized future element type mixed in) bakes and renders without
error, with the unknown type correctly skipped rather than crashing;
`theme_kwargs.py` correctly prefers `dashboard.elements` when present
and falls back to migrating `dashboard.slots` otherwise; and a full
`dashboard_theme.run()` against a fake screen actually streamed real
frames end to end. **Not yet verified on real hardware.**

### Phase 5 — The design canvas — ✅ DONE (pending a real-machine pass)

Built on Phase 4's element list directly: the canvas edits exactly the
same `{id, type, stat, x, y, radius, rotation, color, opacity, z}`
shape that already round-trips through config, so there's no separate
"canvas format" to convert to/from -- what you drag is what
`dashboard_theme.py` renders.

New frontend component `DashboardCanvas.jsx` (the web UI's Dashboard
tab), an SVG overlay drawn on top of the live `/frame.jpg` mirror when
the panel's connected (a placeholder background otherwise):

- **Drag** a gauge to move it (pointer events on the circle), **drag
  its corner handle** to resize (radius = pixel distance from the
  gauge's own center to the pointer, converted back to the same
  fraction-of-`min(width,height)` basis `dashboard_theme.py` uses).
  **Rotate is deliberately not exposed** -- Phase 4 stores/migrates
  `rotation` but doesn't render it yet (the needle would need to
  rotate in lockstep with the ring, and nothing could set a non-zero
  value before this canvas existed to offer it), so a rotate handle
  here would visibly do nothing. It'll get one once rendering support
  lands.
- **Snapping + alignment guides**: dragging within ~1.8% of the canvas
  center or another gauge's x or y snaps to it and draws a dashed
  guide line, independently on each axis.
- **Property panel** for the selected gauge: stat picker (from
  `STAT_DEFS`, via the new `/api/dashboard/meta` metadata endpoint so
  the frontend never needs to import anything from `dashboard_theme.py`
  directly), opacity slider, a custom-color checkbox + picker (falls
  back to the existing left-cyan/right-magenta split when off), and
  numeric X%/Y%/Radius% fields for precise placement alongside dragging.
- **Add/delete**, **bring to front/send to back** (z-order).
- **Undo/redo** (buttons + Ctrl+Z/Ctrl+Y), a plain history-stack of
  committed element-list snapshots -- drag/resize only push one entry
  per gesture (on release), not per pointer-move frame.
- **Save layout** persists the current elements; **saveable named
  presets** (save current as / load / delete) let you keep more than
  one layout around and switch between them.

New backend surface, kept separate from the generic `update_config()`
on purpose: a `dashboard` config patch through that endpoint replaces
the *entire* `dashboard` sub-dict, which would silently wipe
`web_port`/`enable_web`/`background`/`slots` on every layout save.
`controller.dashboard_meta()`/`save_dashboard_elements()`/
`save_dashboard_preset()`/`delete_dashboard_preset()` (routed through
`GET /api/dashboard/meta`, `POST /api/dashboard/elements`,
`POST /api/dashboard/presets`, `POST /api/dashboard/presets/delete`)
merge into the existing `dashboard` dict instead. `theme_kwargs.py`'s
element-resolution logic was pulled into a shared
`resolve_dashboard_elements(cfg)` so `dashboard_kwargs()` (what
actually starts the theme) and `dashboard_meta()` (what the canvas
reads) can never disagree about "what's the layout right now".

Verified: pure-function geometry/color/snapping helpers unit-tested in
isolation (hex/rgb conversion, accent derivation, resize-radius math,
snap-distance logic); the production frontend build is clean; and a
full Playwright run against the real built frontend + a live backend
(no panel attached) confirmed, with screenshots at each step, the
default layout renders correctly, clicking a gauge selects it and
opens the property panel, dragging moves it and updates the panel
live, Save Layout persists to `app_config.json`, Add Gauge/Delete/
Undo round-trip correctly, and preset save actually reaches the
backend. The saved custom layout from that browser session was then
fed back through the real `dashboard_theme.build_static_background()`/
`render_frame()` (full cairo rendering, not a stub) and rendered
correctly. **Not yet verified on real hardware** -- specifically,
editing live over an actual panel's `/frame.jpg` mirror rather than
the "no screen connected" placeholder.

**Fix (feature-parity gap):** the canvas above covered gauge layout
but the web UI still had no way to set the panel background -- style
preset, color scheme, or a custom image -- something `app.py`'s
Tkinter Dashboard tab has always had. Added a Background section
under the canvas (style + scheme selects, plus a plain text image-path
field when "Custom image" is picked -- no native file browse, same
reasoning as the video theme's path field in Phase 3), backed by a new
`controller.save_dashboard_background()` / `POST /api/dashboard/
background` that merges into the `dashboard` dict the same way the
elements/preset endpoints do, and `dashboard_meta()` now also returns
the resolved background plus `BACKGROUND_PRESETS`/
`BACKGROUND_COLOR_SCHEMES` label metadata. Verified headlessly
(endpoint round-trip, `theme_kwargs.dashboard_kwargs()` picks up the
saved value with `web_port`/`elements`/`presets` all left untouched)
and via a Playwright session against the real built frontend + live
backend: switching to Custom image, typing a path, and saving all
worked with no console errors, and the value persisted server-side.

### Phase 6 — Richer elements and options — ✅ DONE (pending a real-machine pass)

Three new element types alongside the existing gauge, plus one new
per-gauge styling option -- all additive to Phase 4's element model
(`{id, type, x, y, z, opacity, ...type-specific fields}`), so an old
config with only gauge elements keeps rendering exactly as before.

- **Text labels** (`type: "text"`): a free-standing string, not bound
  to a stat -- its own `text`/`font_size`/`color`/`align`. Fully
  static (baked into the background image at Start/Apply time, same
  as the gauge titles), since the text itself never changes frame to
  frame.
- **History graphs** (`type: "graph"`): a line or bar chart
  (`style`) plotting a bound `stat`'s recent values over
  `history_seconds`, in its own `width`/`height` box (a rectangle, not
  a gauge's circle, so it gets independent width/height instead of one
  shared `radius`). The border+title bakes into the static background
  like everything else; the actual bars/line are the one genuinely
  dynamic thing Phase 6 adds -- redrawn every frame in `render_frame()`
  from a rolling per-element `deque` that `run()`'s loop maintains
  itself (sized from `history_seconds`/the loop's own 10Hz period,
  since `render_frame()` has no way to know how much wall-clock time
  actually elapsed between frames). A `None` sample (stat unavailable)
  leaves a gap rather than plotting a false zero.
- **Custom images** (`type: "image"`): an arbitrary photo/logo dropped
  onto the layout as its own positioned/sized element -- cover-fit and
  alpha-composited at build time, same tolerance for a bad/missing/
  unreadable path as the background image and app.py's background
  picker already have (skipped silently, no crash). The image path is
  a plain text field in the canvas, same reasoning as the video
  theme's path field and the background picker's custom-image path --
  a browser file input can't hand back a real filesystem path.
- **Gauge gradients** (`color2` on a `type: "gauge"` element): an
  optional second ring color -- when set, the static track and the lit
  value arc both sweep from `color` to `color2` instead of the single-
  color fade every gauge has always had. `None` (the default) keeps
  today's look exactly as it was; needle/hub stay single-toned to keep
  the change additive rather than a full gauge-rendering rewrite.

The web UI's design canvas (`DashboardCanvas.jsx`) gained "+ Add
text"/"+ Add graph"/"+ Add image" buttons alongside "+ Add gauge",
type-specific SVG representations on the canvas (a labeled box with a
resize handle for graph/image, draggable text for labels), a resize
handle that adjusts width+height independently for graph/image and
font_size for text (rather than one shared radius), and a property
panel that swaps in the right fields per element's `type` --
gauge gained the gradient toggle + second color picker alongside its
existing fields. No backend schema change was needed beyond the
gradient's `color2` field: `save_dashboard_elements()`'s "is a list"
validation already accepted an arbitrary element shape, so the new
types just work.

Verified headlessly: `build_static_background()`/`render_frame()`
render a mixed layout (one of each new type, plus a gradient gauge)
with no exceptions, correctly skip a bad image path, and a plain
`DEFAULT_ELEMENTS` layout (no new types at all) is an exact regression
check; a full `run()` loop against a fake screen confirms the
history deque accumulates and feeds `render_frame()` without error.
A full Playwright session against the real built frontend + live
backend added one of each new type via the canvas UI, edited the text
label's content, saved the layout, and confirmed the saved config --
elements of all three new types plus the gradient field -- round-trips
through the real `theme_kwargs.dashboard_kwargs()` -> `dashboard_
theme.build_static_background()`/`render_frame()` pipeline (actual
cairo rendering, not a stub) and renders correctly, including the
plotted line graph and the gracefully-skipped empty image path.
**Not yet verified on real hardware.**

**Fix (feature-parity gap): the "nothing playing" placeholder.**
`app.py`'s Tkinter Dashboard tab has always let you set a custom
placeholder image for when Spotify isn't playing (`default_art_path`,
applied live -- `dashboard_theme` re-checks it every frame, no Stop/
Start needed), but that setting never made it into the web UI at all.
Separately, the message shown in place of the track title has always
been one hardcoded line ("Life is like a door never trust a cow
because the sun can't swim"), never an actual setting in either app.
Both are now real: `dashboard_theme.py` gained `set_not_playing_
message()`/`get_not_playing_message()` mirroring the existing art-path
pair (same "re-read every frame" live-apply behavior, `DEFAULT_NOT_
PLAYING_MESSAGE` as the fallback), a new merge-safe `controller.
save_dashboard_now_playing()` / `POST /api/dashboard/now_playing`
applies both to the running dashboard immediately in addition to
persisting them, and the canvas gained a "Nothing playing" placeholder
section (plain text fields for the image path and the message,
alongside Background) -- `app.py` gained a matching message field
next to its existing image-path one. Verified headlessly (message
falls back to the default when cleared, a forced-`_MEDIA_OK` render
confirms a custom message actually shows up in place of the track
title, and the merge-safe endpoint round-trips without touching other
dashboard config) and via a Playwright session against the real built
frontend + live backend confirming the section saves and persists.

**Fix: image settings now store a managed copy, and are picked, not
typed.** Every image setting (dashboard background, the "nothing
playing" placeholder, a Phase 6 image element) used to store whatever
path a user typed or browsed to, verbatim -- fragile, since moving,
renaming, or deleting that file afterward silently breaks the feature
with no obvious explanation (the render code's tolerant catch-and-
fall-back was exactly what made this easy to overlook). New
`image_store.py` copies a picked image into an app-owned, per-user
folder (`%LOCALAPPDATA%\HongtaiScreen\images\`, created on first use)
the moment it's picked, content-hash deduplicated and PIL-validated,
and it's that copy's path that gets saved -- the original file can
move or disappear afterward with no effect. The web UI's three
plain-text "type a path" fields are now real file pickers: picking a
file uploads its bytes to a new `POST /api/dashboard/upload_image`
(`controller.upload_dashboard_image()`), which stores it and hands
back the managed path for the existing merge-safe endpoints to save,
with the stored file's name shown as a caption and a Clear button on
the placeholder-image field. `app.py`'s Tkinter Browse dialogs now
route the real path `askopenfilename()` returns through
`image_store.store_image_file()` and keep the returned managed-copy
path instead of the raw browsed one, so Tkinter gets the same fix.
Verified headlessly (dedup, validation, survives deleting the
original, full upload -> save round-trip for all three locations) and
via a Playwright session against the real built frontend + live
backend picking a real file for background/now-playing/element,
confirming the managed path is what's saved and no console errors.

**Fix (data loss): Tkinter's Dashboard tab could wipe out a web-canvas
layout/presets.** `_save_current_config()` rebuilt the whole
"dashboard" config dict from scratch out of only the fields Tkinter
has controls for -- `elements` and `presets` are web-canvas-only
concepts with no Tkinter UI, so they got silently dropped every time
Tkinter saved anything (closing to tray, Stop, switching tabs, not
just an explicit Save). Fixed to merge into the existing "dashboard"
dict instead of replacing it, matching the merge-safe pattern the web
UI's own save endpoints already use. Verified with a logic-level test
confirming `elements`/`presets` survive a Tkinter save unchanged.

**Feature: the middle column is a choice now, not just Spotify.** New
`dashboard.middle_content` setting (`"spotify"` default/`"weather"`/
`"none"`), since not everyone wants a now-playing display (or runs
Spotify). `"weather"` is a new `weather.py` module -- Open-Meteo,
free/no-API-key, geocodes a typed place name and polls current
conditions every 10 minutes from a background thread -- rendered as a
hand-drawn glowing icon + temperature + description + feels-like/
humidity + resolved place name, with tolerant fallbacks for no location
set or a lookup failure. `"none"` draws nothing there. Both UIs gained
a "Middle content" section (Show picker, plus location/units for
Weather); the "Nothing playing" section now only shows for Spotify. New
merge-safe `controller.save_dashboard_middle_content()` / `POST /api/
dashboard/middle_content` applies live, same pattern as the now-playing
settings. Verified headlessly (mocked-HTTP geocode/fetch, live
location/unit changes waking the poll thread immediately, rendered
frames for all three modes and their fallbacks, the endpoint's
round-trip and its rejection of an unknown value) and via a Playwright
session against the real built frontend + live backend.

**Feature: "Keep the panel updating while Windows is locked" (on by
default).** Mirrors the vendor XTRM Lab app's own "Keep playing when
screen is off" toggle (found reverse-engineering its app.asar --
Electron's `powerMonitor` stops rendering on lock/suspend unless it's
on); this app had no equivalent before, always rendering regardless.
New `power_state.py` detects a Windows lock via `ctypes`'
`OpenInputDesktop()` (no extra dependency), polled every 2s. Only
covers a screen *lock* -- true system suspend freezes the whole process
anyway, nothing to pause/resume there. All four themes' render loops
check `power_state.should_pause()` before their per-frame work (not
just the panel push) and skip that frame while it applies, resuming the
instant Windows unlocks. New `controller.set_keep_active_when_locked()`
/ `POST /api/keep_active_when_locked`, a matching Tkinter checkbox, and
a System-panel checkbox in the web UI, all applying immediately.
Verified headlessly (lock detection stubbed both ways, an end-to-end
run against a fake screen confirming frames stop/resume exactly on the
setting flip) and via Playwright against the real built frontend.

**Feature: the now-playing widget is a movable element, not just a
fixed middle-column display.** New `"media"` element type (`+ Add
now-playing` in the canvas toolbar) -- the same album art + track/
artist + progress bar as `_draw_spotify_middle()`, but with its own
x/y/width/height/opacity like any other canvas object, independent of
the "Middle content" setting (add one regardless of whether that's set
to Spotify/Weather/None). New `dashboard_theme._media_box()`/
`_draw_media_element()`; fully dynamic (redrawn every frame, like a
graph's plotted line) since playback position advances continuously,
so nothing about it bakes into the static background. No backend
validation needed since `elements` already round-trips arbitrary
dicts. Also: the browser's unstyled default file-picker button (used
for the background image, an image element, and the now-playing
placeholder) is now themed to match the app, and every element's
property panel gained "Center horizontally"/"Center vertically"
buttons for exact 50% placement without dragging to the snap guide.
Verified by rendering a standalone `media` element (with/without live
media, opacity < 1) and via Playwright against the real built frontend
+ live backend.

**Fix: the canvas now shows the actual picked image, and the resize/
save workflow is legible.** User feedback: picking an image only
updated a filename label -- the canvas kept drawing a generic "IMAGE"
placeholder, so it looked broken, and it wasn't obvious dragging the
one corner handle always changed width and height together, or that
this canvas is already a live preview (no Save/Start needed just to
see an edit reflected here). New `GET /api/dashboard/image?path=`
serves a previously-picked image's actual bytes back (restricted to
`image_store.py`'s own managed folder), so an image element renders
the real picture inline (clipped, opacity-aware) and all three file
pickers show a thumbnail, the instant a file's picked. Graph/image/
media elements gained separate width-only and height-only edge
handles alongside the existing corner handle. A "Save layout*" /
"Unsaved changes" indicator now shows once the layout differs from
what's actually saved, and the top hint spells out the three-stage
model explicitly (canvas preview is instant; Save layout persists it;
Start/Apply pushes it to the panel). Verified via Playwright against
the real built frontend + live backend: upload → inline render +
thumbnail in one interaction, a real mouse-drag on the width-only
handle leaving height untouched, and the dirty indicator's on-edit/
on-save transitions.

**Fix: the panel port is a "Detect screens" dropdown now, first on the
page, and the app is disabled until one's picked.** The old free-typed
port text field also had a latent bug -- its hint claimed "Port needs
Save" but no Save button was ever wired up for it. New `GET /api/ports`
(`controller.list_ports()`) exposes the same USB-VID scan
(`find_hongtai_ports()`) the Tkinter Refresh button already used, now
over HTTP. The new "Panel port" section (moved above Preview) scans on
load and on "Detect screens", auto-picks the obvious choice when
exactly one screen is found and nothing's selected, and saves a pick
immediately (no separate Save step). Controls/settings/the dashboard
canvas are disabled with an explanatory hint until a port -- a
specific one, or the explicit "Auto-detect" option -- is actually
chosen, rather than silently defaulting. Verified via Playwright: the
disabled state before any pick, picking "Auto-detect" persisting and
unlocking Controls immediately, and the selection surviving reload.

**Fixes: image elements now show the whole picture and size themselves
from it; now-playing elements can hide any of art/name/time; the clock
is a real element.** Image elements were cover-fit (crop to fill),
which cropped a freshly-added element's small default box down to a
sliver of most real pictures, and made a width-only resize look like
the picture was being overwritten rather than scaled. New `fit` field
("contain" default -- whole image visible, letterboxed; "cover"; or
"stretch") on image elements, plus a picked file's own aspect ratio
now sets the new element's box client-side (no upload round-trip)
instead of leaving the generic default square. Media elements gained
`show_art`/`show_name`/`show_time` toggles (each on by default) so any
combination can be shown. And the clock -- previously the one thing
`render_frame()` always drew unconditionally at a fixed spot -- is now
a `"clock"` element type (x/y/font size/color/opacity/show-seconds),
addable/movable/deletable like anything else; `DEFAULT_ELEMENTS`
includes one at the exact old fixed position, and `render_frame()`
only falls back to the old hardcoded draw when a saved `elements` list
has no clock entry at all, so nothing changes for a pre-upgrade config
until it's actually edited. Verified headlessly (a 4:1 test image
fully visible/letterboxed under contain-fit; art-only and time-only
media renders, which also caught and fixed a real crash -- a
non-integer coordinate reaching the glow-draw helper whenever
`show_art` was off; `DEFAULT_ELEMENTS` rendering identically to the
old fixed clock, and a clock-stripped `elements` list still rendering
the fallback pixel-identical to before) and via Playwright against the
real built frontend + live backend.

**Cleanup: removed the static clock/Spotify fallback paths and replaced
them with a real one-time migration.** The clock's "no element -> draw
the old hardcoded clock" fallback in `render_frame()` and
`_draw_spotify_middle()`/the `"spotify"` `middle_content` option (the
original fixed, un-movable, always-on now-playing display, predating
and silently overlapping the `"media"` element it's the actual
replacement for) are both gone. `slots_to_elements()` now appends a
default clock and media element directly (via new
`dashboard_theme.default_clock_element()`/`default_media_element()`),
so every layout derived fresh from `slots` -- `DEFAULT_ELEMENTS` and
`theme_kwargs.resolve_dashboard_elements()`'s fallback alike (the
latter didn't even get a clock before this) -- has both with nothing
to migrate. The one case that can't just derive its way out of this --
a config with a concrete saved `elements` list from before either
type existed -- gets a new `config_store.migrate_dashboard_elements()`,
called once from both `AppController.__init__` and `App.__init__`
right after `load_config()`. It adds a clock if missing, and a media
element (+ resets `middle_content` to `"none"`) if there's no media
element and `middle_content` was `"spotify"`/unset -- leaving alone
anyone who'd already deliberately chosen weather/none. Flagged via
`dashboard._migrated_elements_v1` so it's truly one-time: deleting the
clock/now-playing element afterward sticks. Verified headlessly (six
migration scenarios covering no-config/slots-only/old-saved-list with
each middle_content value/already-migrated/re-migration, plus a full
render pass over the new default layout with no crash) and via
Playwright against the real built frontend + a live backend seeded
with a pre-migration config: the backend migrated and persisted it on
startup, the canvas showed both widgets as separately selectable/
movable elements, the Middle content dropdown offered only Weather/
None, and the "Nothing playing" placeholder section rendered
unconditionally instead of being gated behind the removed `"spotify"`
option.

**UX pass: element list, arrow-key nudging, collapsible sections.**
Selecting anything meant clicking it directly on the canvas, which
doesn't work for small or fully-overlapped elements -- no way to even
tell two things were stacked there. New element list panel
(`DashboardCanvas.jsx`) shows every element by type/label/id, click to
select, highlighted to match the canvas selection. This also surfaced
and fixed a real bug: `makeId()`'s uniqueness check was scoped to
whatever array the current tab happened to have loaded, not globally
unique, so a stale tab (open across a backend-side config migration,
say) could generate an id that collided with one already saved --
exactly what caused a duplicate, only-one-selectable clock. `makeId()`
now always appends a random suffix. New arrow-key nudging (1%/5% with
Shift, matching the property panel's own X%/Y% units) for finer
positioning than mouse-dragging allows, going through the same
`commit()` path a drag does so Undo covers it too. New
`Collapsible.jsx` wraps every top-level section (both `App.jsx`'s and
`DashboardCanvas.jsx`'s) behind a click-to-toggle header with its
open/closed state remembered in `localStorage` -- System/Log/the three
occasional dashboard sub-settings default collapsed, cutting a lot off
what used to be one long scroll. Verified via Playwright: list rows
select their matching canvas element, arrow keys nudge by the expected
amount and Undo reverts it, and each section's collapsed/open state
matches its configured default on load.

**Fixed the clock visually duplicating itself when the canvas is
overlaid on the live panel frame.** The SVG mockup always drew a
hardcoded sample time on top of the canvas; whenever the canvas is
overlaid on the live frame, that frame already shows the real,
ticking clock at the same spot, so the sample text landed right on
top of it. It now only renders when there's no live frame under it to
collide with (an invisible hit-rect keeps it clickable either way).
Verified via Playwright, faking the connected state and frame image
via `page.route()` since the test backend has no real hardware.

**Live layout/background apply -- the "ghost element" and "Reset to
defaults keeps my images" bugs, and their shared root cause.**
`dashboard.elements`/`background` were only baked into the running
panel's static image at Start/Apply time (a deliberate perf choice,
see `build_static_background()`'s docstring), so moving an element on
the live canvas moved the SVG mockup instantly while the real panel
kept showing the old position until a manual restart -- reading as
duplication -- and the same staleness meant a canvas Reset didn't
clear whatever was still baked into the live panel. Both are fixed the
same way the "Nothing playing" placeholder and Middle content were
already live: `dashboard_theme.set_pending_dashboard_layout()` queues
an elements/background edit, and the running render loop rebakes with
it (a full rebake -- cheap next to a 100ms frame budget) at the top of
its very next frame, no Stop/Start needed.
`save_dashboard_elements()`/`save_dashboard_background()` in
`controller.py` queue it right after persisting to config. Verified
headlessly (queuing elements-only/background-only/combined updates
each land the right keys; both controller methods queue correctly)
and via Playwright (adding an image element then Reset to defaults
removes it from the element list).

**Clock customization: analog styles, more digital formats, a custom
image face.** A clock element now picks a `face` -- digital (the only
option before this, and every existing saved clock is one, unchanged
in appearance), analog, or image -- the same way a gauge picks a stat.
Digital gained `hour_format` (24h/12h with AM/PM) and an optional
`show_date` line. Analog is a procedurally-drawn round face (ticks,
hour/minute/second hands from the real system time, redrawn every
frame same as digital always was) in one of three `analog_style`s:
Classic, Minimal, Neon -- sized by `radius`, the same
fraction-of-min(width,height) convention as a gauge. Image lets you
pick any picture (same upload flow as an image element) as a
decorative clock skin, with the digital time drawn on top of it,
shadowed for legibility over any picture's own colors. Backend:
`_draw_clock_element()` dispatches to
`_draw_analog_clock_face()`/`_draw_image_clock_face()`, with a small
path-keyed cache for a custom face image so it isn't re-decoded from
disk 10 times a second. Frontend: the canvas SVG mockup and property
panel both grew per-face previews/controls; `dashboard_meta()` exposes
the option lists (`clockFaces`/`clockAnalogStyles`/`clockHourFormats`)
so the frontend doesn't hardcode them. Every new field falls back via
`.get()` to reproduce the exact old digital look, so no migration is
needed for an existing saved clock element. Verified: `render_frame()`
renders all three faces to a real image with no crash (digital in both
hour formats with/without a date line, analog in all three styles with
real ticking hands, image with a fake decorative PNG); Playwright
confirms switching faces shows the right controls and the canvas
mockup updates to match.

**"Nothing playing" placeholder folded into the now-playing element's
property panel.** It used to be a standalone, always-visible
collapsible section regardless of whether a now-playing element even
existed on the layout; now it only appears in the property panel when
a now-playing element is selected, matching every other
element-specific setting. Purely a frontend display change -- the
settings are still shared dashboard-level config, just conditionally
shown instead of always rendered as its own section.

**Two-column page layout.** The single centered `max-width: 720px`
column wasted most of a wide window's width, most visibly on the
design canvas. New `.app-columns` CSS grid: a fixed 280-380px left
column for app-level settings read once and left alone (Panel port,
Controls, Config, System, Log), and a flexible right column for the
Preview and whatever the current theme needs (including the canvas,
free to use whatever width is left). Collapses back to one column
below ~860px.

**Likely fix for a real overnight memory leak (~2GB RSS).** Prime
suspect: the dashboard theme's Spotify now-playing poll called
`MediaManager.request_async()` fresh every second instead of once,
each call building a whole new WinRT/COM object graph that doesn't
reliably get released on a never-idle asyncio loop. Fixed by caching
the manager; also added a periodic RSS log line so a real leak is
visible from the Log panel alone. Not yet independently confirmed
against real hardware -- this is Windows/WinRT-only and can't be
exercised in a sandbox.

**No more "already running" dialog on a second launch.** Double-
clicking the desktop icon while the app is already running used to
fall back to a `messagebox.showinfo()` whenever `FindWindowW` /
`SetForegroundWindow` failed to activate the existing window -- which
was most of the time, since Windows can silently refuse to let a
background process steal foreground focus, with no way for the caller
to detect that. Replaced with a mechanism that can't fail that way: a
second launch touches a sentinel file's mtime (`SHOW_TRIGGER_PATH`),
and the *running* instance's own existing 100ms poll timer notices the
change and raises its own window from its own Tk thread. `FindWindowW`
is still tried first as a same-instant bonus; the file-touch path is
the one that's actually guaranteed to work. No dialog is ever shown to
the end user for this any more.

**Preview and config redesigned again, this time side by side.** The
two-column layout above still stacked Preview directly on top of
whatever the current theme needed, which still forced scrolling
between them and left a visibly empty gap under the shorter left
column. Preview and the theme's settings now sit side by side in a
flex row (for Video/Webpage/Clock); the dashboard theme skips the
separate Preview entirely (its own canvas already shows the live
frame) and instead gets the full row for its own internal side-by-side
split -- canvas on the left, element list + property panel in a
sidebar on the right. Both splits wrap back to one column below their
combined minimum widths. This is still a page in whatever browser tab
the user has open, not a window this app controls -- Phase 7's
packaged webview window is what will actually get to pick a wide
default shape.

**Rows redesigned again, to the exact shape requested.** One full-width
Controls row (panel port + theme picker + Start/Stop/Apply, merged into
one section), the selected theme's own config next to Preview right
below it, then Brightness, System and Log each as their own full-width
row -- no more left/right page columns at all, just `.app`'s own
top-to-bottom stack. That removes the last source of an empty gap
under a shorter column, since there's no longer a second independently
tall column to be shorter or taller than.

**Root-caused the desktop icon doing nothing at all.**
`startup_registration.py` and `desktop_shortcut.py` both resolved "the
app" via `sys.modules["__main__"].__file__` -- fine only if `app.py`
is the sole caller, but both are reachable from the same control-server
endpoints the web frontend's System panel hits, and that server can
just as well be `scripts/run_backend.py` or `scripts/run_v2_app.py`
(backend_app.py). Toggling Startup or Create Shortcut while either was
`__main__` baked THAT script in instead of `app.py`; since neither
opens `app.py`'s Tkinter window but both share its single-instance
mutex, the real desktop icon could end up "already running" against a
process with no window to ever raise -- silently doing nothing.  Fixed
by resolving via `_app_base_dir()` (the real repo root) in both
functions instead of trusting `__main__`. A shortcut/Startup entry
written before this fix stays stale until re-created once.

**Fixed a gauge's resize handle always showing, selected or not.**
Every other element type (text, graph, image, media, clock) already
gated its resize handle(s) behind `{isSelected && (...)}` in
`DashboardCanvas.jsx`; a gauge's handle rendered unconditionally, a
pre-existing bug that went unnoticed until the design canvas became
the page's full-width centerpiece and made it much more visible.
Fixed to match every other element type. Verified via Playwright
before/after screenshots.

**Removed the "Middle content" setting; weather is now a movable/
resizable element, like now-playing already was.** Weather used to be
a `dashboard.middle_content` global on/off (`"weather"`/`"none"`)
pinned to the fixed column between the two gauge columns, with one
shared `weather_location`/`weather_units` pair for the whole app --
exactly the shape the now-playing display had before Phase 6 turned
it into the `media` element (see above). Weather gets the identical
treatment now: `default_weather_element()`/`_draw_weather_element()`
(mirroring `default_media_element()`/`_draw_media_element()`) replace
`MIDDLE_CONTENT_OPTIONS`/`_middle_content`/`set_middle_content()`/
`get_middle_content()`, which are gone entirely. `location`/`units`
move onto the element itself (so more than one could, in principle,
each show a different place -- weather.py's background poll still
only tracks one location at a time, same single-shared-poll design
`get_media_info()` already has for now-playing, via a new
`apply_weather_from_elements()` helper that picks the first `weather`
element on the canvas and points the poll at it; called at Dashboard
startup and again on every live layout edit, so editing an existing
element's location/units from the design canvas applies without a
Stop/Start). `config_store.migrate_dashboard_weather_element()`
converts a saved `middle_content: "weather"` into a real element
carrying its old location/units, one-time-flagged the same way
`migrate_dashboard_elements()` already is; `middle_content`/
`weather_location`/`weather_units` are dropped from config either way.
Frontend: `+ Add weather` joins the other `+ Add <type>` toolbar
buttons, the property panel gained a Location/Units/Opacity section
for it, and the standalone "Middle content" collapsible section (and
its `/api/dashboard/middle_content` endpoint) are gone. `app.py`'s
Tkinter Dashboard tab drops its Show/Location/Units controls too (a
short pointer to the web design canvas replaces them), so it stops
writing the now-retired config keys back on every save. Verified:
`render_frame()` renders a weather element with no crash, both with a
location set (falls back to "Weather unavailable" with no network
reachable in this sandbox -- the same tolerant-fallback path the old
code had) and without one (shows the "set a location" placeholder);
the `/api/dashboard/meta` endpoint no longer exposes `middleContent`
and exposes `weatherUnitOptions` instead; and via Playwright against
the real built frontend -- clicking "+ Add weather" adds a resizable
element with the right property panel, and the Middle content section
is gone from the page.

**Fixed the element list scrolling unnecessarily by default.**
`.element-list`'s `max-height: 140px` predates the clock/media/weather
elements -- once those joined the 8 default gauges, the default
layout's 10-11 rows didn't fit in 140px any more, so the list was
scrolling even with nothing custom added yet. Raised to 480px (fits
every built-in default at this sidebar's width) and made `overflow-x`
explicit (`hidden` rather than left unset, which let some browsers
resolve it to `auto` too and draw a scrollbar on an axis nothing
actually overflowed). Verified via Playwright: `scrollHeight` now
equals `clientHeight` with the default layout loaded.

**Weather's pieces (icon/temperature/description/feels-like+humidity/
location name) are independently switchable**, the same
`show_art`/`show_name`/`show_time` deal `media` already has --
requested specifically so weather can be shrunk to just an icon, or
just the temperature, to sit next to a now-playing element without
the two eating the whole panel between them. `default_weather_
element()` gained `show_icon`/`show_temp`/`show_description`/
`show_details`/`show_location` (each default True, so an existing
saved element renders unchanged); `_draw_weather_element()` only
draws whichever pieces are on, stacked top-to-bottom starting from the
box's top edge so a disabled piece doesn't leave a gap. The "no
data yet" placeholder message only draws when at least one text piece
is switched on -- an icon-only element with no data yet just stays
blank instead of filling its (likely small) box with a paragraph.
Frontend: five checkboxes in the weather property panel, mirroring
media's Cover art/Track-artist/Progress-time row. Verified:
`render_frame()` renders icon-only, temperature-only, and icon+
temperature combinations with no crash (including the no-location and
lookup-failed fallback paths at a small box size), and via Playwright
against the real built frontend -- all five checkboxes appear, default
checked.

**Media/weather content is now vertically centered in its box instead
of top-anchored, fixing an "inaccurate layout" complaint.** Both
elements' content is a vertical stack (art/icon, then text lines, one
piece after another) that was always started flush with the box's top
edge -- fine when the content happened to fill the box, but the
default box is deliberately generous (room for every piece at once),
so turning pieces off (or just not filling a tall box) left a growing
gap under the visible content, all still inside the box's own
boundary. On the real panel that reads as the box and the content
disagreeing about where things are, which made lining up two elements'
boxes edge-to-edge (the whole point of dragging them next to each
other -- e.g. a compact weather icon+temp next to now-playing) NOT
line up their actual pictures/text. Fixed by pre-measuring exactly how
tall the currently-enabled (and, for weather, actually-present --
a description/detail/location line with no data doesn't reserve
space) pieces are going to render, then starting the whole stack at
`box_y0 + max(0, (box_h - content_h) / 2)` instead of `box_y0`. A
piece being switched off still doesn't leave a gap where it used to
be, same as before -- only where the *whole stack* starts changed.
Verified: rendered a media+weather element side by side, both full and
in a compact icon/art-only configuration -- content sits vertically
centered in each box at every size, matching what dragging the boxes
edge-to-edge visually promises.

**The design canvas's edit-only mockup boxes no longer draw on top of
the real live panel image once connected**, fixing a complaint that the
tinted fill/border/"NOW PLAYING"/"WEATHER" labels visibly sat over the
actual rendered content and "ruined the preview." The design canvas
doubles as a live preview: once connected, it overlays the real live
frame (an actual JPEG snapshot of what the panel is currently showing)
underneath its SVG editor layer, so a graph/image/media/weather
element's real baked content is already drawn there by the frame
itself -- the mockup box was then just a duplicate editor annotation on
top of content that was already visible. The `clock` element type
already got this right (its hands-and-face mockup only draws when
there's no live frame to collide with, or the element is currently
selected, so it's still always locatable/resizable) -- this extends the
same `overLiveFrame` condition (`connected && !!frameUrl`) to the
graph/image/media/weather box-rendering branch in `DashboardCanvas.jsx`
via a new `showMockup` flag: `el.type === "graph" || isSelected ||
!overLiveFrame`. That flag now gates the tinted rect, the picked-image
border rect, and the centered label text -- all three only draw when
there's no live frame to collide with or the element is selected. A
plain `graph` element is the one exception and keeps its mockup box
unconditionally, connected or not, since nothing in the live frame ever
draws where a graph's bars *will* go the way it does for a photo, album
art, or a weather readout -- the mockup is the only indication of where
it'll render. When the mockup is hidden, an invisible `fill="transparent"`
rect takes its exact place so the element's full footprint stays
click/drag-able either way -- selecting, moving, and resizing an
element works identically whether or not its box happens to be visible
at that moment (an SVG `fill="transparent"` is a specified fill, so it
still receives pointer events, unlike `fill="none"`). Verified via
Playwright against the real built frontend: with no screen connected
(pure editor/mockup mode, the only mode reachable without physical
hardware in this environment), every graph/image/media/weather element
still shows its tinted box and label whether selected or deselected,
matching the pre-existing behavior this reuses from `clock` --
confirming the fix didn't regress the disconnected editing path. The
connected-with-live-frame hiding path itself relies on the identical
condition already proven correct for `clock` and could not be exercised
end-to-end without real hardware.

**Gauges got the same treatment**, on a direct follow-up request ("same
things for the gauges, i want them looking as they are looking in the
screen till selected"). A gauge's colored ring plus its stat-title text
is dynamic content too -- the live frame already draws that exact
gauge's real ring and live value at that spot once connected, so the
mockup ring/title was the same kind of duplicate annotation the box
types above had. The gauge branch (the very end of the elements `.map`
in `DashboardCanvas.jsx`, the "original element type" case) now
computes its own `overLiveFrame`/`showMockup` locally (`isSelected ||
!overLiveFrame` -- no `graph`-style always-on exception here, since
every gauge's real content is always baked into the live frame the
same way media/weather/clock's is) and gates the visible `<circle>`
and title `<text>` behind it, with an invisible `fill="transparent"`
circle standing in when hidden so the hit area/drag/click behavior is
unchanged. The selection outline (dashed stroke) and the corner resize
handle were already gated behind `isSelected` and needed no change.
Verified via Playwright in disconnected mode (again the only mode
reachable without the physical panel): the default 8-gauge layout
renders identically to before, both with a gauge deselected and with
one selected (dashed outline + resize handle showing as before).

**Three follow-up bugs reported against that same mockup-hiding work,
fixed together.**

First: **"Reset to defaults" (and any other unsaved edit) looked like
it did nothing while connected.** `showMockup` in each of the three
`overLiveFrame` computations above only checked `isSelected ||
!overLiveFrame`, with nothing accounting for whether the live frame it
was deferring to actually reflected the *current* `elements` -- it only
reflects whatever was last pushed via Save layout (see that button's
own hint text). Reset to defaults touches every element at once and
leaves none of them selected, so with a live frame connected, every
mockup vanished simultaneously while the underlying live frame still
showed the old (un-reset) layout -- from the outside, nothing appeared
to happen at all. Fixed by adding `&& !dirty` to all three
`overLiveFrame` computations; `dirty` (`elements !==
savedElementsRef.current`) already existed to drive the toolbar's "You
have unsaved changes" hint, so this reuses that same signal: any
unsaved edit keeps every mockup visible no matter the connection state,
and mockups only defer to the live frame again once Save layout brings
it back in sync.

Second: **now-playing/weather's selection box didn't shrink or grow
along with which pieces were switched on**, reported as "if we remove
something the box should be now smaller." The earlier content-
centering fix (this same Phase's own `_draw_weather_element()`/
`_draw_media_element()` entry above) re-centered the *content* inside
the box when a piece was turned off, but the box -- the thing you're
actually looking at and lining elements up against on the canvas --
stayed exactly the size it was last dragged to, so a compact icon-only
weather element still carried a box sized for the full readout. Each
show_* checkbox's `onChange` in `DashboardCanvas.jsx`'s property panel
now also computes a new `height` via one of two new browser-side
estimator functions, `estimateWeatherHeight()`/`estimateMediaHeight()`,
mirroring those same two backend functions' content-height pre-
measurement as closely as a client-side estimate reasonably can -- not
exactly, since whether weather data has loaded or something's actually
playing isn't knowable before the theme is even running, so both
estimators always assume the fully-populated case (every enabled piece
has real content), which is the right size to aim for either way.
Height only, not width, since both elements stack their pieces
vertically. Verified via Playwright: adding a weather element and
turning off description/feels-like+humidity/location dropped its
height field from 46% to 36%, and additionally turning off temperature
(icon only) dropped it further to 24%, with the on-canvas yellow
selection box visibly shrinking to match at each step.

Third, and the most involved: **dragging an element snapped its center
to wherever the cursor first landed**, reported as "clicking on a
gauge moves it to where I clicked... which isn't the intended
behavior." `onPointerMove`'s "move" branch set the dragged element's
x/y straight to the cursor's own fraction-of-canvas position on every
move, with nothing recording *where on the element* it had actually
been grabbed -- so grabbing anywhere off-center (an edge, say) made the
element jump until that exact point became the new center. Fixed by
computing `offsetX`/`offsetY` (the gap between the cursor and the
element's center) once at the moment of the grab in
`onPointerDownGauge`, then subtracting that same fixed offset back out
of every subsequent cursor position in `onPointerMove` -- the element
now tracks the cursor's *movement* from the grab point, not its raw
position. This is the shared move handler every draggable element type
uses (gauge, text, graph, image, media, weather, clock's non-selection
hit-shapes), so the fix isn't gauge-specific despite how it was
reported.

Chasing the exact pixel numbers down turned up a second, independently
-triggering bug in the same code path, fixed alongside it:
`onPointerMove` re-queried `svgRef.current.getBoundingClientRect()` on
every single move rather than once, and a drag's very first move flips
`dirty` true (see the "Reset to defaults" fix above) -- which reveals
the "unsaved changes" hint line above the canvas, a real DOM reflow
that shifts the whole canvas box (and this rect) down by however tall
that line is, *in the middle of the gesture*. Every move after the
first was then computing its fraction against a rect whose top had
silently shifted out from under the still-held cursor, so the element
drifted off the cursor's actual movement by that same amount for the
rest of the drag -- on top of, and independent from, the offset bug.
Fixed by snapshotting the rect once in `onPointerDownGauge`/
`onPointerDownHandle` (both now take a `rect` parameter through to
`pointerToFraction()`) and reusing that one snapshot for the entire
gesture instead of re-reading a potentially-shifted DOM on every move.

Verified with a Playwright test that reads an element's exact
(unrounded) position out of the rendered SVG before and after a drag
and compares it against the exact pixel distance the (simulated)
mouse moved, rather than trusting the rounded-to-whole-percent X%/Y%
property fields: before either fix, a pure-vertical 40px drag
registered as only ~20 viewBox units of movement (should be ~32) from
the reflow bug alone, and a 40px-vertical drag grabbed 40px off-center
additionally registered only half the expected vertical delta on top
of that, from the offset bug. After both fixes, a drag's on-canvas
movement matches the simulated cursor's own pixel movement exactly, on
both axes, regardless of where on the element it's grabbed.

**A follow-up report on the "Reset to defaults" fix above: "the
element on the right[, i.e. the element list,] gets deleted so we have
only the default, but the preview itself still shows even the images I
added and everything."** The `!dirty` fix made every element's own
mockup correctly reappear at its default position/size the moment
there's an unsaved edit -- but the actual live-panel photo underneath
it (`<img className="canvas-frame">`, a real snapshot of what the
physical panel is showing *right now*) is a separate piece of this
canvas, rendered unconditionally from `frameUrl && connected` with no
regard for `dirty` at all, since it only ever reflects the last
*saved* layout and Reset alone doesn't save anything. So the visible
result of a Reset while connected was every default mockup box
correctly appearing, layered on top of an unchanged photo of whatever
custom layout was there a moment ago -- any uploaded image, a moved
gauge -- still showing through wherever the new (smaller, default-
sized) mockups didn't happen to cover it. From the outside that reads
as "the preview didn't change," even though the element list (which
has no stale photo of its own to contend with) correctly showed only
the defaults the whole time. Fixed by adding the same `&& !dirty` to
this `<img>`'s own render condition. Any unsaved edit now hides the
photo entirely, dropping the canvas back to the identical plain dark
background pure-editor mode it already uses while disconnected -- so
what's on screen is exactly, and only, what `elements` currently says,
with no leftover photo underneath to disagree with it. The photo
reappears the instant Save layout clears `dirty`, now showing the
real, caught-up panel.

**Two more requests landed together: a toolbar cleanup, and a new
element type.**

Save layout "shouldn't be like adding stuff" -- it used to sit inline
with the "+ Add X" buttons as just another button in that row, always
clickable, with a separate "* You have unsaved changes..." sentence
underneath doing the job of explaining when it actually mattered. That
sentence is gone now; `.canvas-toolbar` is `justify-content:
space-between` with exactly two children -- a new `.canvas-toolbar-
group` wrapping every add-element button plus Undo/Redo/Reset (all
"still building the layout" actions), and Save layout on its own,
which space-between pushes to the toolbar's far right edge. Save
layout is also properly `disabled` (the same greyed-out treatment
Undo/Redo already had) whenever `dirty` is false, gaining its orange
"unsaved" styling only once there's something to actually push --
so the button's own state now carries what the removed sentence used
to say, and it visually reads as a distinct "commit this" action
rather than one more thing you can add.

Second: "we don't have bars, we have gauges, graphs, we need bars" --
a new `bar` element type, a linear meter for one stat's *current*
value (the same reading a `gauge` shows -- live value against its own
min/max -- as a horizontal fill bar instead of a ring), for lining
several stats up as a compact stack, or simply for the look. Not to be
confused with `graph`'s existing "Bar" *style* option, which plots a
scrolling history of past values as vertical bars -- `bar` has no time
axis at all, just always the current reading, the same "no history"
relationship gauge already has to graph.

Backend, in `dashboard_theme.py`: `_bar_box()` (a plain rectangle box,
mirroring `_graph_box()`) and a static/dynamic split mirroring graph's
own -- `_draw_bar_static()` bakes the bound stat's title above the box
into the static background (same `build_static_background()` hook
graph's own static half uses), while `_draw_bar_dynamic()` redraws the
live fill + current-value text every frame in `render_frame()`, the
same "recomputed every frame" treatment a gauge's needle/value gets.
The fill itself reuses `progress_bar_glow()` -- the exact filled-
track-plus-glowing-knob look `_draw_media_element()`'s own playback bar
already draws -- rather than writing a second bar-rendering routine
from scratch; the only wrinkle was that helper builds its own PIL
tiles from `(w + 20, h + 20)`, which needs real ints, so the box's
otherwise-float pixel dimensions get `int()`'d the same way
`_draw_media_element()`'s own `bar_w`/`bar_h` already are. A missing
stat reading draws an empty track and "--" rather than guessing zero,
the same "don't lie about missing data" rule `draw_gauge_dynamic()`
follows for a gauge with no reading yet.

Frontend, in `DashboardCanvas.jsx`: a "+ Add bar" toolbar button,
`makeElement()`'s "bar" case (wider-than-tall by default -- 26%x12% --
since it's meant to read as a single bar, not a roughly-square plot
the way graph's default box is), a Stat/Color/Opacity property panel
(no Style or History fields -- there's nothing time-based to
configure), and its own resizable box on the design canvas, added to
the same box-rendering branch graph/image/media/weather already share.
Its mockup-hiding behavior matches `graph`'s, not `media`/`weather`'s:
both graph and bar always keep their mockup box, connected or not,
because neither one's mockup attempts to draw the real bars/fill (that
needs live history/stat data this editor doesn't have) the way media/
weather's mockup duplicates an actual "NOW PLAYING"/"WEATHER" label the
live frame already shows (and so hides once connected, per the earlier
entries in this Phase) -- a bar's mockup box is the *only* thing
showing where it'll be, same reasoning graph's exception already had.

Verified: direct `render_frame()` calls for a normal reading, a stat
with no data yet, and a very small box (checking the int() fix holds
at the small end) all render without error; Playwright confirms
clicking "+ Add bar" adds a correctly-badged, selectable, resizable BAR
element with its own Stat/Color/Opacity panel, and that the toolbar
changes above (no unsaved-changes sentence, Save layout disabled until
`dirty`, pushed to the right) all hold on a fresh page load and after
adding an element.

Two follow-up fixes landed right after this, both from the same round
of feedback ("when u modify something the background goes outside the
window, i mean you have a picture, keep rendering it, and keep
everything as it is, bar should the user be able to put it vertically
or horizontally"):

First, the `!dirty` fix that made "Reset to defaults" show up
immediately (hiding the live-panel `<img>` photo whenever there was an
unsaved edit) got reverted. It solved the staleness problem, but at a
cost that turned out to matter more: the canvas dropped to a plain
black background for the entire duration of *any* edit -- a drag, a
checkbox flip, even just selecting something -- not just the rare
Reset case, and that read as broken far more often than the staleness
it was fixing ever did. The photo is back to `frameUrl && connected`
with no `dirty` check, so it's now genuinely live -- always rendering,
continuously refreshing, never goes black. The "make an edit visibly
register immediately" job moved onto the mockups instead: every
element type's `showMockup` computation now includes `|| dirty` (the
`!dirty` that used to sit on each branch's own `overLiveFrame` is
gone), so an unsaved edit still draws its mockup on top of the photo
right away -- the photo itself just no longer has to disappear to make
that happen. Net effect: exactly what "Reset to defaults" needed
(mockups update immediately) without the side effect of a black
canvas mid-edit.

Second, `bar` picked up a Horizontal/Vertical `orientation` field
(default `"horizontal"`), so a bar element can now line up as a
vertical meter too -- not just the horizontal fill bar it launched
with. `progress_bar_glow()` (the shared filled-track-plus-glowing-knob
routine, also used by the now-playing progress bar, which stays
horizontal-only) gained a `vertical=` parameter that swaps which axis
the fill, rounded ends, and knob travel along, while the caller-
supplied track rect (`[x, y, x+w, y+h]`) stays exactly the same shape
either way -- `_draw_bar_dynamic()` just picks which axis is the
"long" one and which is the capped-at-22px "thickness" one based on
`el["orientation"]`, rather than needing two different box shapes.
The property panel's new Orientation dropdown also swaps the element's
own width/height fields when toggled, so switching to vertical turns
a wide-short box tall-narrow to match (and back again) -- purely a
frontend convenience, not something the backend requires.

The one real wrinkle: a vertical bar's live value text needed a new
home. The horizontal layout puts it past the bar's far end along its
own thickness axis (above the bar, at its right edge) -- rotating that
90° literally would put the vertical bar's text below it, which
seemed the obvious choice at first, but that's exactly where the knob
sits when a reading is near its *minimum* (the knob's position runs
from the top at 100% fill down to the bottom at 0%) -- i.e. the
common, idle-reading case, not a rare one. Pinning the text to the
*top* of the bar instead only risks colliding with the knob near 100%
fill, which is the same rare-case trade-off the horizontal layout's
own text placement already quietly accepts (its text can end up right
on top of the knob once a reading is close to its max too) -- so this
isn't a new class of bug, just the existing one made symmetric instead
of hitting the common case for one orientation and the rare case for
the other.

Verified: direct `render_frame()` calls for a horizontal bar, a
vertical bar, and a vertical bar with a missing stat reading (confirms
the empty track + "--" text renders with no knob/text collision at the
idle position that used to be the problem) all produced the expected
image; a Playwright pass added a bar element, confirmed the Orientation
dropdown defaults to Horizontal, and confirmed switching it to Vertical
both updates the dropdown and swaps Width%/Height% (26/12 -> 12/26) as
well as the on-canvas mockup box's shape.

Actually seeing the vertical bar rendered on real hardware (rather than
just this sandbox's own test renders) turned up two more issues in the
same round, both fixed right after landing the above.

First: "bar is looking bad? what is that?" -- the huge white ball
sitting on the bar wasn't a rendering bug exactly, more a scale
mismatch nobody had actually looked at on real hardware yet.
`progress_bar_glow()`'s knob radius has always been the track's own
thickness times 1.7 -- tuned against the now-playing progress bar's
thin 6px track, where that comes out to a reasonable ~20px knob. The
bar element's track is much thicker (~22px, capped, vs. that bar's
6px), and the same 1.7 multiplier against it produces a ~75px ball --
more than 3x the track's own thickness, dwarfing it and half-covering
the title/value text next to it. Gave `progress_bar_glow()` a
`knob_scale` parameter (default 1.7, so the now-playing bar is
completely unaffected) and had `_draw_bar_dynamic()` pass 0.8 for its
own call -- picked visually against a render, not derived from
anything, just small enough that the knob reads as a handle sitting on
the track rather than a separate blob swallowing it.

Second: "weather box still not containing it, it still overflowing" --
a real screenshot showed the "Feels 32°C · 57% humidity" detail line
spilling out past a selected weather element's dashed selection
outline, running into the clock below it. The root cause: WEATHER MODE
Phase 6's `_draw_weather_element()` only ever *centers* its
icon/temp/description/details/location stack within the element's
box height -- it has never clipped or scaled anything to fit -- so a
box shorter than what the enabled pieces actually need (this one was
19%x9%, i.e. ~86x43px, nowhere near enough for a full five-piece
readout) just let the content spill past the box's edges silently.
Nothing before this checked that the stored height was even large
enough in the first place.

First attempt: added `_weather_content_height()` (the same content-
height pre-measurement `_draw_weather_element()` already did
internally, pulled out so `_weather_box()` could call it too) and had
`_weather_box()` take `max(stored_height, content_height + 12)` instead
of trusting the stored height blindly. Verified with a direct
`render_frame()` call and shipped -- but the very next screenshot from
real hardware showed why this was the wrong fix: growing the box to
217px (from a stored ~43px) to fit the full five-piece readout did
stop weather's *own* content from overflowing, but the box's much
larger real footprint now overlapped the now-playing element sitting
above it and the clock sitting below -- still visibly broken, "Brain
Power Amadeus" and "19:39:25" now bleeding into/past the weather
element's own dashed selection outline, just a different collision
than before. Growing an element's box to fit its content doesn't
actually respect the layout the user built around it.

Reworked into the opposite approach: `_weather_box()` now always
returns exactly `el["width"]`/`el["height"]` (same as every other
resizable element, no exception for weather) -- it never grows past
what those fields say, full stop. `_draw_weather_element()` instead
scales its *content* down to fit whatever box that turns out to be:
icon size and every font size (temp/description/details/location) now
shrink together by one `scale` factor -- `box_h / content_h_natural`,
computed from the pieces' natural full-size heights, floored at 0.55 so
text never shrinks into illegibility on a small panel -- whenever that
natural combined height would be taller than the box. Below that
floor, a genuinely too-small box just clips rather than either
overflowing or forcing a resize on its own. Scaled font objects are
fetched through a new `_cached_scaled_font()` (`load_font()` wrapped in
`functools.lru_cache`) so an element whose scale factor stays constant
frame to frame -- the common case, since it only changes if the box
itself is resized -- doesn't reload a TrueType font from disk on every
single frame. `estimateWeatherHeight()` on the frontend is unchanged as
a checkbox-toggle *suggestion*, but the "also apply it as a floor on
the mockup's rendered height" piece from the first attempt was removed
-- the on-canvas mockup now always matches `el.width`/`el.height`
exactly, same as the real panel.

Verified: a direct `render_frame()` call reproducing the screenshot's
exact layout -- a small 19%x9% weather box with all five pieces on,
positioned between a now-playing element above and a clock below --
confirmed the box no longer grows past its own dimensions (stays
~60x43px, only the pre-existing generic 60px floor every box type
already had) and every piece, including the detail line, renders as a
compact but fully-contained readout with no bleed into either
neighbor; a separate `render_frame()` pass for a horizontal bar and the
now-playing progress bar side by side (from the knob fix above)
confirmed the knob-scale change only affected the bar element; a
Playwright pass typed 19/9 into a selected weather element's
Width%/Height% number inputs and confirmed the on-canvas mockup box
now stays at exactly that size instead of growing past it.

A third issue, spotted right after: "when i move an object, why do
other objects get highlighted?" -- a regression from the "keep the live
panel photo always visible" fix earlier in this same phase, which added
`|| dirty` to every element type's `showMockup` computation so that a
mass change with nothing selected (Reset to defaults being the whole
reason it was added) would still visibly update. The bug in that:
`dirty` goes true on *any* edit, not just a mass one -- dragging a
single gauge, or flipping one checkbox in the property panel, is just
as "dirty" as clicking Reset to defaults. So an ordinary drag on one
element also flipped every *other* box-type element's (image, now-
playing, weather) mockup border into view for the whole gesture, even
though nothing about them had changed -- which is exactly what read as
"other objects getting highlighted" when you're only touching one.

Fixed by scoping the force-show behavior to when it's actually needed:
a new `forceAllMockups = dirty && !selectedId` replaces the bare
`dirty` check everywhere it appeared. Reset to defaults (and an
Undo/Redo landing on a state with nothing selected) clears
`selectedId`, so `forceAllMockups` still kicks in there and forces
every mockup to show, same as before. An ordinary drag or property-
panel edit, though, always has the element being edited as the
selected one -- `isSelected` on its own already makes that element's
mockup show, with no need for `forceAllMockups` to also cover it -- so
gating on `!selectedId` means a plain drag no longer trips the "show
everything" path at all, and every other, untouched element's mockup
goes back to just deferring to `overLiveFrame` like normal.

Verified: the `dirty && !selectedId` logic itself checked directly
against the three cases it has to tell apart (a drag with something
selected --> false, Reset with nothing selected --> true, no unsaved
edits at all --> false) all came out correct; a Playwright pass added a
second box-type element (an image, alongside a gauge) and dragged the
gauge across the canvas, confirming the image element's own mockup box
stayed untouched throughout the whole gesture -- only the gauge being
dragged moved or changed appearance.

A fourth and fifth issue came in together, in one message with no
screenshots: "sometimes when selecting something, i think it gets moved
a bit, because as soon as i click on an object, the save layout
activates, also the bar looks highlighted by default."

The first half traces back to a property real mice/trackpads/touch
input all share, and one that's easy to forget when reasoning about
pointer events in the abstract: a "click" is never *exactly* zero pixels of
movement between pointerdown and pointerup. There's essentially always
a stray pointermove event or two carrying a pixel or two of incidental
jitter, and `onPointerMove`'s "move" branch ran every single one of
those straight through to `setElements(prev => prev.map(...))` --
which allocates a brand-new array (and a brand-new object for the
moved element) *even when* the computed `nx`/`ny` come out numerically
identical to what was already stored. `dirty` is computed as a strict
`elements !== savedElementsRef.current` reference check, not a value
comparison, so that alone was enough to flip `dirty` true -- and light
up "Save layout*" -- on what the user experienced as simply clicking to
select something, with no drag intended at all. The "gets moved a bit"
half of the same report is the same mechanism seen from the other
side: since the jitter's computed `nx`/`ny` aren't *guaranteed* to be
bit-for-bit identical to the element's stored position (only usually
close enough not to notice), an unlucky pointermove could in principle
commit a barely-visible unintended nudge along with the reference
change.

Fixed with a small minimum-distance gate before a move/resize gesture
is allowed to touch `elements` at all: a new `MOVE_THRESHOLD_PX = 4`
constant, and both `onPointerDownGauge` and `onPointerDownHandle` now
also record where the gesture started in real screen pixels
(`startClientX`/`startClientY`) plus a `moved` flag on `dragRef.current`
(initially `false`). `onPointerMove` measures the straight-line pixel
distance from that start point on every call; while it's still under
`MOVE_THRESHOLD_PX` and `moved` is still `false`, it returns immediately
without calling `setElements` at all -- no state change means `elements`
keeps its exact prior reference, so `dirty` stays `false` and Save
layout doesn't activate. The very first pointermove that crosses the
threshold flips `moved` to `true`, and every pointermove after that
behaves exactly as before (this is a one-way gate for the gesture, not
a per-event re-check, so a real drag that happens to pause and jitter
mid-gesture never "un-arms"). `endDrag` was given the same `moved` flag
to check: a gesture that never crossed the threshold never touched
`elements`, so there's nothing to undo, and skipping the Undo-stack push
in that case avoids cluttering it with a no-op entry every time the user
merely clicks something. Verified with a Playwright pass driving raw
`page.mouse` down/move/up events (rather than `.click()`, which doesn't
model real per-pixel movement) on a gauge: a down, two 1-2px moves, and
an up left "Save layout" reading exactly "Save layout" (inactive),
while the identical sequence followed by a real 20px move correctly
flipped it to "Save layout*" -- confirming the fix suppresses jitter
without breaking real drags.

The second half -- "the bar looks highlighted by default" -- turned out
to be a real, separate bug in the `bar` element's `showMockup`
computation, not a perception issue. When `bar` was added earlier in
this phase, its box-drawing code was folded into the same branch as
`graph`, `image`, `media`, and `weather` (they're all box-shaped
elements sharing one render path), and `showMockup` picked up
`el.type === "bar"` alongside `el.type === "graph"` in the "always show
this mockup, connected or not" case. That reasoning is correct for
`graph`: its mockup is a rough placeholder box that has no way to
preview a real, continuously-updating line/history, so there's no
"accurate live version" for it to defer to even when connected -- it's
the only thing showing where a graph will be. It does not hold for
`bar`: a bar's mockup is just its accent-colored fill/border/label
drawn at a fixed demo fraction, which is a perfectly reasonable stand-in
but not meaningfully different in kind from a gauge's mockup (a demo
needle position) or a clock's (a static digital/analog face) -- both of
which already defer to the live frame photo once connected, showing
their mockup only when selected, disconnected, or forced. Left grouped
with `graph`, a saved and *unselected* bar element kept its tinted
fill/border/label drawn permanently on top of the accurate live photo,
which is exactly what reads as "looking highlighted by default" -- it
visually resembles the dashed selection treatment even with nothing
selected, forever, because the mockup never stopped being drawn.

Fixed by removing `el.type === "bar"` from `showMockup`'s unconditional
list, leaving `bar` to fall through to the same
`isSelected || !overLiveFrame || forceAllMockups` condition gauge/
clock/media/weather already use -- selected, disconnected, or right
after a Reset-to-defaults/Undo-Redo with nothing selected. `graph`
keeps the original always-show behavior; only `bar` moved over. No
other code needed to change: the branch already had a transparent
hit-rect fallback for the `!showMockup` case (used by every other type
in this group), so `bar` picked that up for free and stays fully click/
drag-able either way. Verified by checking the `showMockup` boolean
directly against the selected/connected/forceAllMockups matrix for both
`bar` and `graph` -- `bar` now comes out `false` (deferring to the live
photo) in the connected-and-unselected-and-not-forced case where
`graph` still correctly comes out `true`.

Right after the knob went away, the same conversation asked for the bar
to be "more customisable" in general -- a color picker, gradients ("having
only two colors isn't that cool"), and a gradient direction ("the
gradient is only linear, maybe i want it to be left to right, not just
down to up"). Read against what the app already had: gauge's own
"2nd color" option (`color2`) is exactly a two-stop gradient with no
direction control of its own (an angular sweep around the ring, not a
screen-axis choice), and the only *linear* two-stop gradient anywhere
in the theme (`_linear_gradient()`, used for background presets) is
hardcoded top-to-bottom. So the ask was for the bar to do noticeably
better than both of those: more than two stops, and a real left-right/
top-bottom choice.

Landed as three additions. First, `show_knob` (default `False` for
every bar now, matching the "i dont like [it]" feedback from the fix
just above) lets it be turned back on per-bar for anyone who does want
it -- previously it was unconditionally drawn with no way to turn it
off at all. Second, a `gradient` flag plus `gradient_colors` (a list of
2-4 RGB stops, not capped at two) replaces the single flat `color` fill
when turned on. Third, `gradient_direction` ("horizontal" or
"vertical") picks which screen axis the gradient itself runs across --
deliberately kept independent of the bar's existing `orientation`
field (which picks which axis the *value* fills along): a horizontal
bar can have a top-to-bottom gradient, a vertical bar a left-to-right
one, or either can match its own fill direction, whichever looks
better for a given stat.

The backend work: `_linear_gradient_multi(width, height, colors,
direction)` generalizes `_linear_gradient()` (left completely alone --
its own two callers only ever need a fixed top-to-bottom two-stop
gradient, so there was no reason to touch it) to an arbitrary stop
count and either axis, via the same vectorized-numpy approach --
`np.linspace` walks 0..(n-1) across whichever axis is picked, and each
position's color is linearly interpolated between whichever pair of
adjacent stops it falls between. `progress_bar_glow()` (shared with the
now-playing progress bar's own playback meter) gained `show_knob`
(defaults `True`, so the now-playing bar's own knob is unaffected --
only the bar element's caller passes `False`) and `fill_colors`/
`fill_direction` params. The trickiest part was keeping colors anchored
correctly as the bar's value (and so its visible fraction) changes
frame to frame: a naive approach -- building the gradient sized to just
the currently-filled portion -- would make every color slide/rescale
across the bar as the fraction grows, which reads as constant color
*shifting* rather than a fixed-position gradient being gradually
revealed (unlike a solid fill, where "more of the same color" showing
is imperceptible, more of a *shifting* gradient is very noticeable).
The fix, in the new `_bar_fill_subtile()` helper: build the full-size
gradient at the bar's *entire* track dimensions first, then slice out
just the pixels for whatever sub-region is currently visible (the
left `fill_w` columns for a horizontal bar, the bottom `fill_h` rows
for a vertical one, matching the direction the fill already grows
from) -- so a given x/y position's color is fixed relative to the
track's full length, and only how much of that fixed gradient is
uncovered changes as the value moves. The rounded-end mask, though, is
deliberately NOT sliced from a full-size mask the same way -- that
would leave the visible end of the fill with a hard, square-cut edge
instead of a rounded cap, since the full track's rounding only happens
at its own two ends, not at wherever the current fraction happens to
cut off. Building the mask fresh at the actual small sub-region size
(same as the original flat-fill version already did) keeps the same
rounded-cap look regardless of gradient vs. solid.

The frontend work: the bar's own property panel picked up a "Show
knob" checkbox, a "Gradient fill" checkbox that swaps the single Color
picker for a Direction dropdown once checked, and (only once gradient
is on) a row of 2-4 color-stop swatches with a "+ Add color stop"
button (capped at 4) and a "✕" remove button per stop once there are
more than 2 (so it can never be reduced below the minimum a gradient
needs). Turning gradient on for the first time seeds two stops from
whatever flat color was already set plus gauge's own default second
color, rather than starting from scratch, so the bar doesn't visually
jump the moment the checkbox is ticked. The on-canvas mockup itself
was upgraded too -- rather than showing a flat `accent`-colored swatch
regardless of gradient settings (which would make picking colors blind
until Save layout, a bad editing experience for something whose whole
point is to preview live), it now defines an actual SVG
`<linearGradient>` from the selected stops/direction and fills the
mockup rect with it, so the effect of every control shows immediately,
matching how every other bar property already updates live in this
canvas.

Verified: rendered `_draw_bar_dynamic()` directly for a 2-stop and a
3-stop horizontal gradient, a vertical-direction gradient on a
horizontal bar, a horizontal-direction gradient on a vertical bar, and
knob on vs. off -- all matched the intended look (colors anchored
correctly at a fixed 62%/40% fraction, rounded caps intact, knob
appearing only when requested). A Playwright pass then drove the new
panel controls end to end on a freshly-added bar: ticking Gradient
fill switched the Color picker to a Direction dropdown and showed the
mockup's gradient live; adding a third color stop showed all three
swatches with remove buttons; switching direction to Top → Bottom
re-oriented the mockup's gradient band from horizontal to vertical.

**Gradient fill generalized from bar to text, graph, clock, and
gauge; plus a no-commitment "Preview on screen" button.** Two
requests from the same follow-up: (1) the bar element's gradient UI
(2-4 color stops + direction) was worth having everywhere else a
color picker sits, not just on bar; (2) editing a layout only ever
showed the change *after* clicking Save layout, which is a bad fit
for "let me see if I like this" -- the person wanted to see a change
on the real panel without it being permanent yet.

Frontend: the bar-specific gradient JSX from Phase 6 was pulled out
into a standalone `GradientFillControl` component (module scope, not
nested in `DashboardCanvas`) parameterized by `defaultColor`,
`secondColor`, `showSolidColorWhenOff`, and `directionOptions`, so
every element's property panel can mount the same checkbox +
Direction dropdown + 2-4 color-stop swatches UI against its own
`gradient`/`gradient_colors`/`gradient_direction` fields. Text,
graph, and clock's digital face all dropped their old flat
`<input type="color">` in favor of it; gauge kept its separate
"Custom color" checkbox (a nullable single-color override) alongside
the new gradient control, since the two are independent settings, and
passes its own three-option direction list (Diagonal/Left → Right/
Top → Bottom) matching what a round gauge ring actually looks
sensible swept along. The on-canvas mockup preview needed the same
live-gradient treatment Phase 6 gave the bar, so a shared
`gradientFill(el, idPrefix, fallbackColor)` helper (returns either a
flat CSS color or a `url(#id)` reference plus the `<linearGradient>`
JSX to drop in a `<defs>` block) replaced the bar-only inline version
and is now called from the box/text/clock/gauge render branches
alike.

Backend, three different techniques depending on what's actually
being colored: **text and clock's digital face** render as a plain
alpha mask first (draw the glyphs at fill=255 on a black `L`-mode
image), then a full-size `_linear_gradient_multi()` RGB gradient is
built, converted to RGBA, and has that mask applied via `.putalpha()`
-- same trick as recoloring a stencil. **Graph** (both bar-style and
line-style tiles) draws its existing tile normally to get the right
shape and alpha channel, then swaps in a gradient the same way,
reusing the tile's own alpha rather than a separately-drawn mask.
**Gauge** didn't need any of that -- cairo's `LinearGradient` already
supports `add_color_stop_rgba(offset, r, g, b, a)` for an arbitrary
number of stops natively, so both `draw_gauge_static()` and
`draw_gauge_dynamic_tile()` just loop over 2-4 stops instead of
hardcoding a single accent-to-accent2 fade. Gauge's older single
`color2` field (always swept diagonally) still works on layouts saved
before this change -- a new `_element_gauge_gradient()` helper prefers
the new fields but falls back to folding `color2` into a 2-stop
diagonal gradient when they're absent, so nothing already saved
needed migrating.

"Preview on screen" reuses the exact mechanism Save layout already
had for showing an unsaved edit live (`dashboard_theme.set_pending_
dashboard_layout()`, which the render loop picks up on its very next
frame) but skips `config_store.save_config()` entirely -- the canvas's
current elements go straight to the panel, a `threading.Timer` waits
5 seconds, then a new `controller.py` method reverts the pending
layout back to whatever's actually saved on disk (or the defaults, if
nothing's saved yet). The timer is tracked on the controller
(`self._preview_revert_timer`, guarded by the same lock everything
else in there uses) and is cancelled if a new preview starts, or if a
real Save happens, before it fires -- so an in-flight preview can't
revert *after* the person decided to keep it and hit Save, and two
quick successive previews don't race each other's reverts. The new
button sits next to Save layout, shows a live "Previewing... Ns" countdown
label while active, and -- like Save layout -- is disabled unless the
Dashboard theme is actually the one running (there's nothing to show
a preview on otherwise).

Verified: confirmed via Playwright that the Preview button is
disabled when the Dashboard theme isn't running (matching Save
layout's own gating). Rendered each of the four backend gradient
paths directly (text, graph in both line and bar style, clock digital
face, gauge in both diagonal and horizontal direction) and inspected
the output images -- correct stop colors and positions in every case,
existing shape/mask/alpha preserved. Drove the new property-panel
gradient controls for text, graph, and gauge end to end through
Playwright (toggle on, confirm the Direction dropdown and color
swatches appear, confirm the on-canvas mockup updates live) and
confirmed gauge's separate "Custom color" checkbox is untouched by
the new gradient section sitting next to it.

**Bring to front/Send to back fixed for dynamic elements; vertical/
horizontal bar fixed to show a fill at low values.** Two bug reports
from actually using the canvas day to day, both traced to
`dashboard_theme.py`'s render side rather than the frontend (the
buttons and the bar's value math were both already correct).

`z`-order: `build_static_background()` has always sorted elements by
`z` before baking them into the one-time background image, so bringing
a *text* or *image* element (the only two fully-static types) to front
worked. But `render_frame()` -- the per-frame loop that draws every
*dynamic* element (gauge, graph, bar, media, clock, weather) fresh
every frame on top of that background -- walked the plain `elements`
list, never sorting it, so `z` was silently ignored for every element
type that actually needs live redrawing. Since a typical dashboard is
almost entirely gauges/bars/clock/media, this made the buttons look
completely broken in practice even though the data (`el.z`) was
updating correctly the whole time -- confirmed by inspecting the SVG
DOM order in the design canvas (which *does* reorder correctly, since
it renders straight from the sorted `elements` array) versus the
actual rendered frame (which didn't). Fixed by sorting
`layout["elements"]` by `z` in `render_frame()` too, matching the
static bake. Still a known gap: since static elements are baked into
the background once and every dynamic element is layered on top of
that per frame afterward, a dynamic element can never be sent behind a
static one (or a static one brought in front of a dynamic one) no
matter what `z` says -- true full-stack ordering across that boundary
would need static elements re-drawn per frame too, which is future
work if it turns out to matter in practice; ordering *within* the
dynamic group, which is the case that was actually reported and the
overwhelming majority of real layouts, now works.

Bar low-value fill: `progress_bar_glow()` only drew the filled portion
at all when the fill's own height/width (in pixels) exceeded the
track's thickness, which is also the fixed radius `_bar_fill_subtile()`
used for its rounded-end mask -- below that (roughly thickness ÷
track-length as a fraction, ~15% for the default bar proportions), it
skipped drawing the fill outright to avoid an oversized-radius rounded
rect, leaving just the empty track and the knob with no visible color
at all. Since this is exactly the range a CPU-usage bar sits in at
idle, it read as the bar being permanently stuck at 0% until usage
spiked. Fixed by having `_bar_fill_subtile()` clamp its own radius to
half of whichever of its width/height is smaller before drawing the
mask, and loosening `progress_bar_glow()`'s guard to draw for any
nonzero fill rather than only once it clears the track's thickness --
now even a couple of percent shows as a small rounded dot that grows
smoothly into the full pill shape, instead of nothing.

Verified: rendered `progress_bar_glow()` directly at 2%, 5%, 10%, 15%,
and 30% on a vertical bar (thickness 22px over a 200px track, so the
old cutoff sat at 11%) -- all five now show a visible, correctly-
positioned and correctly-sized fill, where the first three used to
render nothing but the knob. Rendered two overlapping gauges twice,
swapping which one had the higher `z`, and confirmed the frame output
visibly changes which gauge's ring/needle/value text sits on top each
time -- previously identical regardless of `z`. Re-ran a full default-
layout render end to end to confirm the sort didn't break anything
else.

**Preset picker: pictures instead of a name dropdown.** The saved-
presets `<select>` told a person nothing about what a preset actually
looked like until after loading it. `dashboard_theme.
render_preset_thumbnail()` renders a small preview through the exact
real render pipeline (fixed demo stats instead of live hardware
readings, `media=None` reusing the existing "nothing playing"
placeholder, a short synthetic wave for any graph element), so a
thumbnail can never drift from what the preset would actually show.
`AppController._dashboard_preset_thumbnails()` renders one per saved
preset (against the currently configured background -- presets only
ever store `elements`) as a base64 PNG data URI, returned from both
`dashboard_meta()` and the save/delete preset endpoints. The web UI's
picker is now a grid of cards: click a thumbnail to load it
immediately, a per-card Delete button arms on the first click and only
deletes on the second. Verified against a mocked backend driven
through a headless browser -- two real, visibly different thumbnails
rendered, click-to-load and the two-step delete both behaved
correctly, no console errors.

**6 built-in dashboard presets, and presets can save their own
background.** `dashboard_theme.BUILTIN_DASHBOARD_PRESETS` -- Neon
Horizon (futuristic), Bubblegum (cute), Panic Mode (funny), Mission
Control (informative), Midnight Minimal, and Arcade RGB -- seeded into
`dashboard.presets` on a fresh config's first load only
(`config_store.seed_builtin_dashboard_presets()`; never re-seeded once
that key exists in any shape, so deleting one is a real, respected
choice). Fixed a real bug found while tuning them:
`render_preset_thumbnail()` rendered straight onto a half-size canvas
while `Fonts()` uses fixed pixel sizes calibrated for the panel's real
960x480 resolution, so titles clipped ("CPU LOAD" -> "PU LOAD") --
fixed by rendering at the reference resolution and resizing the
finished image down instead. Presets can now carry their own
background (`{"elements": [...], "background": {...} or None}`,
`save_dashboard_preset()`'s new optional `background` param);
`config_store.migrate_dashboard_preset_shape()` upgrades old
bare-list presets to this shape on load. Loading a preset stages its
background into the draft alongside its elements, same "not applied
until Save" deal as elements already had. Verified against a mocked
backend serving the real 6 built-ins: all render as distinct pictures,
loading one with its own background updates both the element count
and the background draft, delete-confirm still works against the full
set, no console errors.

Two more requested since: a text element can now bind to a stat (a
"Source" toggle -- Custom text vs Live stat -- plus a `template` like
`"CPU {value}"`) instead of only ever showing a fixed typed string, so
a hand-designed layout can carry live readings as plain labels, not
just gauges/bars. This is the one case a text element is now redrawn
per-frame instead of baked into the static background once.
`dashboard_theme.py` also gained 4 bundled "photo" backgrounds (Aurora
Glow, Deep Nebula, Synthwave Sunset, Bokeh Night --
`BUNDLED_BACKGROUND_IMAGES`, generated by the new one-off `scripts/
generate_backgrounds.py`, no external image model, just layered PIL/
numpy blur+gradient work) alongside the existing flat-gradient/
line-art modes, resolved through the exact same cover-fit-and-darken
path a user's own uploaded "Custom image" already used. Verified
end to end (headless browser against a mocked backend): the Source
toggle shows/hides the Stat+Template fields and the plain Text field
correctly, the element list and on-canvas preview both reflect a bound
element's live-value placeholder, and all 4 bundled styles correctly
hide the "Color scheme" picker (a photo isn't tinted) while a
procedural style keeps showing it -- no console errors.

Immediately caught a real problem with the "seed the 6 built-ins into
app_config.json once" design from earlier: this dev config already had
`dashboard.presets` populated with plain copies of the 6 built-ins
(from a prior seed), so restarting the app after code changes never
showed anything new -- the picker was reading those frozen copies, not
the current code. Reworked to a merge instead: `dashboard_theme.
BUILTIN_DASHBOARD_PRESETS` is no longer copied into app_config.json at
all; `config_store.resolve_dashboard_presets()` merges it with
whatever's actually saved fresh on every read (saved side wins on a
name collision -- how saving over a built-in's name customizes it), so
an app update to a built-in (or a new one) reaches every install
immediately, no migration needed for *future* changes. Deleting a pure
built-in records its name in a new `dashboard.dismissed_builtin_
presets` list instead of removing something that was never actually
stored. `migrate_strip_redundant_builtin_presets()` is the one-time
cleanup for existing installs (like this dev config) that already had
the old seeded copies -- removes a `presets` entry only when its name
*and* content still exactly match a current built-in (an untouched
copy); anything a person actually edited under a built-in's name is
left alone as a real customization. Verified directly: seeding the old
shape (2 real presets + all 6 built-ins copied in verbatim, plus the
old seed flag) and running the new migration strips exactly the 6
copies back out, leaving the 2 real presets in app_config.json while
the merge still shows all 8; a built-in resaved with an edited field
survives the strip (content no longer matches, so it's kept as a real
override) and the merge correctly shows the edited version.

Added 4 more built-ins right after -- Northern Lights (aurora), Deep
Space (nebula), Outrun Drive (synthwave), City Nights (bokeh) -- one
per bundled photo background, since none of the original 6 (all
procedural backgrounds) had ever actually used them; each rendered and
visually checked through render_preset_thumbnail() the same way the
original 6 were. 10 built-ins total. Also added a Duplicate button to
every preset card (next to Delete) -- works on a built-in or a saved
preset identically, since the picker already reads both through
resolve_dashboard_presets()'s merged view; it's just another
save_dashboard_preset() call under an auto-generated "(copy)"/"(copy
N)" name. Verified: all 10 built-ins render as distinct picture cards,
duplicating one produces a correctly-named copy with no naming
collision on a second duplicate, no console errors.

Two bugs reported back against that same round, from a screenshot of
the running app with "Outrun Drive" loaded: its network reading showed
as garbled overlapping text ("NENET.GM/s" instead of "NET 12.4M/s"),
and the new Duplicate/Delete buttons were squeezing preset names down
to "N...", "B...", etc. Root cause on the first one: every text
element -- custom or stat-bound -- is already baked into the panel's
real live frame once connected, but the SVG design-canvas overlay was
drawing its own copy of that same text unconditionally, with no gate
at all (every other element type -- box/image/media/weather, the
clock -- already had a `showMockup`/`showClockMockup` gate deferring
to the live frame once connected, added back when those were built;
text just never got one). For a stat-bound element that's a genuinely
different string overlapping (its fixed "--" placeholder vs. the real
value) -- the garbled smear reported; for plain custom text it's the
same string rendered twice with slightly different font metrics (SVG
vs. the backend's PNG) -- a visible ghost/blur in an actual screenshot,
even though "identical content overlapping invisibly" had been the
assumption when the stat-bound Source toggle was built. Gave text
the same gate as everything else: `isSelected || !overLiveFrame ||
forceAllMockups`, with an invisible hit-rect standing in for the
hidden `<text>` so a not-currently-selected stat-bound element can
still be clicked and dragged. Nothing about the actual data stream was
ever broken -- this was purely the editor's own overlay; verified by
rendering Outrun Drive's real backend frame (`render_preset_
thumbnail()`'s own pipeline, fed plausible stats including a network
figure) into a mock `/frame.jpg` and confirming headless-browser: zero
SVG `<text>` nodes render over it unselected (both the title and the
network reading come through clean, matching the live frame exactly),
and the stat-bound element's mockup reappears correctly the moment
it's selected for editing. Second bug: collapsed Duplicate + Delete
behind a single "..." button per card (opens a small menu, closes on
an outside click or once an action completes) instead of two
always-visible full-label buttons, freeing the footer's width back to
the name. Verified: duplicate-then-delete both still work end to end
through the new menu, the menu opens/closes correctly, and preset
names not aggressively long render in full.

Immediately caught a second problem with that same menu, this time
from a screenshot: the popover started opening but was cut off
mid-button ("Deep Space"'s menu showing only a sliver of "Duplicate").
`.preset-card` had `overflow: hidden` on it -- there to round the
thumbnail image's top corners down to match the card's own
`border-radius` -- and that clipped the menu too, since it's
positioned (deliberately) outside the footer's own box so it can float
over the thumbnail above it rather than getting squeezed into the
footer's few remaining pixels. Moved the corner-rounding onto `.preset-
card-thumb` itself (`border-radius: 7px 7px 0 0; overflow: hidden`,
scoped to just the thumbnail) and dropped `overflow: hidden` from the
card entirely. Verified at the user's own narrow viewport width (5
columns, same as their screenshot): the popover's bounding box now
sits fully outside the card with both buttons visible and clickable,
and duplicating through it still works end to end.

Reported next, with a screenshot: loading "Panic Mode" showed a broken
mashup -- its own gauges/text overlapping ghosted labels and a stray
icon that turned out to belong to whatever theme the panel was
actually still running. Two separate things were going on. First, the
design canvas's live-panel photo (`.canvas-frame`) only ever updates
from the real hardware, and loading a preset doesn't push anything to
the hardware by itself (Save layout/Save background or Preview on
screen do that) -- so right after a load, the photo underneath was
still the *previous* theme, while `forceAllMockups` (dirty, nothing
selected) correctly drew the *new* preset's mockups on top of it. Two
unrelated designs, stacked. Fixed by showing that preset's own pre-
rendered thumbnail (`meta.presetThumbnails[name]` -- the exact image
the picker card itself already uses, layout and background baked
together) as the canvas backdrop instead, for exactly the window
between loading and the next thing that makes it stale (a drag,
property edit, undo/redo, Reset to defaults, or an actual Save/
Preview) -- tracked via a ref holding the `elements` array reference
the thumbnail was captured for, cleared by a small effect the moment
`elements` moves on to anything else. `forceAllMockups` also had to
learn about this: with an accurate thumbnail already showing every
element in place, forcing the SVG mockups on top too would have
just re-introduced the same double-render problem against accurate
content instead of stale content, so it's now suppressed for that one
window (`hasAccurateBackdrop`, replacing each mockup gate's own local
`connected && frameUrl` check, folds in "or presetPreviewUrl is
standing in for it").

Second, and the actual reason the *background* specifically never
showed up even after intentionally trying it on the real panel:
"Preview on screen" only ever sent the edited elements to
`preview_dashboard_elements()`, never the background, even though a
loaded preset (or a hand-edited background dropdown) stages one right
alongside the elements. `preview_dashboard_elements()` now takes an
optional `background` too, snapshots the currently-saved background
the same way it already snapshots saved elements, and reverts both
together when the preview window ends; `save_dashboard_background()`
picked up the same pending-preview-cancel guard
`save_dashboard_elements()` already had, so a real Save landing mid-
preview can't get silently reverted by that preview's timer a few
seconds later. Verified: loading a preset now shows its own clean,
accurate render immediately (zero overlapping SVG text or shapes
until something's actually selected or edited); dragging an element
correctly falls back to the real live photo + per-element mockups;
Save layout clears the stand-in thumbnail; Preview on screen's request
now carries both the elements and the preset's own background.

Next question back, almost immediately: "clicking on a preset it only
shows it's image, the gauges and graphs dont move?" -- exactly right,
and a real regression to flag, not just a nice-to-have. The static
thumbnail fix above traded "two designs smashed together" for "a
frozen picture" -- better, but a needle that never moves and a network
reading stuck at one number for as long as you're looking at it reads
as its own kind of broken, especially right after the *previous*
fix's real live frame really had been visibly ticking (just showing
the wrong design). Rebuilt it properly instead of patching the
symptom: a new `dashboard_theme.render_live_preview()` renders
`elements`/`background` with this machine's actual current stats --
the exact same psutil/SystemInfos.exe/pynvml/winsdk calls run()'s own
render loop makes every frame, just called once per request instead of
in a 10Hz loop -- and a new `POST /api/dashboard/live_preview`
(`controller.py`'s `render_dashboard_live_preview()`) exposes it. The
design canvas polls that endpoint on a ~1.2s interval (EDITING_
PREVIEW_POLL_MS) whenever `elements`/`bgDraft` differ from what's
actually saved and there's no active "Preview on screen" countdown
already showing the real thing for real -- covering a loaded preset,
a drag, a property edit, undo/redo, Reset to defaults, all the same
"the real live photo doesn't reflect this yet" cases the static
thumbnail covered, just kept alive with fresh numbers on every tick
instead of frozen at whatever it looked like the moment it was
rendered. `forceAllMockups`/`hasAccurateBackdrop` didn't need to
change shape at all -- editingPreviewUrl slotted into the exact same
"is there an accurate backdrop right now" role presetPreviewUrl had,
just refreshed repeatedly instead of set once.

Caught one real bug writing the "stop polling once saved" half:
saveLayout() marks things saved by mutating `savedElementsRef.current`
(a ref) to match `elements`, which already equals the just-saved array
-- there's no `setElements()` call to go with it, since nothing about
`elements` itself needs to change. But a ref mutation alone doesn't
retrigger a `useEffect` whose dependency array never actually changed,
so the polling loop from before the save just kept ticking forever
with a stale closure, silently never noticing `dirty` had gone false.
Fixed with a plain `savedVersion` counter, bumped on every successful
save and added to the effect's dependency list purely to give it a
reason to re-run and notice.

Verified against this sandbox's own real (if modest) CPU/RAM/network
readings, not fixed placeholder numbers: loading a preset shows a
live-rendered JPEG immediately, three separate polls over ~2.8s each
actually reached the backend (confirmed via a call counter added to
the mock server) and produced genuinely different images between at
least some of them, and clicking Save layout stops the polling
outright and hands back to the real (mocked) live frame -- verified
both by the frame's `src` switching back to `/frame.jpg` and by the
poll counter staying flat afterward instead of continuing to climb.

Next request, once all three of the above shipped: "save 'my preset'
and its wallpaper as a default theme that ship with the app, and name
it what u see fit" -- take the user's own custom-saved preset (an
8-gauge full-stats layout: CPU load/RAM/GPU load/network in the four
corners, GPU temp/CPU freq as smaller side gauges, disk/VRAM as mini
gauges along the bottom) and the custom photo they'd set as their
background at the time, and ship both together as an 11th built-in
(`BUILTIN_DASHBOARD_PRESETS`), naming it since they explicitly
delegated that. Read their live `app_config.json` off the device to
get the preset's exact element definitions (its own `background` was
`None`, so what it actually renders with is the app's global
`dashboard.background`, an `"image"`-mode entry pointing at a custom
upload) and requested access to the folder those uploaded images live
in (`...\HongtaiScreen\images`) to get at the actual file, which the
user granted. The photo itself: a circuit-board render, magenta/purple
fading into teal/cyan hexagonal traces -- named it "Circuit Bloom".
Center-cropped and resized it to the same 1920x960 JPEG-quality-90
convention the other 4 bundled photo backgrounds already use
(`assets/backgrounds/circuit_bloom.jpg`), and added it as a genuinely
new mode (`"circuit"`) in both `BUNDLED_BACKGROUND_IMAGES` and
`BACKGROUND_PRESETS` -- it goes through the exact same cover-fit +
darken code path every bundled or user-uploaded photo background
already does, so nothing in the renderer needed to change, and
`packaging/hongtai_screen.spec`'s existing `assets/backgrounds/*.jpg`
glob already covers it with no spec edit needed. The 8 gauges in the
new preset entry are a verbatim copy of the saved layout's own ids,
stats, positions, radii and z-order; like the original, none of them
set an explicit `color`, so they fall back to the standard left-half-
cyan/right-half-magenta accent split rather than needing one hardcoded
in. 11 built-ins total now. Verified: `render_live_preview()` on the
new preset's own elements/background renders correctly standalone;
regenerating the design-canvas meta through the real `AppController.
dashboard_meta()` (not a hand-built mock) shows "Circuit Bloom" as an
11th distinct picture card with a correct thumbnail; loading it in a
headless browser shows the full background and all 8 gauges live and
in place immediately, with no mashup and no clipping -- the same
picker/preview path the 3 fixes right above this one had just gone
through.

Next request, right after: "create two app presets, that uses text
field stats, like the image i sent u, something girly" -- a photo of a
pastel floral fan-controller readout (a title, soft ornate corner
flourishes, CPU/GPU stats as plain label+value text rows, no gauges),
asking for two built-ins in that style. Two things from the reference
photo weren't reproduced: its own illustrated artwork isn't copied,
and neither is its exact field list -- it showed CPU Temp/CPU Power/
GPU Freq, none of which this app has a live sensor reading for
(`STAT_DEFS` has no `cpu_temp`, `cpu_power`, or `gpu_freq` -- only
`gpu_temp`/`gpu_power` exist, and only on the GPU side). What carried
over was the *style*: text-only stat rows, no gauges at all -- a first
for this preset set -- laid out in two labeled columns, over a new
pastel floral photo background.

Two new bundled backgrounds first (`cherry_blossom.jpg`, `lavender_
bloom.jpg`, alongside the existing aurora/nebula/synthwave/bokeh/
circuit ones), built with the same plain-PIL layered-blob approach
`scripts/generate_backgrounds.py` already used for those, not an
external image model. A new `_draw_petal_flower()` helper composites
5 soft-edged ellipse "petals" in a ring (each just another `soft_
blob()` call, the same primitive every existing background already
leans on) around a brighter center, scattered across a rose-to-blush
gradient for one and a lilac-to-steel-blue gradient for the other,
plus a couple of loose single-petal blobs drifting between the
flowers and two soft corner glows standing in for an ornate frame
without actually drawing line art. Registered as `"cherry"`/
`"lavender"` in `BUNDLED_BACKGROUND_IMAGES`/`BACKGROUND_PRESETS`, same
as every other bundled photo -- goes through the identical cover-fit +
45%-black-blend darken path, so no renderer changes needed; checked
both post-darken renders directly (`Image.blend(img, black, 0.45)`,
what `_build_background_image()` actually does) before committing to
the palette, since a background this pastel could plausibly wash out
under that blend -- it doesn't, both land as a legible dusty-rose/
steel-lilac mid-tone.

Then the two preset entries themselves: "Cherry Blossom" (CPU column:
Load/Freq/Peak/RAM; GPU column: Load/Temp/Power/VRAM) and "Petal
Dream" ("System" column: Load/RAM/Disk/Swap; "Graphics" column:
Load/Temp/Power/VRAM; plus a centered Network reading) -- deliberately
different stat selections from each other so they don't read as a
recolor of the same field list, and between them covering every stat
this app can actually read for CPU/GPU/RAM/VRAM/disk/swap/network.
Every stat row is the existing `"stat"` + `"template"` text mechanism
(Northern Lights/City Nights introduced it, "LABEL {value}" -- e.g.
`"LOAD {value}"`, `"TEMP {value}"`), just used far more heavily here
than anywhere else: 8 and 9 stat rows respectively, vs. at most 1 in
any earlier preset. 13 built-ins total now. Verified the same way as
Circuit Bloom right above: `render_live_preview()` on each preset's
own elements/background renders correctly standalone (checked
visually -- title, column headers, all stat rows, clock+date all in
place, no overlap); the real `AppController.dashboard_meta()` (not a
mock) surfaces both as distinct 12th/13th picker cards with correct
thumbnails; loading either in a headless browser shows every row live
and in place immediately, no clipping, no console errors.

Two pieces of direct feedback on that pair, immediately: "u saw the
example i give u? the background integrate into the design, that's a
sophisticated theme, your isn't, it's a simple background on text on
top of it, i dont like it, aslo presssing a preset, and pressing save
layout doesn't update the background". Two separate bugs, one design
and one functional.

The design one first, since it's the bigger piece of work: v1's
generated backgrounds (`scripts/generate_backgrounds.py`'s
`make_cherry_blossom()`/`make_lavender_bloom()`) were a scatter of
soft-blurred flower blobs behind text placed independently on top --
technically "a background" and "some text", but not one integrated
design the way the reference photo's ornate gold corner scrollwork,
title cartouche, and portrait all visibly belong to the same card.
Rewrote both generators with real line art instead of only soft
blobs, and -- the actual fix for "integrate into the design" -- placed
every one of those elements using the *exact same x/y fractions* as
the matching preset's own text elements in dashboard_theme.py (new
`_CHERRY_LAYOUT`/`_PETAL_LAYOUT` constants mirror those elements'
positions verbatim), instead of scattering shapes at random and hoping
they'd roughly line up. Concretely: `_draw_title_banner()` draws a
proper ribbon/pennant silhouette (a single closed polygon with a
V-notch cut into each end) directly behind where the title text lands
-- the first attempt used a rounded-rect outline plus separately-drawn
diagonal "flag" lines that didn't actually meet the rounded corners,
rendering as a pill shape with two disconnected floating chevrons next
to it; fixed by tracing the whole ribbon (including both notched ends)
as one connected polygon. `_draw_corner_flourish()` draws 3 nested
quarter-circle arcs curling in from each corner plus a few
embellishment dots, composited via `Image.transpose()` flips so one
base image serves all 4 corners. `_card_frame_and_divider()` draws a
thin rule under each column header and a vertical divider between the
two stat columns, using the shared layout constants so the divider
lands exactly between where the CPU/GPU (or System/Graphics) columns
actually render. `_draw_flower_medallion()` (built on the existing
`_draw_petal_flower()`, plus a new `_draw_leaf()` -- a small rotated
ellipse, since PIL has no rotated-ellipse primitive -- for the first
time anything in this file needed actual foliage, not just flower
heads) composes a small 3-flower bouquet with leaves tucked under it,
placed in the empty gap between the two stat columns as the
composition's visual anchor -- standing in for the reference photo's
portrait, which v1 had no equivalent of at all. First version of the
bouquet had the leaves *longer than the flowers* and angled out past
their edges, reading as stray antennae rather than supporting
foliage, and the vertical divider drew straight through the middle of
it looking like a skewer; fixed by shortening/steepening the leaves so
only their tips peek out from under the petals, drawing them before
the flowers so the petals layer on top, and giving
`_card_frame_and_divider()` an optional `medallion_cy`/`medallion_gap`
so the divider draws as two segments that stop short of the bouquet
instead of one continuous line through it. The loose flower scatter
(60 petals drifting across the *entire* canvas in v1, including
directly behind the stat rows) is now 9 flowers confined to the
margins outside the two text columns, so the readout itself stays
calm instead of competing with background clutter for attention.

The functional bug: Save layout only ever called `saveDashboardElements
()` -- a loaded preset's own background sat in `bgDraft`, staged but
unsaved, until a *second*, separate "Save background" click further
down the page under Background settings, which the status message
technically mentioned ("Save layout / Save background to apply") but
nothing about the button itself made obvious was still required.
Reported exactly right. Fixed by having Save layout persist both
together in one action whenever a background is staged and actually
differs from what's saved: a new `savedBackgroundRef` (mirroring the
existing `savedElementsRef`) tracks what's actually persisted, and a
`backgroundsEqual()` structural-compare helper is needed because
unlike `elements` (always a fresh array from commit()/undo()/redo(),
so reference equality works for `dirty`), `updateBgDraft()` spreads
into a new object on every keystroke -- reference equality there would
read as "dirty" on every render. `dirty` itself and the live-preview
polling effect were both extended the same way, since neither had ever
covered a background-only edit (no element touched) either -- that
case used to leave `elements === savedElementsRef.current` true and
silently skip the live-preview backdrop entirely, so a background-only
change showed no visual feedback at all until a manual Save background
click. "Save background" stays as its own button for exactly that
case (background-only, no canvas edit); the two now overlap on purpose
rather than one replacing the other. Verified through the mock
harness: loading a preset and clicking Save layout fires both
`/api/dashboard/elements` and `/api/dashboard/background` with the
preset's own background, matching the elements call.

### Phase 6b — Themes that hold up next to the commercial ones

The feedback that kicked this off, with four reference screenshots
attached (a modern all-in-one monitor dashboard, a pastel floral one,
and two loud licensed-key-art ones): "please remove the titles from
the themes, all the themes, what are we kids? we need the super hero
fire spitter lmao ... i want shit like the images i sent u, when i say
like i mean reaaaally like". Three separate problems in that.

**Titles.** Every preset was drawing its own name on the panel
("NORTHERN LIGHTS", "DEEP SPACE", "SYSTEM MONITOR", "Doing great
today!"). None of the commercial themes does that -- the panel is 6.2
inches of prime real estate and the person looking at it already knows
which theme they picked. 15 title/greeting elements removed across 11
presets (done by walking the source's preset literals and deleting
whole element dicts by id, then re-importing and diffing element
counts, rather than by hand). Stat captions like "CPU FREQ" or "BRAIN
USAGE" stayed: those label a reading.

**Typography, which turned out to be the actual gap.** The renderer
could only ever call `load_font()`, which tries DejaVu Sans then
Arial then Segoe UI -- so every theme, whatever its palette, was set
in the same generic UI sans, and that single fact is most of why they
read as hobby projects next to the references (whose entire character
comes from a heavy display face). Fixed properly: six OFL families
now ship in `assets/fonts/` (Poppins, Orbitron, Chakra Petch, Bebas
Neue, Anton, Archivo Black -- obtained as the `@fontsource/*` npm
packages' latin-subset woff2 and converted back to .ttf with fontTools,
since Pillow can't read woff2; ~230KB total, license file alongside),
registered in `FONT_FAMILIES`, resolved per element through the same
`resource_path()` mechanism the bundled backgrounds use, cached by
(size, weight, family) since a per-element family means hitting disk
rather than the OS font cache. `"default"` deliberately maps to
None/None so every element saved before this resolves to exactly the
old candidate list.

**The drawing primitives the reference layouts need.** Rather than
fake these in the background art, four small additions to the
renderer, each of which is independently useful in the canvas:
`plate` on a text element draws a filled rounded chip behind it,
measured from the text's own bbox (a chip painted into the background
instead would have to be hand-fitted to a string it can't see, and
would drift the moment the text or font changed); `show_title` on
gauge/bar/graph and `show_value` on bar let a layout own its labels in
its own face instead of getting a second one in the fallback sans
drawn over it; `background["border"]` recolors or removes the fixed
purple panel frame that was being drawn on every theme regardless of
palette; `background["dim"]` controls the 45%-toward-black blend a
background photo gets -- correct for an arbitrary photo, wrong for a
background that IS the design and is already at final contrast; and
bars take a `track_color`, because the empty track is otherwise always
`dim_color(accent, 0.22)`, i.e. near-black, which is invisible on the
dark themes and a heavy slab on a light one.

Then the themes themselves. All five are built the same way, which is
the real answer to "integrate into the design": the cards and panels
each stat block sits in are drawn into the background art at the same
fraction-of-panel coordinates the elements use, so the chassis and the
readout can't drift apart. `generate_backgrounds.py` grew a small
vocabulary for this -- `_card()` (shadow, translucent fill, hairline
border, clipped accent stripe), `_well()` (the recessed graph plot
area), `_ribbon()` (a multi-stop gradient shown through a soft wavy
band mask, which keeps the color transition smooth in a way drawing
colored shapes can't), `_slab()`, `_halftone()`, `_glitch_bars()`,
`_keyline()`, `_chevrons()`.

  - **Fusion Core** -- the card dashboard. Three ribbon passes
    (a wide dim wash, the ribbon proper, a tight bright core) so the
    sweep still reads through near-opaque cards; first attempt had
    only the middle pass and the cards swallowed it, leaving what
    looked like grey boxes on black.
  - **Neon Pulse** / **Crimson Strike** -- both use the left-art /
    right-readout split every one of the reference screenshots uses,
    with the art zone holding the clock and graphic texture instead of
    the licensed character art it holds in the references. First pass
    had the panels filling nearly the whole canvas with the artwork
    only visible in the gutters; restructuring to that 30/70 split is
    what made them read as designs rather than decorated rectangles.
  - **Cherry Blossom / Petal Dream** -- rebuilt on the same system in
    a light register, which also retired the title banner v2 had
    added (it was framing a title that no longer exists).

Nothing was traced from the references: no characters, logos or
wordmarks, only composition and palette. Verified by rendering all 16
presets through `render_live_preview()` and reviewing them as images
(which is how the bouquet-skewered-by-a-divider, the white-date-on-
white-slash and the swallowed color ribbon were all caught), then in
the browser harness: 16 picker cards, a flagship preset loads and Save
layout persists elements *and* background including the new border/dim
keys, the Font and Label chip controls render for a selected text
element, no console errors. One editor bug surfaced doing that: graph
elements always drew their SVG mockup box and title, even over an
accurate live backdrop -- fine when every graph had a frame, wrong now
that a frameless graph is a deliberate choice, so that exception is
now conditional on `show_frame`.

Next, once those shipped: "defeault themes shouldn't be modified, like
if a user tries to modify them, we create a duplicate of it where the
user does his modifications". The one path that could mutate a
built-in was saving a preset under its exact name -- resolve_dashboard
_presets() prefers the saved side on a collision, so that saved copy
shadowed the code-defined preset permanently. That was deliberate once
(it was *how* you customized a built-in) but it's a bad trade on
inspection: the only way to tweak a built-in also destroyed your
access to the original, with nothing able to bring it back, and froze
that name against every future app update to it -- the exact failure
the merge-on-read design was introduced to avoid, just arrived at from
the other direction.

Now `save_dashboard_preset()` checks the name against BUILTIN_DASHBOARD
_PRESETS and, on a hit, saves under a free derived name instead
(`config_store.free_preset_name()`: "Fusion Core (custom)", then
"(custom 2)"), returning the name it actually used -- the frontend
reports that rather than echoing what was typed, since a save that
silently lands elsewhere is worse than one that refuses. Saving over
one of the person's *own* presets still overwrites in place; the rule
is only about the app's own read-only ones. The picker also warns
before the save, as soon as the typed name matches a built-in, and
badges every built-in card.

Two consequences worth handling rather than leaving:

`migrate_unshadow_builtin_presets()` covers configs that already
shadow a built-in from before this rule -- it renames the saved entry
to "<name> (custom)" so the customization survives and the built-in
reappears, and un-dismisses that name if it had also been deleted
(such a built-in was only ever visible *as* its override, so leaving
it dismissed would make the preset the person was actually using
vanish). It runs after migrate_strip_redundant_builtin_presets(),
which already removes untouched copies, so every collision it sees is
a genuine customization. Flagged `_unshadowed_builtin_presets_v1` so
it runs once and doesn't keep undoing a hand-edited config.

And deleting a built-in, now the only thing that can be done *to* one,
gained a real undo: `restore_dismissed_dashboard_presets()` clears the
dismissed list, exposed as `POST /api/dashboard/presets/restore_
builtins` and a "Restore built-ins" button that appears only while
something is hidden (the delete button itself reads "Hide" on a
built-in, since that's what it does). This isn't scope creep -- the
old behavior had an accidental undo (saving anything under that name
un-dismissed it) that this change removes, so without a replacement
"delete" would have become a one-way door.

Verified in the browser harness: the pre-save warning appears while
typing a built-in's name; saving it leaves the built-in in place and
produces "Fusion Core (custom)", then "(custom 2)" on a second save;
saving over a user's own preset still overwrites rather than piling up
copies; hiding a built-in removes it and offers the restore, which
brings it back and hides the button again; no console errors. The
migration and the naming rule were also exercised directly against a
synthetic config (a customized + dismissed "Deep Space"): the
customization ends up under "Deep Space (custom)", the built-in is
identity-equal to the code-defined one again, and a second migration
run is a no-op.

Then, with a screenshot of the canvas showing "Neon Horizon": "the
graphs keeps getting highlighted on their own (not selected) like
network graph in the image". The graph sat under a gradient-filled box
with "NETWORK" across the middle, on top of an otherwise-correct live
render, with nothing selected.

That box was the editor's mockup overlay. Every element type hides its
mockup once `hasAccurateBackdrop` is true -- graphs were the one
exception, unconditionally drawing theirs (the previous round narrowed
that exception to `show_frame !== false`, which helped the new card
themes but left every ordinary graph exactly as it was). The exception
dated from the reasoning that the SVG layer can't plot the line, so
the box was the only thing making the region findable and draggable.
Both halves were false: the backdrop it defers to is a full render of
the panel -- the graph's frame, title AND line are all in it -- and
the mockup isn't a hairline outline but a filled box (0.55 alpha when
the element has a gradient) with the element's name centered in it, so
"defer to the real thing" turned into "paint a colored slab over the
real thing". Removed the exception; graphs follow the same rule as
everything else now.

Dragging never depended on the mockup: the branch that renders it
falls through to a transparent hit rect of the same geometry when it's
hidden, which is what carries the pointer handler. Verified all four
states in the harness rather than just the reported one -- deselected
over the live frame (hidden), selected (shown), deselected again
(hidden), and with the mock panel reporting disconnected so there's no
accurate backdrop at all (shown, which is the case the original
exception was really protecting) -- plus a real pointer drag on the
hidden graph, grabbed from its own hit rect mapped out of SVG user
units, which moves it and re-arms Save.

Then: "add a button for creating a new custom theme, we only have
duplicate now". Correct, and a real gap rather than a missing
shortcut: every path to a new preset started from an existing one.
Duplicate clones a card; "Save current layout as preset" captures
whatever is on the canvas, which after the previous round's changes is
usually a built-in someone just loaded. So "build my own" actually
meant "load someone else's and delete its elements one at a time" --
and now that built-ins are read-only, the deleting-down-to-nothing
version of that is the *only* version.

`createNewTheme()` saves an empty preset under an auto-numbered "New
theme" name, then puts the canvas into editing it -- the same two
moves loadPreset() makes (commit the elements, keep the background
draft), minus the lookup, since an empty element list and the
already-staged background are known without one. It also prefills the
"Save current layout as preset" box with the new name, so the obvious
next save lands back in that card instead of creating a sibling;
that's only safe because saving over one of your *own* presets
overwrites in place (the copy-instead behavior is built-ins only).

Empty rather than seeded with DEFAULT_ELEMENTS on purpose: "Reset to
defaults" in the toolbar already puts that layout on the canvas, so
seeding it would make this a second Duplicate with fewer options. The
background draft does carry over, because a truly blank start is a
black rectangle, which isn't a useful canvas. Checked that an empty
preset survives the parts of the app that assume elements exist --
render_preset_thumbnail([]) and render_live_preview([]) both render
the background cleanly, and _dashboard_preset_thumbnails() returns a
real data URI for it, so the new card shows its background rather than
the "No preview" fallback.

Verified in the harness: the button adds exactly one card and leaves
the other 16 alone, the canvas comes up with an empty element list,
the name box is prefilled, adding a gauge and saving under that name
updates the same card instead of creating a second, and a second click
names the next one "New theme 2".

**Theme import/export**, which arrived as a concrete errand: "there is
theme created by my friend can u import it?", with a .json and a PNG.
The file turned out to be a valid preset for *this* app -- built
entirely on the features added over the previous rounds (`font`,
`plate`/`plate_opacity`/`plate_pad`/`plate_radius`, `show_title`/
`show_value`, `track_color`, `border`, `dim`), 22 elements, every stat
and font it names real -- with one snag: its background was `"mode":
"nocturne"`, a bundled mode this app doesn't have, because the PNG was
what that mode stood for. Imported it by hand (image copied into the
managed store under image_store.py's own sha1-prefixed naming, preset
injected into app_config.json with `mode: "image"` pointing at the
copy, config backed up first), then built the feature so the next one
doesn't need me.

Export/import as a pair, because sharing only works if both ends
exist and the friend's file had to be produced by hand for lack of
one. The layout was never the hard part -- a preset is already plain
JSON. The images are: every picture a preset references lives at an
absolute path into the *local* managed image folder, which is
meaningless on another machine, and that single fact is what made
"send someone your theme" a manual operation. So export inlines each
referenced image as base64 (`image_b64` + `image_name`, path nulled)
and import materializes them back into the receiving machine's store
via `image_store.store_image_bytes()`, repointing the preset at the
new copies.

Import treats the file as untrusted, which matters more than it might
look: a preset file is something a person got from someone else, and
an `image_path` in one is an instruction to render an arbitrary file
from the receiving disk on a screen. So a path that didn't travel with
the file only survives if `image_store.is_managed()` already claims it
(the re-import-your-own-export case); everything else becomes None,
which every renderer here already handles as "no image". Shape is
checked too (`elements` a list, `background` an object or absent),
while unknown element types and background modes are deliberately left
alone -- the renderers skip what they don't recognize, which is what
lets a file from a newer or differently-configured copy still load.
Names route through save_dashboard_preset(), so an import called
"Fusion Core" copies rather than overwrites, and the filename is
tidied into a title ("nocturne_cathedral.json" -> "Nocturne
Cathedral") unless it already carries capitals, in which case it's
left as sent.

One bug found by testing the round trip rather than a single
direction: export sent `os.path.basename(path)` as the image name,
which still had the 8-char content-hash prefix image_store had added
on storage, so each cycle stacked another (`ab12_ab12_shot.png`) and,
because the name differed, stored a fresh identical copy every time.
Export now strips that prefix; verified stable over three round trips
with one file on disk.

Verified in the harness: Export appears in the card menu and downloads
a .json with images inlined; re-importing it restores the preset; the
friend's real file imports cleanly; malformed JSON and wrong-shaped
JSON each surface their own error instead of failing quietly.

**CPU clock, and a new Volume stat.** "CPU Clock is always showing as
3.4G which isn't accurate, can you verify that, add a new stats to
everything to show pc volume."

The clock was never a display bug. `get_cpu_freq_ghz()` returned
`psutil.cpu_freq().current`, and on Windows psutil gets that from
`CallNtPowerInformation(ProcessorInformation)`, whose `CurrentMhz` on
current hardware is simply the nominal clock -- a constant. So the
stat had been showing the base frequency the whole time, on a machine
whose own vendor app was reading 5.5GHz simultaneously (visible in the
screenshots from the theme rounds). Replaced with the
`\Processor Information(_Total)\% Processor Performance` counter times
the base clock: that counter is a percentage *of base*, exceeds 100
under boost, and is the same quantity Task Manager's "Speed" field
shows. Read through PDH by ctypes rather than adding pywin32/WMI --
`PdhOpenQueryW` + `PdhAddEnglishCounterW` (the English variant so a
localized Windows doesn't break it) + two collections, with the query
kept open and the priming sample returning None. psutil stays as the
fallback, which keeps Linux correct (its `current` genuinely is live)
and covers any Windows SKU missing the counter. Sampled at 1Hz, not
per frame.

Worth being explicit about the limit here: this sandbox is Linux and
the device bridge is a Linux VM with no access to Windows hardware, so
*I can't verify the reading on the actual machine* -- only that the
fallback path behaves and the code runs. That's what
`scripts/check_sensors.py` is for: it prints psutil's number, the perf
counter, and the resulting GHz five times a second apart so the fix
can be checked against Task Manager directly, rather than declared
fixed from here.

The Volume stat is the first entry in STAT_DEFS that isn't about load
or heat -- and the only one the person changes on purpose rather than
watches. `get_volume_percent()` reads the default playback endpoint's
scalar level through pycaw (the slider value, not the master dB level,
which is a different curve and not what anyone means by "my volume is
at 40%"), reporting 0 while muted. The endpoint is resolved once and
cached, since that path goes through COM device enumeration; a device
disappearing under it drops the cache so the next read re-resolves
whatever the default is now, and the render thread gets a
`CoInitialize()` since it's not the thread COM was set up on. pycaw is
optional, like nvidia-ml-py and winsdk before it -- missing means that
one stat reads "--". Registered in STAT_DEFS, which is all it takes:
every element type reads stats through the same registry, and the
frontend's stat picker is built from `dashboard_meta()["stats"]`, so
gauge/bar/graph/text and the picker all got it with no further wiring.
Verified by rendering all four element types bound to it.

### Phase 7 — Packaging and cutover

**Cutover done early (source-run only), at the user's explicit
request** — the desktop shortcut and "Launch at Windows startup" entry
now point at `scripts/run_v2_app.py` (backend_app.py: the control API
+ tray + webview UI showing the React frontend), not `app.py`.
`desktop_shortcut.py`/`startup_registration.py` resolve the target via
`paths.py`'s `_app_base_dir()`, not `sys.modules["__main__"].__file__`
-- trusting `__main__` is what let the Startup entry/shortcut get
silently pointed at whichever script happened to be running the
control server the one time either button was clicked, which is how
the desktop icon ended up doing nothing at all (see CHANGELOG.md).
`backend_app.py` gained its own version of app.py's "show yourself on
a second launch" mechanism (`BackendApp.start_show_watcher()`, a
daemon thread polling `SHOW_TRIGGER_PATH` since this process has no Tk
event loop to piggyback a poll onto) -- without it, double-clicking
the icon while already running would still do nothing, just like the
bug this was meant to fix. `pywebview` is now a required (not
commented-out) line in requirements.txt. `app.py`'s Tkinter GUI is
untouched and still works for manual/headless use (`python app.py`),
it's simply not what gets launched automatically any more.

Still outstanding, now that source-run cutover is done:

- PyInstaller spec bundles the built frontend as data files
  (`_resource_path()` already handles `sys._MEIPASS`) **and packages
  `scripts/run_v2_app.py` + the webview UI, not `app.py`** -- the
  frozen-build branches in `desktop_shortcut.py`/
  `startup_registration.py` still point at the old Tkinter .exe until
  this is done.
- WebView2 presence check with a clear message + download link if
  missing.
- BUILD.md gains the frontend build step.
- Tag v2.0.0.

## Risks and open questions

**WebView2 availability.** Ships with Windows 11 and is normally
present on Windows 10 via Edge, but not guaranteed. Needs a detection
path and a friendly failure, not a crash.

**Two-language project.** Node/npm for the frontend on top of Python
raises the barrier for anyone cloning the repo. Options: commit the
built frontend bundle so `pip install -r requirements.txt && python
app.py` still just works, or require a Node build step. Leaning toward
committing the bundle — the project's whole appeal is that it runs
without ceremony.

**Reopen latency.** Measured in Phase 0 at ~1-2.5s (spawning a process
+ WebView2 init) — an accepted trade-off for keeping background RAM
under budget, not a regression to chase further unless it turns out to
feel worse in the real app than the spike suggested.

**Scope.** This is a large project. Phases 0-3 alone are a substantial
chunk of work, and they only reach parity with what exists today — the
features that motivated the rewrite don't land until Phase 5. Phases
0-1 are cheap and independently useful, so they're a good place to
start without committing to the whole thing.

## Notes on the development loop

The build and test steps for this run on a real Windows machine: the
PyInstaller build, the `npm` frontend build, and anything that touches
the panel over its COM port. Rendering logic and backend code can be
developed and tested headlessly, but the webview shell, tray behavior
and RAM measurements can only be verified on the target machine.
