# Changelog

All notable changes to this project are documented here.

## [Unreleased] — v2.0 rewrite in progress

Internal reorganization, no user-visible feature change. See
`ROADMAP.md` for the full v2.0 plan (webview/React UI, free-form
dashboard designer) this is laying groundwork for.

### Changed
- **Restructured into a proper `src/` layout.** What used to be ~20
  loose `.py` files at the repo root is now: `app.py` (a thin launcher,
  kept at the root since it's what existing Windows Startup entries and
  Desktop shortcuts already point at) plus `src/hongtai_screen_app/`
  (the actual package: the GUI, the app-shell modules, `driver/` for
  the protocol code, `themes/` for the four theme renderers),
  `scripts/` (standalone diagnostics and thin CLI wrappers for the
  themes), `assets/` (`icon.ico`), and `packaging/`
  (`hongtai_screen.spec`). No install step needed — `python app.py`
  still just works, and so does everything under `scripts/`. See
  `README.md`'s "Project layout" section for the map.
- **Phase 1 of the v2.0 rewrite**: pulled everything that isn't
  Tkinter out of the old monolithic `app.py` into its own modules
  (`paths.py`, `config_store.py`, `startup_registration.py`,
  `desktop_shortcut.py`, `single_instance.py`, `theme_worker.py`,
  `tray_icon.py`) so the same logic can eventually be driven by a
  non-Tkinter UI. `app.py` is now a thin Tkinter layer over these.
- Fixed a latent bug this reorganization surfaced: the Windows startup
  launcher and desktop shortcut used to resolve "where's app.py" via
  their own module's `__file__`, which only worked by accident while
  that code lived directly inside `app.py`. Both now resolve the
  actual running script via `sys.modules["__main__"]` instead, which
  is correct regardless of which file the code lives in.
- **Phase 2a of the v2.0 rewrite**: a new, headless HTTP control API
  (`src/hongtai_screen_app/controller.py` + `control_server.py`,
  `scripts/run_backend.py` to run it standalone) that drives the same
  start/stop/apply/config logic as the Tkinter app, bound to
  `127.0.0.1` only. Fully additive — the Tkinter GUI is untouched.
- **Phase 2b of the v2.0 rewrite**: a minimal Vite + React frontend
  (`frontend/`) — connection status, live preview, theme/port/
  brightness/start/stop/apply controls, a live log panel — served by
  the backend itself on the same origin as the control API. The built
  bundle (`frontend/dist/`) is committed so no Node toolchain is
  required to run the app, only to change the frontend's source.
- Fixed a bug Phase 2b's real-hardware pass surfaced: `controller.py`'s
  `_selected_port()` passed the saved port *label* (a whole
  descriptive string) straight to the driver as an openable device
  path instead of translating it back to the real `COM3`-style path
  the way `app.py` already does — starting a theme with a specific
  port saved (not auto-detect) failed with a confusing
  `FileNotFoundError` even with the panel working fine. Fixed to do
  the same rescan-and-match `app.py` uses.
- **Phase 2c of the v2.0 rewrite**: `backend_app.py`
  (`scripts/run_v2_app.py`) -- a new, parallel entry point (not yet
  what `app.py`/Desktop shortcuts point at) that runs the control API,
  a tray icon, and resumes the last-running theme on launch, with its
  tray's "Show" spawning `scripts/run_ui.py` (a small pywebview
  window) as a genuinely separate process, and "Quit" terminating it.
  Confirms the two-process design Phase 0 measured actually works end
  to end.
- Fixed a bug Phase 2b's real-hardware pass surfaced: running the
  backend via `scripts/run_backend.py` wrote its own separate
  `scripts/app_config.json` instead of sharing the real one next to
  `app.py`, because `_app_base_dir()` resolved via
  `sys.modules["__main__"].__file__` — correct when `app.py` was the
  only entry point, wrong once `scripts/run_backend.py` became a
  second one. Now anchored on `paths.py`'s own file location instead,
  so every entry point agrees on the same directory.

- Worked around a Windows-only cosmetic gap in Phase 2c: pywebview's
  `icon=` only works on GTK/Qt (Linux); on Windows it's meant to come
  from freezing the app into a `.exe` with a baked-in icon resource
  (Phase 7's job). Until then, `scripts/run_ui.py` pushes `icon.ico`
  onto the window's `HWND` directly via `WM_SETICON` once it's shown,
  using the same `FindWindowW`-by-title approach `single_instance.py`
  already uses. Best-effort and unverified on a real machine yet.

- **Phase 3 of the v2.0 rewrite**: Video, Webpage and Clock settings
  forms added to the frontend, matching `app.py`'s own fields/hint text
  per theme. No backend changes needed — `update_config()` and
  `theme_kwargs.py` (both Phase 2a) already handled these settings
  generically. The video path is a plain text field rather than a file
  picker, since a browser file input can't hand back a real filesystem
  path for the backend to open.
- **Phase 3.5 of the v2.0 rewrite — live theme switching, no
  reconnect.** Not on the original plan: real-hardware testing of
  Phase 2c/3 surfaced that switching themes fully disconnected and
  reconnected the panel every time, because each theme module owned
  its own connect/loop/disconnect lifecycle instead of just producing
  frames. Fixed at the source: `demo_clock.py`/`video_theme.py`/
  `webpage_theme.py`/`dashboard_theme.py`'s `run()` now accept an
  optional pre-connected `screen=`, and skip connecting/disconnecting
  entirely when given one (plain CLI/GUI use is unaffected). New
  `screen_engine.py` (`ScreenEngine`) holds one persistent connection
  across switches and replaces `ThemeWorker` inside `controller.py`
  only — `app.py`'s Tkinter UI still uses `ThemeWorker`, untouched.
  `controller.py`'s `start()` no longer errors on "already running";
  switching to a different theme while one is active now just works,
  and `apply()` re-applies the running theme's settings without
  disconnecting either. The frontend's theme picker and Start button
  are no longer locked while something's running. Verified headlessly
  with mocked hardware (connection reuse across switches, natural
  finish still disconnects, error recovery still retries+gives up
  correctly, an intentional stop mid-error isn't mistaken for a fault)
  — not yet verified with a real panel.
- **Phase 4 of the v2.0 rewrite — layout model: slots → elements
  (backend).** `dashboard_theme.py`'s 8 gauges no longer come from a
  formula keyed by fixed slot names (`top_left`, `left_secondary`, ...);
  `build_static_background()`/`render_frame()` now walk an arbitrary
  list of gauge elements, each with its own `x`/`y`/`radius` (resolution-
  independent fractions), `color`, `opacity`, `z`-order, and a `stat`
  binding — the data model a future drag/resize design canvas (Phase 5)
  needs to exist at all. The old "big"/"secondary"/"mini" `SLOT_KINDS`
  enum is gone; whether a gauge gets full tick labels or the compact
  look is now derived from its actual baked radius instead. Migration
  is at read time, not a stored schema bump: `slots_to_elements()`
  converts old `slots` picks into equivalent elements using the exact
  same geometry formula that used to be inline, so an existing
  `app_config.json` renders unchanged. `app.py`'s Tkinter Dashboard tab
  is completely untouched — it still calls `dashboard_theme.run(slots=
  ...)` exactly as before; only `theme_kwargs.py` (controller.py's path)
  was switched to build and pass `elements=` instead. Verified
  headlessly: a full rendered frame pixel-diffed against a
  reconstruction of the exact pre-Phase-4 code for the default layout
  came back 99.94% identical (the remainder a documented, expected
  sub-pixel label-position shift from replacing two hardcoded label-gap
  constants with one continuous formula); custom slot overrides, a
  hand-built custom elements list, and an end-to-end run against a fake
  screen all verified working. Not yet verified on real hardware.
- **Phase 5 of the v2.0 rewrite — the dashboard design canvas.** The
  web UI's Dashboard tab is now a real editor: drag a gauge to move it,
  drag its corner handle to resize, click to select and edit its stat/
  color/opacity/position in a property panel, add/delete gauges,
  reorder them (bring to front/send to back), snap to the canvas
  center or another gauge's position while dragging, undo/redo
  (buttons and Ctrl+Z/Ctrl+Y), and save the result — or save it as one
  of several named presets to switch between later. It edits the exact
  same element list `dashboard_theme.py` renders (Phase 4), overlaid on
  the panel's live `/frame.jpg` mirror when connected. New backend
  endpoints (`/api/dashboard/meta`, `/api/dashboard/elements`,
  `/api/dashboard/presets[/delete]`) merge into the saved `dashboard`
  config instead of replacing it wholesale, so saving a layout tweak
  can't accidentally wipe out `web_port`/`enable_web`/`background`/
  `slots`. Rotation is stored per-element but has no handle in this
  canvas yet — `dashboard_theme.py` doesn't render it either, so a
  control for it would visibly do nothing until that lands. Verified
  with unit-tested geometry/color helpers, a clean production build,
  and a full Playwright session against the real built frontend and a
  live backend — select, drag, add, delete, undo, and preset-save all
  confirmed working, and the resulting saved layout was fed back
  through the actual cairo rendering pipeline and rendered correctly.
  Not yet verified on real hardware.
- **Fixed a Phase 5 gap**: the design canvas covered gauge layout but
  dropped the background picker (style preset, color scheme, custom
  image path) that `app.py`'s Tkinter Dashboard tab already had --
  the web UI had no way to set a custom background image at all. Added
  a Background section under the canvas with the same options, plus a
  new merge-safe `save_dashboard_background()`/`/api/dashboard/
  background` endpoint (same "merge into the dashboard dict, don't
  replace it" shape as the elements/preset endpoints) and
  `dashboard_meta()` now also returns the resolved background and the
  preset/scheme label metadata so the frontend never needs to hardcode
  `dashboard_theme.py`'s constants. The image path is a plain text
  field, same reasoning as the video theme's path field in Phase 3 --
  a browser file input can't hand back a real filesystem path. Verified
  headlessly (endpoint round-trip, `theme_kwargs.dashboard_kwargs()`
  picks up the saved value) and via a Playwright session against the
  real built frontend and a live backend: switching to Custom image,
  typing a path, and saving all worked with no console errors, and the
  save persisted correctly server-side.
- **Fixed a real-usage bug in the v2 backend's auto-resume**: quitting
  `backend_app.py` (the v2 tray app) while a theme was running never
  came back up on the next launch, even though `app.py`'s Tkinter app
  has always preserved that. Root cause: `ScreenEngine.close()`
  (called on process shutdown, to release the serial port) and
  `ScreenEngine.stop()` (an explicit Stop) both funneled through the
  same `_teardown()` -> `on_disconnected()` callback, which
  `AppController` treats as "nothing left to auto-resume" and clears
  `auto_resume_tab` accordingly -- correct for an explicit Stop, wrong
  for a plain quit while something was still running. `_teardown()`
  now skips firing `on_disconnected()` when the teardown is happening
  because `close()` is shutting the whole engine down (it still
  physically closes the port either way), so the last theme started
  is what the next launch resumes, exactly as `app.py` already does.
  Verified headlessly with a fake screen/target: quitting mid-run
  preserves and persists `auto_resume_tab`; an explicit `stop()` still
  clears it; switching themes still reuses the connection without a
  false disconnect in between.
- **Phase 6 of the v2.0 rewrite — richer elements and options.** Three
  new element types alongside the existing gauge -- text labels
  (a free string, not bound to a stat), history graphs (line/bar chart
  of a bound stat's recent values, plotted from a rolling deque `run()`
  maintains), and custom images (an arbitrary photo/logo dropped in as
  its own positioned element, same tolerant fallback as the background
  image) -- plus an optional second color (`color2`) on a gauge for a
  gradient ring instead of the old single-color one. All additive to
  Phase 4's element model, so an existing config with only gauges keeps
  rendering unchanged. The design canvas gained matching "+ Add
  text"/"+ Add graph"/"+ Add image" buttons, type-specific canvas
  representations and resize behavior, and a property panel that swaps
  in the right fields per element type. Verified headlessly (mixed-
  layout render, bad-image-path fallback, a full `run()` loop
  accumulating graph history without error, and a `DEFAULT_ELEMENTS`
  regression check) and via a Playwright session against the real
  built frontend + live backend: adding one of each new type, editing
  the text label, and saving all worked, and the saved config rendered
  correctly through the real cairo pipeline afterward. Not yet
  verified on real hardware.
- **Fixed a feature-parity gap: the "nothing playing" placeholder
  image was never exposed in the web UI, and its message was never
  configurable in either app.** `app.py`'s Tkinter Dashboard tab has
  always let you pick a custom placeholder image for when Spotify
  isn't playing, applied live via `dashboard_theme.set_default_art_
  path()` -- but that setting was never carried over to the web UI at
  all. Separately, the text shown in place of the track title
  ("Life is like a door never trust a cow because the sun can't
  swim") has always been a single hardcoded line, not a real setting,
  in either app. Both are now real, live-applying settings:
  `dashboard_theme.py` gained `set_not_playing_message()`/
  `get_not_playing_message()` (same pattern as the existing art-path
  pair -- re-read every frame, so editing it takes effect on the very
  next frame, no Stop/Start), a new merge-safe `controller.
  save_dashboard_now_playing()` / `POST /api/dashboard/now_playing`
  applies both settings to the running dashboard immediately (not just
  on the next Start), and the web UI's canvas gained a "Nothing
  playing" placeholder section (image path + message, both plain text
  fields) alongside Background. `app.py`'s Tkinter tab gained a
  matching message field next to its existing image-path one. Verified
  headlessly (the message falls back to the default when cleared,
  applies live in a rendered frame, and round-trips through the
  merge-safe endpoint without disturbing other dashboard config) and
  via a Playwright session against the real built frontend + live
  backend confirming the new section saves and persists correctly.
- **Every image setting now stores a managed copy instead of a raw
  path, and is picked, not typed.** Dashboard background, the
  "nothing playing" placeholder, and a Phase 6 image element all used
  to store whatever path a user typed or browsed to, verbatim -- fine
  until that file got moved, renamed, or deleted, at which point the
  feature silently fell back to a placeholder with no obvious
  explanation why. New `image_store.py` copies a picked image into
  this app's own per-user folder (`%LOCALAPPDATA%\HongtaiScreen\
  images\`, created on first use), deduplicated by content hash so
  re-picking the same file reuses the existing copy, validated via
  PIL so a non-image or oversized (>25MB) file is rejected up front
  with a clear error instead of failing silently later at render
  time. It's that stored copy's path that ends up in
  `app_config.json` -- the original file can move or disappear
  afterward with zero effect. The web UI's three plain-text "type a
  path" fields are now real `<input type="file">` pickers: picking a
  file uploads its bytes to a new `POST /api/dashboard/upload_image`
  (`controller.upload_dashboard_image()`), which stores it via
  `image_store` and hands back the managed path for the existing
  merge-safe save endpoints to persist, same as before. Each field
  shows the stored file's name as a caption, and the placeholder-image
  field gained a Clear button. `app.py`'s Tkinter Browse dialogs
  (`_pick_dash_art`/`_pick_dash_bg_image`) now route the real path
  `askopenfilename()` returns through `image_store.store_image_file()`
  and keep the returned managed-copy path instead of the raw browsed
  one, so Tkinter gets the same fix. Verified headlessly (folder
  auto-creation, content-hash dedup, stored copy survives deleting the
  original, rejects a non-image and an oversized file with a clear
  `ValueError`, and the full upload -> save round-trip for background/
  now-playing/element) and via a Playwright session against the real
  built frontend + live backend picking a real file for all three
  locations, confirming the upload, the managed path being what's
  saved, the Clear button, and no console errors.
- **Fixed data loss: saving anything from Tkinter's Dashboard tab could
  silently wipe out a layout/presets saved from the web canvas.**
  `app.py`'s `_save_current_config()` rebuilt the entire "dashboard"
  config dict from scratch out of its own controls (`enable_web`,
  `web_port`, `default_art_path`, `not_playing_message`, `slots`,
  `background`) every time it saved -- which happens on far more than
  just an explicit Save (closing the window to tray, Stop, switching
  themes). Since `elements` and `presets` only ever exist as web-canvas
  concepts with no Tkinter controls at all, they got dropped from the
  dict every single time, silently discarding any custom layout or
  saved preset the moment the Tkinter app touched its config next.
  Fixed to merge its own fields into the existing "dashboard" dict
  instead of replacing it, the same merge-safe pattern the web UI's
  save endpoints already use. Verified with a logic-level test
  confirming `elements`/`presets` survive a save while the
  Tkinter-controlled fields still update correctly.
- **The dashboard's middle column is no longer just a Spotify display
  -- it's now a choice: Spotify, Weather, or nothing at all.** Not
  everyone wants a now-playing display glued to their PC's case (or
  runs Spotify at all); this adds a "Middle content" setting
  (`dashboard.middle_content`, default `"spotify"` -- fully backward
  compatible with every config saved before this) with two new
  options: `"weather"` shows a current-conditions readout (a hand-drawn
  glowing icon, temperature, one-line description, feels-like/humidity,
  and the resolved place name) via new `weather.py`, using Open-Meteo
  -- free, no signup, no API key, just a typed city/address -- for both
  geocoding and the actual lookup, polled every 10 minutes from a
  background thread (same reasoning as the existing Spotify poll
  thread: a real network round trip has no business happening inline
  in the 10Hz render loop); `"none"` draws nothing there at all, just
  the background showing through. Both the web UI's canvas and
  `app.py`'s Tkinter tab gained a matching "Middle content" section
  (a Show picker, plus a location/units pair that only appears for
  Weather), and the "Nothing playing" placeholder section now only
  shows when Spotify is actually selected, since it's meaningless
  otherwise. A new merge-safe `controller.save_dashboard_middle_
  content()` / `POST /api/dashboard/middle_content` applies the
  selection to the running dashboard immediately (no Stop/Start),
  mirroring the now-playing settings' live-apply pattern. Verified
  headlessly (geocoding/current-conditions against a mocked HTTP layer,
  the poll thread picking up a location/units change without waiting
  out its own interval, a rendered frame for all three content modes
  including the no-location and lookup-failure fallbacks, and the
  merge-safe endpoint round-tripping and rejecting an unknown
  `middle_content` value) and via a Playwright session against the
  real built frontend + live backend confirming the section saves, the
  Weather-only fields show/hide correctly, and the placeholder section
  hides itself outside Spotify mode, with no console errors.
- **New setting: "Keep the panel updating while Windows is locked"
  (on by default), mirroring the official XTRM Lab app's own "Keep
  playing when screen is off" toggle.** That vendor app's Electron
  `powerMonitor` stops rendering on a Windows lock-screen/suspend event
  unless that setting is on; this app had no equivalent at all before
  now -- it always kept going, silently polling stats and pushing
  frames to a panel nobody's near while the machine sits locked. New
  `power_state.py` detects "is Windows locked right now" with no extra
  dependency (`ctypes`' `OpenInputDesktop()` -- the standard userspace
  trick, no elevation needed), polled every 2 seconds from a background
  thread. True system suspend isn't something userspace can "keep
  active" through -- Windows freezes the whole process during real
  sleep, so there's nothing to pause or resume there; this is
  specifically about a screen *lock* (Win+L, an idle timeout, "Lock"
  from the Start menu), which Windows runs every process straight
  through, unaffected. All four themes' render loops (`dashboard_
  theme.py`, `video_theme.py`, `webpage_theme.py`, `demo_clock.py`)
  now check `power_state.should_pause()` before doing their per-frame
  work (not just before pushing to the panel, so a locked machine
  doesn't keep burning CPU/GPU on stats, video decode, or webpage
  screenshots nobody's watching either) and skip that frame entirely
  when it's on and the setting is off, resuming automatically the
  moment Windows unlocks. New `controller.set_keep_active_when_
  locked()` / `POST /api/keep_active_when_locked` (`system_info()`
  gained `keep_active_when_locked`/`keep_active_supported` keys) and a
  matching `app.py` Tkinter checkbox, both applying immediately, no
  restart needed. The web UI's System panel gained the checkbox too
  (disabled with "(Windows only)" off Windows, same pattern the
  existing startup checkbox already uses). Verified headlessly
  (`OpenInputDesktop`-based lock detection stubbed for both states, the
  setting ignoring lock state entirely when on, and an end-to-end test
  starting `dashboard_theme.run()` against a fake screen confirming
  frames stop the instant the setting is flipped off while locked and
  resume immediately on unlock) and via a Playwright session against
  the real built frontend + live backend, with no console errors.
- **The now-playing widget can be dragged anywhere, not just the fixed
  middle column** -- a new `"media"` element type (`+ Add now-playing`
  in the canvas toolbar) draws the same album art + track/artist +
  progress bar `_draw_spotify_middle()` always has, but positioned and
  sized like any other canvas object (x/y/width/height/opacity), so it
  can sit off-center, get resized, or overlap other elements instead of
  being locked to dead center. It's independent of the "Middle content"
  setting above -- add one whether that's set to Spotify, Weather, or
  None. New `dashboard_theme._media_box()`/`_draw_media_element()`
  render it fresh every frame (like a graph's plotted line, since
  playback position advances continuously) rather than baking it into
  the static background the way text/image elements are. No backend
  validation needed -- `elements` already round-trips arbitrary element
  dicts through config, so a `"media"` entry needed nothing beyond the
  new renderer branch in `render_frame()`.
- **Two small canvas usability fixes.** The browser's unstyled default
  "Choose File" control (used for the background image, an image
  element, and the now-playing placeholder) is now themed to match the
  rest of the app (`input[type="file"]::file-selector-button`, with a
  separate `::-moz-file-selector-button` rule since combining them in
  one selector list would silently invalidate both in browsers that
  don't recognize one of the two). And every selected element's
  property panel gained "Center horizontally"/"Center vertically"
  buttons (set x or y to exactly 0.5) -- faster and more precise than
  dragging until the existing snap-to-center guide catches. Verified by
  rendering a `"media"` element standalone (with and without live
  media, opacity < 1) and via a Playwright session against the real
  built frontend + live backend: adding a now-playing element, styled
  file inputs in both the Image and "Nothing playing" sections, and
  the center buttons independently resetting X and Y on a text element.
- **Fixed the canvas showing a placeholder box instead of the picture
  you just picked, and made the resize/save workflow legible.** Picking
  an image for the background, an image element, or the "Nothing
  playing" placeholder used to only update a filename label -- the
  canvas kept drawing a generic "IMAGE" box, so it looked like the pick
  hadn't done anything. New `GET /api/dashboard/image?path=` (`control_
  server.py`'s `_handle_dashboard_image()` / `controller.read_dashboard_
  image()`, restricted to paths already inside `image_store.py`'s own
  managed folder) serves the actual bytes back, so: an image element
  now renders the real picture directly on the canvas (clipped to its
  box, respecting opacity), and all three file pickers show a small
  thumbnail next to the filename -- all the moment a file's picked, no
  Save/Start/Apply needed, since this canvas was already a live preview
  and just wasn't using real image data. Also added an explicit "Save
  layout*" / "Unsaved changes" indicator (amber-highlighted button once
  `elements` differs from what was last loaded/saved) so it's no longer
  a guess whether a still-live edit has actually been persisted, plus a
  rewritten top hint spelling out the three-stage model (canvas preview
  is instant; Save layout persists it; Start/Apply pushes it to the
  physical panel). And graph/image/media elements gained two more
  resize handles (right edge = width only, bottom edge = height only)
  alongside the existing corner handle (both at once), since a single
  diagonal handle couldn't change just one dimension without a
  pixel-perfect straight drag. Verified via a Playwright session against
  the real built frontend + live backend: uploading an image and seeing
  it render inline plus its thumbnail within the same interaction, an
  actual mouse-drag on the width-only handle confirming height stays
  untouched, and the dirty indicator appearing on edit and clearing on
  save.
- **The web UI's panel port is a "Detect screens" dropdown now, not a
  free-typed text field -- and it's the first thing on the page,
  since nothing else here does anything useful without it.** The old
  "Port (blank = auto-detect)" text field also had a latent bug: its
  hint text said "Port needs Save", but there was never actually a
  Save button wired up for it, so a typed port could only ever take
  effect via the theme-specific settings forms' own Save buttons
  incidentally re-saving the whole config -- easy to miss entirely.
  New `GET /api/ports` (`controller.list_ports()`) exposes the same
  USB-VID serial scan (`driver/hongtai_screen.py`'s
  `find_hongtai_ports()`) the Tkinter app's own "Refresh" button next
  to its port Combobox has always used, now over HTTP. The web UI's
  new "Panel port" section -- moved above Preview, first on the page
  -- runs that scan automatically on load and again on "Detect
  screens", picks the obvious choice automatically when exactly one
  screen is found and nothing's selected yet, and saves a selection
  the instant it's picked (no separate Save step). Controls,
  the per-theme settings sections, and the dashboard canvas are all
  now disabled with a "Select a panel port above" hint until a port
  (a specific one, or the explicit "Auto-detect" option) is actually
  chosen -- previously an unset port silently worked via auto-detect,
  which was convenient with exactly one screen plugged in but gave no
  indication anything needed picking at all otherwise. Verified via a
  Playwright session against the real built frontend + live backend:
  the disabled state and its message before any port is selected,
  picking "Auto-detect" immediately persisting to config and
  unlocking Controls, and the selection surviving a page reload.
- **Fixed an image element not showing the whole picture, and made
  resizing it on one axis alone actually behave like a resize.** Every
  image element (and the background/now-playing images too, though
  those aren't user-resizable boxes) used to be cover-fit -- cropped to
  exactly fill its box on both axes -- which meant (a) a freshly-added
  element's small fixed default box cropped almost any real picture
  down to a sliver of itself before anyone touched it, and (b) dragging
  just the width handle didn't "scale" the image at all from the user's
  perspective -- it only slid the crop window over a picture that
  already filled the box, which reads as the image being overwritten
  rather than resized. New `dashboard_theme._fit_into_box()` supports
  three modes via a new `fit` field on image elements -- `"contain"`
  (new default: the whole image always visible, letterboxed, never
  cropped), `"cover"` (the old crop-to-fill behavior, still available),
  and `"stretch"` (fills the box exactly, ignoring aspect ratio) -- with
  a matching selector in the element's property panel. And a freshly
  picked image now sizes its own box to match: `DashboardCanvas.jsx`
  reads the picked file's actual pixel dimensions client-side
  (`readImageDimensions()`, no upload round-trip needed for just this)
  and sets width/height to fit that aspect ratio at a sensible on-canvas
  size, instead of leaving a brand-new element's small default square in
  place regardless of what was picked.
- **Now-playing elements can show just the pieces you want.** New
  `show_art`/`show_name`/`show_time` fields (each on by default, a
  checkbox per piece in the element's property panel) let a media
  element show any combination of cover art, track/artist text, and the
  progress bar/timestamps -- e.g. cover art alone with no text, or a
  compact time-only readout with no art. Whichever pieces are on stack
  top-to-bottom starting from the top of the box, so turning one off
  doesn't leave a gap.
- **The clock is a movable/addable element now, not a hardcoded fixed
  drawing.** It used to be the one thing `render_frame()` always drew
  unconditionally at one fixed spot, with no element, no property panel,
  and no way to move, restyle, remove, or add a second one. New
  `"clock"` element type (`+ Add clock` in the canvas toolbar) with its
  own x/y/font size/color/opacity and a "Show seconds" toggle -- added,
  dragged, resized (drag its handle, or the Center buttons), and deleted
  like any other element. `DEFAULT_ELEMENTS` now includes one at the
  exact position the fixed clock always occupied, so "Reset to defaults"
  gives back the same look as an editable element. Backward compatible
  with every layout saved before this: `render_frame()` only falls back
  to drawing the old fixed-position clock when the saved `elements` list
  has no `"clock"`-type entry at all, so nothing shifts or disappears for
  an existing config until it's actually edited to add one (at which
  point the fallback stops -- no double clock).

  Verified headlessly: a wide (4:1) test image inside a near-square box
  renders fully visible with letterboxing (not cropped) under the new
  `"contain"` default; a media element with only `show_art` set draws
  art with no text/progress bar, and one with only `show_time` draws the
  progress bar/timestamps with no art or text (this also caught and
  fixed a real bug -- a non-integer coordinate feeding into the glow-draw
  helper whenever `show_art` was off, crashing that render path
  entirely); `DEFAULT_ELEMENTS` includes exactly one clock element and
  renders identically to the pre-existing fixed clock at the reference
  resolution; and an `elements` list with the clock entry stripped back
  out (simulating a pre-upgrade saved layout) still renders the fallback
  clock, pixel-identical to before. Also verified via a Playwright
  session against the real built frontend + live backend: adding a
  clock element and seeing its property panel, uploading a wide (4:1)
  test image to a fresh image element and confirming its width/height
  fields land on the matching aspect ratio automatically, and the
  media element's three show/hide checkboxes and the image element's
  Fit selector both rendering as expected.
- **Removed the two static/legacy dashboard rendering paths the clock
  and now-playing elements were only ever a backward-compatible
  fallback for, and replaced the fallback itself with a real one-time
  migration.** `render_frame()` no longer falls back to drawing a
  hardcoded clock at a fixed spot when a saved `elements` list has no
  `"clock"`-type entry -- that fallback existed only to bridge a saved
  layout from before the clock became an element, and it's now
  guaranteed unnecessary (see the migration below). Removed
  `_draw_spotify_middle()` entirely, along with the `"spotify"` option
  from `MIDDLE_CONTENT_OPTIONS` -- the fixed, always-on, un-movable
  Spotify display it drew in the middle column predates (and, for
  anyone who never touched the design canvas, silently duplicated) the
  now-playing widget's own life as a proper `"media"` element; that
  column now only ever shows Weather or nothing. `middle_content`'s
  default changed from `"spotify"` to `"none"` everywhere it's read
  (`dashboard_theme.py`, `theme_kwargs.py`, `controller.py`, `app.py`)
  to match.

  New `dashboard_theme.default_clock_element()` /
  `default_media_element()` factory functions build a fresh clock/
  now-playing element dict at the same fixed spots those two used to
  occupy -- `slots_to_elements()` now appends both directly (so
  `DEFAULT_ELEMENTS` and every layout derived fresh from `slots`,
  which is most configs, get them automatically, live, with nothing to
  migrate), and `theme_kwargs.resolve_dashboard_elements()`'s
  slots-fallback path now gets a clock element for the first time too
  (previously only `DEFAULT_ELEMENTS` did, via a separate
  concatenation -- an inconsistency this closes).

  The one case that couldn't just derive its way out of this: a
  config with a saved `dashboard.elements` list from before either
  element type existed. New `config_store.migrate_dashboard_elements()`
  handles that -- called once from both `AppController.__init__` (the
  web backend) and `App.__init__` (Tkinter) right after
  `load_config()`, so whichever UI opens a config first migrates it
  for both. It appends a clock element if the saved list has none, and
  a now-playing element (setting `middle_content` to `"none"`) if the
  list has no media element and `middle_content` was `"spotify"` or
  unset -- the signal that a config was actually relying on the
  now-removed static display, as opposed to someone who'd already
  chosen "weather" or "none" on purpose. Flagged done via
  `dashboard._migrated_elements_v1` so it runs exactly once -- deleting
  the clock or now-playing element from the canvas afterward sticks,
  it doesn't get silently re-added on the next launch.

  Verified headlessly: `DEFAULT_ELEMENTS`/`slots_to_elements()` include
  exactly one clock and one media element; `MIDDLE_CONTENT_OPTIONS` has
  no `"spotify"` key and `_draw_spotify_middle` no longer exists;
  `migrate_dashboard_elements()` against six scenarios (no dashboard
  config, slots-only config, an old 8-gauge-only saved list with
  `middle_content` unset, the same with `middle_content` explicitly
  `"weather"`, a list that already has both elements, and a
  re-migration after the flag is set) each landed on the right
  elements/`middle_content`/idempotency; and a full `render_frame()`
  pass over the new default layout (no media playing, media playing,
  and weather middle content) rendered correctly with no crash. Also
  verified via a Playwright session against the real built frontend +
  a live backend seeded with an old-style pre-migration config: the
  backend's own startup migrated and persisted it to disk correctly,
  `/api/dashboard/meta` reflected the migrated elements and
  `middle_content: "none"`, the design canvas showed the clock and
  now-playing widgets as separately selectable/movable elements (not
  the old static display), the Middle content dropdown offered only
  Weather/None, and the "Nothing playing" placeholder section (used by
  any now-playing element, not tied to Middle content) rendered
  unconditionally instead of being gated behind the removed `"spotify"`
  option.
- **Design canvas UX pass: an element list, arrow-key nudging, and
  collapsible sections.** The only way to select something used to be
  clicking it directly on the canvas -- fine for well-spread-out
  elements, but a real problem for small ones (the mini gauges) or
  ones another element sits on top of (the now-playing box overlapping
  several gauges): no way to tell what's there, no way to grab it
  precisely. New element list (`ELEMENT_BADGES`/`elementLabel()`,
  `.element-list` in `DashboardCanvas.jsx`) shows every element by type
  and id regardless of size or stacking; clicking a row selects it
  exactly like clicking it on the canvas would, and the selected row
  highlights to match. Also fixed the root cause of a duplicate-id bug
  this surfaced: `makeId()` used to count `existing.length` in
  whatever array the current browser tab happened to have loaded, which
  isn't unique if the backend's file changes underneath an open tab
  (e.g. a migration runs while the canvas is still open from before
  it) -- two different stale/fresh views can independently compute the
  same next id. It now always appends a short random suffix, so a
  duplicate id can't happen even from a stale tab.

  New arrow-key nudging for the selected element (plain arrow = 1% of
  the panel, Shift+arrow = 5%, matching the property panel's own "X
  %"/"Y %" units) -- mouse-only dragging was fiddly for pixel-level
  alignment, especially on the small gauges. Each nudge commits through
  the same `commit()` path a drag does, so Undo steps through
  individual nudges same as it already did for drags.

  New `Collapsible.jsx` wraps every top-level settings section (Panel
  port, Preview, System, Controls, Config, Log in `App.jsx`; Background,
  Middle content, "Nothing playing" placeholder inside
  `DashboardCanvas.jsx`) behind a click-to-toggle header, open/closed
  state persisted per-section in `localStorage` so it survives a
  reload. System, Log, and the three occasional dashboard sub-settings
  default collapsed; Panel port, Preview, Controls, Dashboard layout,
  and Config default open -- cutting the page down from one long
  scroll past everything to just the sections actually being used.
  Verified via Playwright: the element list renders one row per element
  (badge + label + id) and clicking a row both highlights it and
  selects the matching canvas element (dashed outline, resize handles);
  arrow keys nudge the selected element's X/Y by the expected amount
  (1%/5% with Shift) and Undo reverts a nudge; and the collapsed/open
  state of each section matches its configured default on first load.
- **Fixed the clock element visually duplicating itself on the design
  canvas.** Its SVG mockup always drew a hardcoded sample time string
  ("12:34:56") on top of the canvas, which is fine on its own -- but
  whenever the canvas is overlaid on the live panel frame (`connected
  && frameUrl`), that live frame *already* shows the panel's real,
  actually-ticking clock at the exact same spot, so the sample text
  landed right on top of it -- two different clocks fighting each
  other ("12:34:56" printed over "17:49:16"), read as one clock
  duplicating itself. The sample text now only renders when there's no
  live frame under it to collide with; an invisible hit-rect the same
  size keeps it clickable/draggable either way, and the selection
  outline/handle are unaffected. Verified via Playwright, faking the
  connected state and frame image via `page.route()` interception
  (the test backend has no real hardware, so `connected` is always
  false in normal headless testing).
- **Live layout/background apply -- fixes the "ghost" element and
  "Reset to defaults keeps my images" bugs.** Both turned out to be
  the same root cause: `dashboard.elements`/`background` were only
  baked into the running panel's static background image at
  Start/Apply time (a deliberate perf trade-off, see
  `build_static_background()`'s docstring) -- so dragging an element
  on the live design canvas moved the SVG mockup instantly (it's just
  React state) while the *real* panel kept showing it at the old spot
  until a manual Stop/Start, which reads as the element duplicating
  itself; the same staleness meant "Reset to defaults" reset the
  canvas's own state just fine, but the live panel kept showing
  whatever (any since-removed image included) was baked in at the last
  Start/Apply. Layout and background are now live-appliable the same
  way the "Nothing playing" placeholder and Middle content already
  were: `dashboard_theme.set_pending_dashboard_layout()` queues an
  edit, and the running render loop rebakes with it (a full rebake,
  not a diff -- cheap relative to a 100ms frame budget) at the start of
  its very next frame, no Stop/Start needed. `controller.py`'s
  `save_dashboard_elements()`/`save_dashboard_background()` call it
  right after persisting to config. Verified headlessly: queuing an
  elements-only, background-only, and combined update each landed the
  expected keys for the render loop to pick up on its next iteration;
  `save_dashboard_elements()`/`save_dashboard_background()` both queue
  correctly through `AppController`; and (via Playwright) adding an
  image element then clicking Reset to defaults removes it from the
  element list, same as it always should have.
- **Clock customization: analog styles, more digital formats, and a
  custom image face.** A clock element now picks a `face` --
  "digital" (the only option before this; every existing saved clock
  element is one, and nothing about its look changed), "analog", or
  "image" -- each with its own options, same idea as a gauge picking a
  stat.
  - Digital gained an `hour_format` (24h, or 12h with AM/PM) beyond the
    existing seconds on/off toggle, plus an optional `show_date` line
    underneath the time.
  - Analog is a procedurally-drawn round face (ticks, hour/minute/
    second hands, computed from the real system time every frame,
    never baked into the static background -- same as the digital face
    always was) in one of three styles (`analog_style`): Classic
    (white face, black hands), Minimal (thin ring, no ticks, just
    hands), Neon (dark face, hands/ticks glowing in the clock's own
    color). Sized by `radius`, same fraction-of-min(width,height)
    convention as a gauge's.
  - Image lets you pick literally any picture (reusing the exact same
    upload flow as an image element -- `image_store.py`) as a
    decorative clock face/skin, fit into its `width`/`height` box, with
    the digital time (and optionally the date) drawn on top of it,
    shadowed so it stays legible over any picture's own colors.
  Backend: `_draw_clock_element()` in `dashboard_theme.py` now
  dispatches to `_draw_analog_clock_face()`/`_draw_image_clock_face()`;
  a one-slot-per-path cache (`_clock_face_cache`) avoids re-decoding a
  custom face PNG from disk 10 times a second. Frontend:
  `DashboardCanvas.jsx`'s clock SVG mockup and property panel both grew
  matching per-face previews/controls (analog gets a static "ten past
  ten" preview circle + a radius resize handle; image gets an
  image-element-style picker + width/height handles); `dashboard_meta()`
  exposes `clockFaces`/`clockAnalogStyles`/`clockHourFormats` so the
  frontend doesn't need to hardcode the option lists. Fully backward
  compatible -- every new field has a `.get()` fallback that reproduces
  the exact old digital-only look, so an existing saved clock element
  needs no migration. Verified: `render_frame()` renders all three
  faces (digital in both hour formats with/without a date line, analog
  in all three styles with real ticking hands, image with a fake
  decorative PNG) to a real 960x480 image with no crash and the
  expected pixels-on-canvas; and via Playwright, switching faces in the
  property panel shows the right controls for each and the canvas
  mockup updates to match.
- **"Nothing playing" placeholder settings moved into the now-playing
  element's own property panel** -- it used to be a standalone,
  always-visible collapsible section regardless of whether a
  now-playing element even existed on the layout; now it only shows up
  in the property panel when a now-playing element is selected,
  matching how every other element-specific setting already works.
  Purely a frontend display change -- the settings are still
  dashboard-level config (shared across any now-playing elements, same
  as before), just conditionally shown based on canvas selection
  instead of always rendered as its own section.
- **Two-column page layout** -- `App.jsx`'s single centered
  `max-width: 720px` column wasted most of a wide window's width, most
  visibly on the dashboard design canvas (by far the widest thing on
  the page). New `.app-columns` CSS grid splits the page into a fixed
  280-380px left column (Panel port, Controls, Config, System, Log --
  app-level settings read once and left alone) and a flexible right
  column (Preview and whatever the current theme needs, including the
  design canvas, free to use however much width is left). Collapses
  back to one column (left column's settings first) below ~860px,
  where two side by side would just squeeze the canvas back down to
  the same cramped width this replaced.
- **Likely fix for a real memory leak reported after leaving the app
  running overnight (~2GB RSS by morning).** Prime suspect:
  `_get_media_info_async()` (the Spotify now-playing poll, dashboard
  theme only) called `MediaManager.request_async()` fresh on every
  single poll -- once a second, indefinitely -- instead of requesting
  it once and reusing it the way Microsoft's own guidance for this API
  describes. Each call round-trips to the Windows media broker and
  builds a whole new WinRT projection object graph (manager, session
  list, properties, timeline, playback info); those are COM-reference-
  counted underneath Python's own refcounting, which doesn't reliably
  release the native side just because the Python wrapper goes out of
  scope on a background asyncio loop that's never idle. A leak of only
  ~45KB per call -- entirely plausible for an unreleased COM object
  graph -- accounts for the full ~2GB after a single overnight run at
  1 poll/sec. Fixed by caching the manager (`_get_media_manager()`) so
  only the first poll ever requests one. Also added an occasional (every
  10 minutes) `memory: N MB RSS` log line to the running theme so a
  real leak -- this one or another -- is visible in the Log panel over
  a multi-hour run without needing a profiler attached ahead of time.
  Not independently confirmed against real hardware/Windows yet (this
  is a Windows-only, WinRT-specific code path that can't be exercised
  in a Linux sandbox) -- flagged here as the most likely cause based on
  code review, not a verified fix. If RSS still climbs after this with
  Spotify/media running, the next suspects are the `session`/`props`/
  `timeline`/`playback` WinRT objects themselves (try `del`-ing them
  explicitly before returning) rather than the manager.
- **Double-clicking the desktop icon while the app is already running no
  longer shows a "this is already running" message box** -- it now just
  brings the already-running window to the front, the way any normal
  single-window app behaves. `single_instance.py`'s
  `_bring_existing_window_to_front()` used to fall back to a
  `messagebox.showinfo()` only when `FindWindowW` couldn't locate/
  activate the other instance's window -- which, in practice, was most
  of the time: Windows restricts which processes are allowed to steal
  foreground focus, and a background process calling
  `SetForegroundWindow` can be silently ignored with no way to detect
  that failure from the caller's side. Replaced with a mechanism that
  can't fail that way: the second launch now touches a plain sentinel
  file's mtime (`SHOW_TRIGGER_PATH`, next to `app_config.json`), and the
  *running* instance's own existing 100ms log-queue poll timer
  (`app.py`'s new `App._poll_show_trigger()`) notices the mtime change
  and raises its own window from its own Tk main thread -- guaranteed
  to work, since it's the app raising its own window rather than an
  outside process trying to steal focus. `FindWindowW` +
  `SetForegroundWindow` is still tried first, same-instant, as a bonus
  (feels snappier when Windows allows it); the file-touch path is what
  actually always works, just up to ~100ms slower. No dialog of any
  kind is shown to the end user any more for this case.
- **Redesigned the web frontend's layout again** -- the two-column
  split above (Preview stacked directly on top of the current theme's
  settings/design canvas, in the right column) still forced scrolling
  between the live preview and whatever needed configuring, and left a
  visibly empty gap under the short left column once the right column
  kept going past it. Preview and the current theme's settings now sit
  side by side in a `.preview-and-config` flex row instead of stacked,
  for Video/Webpage/Clock. The dashboard theme is handled differently:
  since `DashboardCanvas` already draws the live frame inside its own
  canvas box, a separate Preview panel next to it would just be the
  same image polled and shown twice, so it's skipped there and
  `DashboardCanvas` gets the full row's width -- which it uses for its
  own internal side-by-side split (`.canvas-layout`): the drag/drop
  canvas on the left, the element list and the selected element's
  property panel stacked in a sidebar on the right. Both splits wrap
  back to a single stacked column automatically below their combined
  minimum widths, same fallback behavior as the outer two-column grid.
  `.app`'s max page width raised from 1400px to 1800px so a wide
  monitor gets more benefit from all of this. Note: this is still a
  page rendered in whatever browser tab the user opens it in, not a
  native window this app controls -- there's no way to make the
  browser's own window wider from here; Phase 7's packaged webview
  window will be able to pick a sensible default size and shape once
  that lands.
- **Frontend rows redesigned again, this time to the exact layout
  requested**: one full-width row for panel port + theme picker +
  Start/Stop/Apply (merged into a single "Controls" section), the
  selected theme's own config next to the live preview right below it
  (same side-by-side reasoning and dashboard-theme exception as
  above), then brightness, System, and Log each getting their own
  full-width row instead of being split into a left/right page column.
  The old two-column `.app-columns` page grid is gone entirely --
  `.app`'s own top-to-bottom flex stack is what lays out every row now,
  so there's no longer a second, independently-tall column that can
  leave an empty gap under a shorter one.
- **Root-caused and fixed why double-clicking the desktop icon could
  appear to do nothing at all** (not even the dialog the earlier fix
  above removed): `startup_registration.py`'s `enable_startup()` and
  `desktop_shortcut.py`'s `create_desktop_shortcut()` both resolved
  "the app" via `sys.modules["__main__"].__file__` -- fine as long as
  the only thing that ever calls them is `app.py` itself, but both are
  reachable from the exact same control-server endpoints the web
  frontend's System panel hits, and that control server can just as
  well be `scripts/run_backend.py` or `scripts/run_v2_app.py`
  (`backend_app.py`, ROADMAP.md Phase 2c) during development/testing.
  Toggling "Launch at Windows startup" or clicking "Create Desktop
  Shortcut" while either of those was `__main__` silently baked THAT
  script into the Startup entry or the desktop shortcut instead of the
  real `app.py`. Since neither of those ever opens `app.py`'s Tkinter
  window, and both call the same `_ensure_single_instance()` mutex
  check, the result was a shortcut/Startup entry that (a) holds the
  same single-instance mutex `app.py` checks, but (b) has no
  "Hongtai Screen Control" window and no `SHOW_TRIGGER_PATH` poll loop
  for `app.py`'s side of that check to ever find -- so double-clicking
  the real desktop icon while one of these was running just silently
  wrote the trigger file, found nothing to raise, and exited. Fixed by
  resolving the app path via `paths.py`'s `_app_base_dir()` (the real
  on-disk repo root) in both functions instead of trusting whichever
  script happened to be `__main__` -- both now always point at the one
  real `app.py` regardless of which entry point's UI triggered them.
  Anyone who already has a stale shortcut/Startup entry from before
  this fix needs to quit whatever's currently running (check the
  system tray) and re-click "Create Desktop Shortcut" / re-tick
  "Launch at Windows startup" once to regenerate it correctly -- this
  fix prevents the problem going forward, it doesn't repair a shortcut
  already written.
- **Cut over the desktop icon and "Launch at Windows startup" to the
  React/webview app, ahead of ROADMAP.md's planned Phase 7 -- at the
  user's explicit request**, once it became clear the fix above just
  correctly restored double-click back to launching Tkinter, which
  wasn't actually what was wanted after this whole session's work went
  into the web frontend. `desktop_shortcut.py` and
  `startup_registration.py` now point at `scripts/run_v2_app.py`
  (`backend_app.py`: the control API + tray icon + a `pywebview`
  window showing the same React frontend) instead of `app.py`.
  `backend_app.py` gained `BackendApp.start_show_watcher()`, a
  background thread polling `SHOW_TRIGGER_PATH` (the same file
  `single_instance.py`'s `_bring_existing_window_to_front()` already
  touches on a second launch) -- without this, double-clicking the
  icon while the backend was already running would have hit the exact
  same "does nothing" bug this whole thread started from, just for a
  different reason (no Tk event loop here to piggyback a poll onto
  like app.py's `_poll_show_trigger()` does, so a plain daemon thread
  does the same job instead). Also swapped `main()`'s two startup
  `print()` calls for the controller's own `_log()` (mirrored to
  `STARTUP_LOG_PATH` for `--autostart`) -- launched via `pythonw.exe`
  with no console attached (which is what actually happens once this
  is what the desktop icon points at), a bare `print()` either goes
  nowhere or can raise outright. `pywebview` moved from a commented-out
  optional line in `requirements.txt` to a required one. `app.py`'s
  Tkinter GUI is untouched and keeps working for manual/headless use
  (`python app.py`), it's simply not launched automatically any more.
  The one known gap: the frozen-build (`.exe`) branch in both files
  still targets the old Tkinter build (`packaging/hongtai_screen.spec`
  hasn't been updated to package `run_v2_app.py` + the webview UI yet)
  -- this cutover only covers running from source
  (`pip install -r requirements.txt`), which is how this app is
  currently run; a proper frozen build of the new stack is still real
  Phase 7 packaging work.
- Fixed a pre-existing design-canvas bug, surfaced once the canvas
  redesign above made it the page's full-width centerpiece: a gauge
  element's resize-handle square rendered unconditionally in
  `DashboardCanvas.jsx`, unlike every other element type (text, graph,
  image, media, clock), which all correctly gate their handle(s) behind
  "only when selected". Fixed by wrapping the gauge handle in the same
  `{isSelected && (...)}` check the others already use.
- **Removed the "Middle content" setting (weather vs. nothing, a global
  on/off pinned to the fixed column between the two gauge columns) and
  made weather a movable/resizable canvas element instead** -- the
  exact same conversion the now-playing display already got earlier in
  this rewrite (from a fixed always-on "spotify" `middle_content`
  choice into the `media` element). Click "+ Add weather" in the
  design canvas to add one; drag/resize it like any other element, and
  set its location/units in its own property panel (previously a
  single global location/units pair shared by the whole app).
  `dashboard_theme.py`'s `MIDDLE_CONTENT_OPTIONS`/`_middle_content`/
  `set_middle_content()`/`get_middle_content()` are gone entirely,
  replaced by `default_weather_element()`/`_draw_weather_element()`
  (mirroring `default_media_element()`/`_draw_media_element()`) and a
  new `apply_weather_from_elements()` helper that points weather.py's
  shared background poll at whichever `weather` element is on the
  canvas (called at Dashboard startup and again on every live layout
  edit, so editing an existing element's location/units from the
  design canvas applies without a Stop/Start, same as everything else
  on this canvas). A saved config that had `middle_content: "weather"`
  is migrated automatically (`config_store.migrate_dashboard_weather_
  element()`, the same one-time-migration pattern
  `migrate_dashboard_elements()` already established) into a real
  `weather` element carrying its old location/units; `middle_content`/
  `weather_location`/`weather_units` are dropped from config either
  way. The web UI's "Middle content" panel (and its
  `/api/dashboard/middle_content` endpoint) are gone; `app.py`'s
  Tkinter Dashboard tab drops its own Show/Location/Units controls too
  (a short pointer to the web design canvas takes their place), so it
  no longer writes those now-retired keys back into the shared config.
- Fixed the design canvas's element list scrolling by default: its
  `max-height: 140px` predated the clock/media/weather elements, so
  once those joined the 8 default gauges the list's 10-11 rows no
  longer fit and it scrolled even with nothing custom added. Raised
  to 480px; also made `overflow-x: hidden` explicit, fixing a phantom
  horizontal scrollbar some browsers drew from only `overflow-y`
  being set.
- **Weather's icon/temperature/description/feels-like+humidity/
  location-name are now independently switchable**, the same
  show_art/show_name/show_time deal `media` already has -- so a
  weather element can be shrunk to just an icon or just the
  temperature, e.g. to sit next to a now-playing element without the
  two eating the whole panel between them. New
  `show_icon`/`show_temp`/`show_description`/`show_details`/
  `show_location` fields (each default True, so an existing saved
  weather element is unaffected); whichever pieces are on stack
  top-to-bottom with no gap left by a disabled one, and the "no data
  yet" placeholder only draws when at least one text piece is on.
- **Fixed now-playing/weather content not matching its own box**,
  reported as the layout looking "not accurate" and making the two
  hard to line up precisely. Both elements' content used to always
  start flush with the box's top edge, so a box taller than its
  (possibly piece-reduced) content left a growing gap underneath, all
  still inside the box -- meaning lining up two boxes edge-to-edge
  didn't actually line up their visible pictures/text. Fixed by
  pre-measuring exactly how tall the currently-enabled pieces are
  going to render and centering that whole stack vertically in the
  box instead of anchoring it to the top.
- **Stopped the design canvas's edit-only mockup boxes from being
  drawn on top of the real live panel image**, reported as the
  tinted fill/border/"NOW PLAYING"/"WEATHER" labels visibly sitting
  over the actual rendered content once connected, "ruining the
  preview." The design canvas doubles as a live preview: once
  connected, it overlays the real live frame (an actual JPEG of what
  the panel is showing) under the SVG editor layer, so the frame
  already draws a graph/image/now-playing/weather element's real
  content at that exact spot -- the mockup box was then just a
  duplicate annotation sitting on top of it. The `clock` element type
  already handled this correctly (its hands-and-face mockup only
  draws when there's no live frame to collide with, or the element is
  selected); this extends the same `overLiveFrame` condition
  (`connected && !!frameUrl`) to the graph/image/media/weather box-
  rendering branch in `DashboardCanvas.jsx` via a new `showMockup`
  flag (`el.type === "graph" || isSelected || !overLiveFrame`) that
  now gates the tinted rect, the picked-image border rect, and the
  centered label text. A plain `graph` element keeps its mockup box
  always, connected or not, since nothing in the live frame draws
  where a graph *will* go the way it does for the others. When the
  mockup is hidden, an invisible `fill="transparent"` rect takes its
  place so the element's full footprint stays click/drag-able even
  though nothing is drawn -- selecting, moving, and resizing an
  element still works exactly the same whether or not its box happens
  to be visible right now. Verified via Playwright that disconnected/
  no-live-frame mode (pure editor use) is unaffected -- every element
  still shows its mockup box whether selected or not, matching the
  pre-existing `clock` behavior this reuses.
- **Extended the same overlay hiding to gauges.** A gauge's ring +
  stat-title text is dynamic content the live frame already draws at
  that exact spot too (the real value baked in, not just the title),
  so it had the same duplicate-mockup problem the box types above
  did. The gauge branch in `DashboardCanvas.jsx` now computes its own
  `overLiveFrame`/`showMockup` (`isSelected || !overLiveFrame`) and
  gates the visible ring + title text behind it, falling back to an
  invisible `fill="transparent"` circle in the same spot so it's
  still click/drag-able either way; the selection outline and resize
  handle are unaffected, same as every other element type. Verified
  via Playwright in disconnected mode: gauges render unchanged both
  selected and deselected.
- **Fixed "Reset to defaults" (and any other unsaved edit) appearing to
  do nothing while connected**, a side effect of the mockup-hiding fix
  just above: `showMockup` was gated purely on `isSelected ||
  !overLiveFrame`, with no way to tell that the live frame it was
  deferring to hadn't actually caught up with the edit yet. The live
  frame only ever shows what was last *saved* -- an edit only reaches
  the physical panel once Save layout is clicked (see that button's own
  hint text) -- so resetting (which touches every element and leaves
  none selected) made every mockup disappear at once while the live
  frame underneath still showed the old, un-reset layout: from the
  outside, absolutely nothing looked like it had changed. Fixed by
  adding `&& !dirty` to all three `overLiveFrame` computations (box
  types, clock, gauge) -- `dirty` (`elements !== savedElementsRef.
  current`) already existed, driving the toolbar's own "You have
  unsaved changes" hint, so this just wires the same flag into the
  mockup-hiding decision: any unsaved edit now keeps every mockup
  visible regardless of connection state, and hiding only resumes once
  Save layout brings the live frame back in sync.
- **Now-playing/weather's selection box now shrinks and grows with
  whichever pieces are switched on**, instead of staying at whatever
  size it was last dragged to. The earlier centering fix
  (`dashboard_theme.py`'s content-height pre-measurement) made the
  *content* re-center inside the box when a piece was turned off, but
  the box itself -- the thing actually being looked at and lined up
  against other elements on the design canvas -- never changed size to
  match, so a compact icon-only weather element still had a box sized
  for the full readout. Each show_* checkbox's `onChange` in
  `DashboardCanvas.jsx` now also computes a new `height` via one of two
  new estimator functions, `estimateWeatherHeight()`/
  `estimateMediaHeight()`, that mirror `_draw_weather_element()`'s/
  `_draw_media_element()`'s own content-height pre-measurement as
  closely as a browser-side estimate can (an exact match isn't
  possible -- whether weather data has loaded, or something's actually
  playing, isn't known until the theme is running, so this always
  assumes the fully-populated case). Height only, not width, since
  these elements stack their pieces vertically. Verified via
  Playwright: adding a weather element and unchecking description/
  feels-like+humidity/location dropped its height from 46% to 36%;
  also unchecking temperature (icon only) dropped it to 24%, with the
  on-canvas selection box visibly shrinking to match at each step.
- **Fixed dragging an element snapping its center to wherever the
  cursor first landed**, reported as "clicking on a gauge moves it to
  where I clicked" -- grabbing a gauge anywhere other than dead center
  (its edge, say) made it jump so that exact point became the new
  center, instead of moving smoothly from wherever it already was.
  `onPointerMove`'s "move" branch set the element's x/y straight to the
  cursor's own fraction-of-canvas position every frame, with nothing
  recording *where on the element* it had actually been grabbed.
  Fixed by computing `offsetX`/`offsetY` (the gap between the cursor
  and the element's center) once at the moment of the grab, in
  `onPointerDownGauge`, and subtracting it back out of every subsequent
  cursor position in `onPointerMove` -- the element now tracks the
  cursor's movement, not its raw position. Affects every element type
  that uses this same shared move handler (gauge, text, graph, image,
  media, weather, clock), not just gauges. While tracking this down, a
  second, independently-triggering bug in the same code path turned up
  and got fixed alongside it: `onPointerMove` re-queried
  `svgRef.current.getBoundingClientRect()` on every single move, and a
  drag's very first move flips `dirty` true (see the entry above),
  which reveals the "unsaved changes" hint line above the canvas -- a
  real layout reflow that shifts the canvas box (and this rect) down by
  however tall that line is, *mid-gesture*. Every move after the first
  was then computing its fraction against a rect whose top had silently
  shifted out from under the still-held cursor, so the element drifted
  off the cursor by that same amount for the rest of the drag. Fixed by
  snapshotting the rect once in `onPointerDownGauge`/
  `onPointerDownHandle` and reusing that same snapshot for the whole
  gesture instead of re-querying it. Verified with a Playwright test
  that reads the dragged element's exact (unrounded) on-canvas position
  before and after a drag and compares it against the exact pixel
  delta moved: previously a pure-vertical 40px drag registered as only
  20 viewBox units of movement (should be ~32) due to the reflow bug
  alone, and a 40px-vertical/off-center grab additionally registered
  only half the expected vertical delta from the offset bug; after both
  fixes, a drag's on-canvas movement matches the cursor's own pixel
  movement exactly, on both axes, however it's grabbed.
- **Fixed "Reset to defaults" still visually showing custom content
  (uploaded images, a moved gauge, whatever was there before) even
  though the element list on the right correctly showed only the
  defaults**, a gap the `!dirty` fix above didn't close: that fix made
  every element's *mockup* correctly reappear at its default position,
  but the actual `<img className="canvas-frame">` underneath it --
  a real photo of what the physical panel is showing *right now* -- is
  a separate piece of the canvas, and it kept rendering regardless,
  since it only ever reflects the last *saved* layout and Reset alone
  doesn't save anything. So the on-screen result was every default
  mockup box correctly showing, layered on top of an unchanged photo
  of the old custom layout still visibly showing through everywhere
  the new mockups didn't fully cover it -- from the outside this read
  as "the preview didn't actually change." Fixed by adding the same
  `&& !dirty` this canvas-frame `<img>`'s own render condition
  (previously just `frameUrl && connected`): now any unsaved edit
  hides the stale photo entirely, dropping the canvas back to the same
  plain dark background pure-editor mode it already uses while
  disconnected, so what's on screen is exactly (and only) what
  `elements` currently says, with no stale photo left to disagree with
  it. The photo reappears the instant Save layout clears `dirty`, now
  showing the real, caught-up panel again.
- **Reworked the design canvas toolbar's Save layout button**, on
  feedback that it "shouldn't be like adding stuff": it used to sit
  inline with the "+ Add X" buttons, always enabled, with a separate
  "* You have unsaved changes..." sentence underneath explaining when
  it actually mattered. Removed that sentence entirely; the toolbar is
  now two groups (`.canvas-toolbar-group` for add-element/undo/redo/
  reset, and Save layout on its own) laid out with `justify-content:
  space-between`, pushing Save layout to the toolbar's far right so it
  reads as a distinct, separate action rather than one more button in
  the "add stuff" row. It's also properly `disabled` (greyed out, the
  same as Undo/Redo already were) whenever `dirty` is false, and only
  enabled -- with its existing orange "unsaved" styling -- once there's
  actually something to push to the panel, so the button's own state
  now carries the information the removed sentence used to.
- **Added a `bar` element type** ("we don't have bars, we have gauges,
  graphs, we need bars") -- a linear meter for one stat's current
  value, the same reading a `gauge` shows (a live value against its
  own min/max) but as a horizontal fill bar instead of a circular ring,
  for lining several stats up as a compact stack or just for the look.
  Not a history/trend view -- that's `graph` (which already has its
  own "Bar" *style* option for a bars-over-time chart; this is a
  different thing, a single always-current reading). Backend:
  `_bar_box()`/`_draw_bar_static()` (box + title, baked into the
  static background like `graph`'s own split) and `_draw_bar_dynamic()`
  (the live fill + value text, redrawn every frame like a gauge's
  needle) in `dashboard_theme.py`, reusing `progress_bar_glow()` --
  the same filled-track-plus-glowing-knob look `_draw_media_element()`'s
  playback bar already draws -- rather than inventing a second bar-
  drawing routine. A missing stat reading draws an empty track and
  "--" instead of guessing zero, same rule `draw_gauge_dynamic()`
  follows. Frontend: "+ Add bar" in the toolbar, a Stat/Color/Opacity
  property panel (no Style/History fields -- there's no time axis to
  configure), and its own resizable box in the design canvas, treated
  the same as `graph`'s mockup box (always shown, connected or not,
  since neither one's mockup attempts to draw the real bars/fill --
  that needs live data this editor doesn't have -- unlike `media`/
  `weather`'s mockup, which duplicates a label the live frame already
  shows and so hides once connected). Verified: direct `render_frame()`
  calls for a normal reading, a missing stat, and a very small box all
  render without error; Playwright confirms "+ Add bar" adds a
  correctly-labeled, selectable, resizable BAR element to the canvas.
- **Reverted the "hide the live panel photo while dirty" fix above**
  ("i dont want a stall image, and i dont want the background black"):
  hiding `<img className="canvas-frame">` whenever there was an unsaved
  edit did stop it from showing stale content after Reset to defaults,
  but at the cost of the whole canvas dropping to a plain black
  background the instant *any* edit was in progress -- a drag, a
  checkbox toggle, even just clicking to select something -- which read
  as broken far more often than the original staleness ever did. The
  photo is back to always rendering whenever `connected && frameUrl`,
  regardless of `dirty`, so the canvas never goes black and always
  shows a live, continuously-refreshing view of the real panel. The
  "does this edit show up immediately" job Reset to defaults needed
  moved onto the mockups instead: every element type's `showMockup`
  now also checks `dirty` (`... || dirty`, previously the `!dirty` sat
  on `overLiveFrame` itself), so an unsaved edit still draws its mockup
  immediately on top of the (possibly stale-until-Save) photo, without
  making the photo itself disappear to do it.
- **Added a Horizontal/Vertical orientation to the `bar` element**: a
  new `orientation` field (default `"horizontal"`) picks which way the
  fill runs -- left-to-right within the box's width, or bottom-to-top
  within its height -- via a new `progress_bar_glow(..., vertical=...)`
  parameter that swaps which axis the fill/rounded-ends/knob travel
  along while keeping the same track rect either way. The property
  panel's new Orientation dropdown also swaps the element's own width/
  height when toggled (so switching to vertical turns a wide-short box
  tall-narrow, matching the new fill direction, and back again).
  Vertical bars place their live value text to the right of the bar,
  pinned to its top, instead of directly below it -- an initial version
  put it below, which collided with the fill's knob at low/idle values
  (the common case, since the knob sits at the bottom of the track when
  the reading is near its minimum); pinning to the top only risks the
  same rare-case collision the horizontal layout already accepts (text
  near the knob once a reading is close to its max). Verified via
  direct `render_frame()` calls (horizontal, vertical, and vertical
  with a missing stat reading -- confirms the empty-track "--" case
  renders with no text/knob collision) and a Playwright pass confirming
  the Orientation dropdown appears, swaps Width%/Height%, and updates
  the on-canvas mockup's shape.
- **Shrunk the bar element's knob** ("bar is looking bad? what is
  that?") -- on real hardware, `progress_bar_glow()`'s knob (radius =
  track thickness × 1.7) came out to a ~75px ball on the bar's ~22px-
  thick track, dwarfing the track itself and swamping the title/value
  text around it; that multiplier had only ever been tuned against the
  now-playing progress bar's much thinner 6px track (~20px knob there).
  `progress_bar_glow()` gained a `knob_scale` parameter (default 1.7,
  unchanged, so the now-playing bar's look is untouched) and the bar
  element's own call now passes `knob_scale=0.8`, bringing its knob
  down to a reasonable size relative to the track.
- **Fixed the weather element's content overflowing its own box**
  ("weather box still not containing it, it still overflowing") --
  `_draw_weather_element()` only ever centers its icon/temp/description/
  details/location stack within the box height, it never clips or
  scales it down, so a box shorter than what the enabled pieces
  actually need (a manual resize, or a stale height left over from
  before a piece was switched back on) just let the content spill past
  the box silently -- most visibly the "Feels 32°C · 57% humidity"
  detail line running into whatever sat below it.

  First attempt: grow `_weather_box()` to fit the content
  (`_weather_content_height()`) whenever the stored height was smaller.
  That did stop weather's own content from overflowing, but a follow-up
  screenshot on real hardware showed a worse side effect: growing the
  box made a small element's real on-panel footprint balloon well past
  what its own width/height fields said, so it started overlapping the
  now-playing element sitting above it and the clock sitting below it
  instead -- still visibly broken, just a different collision.

  Replaced that with the reverse: the box now always stays exactly
  `el["width"]`/`el["height"]`, no exceptions, and
  `_draw_weather_element()` instead scales its *content* down to fit
  whatever box that is -- icon size and every font size shrink together
  (a new `scale = box_h / content_h_natural` factor, floored at 0.55 so
  text never shrinks past legible) whenever the enabled pieces'
  natural, full-size combined height would be taller than the box.
  Scaled fonts are loaded through a new small `_cached_scaled_font()`
  (an `lru_cache`-wrapped `load_font()`) so a per-element scale factor
  that's the same frame to frame doesn't re-hit FreeType on every
  single frame. `_weather_box()` lost the grow-to-fit logic entirely;
  `estimateWeatherHeight()` on the frontend (used to auto-fit the box
  on each show_* checkbox toggle) is unchanged as a *suggestion*, but
  is no longer also applied as a floor on the mockup's rendered height
  -- the mockup now always matches `el.width`/`el.height` exactly, same
  as the real render. Verified via a direct `render_frame()` call
  reproducing the screenshot's exact layout (a small 19%×9% weather box
  with all five pieces on, sandwiched between a now-playing element and
  a clock): the box no longer grows past its own dimensions and every
  piece renders as a compact, fully-contained readout instead, with no
  overlap into either neighbor; a Playwright pass confirms the on-canvas
  mockup box now stays at exactly the Width%/Height% the property panel
  says, even shrunk down to 19%/9%.
- **Fixed unrelated elements' mockup boxes/borders lighting up while
  dragging something else** ("when i move an object, why do other
  objects get highlighted?") -- a regression from the "keep the live
  panel photo always visible" fix a few entries up, which added
  `|| dirty` to every element type's `showMockup` so a mass change like
  Reset to defaults (nothing selected) would still visibly update. The
  bug: *any* edit sets `dirty`, not just a mass one -- so an ordinary
  drag on a single gauge, or a single checkbox flip, also flipped
  `dirty` true, which meant every *other* box-type element (an image,
  now-playing, weather) lit up its mockup border for the whole gesture
  too, even though only one element was actually being touched.
  Replaced the bare `dirty` check with a new `forceAllMockups = dirty
  && !selectedId`: a mass change like Reset to defaults (or an
  Undo/Redo landing on nothing selected) has no selection, so this
  still kicks in and forces every mockup to show; an ordinary drag or
  property-panel edit always has the element being edited selected, so
  `isSelected` alone already covers that one element, and
  `forceAllMockups` no longer fires for everyone else's. Verified: the
  underlying `dirty && !selectedId` logic checked directly against the
  three scenarios it needs to tell apart (drag with a selection -->
  false, Reset with no selection --> true, no edits --> false); a
  Playwright pass added a second box-type element (an image) alongside
  a gauge and confirmed dragging the gauge moves only the gauge, with
  the image element's own mockup box unaffected throughout the drag.
- **Fixed clicking an element sometimes nudging it and always marking
  the layout unsaved** ("sometimes when selecting something, i think it
  gets moved a bit, because as soon as i click on an object, the save
  layout activates"). A mouse/trackpad/touch "click" is never *exactly*
  zero pixels of movement between pointerdown and pointerup -- there's
  always a stray pointermove carrying a pixel or two of jitter -- and
  every pointermove during a drag gesture ran straight through to
  `setElements()`'s `prev.map(...)`, which allocates a new array (and a
  new element object) even when the computed x/y come out numerically
  unchanged. `dirty` is a strict `elements !== savedElementsRef.current`
  reference check, so that alone was enough to flip it true and light up
  "Save layout*" on a plain click, and in principle also let a stray
  jitter pixel or two land as a barely-visible unintended move. Fixed
  with a `MOVE_THRESHOLD_PX = 4` guard: `onPointerMove` now measures real
  screen-pixel distance from where the gesture started and does nothing
  at all -- no `setElements` call -- until that's crossed, so a plain
  click leaves `elements` at its exact prior reference and `dirty` stays
  false; once the threshold is crossed even once, the rest of the
  gesture behaves exactly as before. `endDrag` also now skips pushing a
  before/after pair onto the Undo stack for a gesture that never crossed
  the threshold, since nothing changed. Verified with a Playwright pass:
  a real mouse down/move-1px/move-2px/up sequence on a gauge left "Save
  layout" inactive, while the same sequence with a 20px move afterward
  correctly flipped it to "Save layout*".
- **Fixed the `bar` element always showing its mockup box/border/label,
  even saved and unselected** ("the bar looks highlighted by default").
  `bar` had been lumped in with `graph` in `showMockup`'s "always show,
  connected or not" case when it was added, on the reasoning that
  neither mockup can preview live data this editor doesn't have. That
  reasoning holds for `graph` (a rough placeholder that can't fake a
  real line/history), but not for `bar` -- its mockup is just an
  accent-colored fill/border/label at a fixed demo fraction, close
  enough to the real thing (same as a gauge's demo needle) that keeping
  it drawn permanently on top of an accurate live photo just reads as a
  permanently-selected-looking box. `bar` now defers to the live frame
  exactly like gauge/clock/media/weather already do (mockup only when
  selected, disconnected, or right after a Reset/Undo-Redo with nothing
  selected); only `graph` keeps the unconditional mockup. Verified the
  `showMockup` boolean directly against the selected/connected/
  forceAllMockups matrix for both `bar` and `graph`.
- **Made the `bar` element's fill fully customizable**: an optional
  "Show knob" toggle (off by default now -- direct follow-up feedback
  on the round white handle from the previous entry was "i dont like
  [it]"), and a "Gradient fill" option with 2-4 color stops (not just
  the two colors gauge's own gradient option is limited to) and a
  direction picker -- Left→Right or Top→Bottom, independent of the
  bar's Orientation (fill direction), so a horizontal bar can gradient
  top-to-bottom and a vertical one left-to-right if that reads better
  than matching the value's own fill axis. Backend: a new
  `_linear_gradient_multi()` generalizes the existing `_linear_gradient()`
  (kept as-is for its own fixed top-to-bottom two-stop callers) to an
  arbitrary stop count and either axis; `progress_bar_glow()` gained
  `show_knob` and `fill_colors`/`fill_direction` params, and a new
  `_bar_fill_subtile()` helper slices the *filled* portion's colors out
  of a virtual full-bar-sized gradient (so a given spot on the bar keeps
  its color as the value changes -- only how much of the gradient is
  revealed moves) while still building the rounded-end mask at the
  actual small fill size (so the visible end keeps its rounded cap
  instead of a hard-cropped edge). Frontend: the bar's on-canvas mockup
  now renders an actual SVG `linearGradient` matching the selected
  stops/direction instead of a flat color swatch, so picking colors
  shows the real result immediately. Verified by rendering the backend
  helpers directly (2-stop and 3-stop horizontal, vertical-direction-on-
  horizontal-bar, horizontal-direction-on-vertical-bar, knob on/off) and
  a Playwright pass driving the new panel controls end to end (add a
  bar, toggle gradient on, add a third stop, switch direction, confirm
  the mockup gradient updates each time).
- **Extended the bar element's gradient-fill option (2-4 stops +
  direction) to text, graph, clock's digital face, and gauge**, and
  added a **"Preview on screen" button** so a layout edit can be shown
  on the real panel for 5 seconds without Save layout first, then
  reverted automatically. Frontend: the bar-only gradient JSX became a
  shared `GradientFillControl` component and a shared `gradientFill()`
  mockup helper, reused by every element's property panel and canvas
  preview; gauge keeps its separate "Custom color" override alongside
  the new gradient option, with its own Diagonal/Left→Right/Top→Bottom
  direction choices. Backend: text and clock's digital face gradient
  by drawing the glyphs as an alpha mask and recoloring it with
  `.putalpha()`; graph reuses its existing tile's alpha the same way;
  gauge uses cairo's native `add_color_stop_rgba()` for an arbitrary
  stop count, with the older single `color2` field still honored
  (folded into a 2-stop diagonal gradient) on layouts saved before
  this change. Preview reuses Save layout's existing "push to the
  live render loop without restarting" mechanism but skips writing to
  disk, and a `threading.Timer` reverts to whatever's actually saved
  after 5 seconds -- cancelled if a new preview or a real Save happens
  first. The button is disabled whenever Save layout would be, too
  (Dashboard theme not running). Verified by direct-rendering all four
  new backend gradient paths, a Playwright pass confirming the Preview
  button's disabled state and the new gradient panels on text, graph,
  and gauge (direction dropdown, color swatches, live mockup update,
  gauge's Custom color checkbox left intact).

- **Fixed Bring to front/Send to back doing nothing for most elements.**
  The canvas already tracked each element's `z` and sorted by it
  correctly when baking the *static* layer (text/image, baked once into
  the background image) -- but `render_frame()`'s per-frame loop for
  every *dynamic* element (gauge, graph, bar, media, clock, weather --
  i.e. nearly everything a real dashboard is made of) walked the raw
  `elements` list instead, completely ignoring `z`. So reordering two
  overlapping gauges, or a now-playing box and the gauges behind it,
  visibly did nothing, because the thing actually drawn on top every
  frame never consulted the order the buttons were changing. Fixed by
  sorting that loop by `z` too, same as the static bake already did.
  Note: a dynamic element still always draws on top of every static
  (text/image) one regardless of `z`, since static elements are baked
  into the background once and dynamic ones are layered on top of that
  afterward every frame -- ordering *within* the dynamic group (the
  common case) now works, ordering across the static/dynamic boundary
  is a separate, deeper limitation left for later.
- **Fixed the bar element showing nothing at all below roughly its own
  thickness ÷ track length as a fraction** (~15% for a typical bar --
  e.g. a vertical CPU-usage bar reading flat empty until usage spiked).
  `_bar_fill_subtile()`'s rounded-corner mask used a fixed radius (half
  the track's thickness) that could exceed the actual filled sliver's
  own height/width at a low value, so `progress_bar_glow()` skipped
  drawing the fill entirely rather than risk a malformed rounded rect --
  the knob still moved, but the colored fill (and gradient, if any)
  stayed invisible until the value climbed past that threshold, reading
  as a stuck 0%. Fixed by clamping the mask's radius to the sliver's
  own size before drawing, so any nonzero value now shows a proportional
  fill down to a small round dot at the very bottom/left instead of
  nothing.

- **"Launch at Windows startup" switched from a Startup-folder .vbs to
  a Task Scheduler task (logon trigger).** A user report -- the app
  taking over a minute to appear after logging in, versus 5 seconds
  for another vendor's startup app on the same machine -- traced to the
  launch mechanism itself, not anything about this app's own code:
  items in the Startup folder (and the Run registry key) are exactly
  what Windows' own post-login "boot storm" mitigation deliberately
  staggers/delays, sometimes by minutes, to keep the system responsive
  right after logon. A Task Scheduler task with an "at logon" trigger
  fires directly off that event instead, bypassing that throttling --
  the standard fix for a slow-to-appear startup app. An existing
  install migrates automatically the next time the "Launch at Windows
  startup" checkbox is touched (off, or back on) -- no manual file
  cleanup needed, and toggling it never leaves both the old and new
  mechanism registered at once (which would have launched the app
  twice at login).

- **Fixed action errors (including a failed "Launch at Windows startup"
  toggle) being invisible unless the Controls section happened to be
  open.** Every button that fails goes through the same shared error
  state, but it was only ever rendered inside the Controls -- panel &
  theme section -- so a failure from a button anywhere else on the page
  (the System section's startup/shortcut controls, among others) set
  the error just fine, it just landed nowhere the person was looking,
  read as "nothing happens, no error anywhere." Moved that banner to
  right under the header, above every section, so it's visible no
  matter which one is open or closed, and gave it a bit more visual
  weight (background tint, border, padding) so an error can't blend in
  as another line of small text. Verified with Playwright: opened
  System with Controls collapsed, triggered a failing action, and
  confirmed the banner renders above Controls regardless.

- **Added detailed logging around the "Launch at Windows startup"
  toggle**, after a report that the checkbox still didn't work with no
  error anywhere, even after the previous two fixes to this area.
  Every step now writes to the app's own Log panel: the exact command
  that would run at logon, the literal `schtasks.exe` invocation, its
  exit code and both stdout/stderr, whether a leftover old-style
  launcher got cleaned up, and a re-check right after saying whether
  Windows now actually reports the task as registered. Previously only
  a failure's single exception message was visible (nothing at all on
  a call that "succeeded" but didn't actually take) -- this makes it
  possible to tell apart "schtasks refused" (permissions/policy),
  "schtasks silently didn't do what was asked", and "it worked, but
  something downstream is reporting it wrong" by reading what actually
  happened, rather than guessing from a blank Log panel. Threaded
  through both the web UI's controller and the Tkinter app's own
  "Launch at startup" checkbox, so either one logs the same way.

- **"Launch at Windows startup" now retries with a UAC elevation
  prompt if the plain attempt is refused.** The new logging above
  immediately paid off: a real machine came back with `schtasks
  exited 1 -- stderr: ERROR: Access is denied.` on the very first
  (non-elevated) attempt, even for a `/rl limited` task (one that
  only ever *runs* at the person's own privilege level once it fires
  at logon -- evidently *creating* it still needed an elevated
  creator, on that account/policy). A plain attempt is still tried
  first and is enough on most machines, so most people never see a
  prompt at all; only when that specific attempt looks like a
  permissions refusal does it retry via PowerShell's `Start-Process
  -Verb RunAs` (the actual UAC consent dialog), captures that elevated
  attempt's own exit code and stdout/stderr the same way, and logs
  every step of it too. Declining the prompt surfaces as a clear
  "permission prompt was declined" error rather than the login-task
  silently never existing with no explanation.

- **Fixed the elevated retry itself failing before it could even show
  a UAC prompt** -- the very first version tried `-Verb RunAs`
  directly on schtasks.exe together with `-RedirectStandardOutput`/
  `-RedirectStandardError` (to capture its output the same way the
  plain attempt does), which PowerShell immediately rejected with
  "Parameter set cannot be resolved using the specified named
  parameters" -- confirmed by a real run, where no UAC prompt ever
  appeared at all. Output redirection and elevation are two different,
  mutually exclusive parameter sets on `Start-Process`: an elevated
  child runs with a different token in effectively a different
  session, so its streams can't be piped back across that boundary
  the normal way, and PowerShell refuses to even attempt the
  combination. Fixed by elevating a small temporary `.ps1` script
  instead of schtasks directly -- the script runs *inside* the
  elevated process and writes schtasks' own combined output and exit
  code to two plain temp files with ordinary `Out-File`, so nothing
  needs to cross the elevation boundary at all; the outer, unelevated
  `Start-Process -Verb RunAs -Wait` call only elevates and waits, with
  no redirection parameters to conflict. The script and its two output
  files are temporary (a random suffix per attempt) and always cleaned
  up afterward, success or failure.
- **Fixed the elevated retry reporting a real success as a failure** --
  with the `.ps1`-script fix above in place, a real run showed the UAC
  prompt appearing, being approved, and schtasks itself printing
  `SUCCESS: The scheduled task "HongtaiScreenApp" has successfully
  been created.` to its own output file, yet the app logged `elevated
  schtasks exited -1` and left the "Launch at Windows startup"
  checkbox unchecked. Cause: the inner elevated script writes both
  output files with `Out-File -Encoding utf8`, and Windows PowerShell
  5.1's `utf8` encoding (unlike PowerShell 7's same-named one) always
  prepends a UTF-8 byte-order-mark; reading that back with Python's
  `encoding="utf-8"` left the BOM glued onto the text as a leading
  `﻿` character, which `str.strip()` doesn't remove since it
  isn't whitespace -- so `int("﻿0")` raised `ValueError` and fell
  through to the `-1` fallback every single time, regardless of what
  schtasks actually returned. Fixed by reading both temp files with
  `encoding="utf-8-sig"` instead, which strips a leading BOM when
  present and is otherwise identical to `"utf-8"`; also added a log
  line on that fallback path so a future parse failure (if any) names
  the actual exception instead of just showing `-1`.
- **Fixed the tray icon's "Show window" doing nothing when the window
  was already open but behind another window.** `backend_app.py`'s
  `_on_show()` (Phase 2c, the current entry point now that the
  desktop shortcut and Startup entry both point at `run_v2_app.py`)
  only ever checked whether the UI process was *alive*, and if so did
  nothing at all -- reasonable the first time this was written (only
  one thing could call it), but in practice indistinguishable from a
  broken tray icon: the window is genuinely open, just not on top,
  and clicking "Show window" visibly did nothing about that. Fixed by
  adding `_focus_ui_window()`, which brings the existing window to
  the front (and restores it if minimized) via the same
  `FindWindowW`/`SetForegroundWindow` mechanism `single_instance.py`
  already uses for the old Tkinter app's second-launch case, matched
  against the webview window's title (now passed explicitly as
  `run_ui.py --title`, via a new shared `UI_WINDOW_TITLE` constant,
  instead of relying on that script's own default staying in sync).
  Logs whether it actually found and focused the window, same
  step-by-step-logging habit as the startup-registration work above,
  so a report of "still doesn't come up" is diagnosable from the Log
  panel rather than a guess -- `SetForegroundWindow` is Windows'
  own best-effort API (a background process can be denied the right
  to steal focus outright, with no reliable way to detect that from
  here), so this is the same caveat `single_instance.py` already
  documents, not a new limitation.
- **Dashboard presets are now a grid of actual rendered pictures
  instead of a `<select>` of plain names.** The old picker made
  picking a preset a memory test -- "Streaming layout" and "Minimal"
  tell you nothing about what either one actually looks like until
  after you've loaded it. `dashboard_theme.py` gained
  `render_preset_thumbnail()`, which renders a small preview image of
  a set of elements through the *exact same*
  `build_static_background()`/`render_frame()` pipeline the real panel
  renders through (not a separate lightweight mock, so a thumbnail can
  never drift from what loading the preset would actually show) --
  fixed, plausible demo stat values stand in for live psutil/GPU
  readings (no hardware connection needed to render one), `media=None`
  reuses the theme's own existing "nothing playing" placeholder, and
  each graph element gets a short synthetic wave instead of an empty
  history buffer. `AppController._dashboard_preset_thumbnails()`
  renders one PNG per saved preset (against the currently configured
  background, since presets only ever store `elements`, never their
  own background) and hands them back as `data:image/png;base64,...`
  strings -- both from `dashboard_meta()` (so the picker has pictures
  on page load) and from `save_dashboard_preset()`/
  `delete_dashboard_preset()`'s own responses (so saving or deleting a
  preset updates the picker's pictures without a full page reload). A
  single malformed saved preset logs and is skipped rather than
  blanking the whole picker.

  `DashboardCanvas.jsx`'s preset picker is now a grid of cards (one
  per preset): clicking a card's thumbnail loads it immediately --
  seeing the picture and picking it is the same gesture, no separate
  Load button -- and each card has its own Delete button that arms on
  the first click ("Confirm?", auto-disarming after 3 seconds) and
  only actually deletes on a second, so a card that loads on a single
  click doesn't leave a one-misclick-from-losing-a-layout trap next to
  it. A preset whose thumbnail somehow failed to render still gets a
  clickable card with a "No preview" placeholder instead of vanishing
  from the picker entirely.

  Verified with a mocked backend (two saved presets, real thumbnails
  rendered through the actual pipeline) driven through a headless
  browser: both thumbnails render correctly and look visibly
  different, clicking a card's thumbnail loads that preset (element
  count changes, status line confirms), the delete button's first
  click arms it ("Confirm?") without deleting, and the second click
  deletes only that card, leaving the other preset's card untouched --
  no console errors at any point.
- **6 dashboard presets shipped from day one, and presets can now save
  their own background.** A brand-new install's preset picker used to
  be empty; `dashboard_theme.py` gains `BUILTIN_DASHBOARD_PRESETS`,
  seeded into `dashboard.presets` on a fresh config's very first load
  by `config_store.seed_builtin_dashboard_presets()` -- from that point
  on they're ordinary presets (renameable, editable, deletable), and
  deleting one is never silently undone on a later launch, since
  seeding only ever applies when `presets` is entirely absent, not
  merely emptied out. The six: **Neon Horizon** (futuristic -- a
  starfield HUD, cyan/magenta gradient gauges, a live network graph,
  a gradient VRAM bar), **Bubblegum** (cute -- soft pink/lavender/mint
  pastel gauges and rounded knob-bars on a purple gradient, a minimal
  analog clock), **Panic Mode** (funny -- deadpan captions like "BRAIN
  USAGE", "SNACK STORAGE" and "WILL TO LIVE" relabeling perfectly
  ordinary CPU/RAM/network/battery gauges on a red grid), **Mission
  Control** (informative -- 8 gauges, a CPU history graph, a GPU power
  bar and a date-showing clock, as much at once as reasonably fits),
  **Midnight Minimal** (a big analog clock and almost nothing else),
  and **Arcade RGB** (a rainbow gradient shared across a bar-style
  graph, a gradient bar and two gradient gauges, neon clock, starfield
  background).

  Getting there needed a real bug fixed first:
  `render_preset_thumbnail()` was rendering straight onto a half-size
  (480x240) canvas while `Fonts()` uses fixed *pixel* sizes calibrated
  for the panel's real 960x480 resolution -- so every title/label came
  out roughly twice as large relative to its own gauge as a real
  render, clipping text like "CPU LOAD" down to "PU LOAD" at the
  edges. Fixed by rendering at the reference resolution first and
  resizing the *finished image* down to the requested thumbnail size,
  which is also how every one of these 6 presets' layouts got tuned:
  by actually rendering each one with this function and looking at the
  result -- the same function the picker itself uses -- rather than
  placing elements by guessed coordinates, which is what caught (and
  let this fix) the title clipping plus a couple of caption/title text
  collisions in early drafts of Panic Mode and Midnight Minimal.

  Presets can now also carry their own background: `save_dashboard_
  preset()` takes an optional `background` snapshot (the design
  canvas's "Save as preset" button sends its current background
  draft), stored per-preset as `{"elements": [...], "background":
  {...} or None}` -- `None` (every preset saved before this, and any
  preset saved without changing the background) means "no background
  of its own," and thumbnail rendering/loading both fall back to
  whatever's globally configured in that case, unchanged from before.
  `config_store.migrate_dashboard_preset_shape()` upgrades any
  existing installs' old bare-`elements`-list presets to this same
  shape (with `background: None`) the first time they're loaded, so
  every preset -- built-in or hand-saved, old or new -- looks and
  loads the same way. Loading a preset now stages its background into
  the background draft alongside its elements (the same "loaded, not
  yet applied" deal `commit()` already gives elements -- Save layout/
  Save background are still what actually pushes either to the
  physical panel), so a click-to-load card restores the exact look a
  preset was saved with, not just its gauge positions.

  Verified end to end against a mocked backend serving the real 6
  built-in presets (actual rendered thumbnails, not placeholders):
  all 6 cards render as visibly distinct pictures, loading a preset
  with its own background (Bubblegum) correctly updates both the
  element count and the background draft's mode (confirmed by opening
  the Background section afterward and reading the select's value),
  and the delete-confirm flow still behaves correctly against a full
  6-preset set -- no console errors.
- **Text elements can now show a live stat instead of just a fixed
  string.** A text element's property panel gains a "Source" picker
  ("Custom text" vs "Live stat"); switched to the latter, it takes a
  `stat` (any `STAT_DEFS` key, same picker a gauge/graph/bar already
  has) and a `template` (default `"{value}"`, e.g. `"CPU {value}"` or
  `"{label}: {value}"`), and renders that stat's current formatted
  reading every frame instead of one baked-in string -- so a person
  designing their own layout can drop "GPU 58°" or "Battery: 80%"
  anywhere on the canvas as a plain label, without a gauge/bar's own
  ring or fill. `dashboard_theme._resolve_text_content()` does the
  formatting (falling back to the bare value on a bad/unknown template
  placeholder, and to "--" for a missing reading, same as a
  disconnected gauge); a bound element is the one case a text element
  is now redrawn every frame (`render_frame()`) rather than baked into
  the static background once, same dynamic/static split every other
  element type already has. A free-standing (unbound) text element is
  unaffected either way -- still baked in once, same as before.
- **Four built-in "photo" backgrounds** -- Aurora Glow, Deep Nebula,
  Synthwave Sunset, Bokeh Night -- alongside the existing flat-gradient/
  line-art background modes (grid/starfield/radial/solid), for people
  who want something that reads as an actual picture without having to
  go find and upload one themselves. Each is a real pre-rendered image
  (`assets/backgrounds/*.jpg`, built by the new one-off `scripts/
  generate_backgrounds.py` -- layered soft-edged color blobs, blur, and
  a starfield, all plain PIL/numpy math, no external image model
  involved) rather than another procedural draw, picked from the same
  "Style" dropdown as every other background and rendered through the
  exact same cover-fit-and-darken path a user's own uploaded "Custom
  image" already used (`dashboard_theme.BUNDLED_BACKGROUND_IMAGES` maps
  each to its shipped file, resolved via `paths.resource_path()`, same
  place `icon.ico` is found). The "Color scheme" picker hides itself
  for these four (and for "Custom image") in both the web canvas and
  the Tkinter GUI, same as it already did for a custom image -- a photo
  isn't tinted. Bundled into the PyInstaller build the same way
  `icon.ico` is (`packaging/hongtai_screen.spec`'s `datas`).
- **Built-in dashboard presets are no longer copied into
  `app_config.json`.** They used to be: a fresh install's `dashboard.
  presets` got the 6 built-ins seeded into it once
  (`seed_builtin_dashboard_presets()`), and from that point on they
  were just ordinary saved data -- permanently frozen at whatever they
  looked like the moment they were copied in, since app_config.json is
  a person's own data and nothing should quietly rewrite it. That
  meant an app update that improved a built-in, or added a new one,
  would never reach an install that had already launched once.
  `config_store.resolve_dashboard_presets()` now merges `dashboard_
  theme.BUILTIN_DASHBOARD_PRESETS` (code, evaluated fresh on every
  read) with whatever's actually saved under `dashboard.presets` --
  the saved side wins on a name collision, which is how saving over a
  built-in's own name customizes it. Deleting a pure built-in (one
  with no saved override) records its name in a new `dashboard.
  dismissed_builtin_presets` list instead, since there was never a
  copy in `presets` to actually remove -- deleting stays a real,
  permanent choice either way. A one-time migration,
  `migrate_strip_redundant_builtin_presets()`, cleans up an existing
  install's old seeded copies -- removing a `presets` entry only when
  its name AND content still exactly match a current built-in (an
  untouched copy); an entry a person actually edited under a built-in's
  name is left alone, since that's a real customization, not a stale
  copy.
- **4 more built-in presets that showcase the bundled photo
  backgrounds** -- Northern Lights (aurora), Deep Space (nebula),
  Outrun Drive (synthwave), City Nights (bokeh) -- added alongside the
  original 6, which all used a procedural background, so a fresh
  install's picker also demonstrates the photo backgrounds without
  anyone having to build a layout for one from scratch. Each is
  deliberately sparser than the original 6 (a photo background is
  already visually busy on its own); Northern Lights and City Nights
  also use a stat-bound text element instead of only gauges/bars, as a
  worked example of that. 10 built-ins total now.
- **A "Duplicate" button on every preset card**, next to Delete --
  works identically on a person's own saved preset or one of the app's
  built-ins, since the picker already reads both through the same
  merged view (`config_store.resolve_dashboard_presets()`). Saves an
  exact copy of that preset's elements and background under a new name
  (`"<name> (copy)"`, or `"(copy 2)"`, `"(copy 3)"`, ... if that name's
  already taken) -- an ordinary save, nothing built-in-specific about
  it. The obvious way to build a variant of an existing preset (built-
  in or not) without editing the original out from under yourself.
- **Fixed: a stat-bound (or any) text element's design-canvas mockup
  double-rendering over the panel's real live frame**, showing up as
  garbled overlapping text -- reported against "Outrun Drive"'s
  network reading, seen as "NENET.GM/s" instead of a clean "NET
  12.4M/s". Every text element (custom or stat-bound) is already baked
  into the live frame once the panel's connected -- `build_static_
  background()`/`render_frame()` on the backend draw it there -- so
  the SVG editor drawing its own copy of that same text on top,
  unconditionally, put two renderings of the same string at the same
  spot: identical but slightly different font rendering for custom
  text (a visible ghost/blur), and a genuinely different string for
  stat-bound text (its fixed "--" placeholder vs. the real live
  value), which is what produced the garbled smear. Every other
  element type (box/image/media/weather, the clock) already had a
  `showMockup`/`showClockMockup` gate for exactly this -- hide the SVG
  copy once the real live frame is already showing it, and only draw
  the SVG version when it's actually needed (selected for editing, not
  yet connected, or a layout-wide "dirty, nothing selected" edit) --
  text elements just never got the same gate when they were built.
  Nothing about the underlying stream was broken; this was purely the
  design canvas's own overlay.
- **Fixed: the preset card's Duplicate/Delete buttons crowding out the
  preset's own name**, truncating it to "N...", "B...", etc. on
  anything but a very short name. Both actions are now behind a single
  "..." button in the card footer (opens a small menu with Duplicate
  and Delete, closes on an outside click or once an action completes),
  so the name gets the footer's width back.
- **Fixed: that new "..." menu's popover getting clipped off** --
  visible starting to open but cut off mid-button, reported against
  the "Deep Space" card. `.preset-card` had `overflow: hidden` (to
  round the thumbnail image's top corners to match the card), which
  also clipped the popover since it's deliberately positioned outside
  the footer's own box to float over the thumbnail above it. Moved
  that clipping onto `.preset-card-thumb` itself (the only part that
  actually needs rounded corners) and dropped `overflow: hidden` from
  the card, so the popover is free to render fully.
- **Fixed: loading a preset showed a broken-looking mashup instead of
  a clean preview** -- the new layout's mockups drawn on top of
  whatever the *previous* theme still looked like on the physical
  panel, since nothing had actually been pushed there yet. The design
  canvas's live-panel photo now only updates from the real panel once
  something's actually been applied; right after loading a preset it
  shows that preset's own pre-rendered thumbnail instead (the same
  accurate image the picker card itself uses, layout and background
  together) until an edit, Save, or Preview makes something else the
  accurate view again. Also fixed the underlying reason a preset's
  background never showed up on the actual panel from "Preview on
  screen": that button only ever sent the edited *elements*, never the
  background, even when one had just been staged from a loaded preset
  (or edited by hand) -- `preview_dashboard_elements()` now takes an
  optional `background` too and reverts both together when the preview
  window ends, and `save_dashboard_background()` cancels a pending
  preview revert the same way `save_dashboard_elements()` already did,
  so a Save landing mid-preview can't get stomped back afterward.
- **The preset-load preview above now genuinely moves instead of being
  a frozen picture.** The static thumbnail fixed the broken-mashup
  look, but a needle that never moves and a live network reading stuck
  at one number reads as its own kind of "this is broken". A new
  `render_live_preview()` (`dashboard_theme.py`) renders the current
  design with this machine's actual live stats (same psutil/
  SystemInfos.exe/pynvml/winsdk calls the real render loop makes) each
  time it's called; the design canvas polls a new `/api/dashboard/
  live_preview` endpoint on an interval (~1.2s) whenever there's an
  unsaved edit the real live panel photo doesn't reflect yet -- a
  loaded preset, a drag, a property change, all the same cases the
  static thumbnail covered, just kept fresh now instead of rendered
  once. Skips polling during an active "Preview on screen" countdown,
  since the real panel is already showing this exact design for real at
  that point. Verified: loading a preset shows genuinely different
  numbers (this sandbox's own real CPU/RAM/network readings, not fixed
  placeholder values) across repeated polls, and Save layout correctly
  stops the polling and hands back to the real live frame.
- **A new built-in preset, "Circuit Bloom"** — requested by name: ship
  a user's own saved "my preset" layout (an 8-gauge full-stats board:
  CPU load/RAM/GPU load/network in the four corners, GPU temp/CPU freq
  as two smaller side gauges, disk/VRAM as two mini gauges along the
  bottom) together with the magenta-to-teal circuit-board photo they'd
  set as their background, as an 11th built-in so it ships with every
  install instead of staying local to their one machine. The photo
  (`75e1317d_background.png`, a custom upload, not one of the existing
  4 bundled backgrounds) was center-cropped and resized to the
  standard 1920×960 JPEG-quality-90 convention the other bundled
  backgrounds already use (`assets/backgrounds/circuit_bloom.jpg`),
  and added as a new `"circuit"` entry in both `BUNDLED_BACKGROUND_
  IMAGES` and `BACKGROUND_PRESETS` — no renderer changes needed, same
  cover-fit + darken code path every bundled/uploaded photo background
  already goes through. Every gauge is a verbatim copy of the saved
  layout's own ids/stats/positions/radii/z-order; none of them set an
  explicit color, so they pick up the standard left-half/right-half
  cyan/magenta accent split, same as in the original. Already matched
  by the existing `assets/backgrounds/*.jpg` glob in `packaging/
  hongtai_screen.spec`, so no packaging changes needed either.
  Verified: renders correctly standalone (`render_live_preview()`),
  appears as the 11th card in the preset picker with a correct
  thumbnail, and loads cleanly (no mashup, no clipping) showing all 8
  gauges live over the full background.
- **Two more built-ins, "Cherry Blossom" and "Petal Dream"** —
  requested as a pair: "two app presets, that uses text field stats
  ... something girly", modeled after a photo of a pastel floral
  fan-controller readout (a title, soft corner flourishes, CPU/GPU
  stats laid out as plain label+value text rows, no gauges at all).
  Two things from that reference photo don't carry over as-is: its
  specific artwork (an illustrated character) isn't reproduced, and
  neither is its exact field list -- "CPU Temp"/"CPU Power"/"GPU Freq"
  have no matching `STAT_DEFS` entry in this app (no CPU-side temp or
  power sensor reading, no GPU frequency one wired up), so each preset
  draws from whichever of the 14 real stats fit its column instead.
  What *does* carry over is the style: both are the first two built-
  ins with zero gauges -- every stat is a `"stat"` + `"template"` text
  row (the mechanism Northern Lights/City Nights introduced), arranged
  in two labeled columns over a new pastel floral photo background.
  Two new bundled backgrounds (`assets/backgrounds/cherry_blossom.jpg`,
  `lavender_bloom.jpg`, both 1920×960 JPEG quality 90, same convention
  as the other bundled photos) were generated with the same plain-PIL
  layered-blob technique `scripts/generate_backgrounds.py` already
  used for aurora/nebula/synthwave/bokeh -- new `_draw_petal_flower()`
  helper composites several soft-edged ellipse "petals" in a ring
  around a brighter center via the existing `soft_blob()` primitive,
  scattered across a rose or lilac gradient -- original generated art,
  not a copy of the reference photo's own illustration. Registered as
  `"cherry"`/`"lavender"` entries in `BUNDLED_BACKGROUND_IMAGES`/
  `BACKGROUND_PRESETS`, same as every other bundled photo -- no
  renderer or packaging changes needed. "Cherry Blossom": CPU column
  (Load/Freq/Peak/RAM) and GPU column (Load/Temp/Power/VRAM) in a soft
  pink-on-rose palette. "Petal Dream": "System" column (Load/RAM/Disk/
  Swap) and "Graphics" column (Load/Temp/Power/VRAM) plus a centered
  Network reading, in a lilac-on-steel-blue palette -- deliberately a
  different stat selection than Cherry Blossom so the two don't read
  as re-skins of the same field list. 13 built-ins total now. Verified:
  both render correctly standalone; the real `AppController.
  dashboard_meta()` surfaces both as distinct picker cards with correct
  thumbnails; loading either in a headless browser shows every label/
  value row and the clock+date live and in place, no overlap or
  clipping.
- **Fixed: Save layout not applying a loaded preset's background** —
  reported directly: "pressing a preset, and pressing save layout
  doesn't update the background". Save layout only ever called
  `saveDashboardElements()`; a preset's own background sat in
  `bgDraft` until a *second*, separate "Save background" click further
  down the page, which nothing in the UI actually made clear was
  still required. Save layout now persists both together in one
  action whenever a background is staged and differs from what's
  saved (a new `savedBackgroundRef`, mirroring the existing
  `savedElementsRef`, plus a `backgroundsEqual()` structural-compare
  helper since `bgDraft` is a fresh object on every edit, not a stable
  reference the way `elements` is). `dirty` and the live-preview
  polling effect both now cover a background-only change too, not
  just an elements change -- a background-only edit used to silently
  need its own separate Save click with no visual "you have unsaved
  changes" cue at all. "Save background" stays as its own button, for
  a background-only edit made directly in the Background section
  without touching the canvas. Verified: loading a preset and clicking
  Save layout now fires both `/api/dashboard/elements` and
  `/api/dashboard/background` with the preset's own background.
- **Cherry Blossom/Petal Dream redesigned** — direct follow-up
  feedback on the pair added just above: "the background integrate
  into the design, that's a sophisticated theme, yours isn't, it's a
  simple background on text on top of it". Fair complaint against the
  reference photo -- v1's generated backgrounds were a blurred scatter
  of flower blobs sitting *behind* independently-placed text, not a
  design the text was actually part of. Rewrote both generators
  (`scripts/generate_backgrounds.py`) with real line art positioned
  using the *same x/y fractions* as the matching preset's own text
  elements, not random placement: a proper ribbon/banner shape (a
  closed polygon with V-notch ends) sits directly behind the title,
  thin rule lines underline each column header, a vertical divider
  separates the two stat columns, ornate nested-arc corner swirls
  frame the whole card, and a small 3-flower bouquet with leaves
  anchors the empty gap between the columns -- standing in for the
  reference photo's portrait as the composition's visual focal point,
  the way v1 had nothing playing that role at all. The divider now
  stops short of the bouquet instead of drawing straight through it,
  and the loose flower scatter is confined to the margins outside the
  text columns instead of drifting across the stat rows themselves.
  Verified: `render_live_preview()` on both shows the ribbon-framed
  title, ruled column headers, corner swirls, and the bouquet sitting
  cleanly in the gap with no divider line cutting through it.
- **Every built-in preset's title is gone.** Requested directly, and
  right: a theme spending its best real estate writing its own name
  ("NORTHERN LIGHTS", "DEEP SPACE", "SYSTEM MONITOR", "Doing great
  today!", ...) is the one thing none of the commercial LCD themes
  people actually run does. 15 title/greeting elements removed across
  11 presets. Stat captions ("CPU FREQ", "BRAIN USAGE") stay -- those
  label a reading, they aren't a banner.
- **Bundled display fonts, and a per-element font picker.** Until now
  this theme could only draw in whatever generic UI sans the OS had
  (DejaVu Sans on Linux, Arial on Windows), which is most of why every
  preset read as "system readout" no matter how its colors or layout
  were arranged -- typography is where the look of the reference
  themes actually lives. Six families now ship with the app under
  `assets/fonts/` (Poppins, Orbitron, Chakra Petch, Bebas Neue, Anton,
  Archivo Black -- all SIL OFL 1.1, see `assets/fonts/LICENSES.md`),
  registered in `dashboard_theme.FONT_FAMILIES`, selectable per text
  and clock element via a new "Font" picker in the property panel, and
  bundled into frozen builds by `packaging/hongtai_screen.spec`. An
  element with no font set resolves to exactly the old system-font
  list, so nothing that already exists re-renders differently.
- **Label chips for text elements.** A new `plate` color (plus
  `plate_radius`/`plate_pad`) draws a filled rounded tag behind a text
  element, auto-sized to the string -- the "TEMP" / "USAGE" / "POWER"
  label-tag look the reference themes use everywhere. Sized from the
  measured text rather than a fixed box, so a chip always fits its
  label at any font or size. Exposed as a "Label chip" control.
- **Themeable panel frame, meter tracks, and title toggles.** The
  panel border was a fixed purple rounded rect drawn on *every* theme,
  which fought any palette that wasn't purple-ish; `background
  ["border"]` now recolors it or turns it off. `background["dim"]`
  controls how far a background photo is blended toward black before
  anything is drawn on it (still 0.45 by default, which is what an
  arbitrary photo needs; the new card-based themes ship 0 since
  they're drawn at final contrast already). Bars take a `track_color`
  so a light theme's empty meter reads as a pale channel instead of a
  near-black slab. Gauge, bar and graph elements gained `show_title`
  (and bar `show_value`, graph `show_frame`) so a layout can label and
  frame things in its own typography instead of getting a second,
  differently-styled label drawn on top -- all exposed as checkboxes.
- **Three new flagship presets, and the floral pair rebuilt to match**
  — the rest of the same feedback ("we need the super hero fire
  spitter"), measured against the commercial LCD themes the user had
  in hand. All five are built the same way: the cards/panels each stat
  block sits in are drawn into the background art at the *same*
  fraction-of-panel coordinates as the elements themselves (see
  `scripts/generate_backgrounds.py`), the type is set in the bundled
  display faces, labels sit in chips, and meters carry the theme's
  gradient.
  - **Fusion Core** — dark card dashboard: a teal→violet→magenta→amber
    sweep behind matte panels with accent edges, rings with their
    readings inside, slim gradient meters, and history graphs drawn
    into recessed wells. 37 elements.
  - **Neon Pulse** — acid yellow / magenta / cyan, hard diagonals,
    halftone, glitch slivers, Anton headers and chip labels.
  - **Crimson Strike** — red / black / bone, comic halftone, torn
    white slash, outlined readout panels.
  - **Cherry Blossom / Petal Dream** — rebuilt on the same card system
    in a light register (the only light themes in the set): two
    translucent cards, chip labels, pale meter tracks, floral
    scrollwork and bouquets in the margins. Their old title banner is
    gone with the titles it framed.
  Every character, logo and wordmark from the reference images was
  left out -- what's taken is the composition and palette language,
  drawn from scratch.
  Verified: all 16 presets render correctly through
  `render_live_preview()` and were reviewed as images; the design
  canvas shows 16 picker cards; loading a flagship preset and clicking
  Save layout persists both its elements and its background (including
  the new border/dim keys); the new Font and Label chip controls
  render for a selected text element; no console errors.
- **Built-in presets are read-only now; editing one saves a copy.**
  Requested directly: "default themes shouldn't be modified, like if a
  user tries to modify them, we create a duplicate of it where the
  user does his modifications". Saving a preset under a built-in's
  exact name used to overwrite it — the saved copy shadowed the
  code-defined one from then on, which is a bad bargain in both
  directions: the only way to tweak a built-in also destroyed your
  access to the original (nothing could bring it back), and it froze
  that slot against any future app update to it. `save_dashboard_
  preset()` now detects the collision and saves under a free derived
  name instead ("Fusion Core" → "Fusion Core (custom)", then
  "(custom 2)", …), returning the name it actually used so the UI
  reports what happened rather than claiming a save that landed
  somewhere else. Saving over one of *your own* presets is unchanged —
  it still overwrites in place, which is what editing your own work
  should do.
  - A one-time migration (`migrate_unshadow_builtin_presets()`)
    renames any existing saved preset that shadows a built-in aside to
    "<name> (custom)", so an install that had already customized one
    keeps that work *and* gets the original back. It runs after the
    existing strip-redundant-copies migration, so what it sees is
    always a real customization rather than a stale duplicate.
  - Deleting a built-in is now the only thing that can be done *to*
    one, so it got an undo: the button reads "Hide" for a built-in,
    and a "Restore built-ins" button appears while any are hidden
    (`restore_dismissed_dashboard_presets()`, `POST /api/dashboard/
    presets/restore_builtins`). It used to have an accidental undo —
    saving anything under that name un-dismissed it — which this
    change would otherwise have quietly removed.
  - The picker marks every built-in with a small ◆ and warns *before*
    you save, as soon as the name you've typed matches one.
  Verified in the browser harness: the warning appears while typing a
  built-in's name; saving it leaves the built-in in place and creates
  "Fusion Core (custom)", then "(custom 2)" on a second save; saving
  over a user's own preset still overwrites it rather than piling up
  copies; hiding a built-in removes it from the picker and offers the
  restore, which brings it back; no console errors.
- **Fixed: graphs highlighting themselves in the design canvas.**
  Reported with a screenshot of "Neon Horizon": the network graph sat
  under a bright gradient-filled box with "NETWORK" written across it,
  while nothing was selected. That box was the editor's own mockup
  overlay, not the render. Every other element type hides its mockup
  once the canvas is showing an accurate backdrop (the panel's live
  frame, or the live-rendered preview of an unsaved edit) — graphs
  were exempt, on the reasoning that the SVG overlay can't plot the
  line so the box was the only thing making the region locatable. Both
  halves of that were wrong: the backdrop draws the graph *in full*
  (frame, title and line), so there was nothing to locate that wasn't
  already drawn, and the mockup is a filled box with the element's
  name across it rather than a faint outline, so forcing it over the
  real render read exactly as reported — a graph lighting itself up
  for no reason. Graphs now follow the same rule as everything else.
  Dragging is unaffected: the transparent hit rect that stands in for
  a hidden mockup already covered that. Verified in all four states —
  deselected over a live frame (hidden), selected (shown), deselected
  again (hidden), and with the panel disconnected so there's no
  backdrop to defer to (shown) — plus that the graph is still
  draggable while its mockup is hidden.
- **A "+ New theme" button**, next to the Presets label. Requested:
  "add a button for creating a new custom theme, we only have
  duplicate now" — which was exactly right. Every route to a new
  preset started from an existing one: Duplicate copies a card, and
  "Save current layout as preset" bottles up whatever happens to be on
  the canvas, so building something of your own meant first taking
  someone else's layout apart element by element. This saves an empty
  preset (auto-named "New theme", "New theme 2", …), drops the canvas
  straight into editing it, and prefills the save box with its name so
  "Save as preset" updates that same card instead of spawning another.
  Deliberately empty rather than seeded with the default gauges —
  "Reset to defaults" already puts that layout on the canvas, so
  seeding it here would just be a second, worse Duplicate — though it
  does keep whatever background is currently staged, since starting on
  a bare black rectangle is a strange kind of blank. Verified: the
  button creates the card without disturbing the other presets, the
  canvas comes up with no elements, adding one and saving under the
  prefilled name updates that card rather than making a second, a
  second click produces "New theme 2", and an empty theme still
  renders a real (background-only) thumbnail instead of a "No preview"
  card.
- **Import and export themes.** Prompted by a theme a friend had made
  and shared as a .json — which, before this, could only be installed
  by hand-editing `app_config.json` and copying its background picture
  into the managed image folder manually. Both directions now exist:
  "Export" on any preset card's ⋯ menu saves it as a .json, and
  "Import theme" next to "+ New theme" takes one back.
  - The hard part isn't the layout — a preset is already plain JSON —
    it's the pictures. Any image a preset references (background,
    image element, clock face) is stored as an absolute path into
    *this* machine's managed image folder, which means nothing on
    anyone else's. Export therefore inlines each referenced image as
    base64 and drops the path; import writes those bytes into the
    receiving machine's own image store and repoints the preset at the
    new copies. The exported file is self-contained.
  - **Import doesn't trust a path it's given.** A preset file comes
    from someone else, and a bare `image_path` in one would otherwise
    let it aim the panel at any file on the receiving disk. Only
    images that actually travelled with the file, or paths already
    inside this machine's image store, survive; anything else is
    dropped to None, which every renderer here already treats as "no
    image". Shape is validated too (`elements` must be a list,
    `background` an object), so a malformed file reports an error
    instead of half-loading.
  - Importing under a built-in's name lands as a copy, same as any
    other save. Imported themes are named from the filename, tidied
    up: `nocturne_cathedral.json` becomes "Nocturne Cathedral", while
    a name that already has capitals is left exactly as sent rather
    than being mangled into "Gpu Monitor".
  - Export strips the 8-character content-hash prefix `image_store`
    adds to a stored copy before sending the name along — otherwise
    every export/import round trip stacked another prefix
    (`ab12_ab12_shot.png`) and stored a second identical copy instead
    of deduplicating onto the first. Verified stable across three
    round trips, one file on disk.
  Verified end to end: exporting a preset downloads a .json with its
  images inlined, re-importing that file restores it, a real
  friend-made theme file imports cleanly, and malformed JSON and
  wrong-shaped JSON each surface a specific error rather than failing
  silently.
- **Fixed: CPU Clock stuck on one number.** Reported as "always
  showing as 3.4G which isn't accurate" — and it wasn't a rounding or
  smoothing problem, the reading never contained the live clock at
  all. `get_cpu_freq_ghz()` was `psutil.cpu_freq().current`, which on
  Windows comes from `CallNtPowerInformation(ProcessorInformation)`;
  on modern machines Windows reports the *nominal* clock there, so the
  stat sat on the base frequency forever while the vendor app read
  5.5GHz on the same CPU at the same moment. It now reads the
  `\Processor Information(_Total)\% Processor Performance` performance
  counter (PDH, via ctypes — no new dependency) and multiplies it by
  the base clock, which is how Task Manager's own "Speed" field is
  derived and which goes above 100% when boosting. psutil remains the
  fallback, so Linux (where its `current` really is live) and any
  machine missing that counter are unaffected. Sampled once a second
  rather than per frame.
- **A new "Volume" stat**, available to every element type (gauge,
  bar, graph, stat-bound text) like any other. Reports the PC's master
  output level 0-100%, and 0 while muted — a gauge sitting at 40% with
  nothing audible would be the wrong answer to "what's my volume".
  Needs `pycaw`, which is an optional dependency: without it the stat
  reads "--" and nothing else changes. Install with
  `py -m pip install pycaw` (it's in requirements.txt now).
- **`scripts/check_sensors.py`**, a read-only diagnostic for exactly
  the two readings above, since neither can be verified anywhere but
  the Windows machine the panel is on. Prints psutil's clock, the perf
  counter, and the resulting frequency five times a second apart (to
  compare against Task Manager), plus whether pycaw is present and
  whether the volume reading follows the slider.

## [1.0.0] — 2026-08-29

First tagged release. Everything below shipped before this tag existed
as a version number, so it's grouped here as the 1.0.0 baseline rather
than split into artificial pre-releases.

### Added
- From-scratch protocol driver (`hongtai_screen.py`) for the XTRM Lab
  6.2" panel (Hongtai Technology controller), reverse-engineered from
  the vendor app's own JavaScript. See `FINDINGS.md` for the protocol
  reference.
- Four themes: `demo_clock.py`, `video_theme.py`, `dashboard_theme.py`,
  `webpage_theme.py`.
- Desktop app (`app.py`, Tkinter) wrapping all four themes with a
  single Start/Stop/Apply workflow, saved settings
  (`app_config.json`), a live Log panel, single-instance guard, a
  system tray icon, launch-at-startup, and a windowless launcher
  (`make_launcher.py` / `Launch Hongtai Screen.vbs`).
- Dashboard theme: 8 independently assignable gauge slots covering 14
  live stats (CPU/GPU load, peak-core load, CPU freq, GPU temp/power,
  RAM, swap, VRAM, disk usage/activity, network, process count,
  battery), each degrading gracefully if its data source is
  unavailable.
- Dashboard background customization: 5 styles (default hex-grid,
  grid, starfield, radial, solid) × 5 color schemes, or a custom
  uploaded photo.
- Dashboard tab: two-column gauge-slot layout, dynamic window
  autosizing (so the Log panel is never hidden), and an
  **Apply (restart)** button that reloads the running theme in one
  click instead of a manual Stop then Start.
- Live web mirror: watch the panel from a phone/laptop on the same
  network, with visible connect/disconnect/error logging routed into
  the app's own Log panel (previously silent when launched without a
  console), and an **Open in browser** button once it's live.
- Auto-recovery from the panel freezing (`blind_restart`), independent
  of a full power cycle.

### Fixed
- Web mirror leaked its listening socket on Stop (`server_close()` was
  never called), causing "port already in use" on the next Start.
- Web mirror diagnostics used bare `print()`, which is silently
  discarded under a windowless (`pythonw.exe`) launch — you could
  never see a real bind error. Now routed through the same logger the
  GUI displays.
- Web mirror could keep serving a frozen frame from a dead server
  instance after a restart, because `HTTP/1.1` keep-alive connections
  outlive `shutdown()`/`server_close()`. Forced to `HTTP/1.0` so every
  poll opens a fresh connection to whichever server is actually
  listening.
- Crash-on-launch with no visible error message when a file delivery
  was out of sync with the running app (windowless launches swallow
  uncaught exceptions with no console to show them).

### Removed
- `aio_probe.py`, an experimental probe from an earlier, since
  abandoned line of investigation into the vendor app's AIO/fan
  control panel. Not part of the released feature set.
