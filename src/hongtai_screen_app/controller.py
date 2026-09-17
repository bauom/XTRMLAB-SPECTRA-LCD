"""
controller.py -- AppController: the same start/stop/apply/config logic
the Tkinter App class drives (see app.py), with no Tkinter dependency
at all. Built for control_server.py's HTTP API (ROADMAP.md Phase 2),
and the shape the eventual webview UI's backend is meant to run on.

The Tkinter app is NOT wired to this yet -- it keeps its own inline
copy of this logic for now (see theme_kwargs.py's docstring for why),
including its own ThemeWorker-based start/stop. This class is
additive: a second, independent way to drive the same underlying
modules (config_store, theme_kwargs, the driver), fully headless and
fully testable without a display -- and, unlike the Tkinter app, built
on ScreenEngine (screen_engine.py) instead of ThemeWorker, so switching
themes here reuses one persistent connection instead of reconnecting.
"""
import io
import json
import os
import queue
import re
import sys
import threading
from collections import deque

import base64

from . import config_store
from . import desktop_shortcut
from . import image_store
from . import startup_registration
from . import theme_kwargs
from . import weather
from . import power_state
from .driver import hongtai_screen
from .screen_engine import ScreenEngine
from .themes import dashboard_theme


class AppController:
    """One process's worth of "what's running, what's configured, what
    just got logged" -- everything a UI (HTTP API today, a webview
    frontend eventually) needs to drive the app without any of them
    touching a ScreenEngine or the driver directly.

    Thread-safety: every public method takes `_lock` internally, so
    this is safe to call concurrently from multiple HTTP handler
    threads (ThreadingHTTPServer spins one thread per connection).
    """

    LOG_HISTORY = 400          # lines an SSE client catches up on when it connects

    def __init__(self):
        self.cfg = config_store.load_config()
        migrated = config_store.migrate_dashboard_elements(self.cfg)
        migrated = config_store.migrate_dashboard_weather_element(self.cfg) or migrated
        migrated = config_store.migrate_strip_redundant_builtin_presets(self.cfg) or migrated
        # Runs after the strip above: that one removes untouched copies
        # of built-ins, so whatever's still colliding by the time this
        # runs is a real customization to rename aside rather than drop.
        migrated = config_store.migrate_unshadow_builtin_presets(self.cfg) or migrated
        migrated = config_store.migrate_dashboard_preset_shape(self.cfg) or migrated
        if migrated:
            config_store.save_config(self.cfg)
        self._lock = threading.RLock()
        power_state.set_keep_active_when_locked(self.cfg.get("keep_active_when_locked", True))
        power_state.start_polling()
        self.running_theme = None      # display name, e.g. "Dashboard"
        self.active_screen = None      # set via on_connected once connect() succeeds
        self._log_history = deque(maxlen=self.LOG_HISTORY)
        self._log_subscribers = []     # list of queue.Queue, one per SSE client
        self._closed = False
        # The running threading.Timer for an in-progress dashboard
        # preview (see preview_dashboard_elements()), or None -- tracked
        # so a second preview (or a real Save) can cancel a still-
        # pending revert instead of leaving it to fire later and stomp
        # on whatever's showing by then.
        self._preview_revert_timer = None

        # One persistent connection for this controller's whole
        # lifetime (ROADMAP.md's live-theme-switching rewrite) --
        # switching themes reuses it instead of reconnecting; see
        # screen_engine.py's module docstring for the full reasoning.
        self.engine = ScreenEngine(
            log=self._log,
            on_connected=self._on_screen_connected,
            on_disconnected=self._on_screen_disconnected,
            on_finished=self._on_theme_finished,
        )

    # ------------------------------------------------------------------ #
    # logging / pub-sub -- ThemeWorker calls this exactly like the
    # Tkinter app's self._log does; SSE clients get every line pushed to
    # them live, plus the last LOG_HISTORY lines on first connect.
    # ------------------------------------------------------------------ #
    def _log(self, msg):
        msg = str(msg)
        with self._lock:
            self._log_history.append(msg)
            subs = list(self._log_subscribers)
        for q in subs:
            q.put(msg)

    def subscribe_log(self):
        q = queue.Queue()
        with self._lock:
            for line in self._log_history:
                q.put(line)
            self._log_subscribers.append(q)
        return q

    def unsubscribe_log(self, q):
        with self._lock:
            if q in self._log_subscribers:
                self._log_subscribers.remove(q)

    # ------------------------------------------------------------------ #
    # state / config
    # ------------------------------------------------------------------ #
    def state(self):
        with self._lock:
            screen = self.active_screen
            return {
                "running_theme": self.running_theme,
                "worker_alive": self.running_theme is not None,
                "port": self.cfg.get("port", config_store.AUTO_DETECT),
                "brightness": self.cfg.get("brightness", 90),
                "active_tab": self.cfg.get("active_tab", 0),
                "connected": screen is not None,
                "screen_info": self._screen_info(screen) if screen is not None else None,
            }

    @staticmethod
    def _screen_info(screen):
        info = getattr(screen, "info", None)
        if info is None:
            return None
        return {
            "width": info.width, "height": info.height,
            "version": info.version, "model": info.model, "uid": info.uid,
        }

    def get_config(self):
        with self._lock:
            return dict(self.cfg)

    def update_config(self, patch: dict):
        """Shallow-merges `patch` into the config and saves it -- same
        "last write wins, per top-level key" shape app_config.json
        already has (e.g. a `dashboard` patch replaces the whole
        `dashboard` sub-dict, matching how the Tkinter app's own
        _save_current_config() writes it)."""
        if not isinstance(patch, dict):
            raise ValueError("config patch must be a JSON object")
        with self._lock:
            self.cfg.update(patch)
            config_store.save_config(self.cfg)
            return dict(self.cfg)

    def set_brightness(self, value):
        """Brightness is special-cased instead of going through
        update_config(): app.py's own slider applies it *live*, with no
        restart, by calling screen.set_brightness() directly on the
        running HongtaiScreen the moment the slider moves (it's just a
        per-frame software dim -- see the driver's set_brightness()
        docstring -- so there's nothing to reconnect). A generic config
        patch only takes effect on the next Start/Apply, which would
        make the web UI's brightness slider feel broken by comparison
        (dial it down, nothing visibly happens until you restart the
        theme). This does both: persists the new value to
        app_config.json like any other setting, AND, if a screen is
        currently connected, pushes it to the panel immediately."""
        try:
            value = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            raise ValueError(f"brightness must be a number 0-100, got {value!r}")
        with self._lock:
            self.cfg["brightness"] = value
            config_store.save_config(self.cfg)
            screen = self.active_screen
        if screen is not None:
            try:
                screen.set_brightness(value)
            except Exception as e:  # noqa: BLE001 -- surfaced in the log either way
                self._log(f"(brightness change failed: {e})")
        return {"brightness": value}

    # ------------------------------------------------------------------ #
    # Windows integration -- "Launch at Windows startup" / desktop
    # shortcut. Thin wrappers around startup_registration.py/
    # desktop_shortcut.py (Phase 1's extraction already made these
    # Tkinter-independent); routed through here rather than called
    # directly from control_server.py so every controller action is
    # logged/handled the same way, and so a future caller other than
    # the HTTP API (a future in-process UI, say) gets the same surface.
    # ------------------------------------------------------------------ #
    def system_info(self, log=None):
        """`log`: pass self._log to have the schtasks /query this runs
        (via startup_registration.is_startup_enabled()) show up in the
        app's own Log panel -- left quiet (the default) for the plain
        polled GET /api/system calls the frontend makes on every page
        load, since logging every one of those would just be noise;
        set_startup() below passes it explicitly, since "did the
        checkbox's action actually take" is exactly the question being
        investigated right after toggling it."""
        with self._lock:
            keep_active = self.cfg.get("keep_active_when_locked", True)
        return {
            "platform": sys.platform,
            "startup_supported": sys.platform == "win32",
            "startup_enabled": startup_registration.is_startup_enabled(log=log or (lambda m: None)),
            "keep_active_when_locked": keep_active,
            "keep_active_supported": power_state.IS_WINDOWS,
        }

    def set_keep_active_when_locked(self, value):
        """Whether a running theme keeps pushing frames to the panel
        while Windows is locked (True, the default -- this app's
        original behavior, before this setting existed) or pauses and
        resumes automatically on unlock (False) -- see power_state.py's
        docstring, and the official XTRM Lab app's own "Keep playing
        when screen is off" setting this mirrors. Applies immediately,
        the same "no restart needed" deal as set_brightness() above,
        since every theme's render loop checks power_state.should_
        pause() itself on every frame rather than a decision baked in
        at Start time."""
        value = bool(value)
        with self._lock:
            self.cfg["keep_active_when_locked"] = value
            config_store.save_config(self.cfg)
        power_state.set_keep_active_when_locked(value)
        power_state.start_polling()
        return {"keep_active_when_locked": value}

    def set_startup(self, enabled):
        """Every step of this -- the exact command registered, the
        exact schtasks.exe invocation and its exit code/stdout/stderr,
        and the re-check right after -- goes through self._log(), so
        it's all sitting in the app's own Log panel afterward rather
        than only a one-line exception message (or, if this succeeds
        but the *effect* still doesn't seem to stick, nothing at all).
        Added after a report that toggling the checkbox "didn't work"
        with no visible error either way -- this makes it possible to
        actually tell apart "schtasks refused" (permissions, policy),
        "schtasks silently didn't do what was asked", and "it worked,
        but something's rendering the result wrong", by reading exactly
        what happened instead of guessing."""
        if enabled:
            startup_registration.enable_startup(log=self._log)
        else:
            startup_registration.disable_startup(log=self._log)
        info = self.system_info(log=self._log)
        self._log(f"(startup: now reports enabled={info['startup_enabled']})")
        return info

    def create_desktop_shortcut(self):
        """Raises on failure (non-Windows, no Desktop folder, cscript
        error) -- same as desktop_shortcut.create_desktop_shortcut()
        itself; control_server.py's do_POST already turns a RuntimeError
        into a 400 with the message intact."""
        return {"path": desktop_shortcut.create_desktop_shortcut()}

    def upload_dashboard_image(self, filename, data_b64):
        """Saves a browser-picked image (base64-encoded, since a
        browser file input can only hand back the file's *content*, not
        a real filesystem path the way Tkinter's Browse dialog can) into
        this app's own managed image folder (image_store.py) and
        returns its stored path -- the caller then saves THAT path
        through save_dashboard_background()/save_dashboard_now_playing()/
        save_dashboard_elements(), same as if it had been typed in
        directly. Raises ValueError on a missing/invalid `data_b64`, or
        whatever image_store.store_image_bytes() raises for a file
        that's too large or doesn't actually decode as an image."""
        if not data_b64:
            raise ValueError("no image data given")
        try:
            data = base64.b64decode(data_b64, validate=True)
        except Exception as e:  # noqa: BLE001 -- malformed base64
            raise ValueError(f"invalid image data: {e}")
        path = image_store.store_image_bytes(data, filename)
        return {"path": path}

    def read_dashboard_image(self, path):
        """Returns (bytes, path) for a previously-uploaded/picked image,
        so the web UI's canvas can show the actual picture it just
        stored -- not just its filename -- the moment it's picked,
        without needing Start/Apply or even Save. Only ever reads a
        path already under image_store.IMAGES_DIR (image_store.
        is_managed()): the canvas only ever hands this back a path IT
        was given by upload_dashboard_image()/Tkinter's Browse dialog in
        the first place, never anything the browser typed in itself, but
        this is still the one place a client-supplied filesystem path
        reaches disk, so it's checked regardless. Raises ValueError for
        anything outside that folder or that doesn't exist."""
        if not path or not image_store.is_managed(path):
            raise ValueError("not a managed image path")
        if not os.path.isfile(path):
            raise ValueError("image not found")
        with open(path, "rb") as f:
            return f.read(), path

    # ------------------------------------------------------------------ #
    # Dashboard design canvas (ROADMAP.md Phase 5) -- reading/writing
    # dashboard.elements (and named presets of it) gets its own small
    # surface instead of going through the generic update_config(),
    # because update_config()'s per-top-level-key merge would replace
    # the ENTIRE "dashboard" sub-dict on every save -- fine for Phase 3's
    # video/webpage forms (their own top-level keys), but saving just a
    # layout tweak through it would silently wipe out web_port/
    # enable_web/background/slots. These methods merge into the existing
    # "dashboard" dict instead, so the canvas can save a layout without
    # knowing or caring what else is in there.
    # ------------------------------------------------------------------ #
    def dashboard_meta(self):
        """Everything the design canvas needs to initialize itself: the
        layout it would actually render right now (resolved the same
        way dashboard_kwargs() resolves it, so the canvas can never
        drift from what Start/Apply actually renders), the built-in
        default layout to reset to, any saved named presets, and enough
        STAT_DEFS metadata to populate a per-gauge stat picker without
        the frontend needing to import anything from dashboard_theme.py
        itself."""
        with self._lock:
            cfg = dict(self.cfg)
        d = cfg.get("dashboard", {}) or {}
        # The built-ins (dashboard_theme.BUILTIN_DASHBOARD_PRESETS)
        # merged with whatever's actually saved in this config -- see
        # config_store.resolve_dashboard_presets()'s own docstring for
        # why they're not stored in app_config.json at all.
        presets = config_store.resolve_dashboard_presets(cfg)
        return {
            "elements": theme_kwargs.resolve_dashboard_elements(cfg),
            "defaults": dashboard_theme.DEFAULT_ELEMENTS,
            "presets": presets,
            "presetThumbnails": self._dashboard_preset_thumbnails(presets),
            # Which of those names are the app's own read-only presets,
            # and which built-ins are currently deleted -- the picker
            # marks the first as built-in (and warns that saving over
            # one saves a copy instead, see save_dashboard_preset()),
            # and offers to restore the second.
            "builtinPresets": list(dashboard_theme.BUILTIN_DASHBOARD_PRESETS),
            "dismissedBuiltinPresets": list(d.get("dismissed_builtin_presets") or []),
            "stats": {
                key: {"label": meta["label"], "title": meta["title"]}
                for key, meta in dashboard_theme.STAT_DEFS.items()
            },
            "background": dict(dashboard_theme.DEFAULT_BACKGROUND, **(d.get("background") or {})),
            "backgroundPresets": dict(dashboard_theme.BACKGROUND_PRESETS),
            "widgetStyles": dashboard_theme.widget_styles.STYLES,
            # Modes that are a photo, not a tinted procedural draw --
            # "image" (a user's own upload) plus every bundled one
            # (BUNDLED_BACKGROUND_IMAGES) -- so the frontend knows when
            # to hide the "Color scheme" picker (a photo isn't tinted)
            # without having to duplicate that key list itself.
            "backgroundImageModes": ["image", *dashboard_theme.BUNDLED_BACKGROUND_IMAGES.keys()],
            "backgroundSchemes": {
                key: {"label": scheme["label"]}
                for key, scheme in dashboard_theme.BACKGROUND_COLOR_SCHEMES.items()
            },
            # The display faces bundled with the app (dashboard_theme.
            # FONT_FAMILIES) for the text/clock property panel's font
            # picker -- same "let the backend own the list, the
            # frontend just renders it" shape as the stat/background/
            # scheme lookups above.
            "fontFamilies": {
                key: {"label": spec["label"]}
                for key, spec in dashboard_theme.FONT_FAMILIES.items()
            },
            "nowPlaying": {
                "default_art_path": d.get("default_art_path") or None,
                "not_playing_message": d.get("not_playing_message") or None,
                "default_message": dashboard_theme.DEFAULT_NOT_PLAYING_MESSAGE,
            },
            "clockFaces": dict(dashboard_theme.CLOCK_FACES),
            "clockAnalogStyles": dict(dashboard_theme.ANALOG_CLOCK_STYLES),
            "clockHourFormats": dict(dashboard_theme.DIGITAL_CLOCK_HOUR_FORMATS),
            # Weather's own location/units now live on its element (see
            # default_weather_element()) -- this is just the unit-name
            # dropdown's options, the same kind of small lookup table
            # clockFaces/clockAnalogStyles/clockHourFormats are for the
            # clock element's property panel.
            "weatherUnitOptions": dict(weather.UNIT_OPTIONS),
        }

    def save_dashboard_elements(self, elements):
        """Persists a new gauge layout and, if the dashboard theme is
        currently running, applies it live -- same "no Stop/Start
        needed" deal as save_dashboard_now_playing() (see
        dashboard_theme.set_pending_dashboard_layout()). It used to only
        take effect on the next Start/Apply, since the layout is baked
        into a static image once for performance (see
        build_static_background()'s docstring); the running render loop
        now rebakes with the new elements on its very next frame
        instead, which is also what fixed the design canvas visibly
        showing an element at its new position while the real panel
        kept showing it at the old one. Doesn't validate element shape
        beyond "is it a list" -- a malformed element just fails loudly
        inside dashboard_theme.py's own render path, same as a bad
        video path or URL does for those themes."""
        if not isinstance(elements, list):
            raise ValueError("elements must be a list")
        with self._lock:
            # A real Save makes `elements` the new source of truth, so
            # any still-pending preview revert (see
            # preview_dashboard_elements()) would otherwise fire later
            # and stomp this save back to whatever was saved *before*
            # it, undoing it from underneath the user a few seconds
            # after they saved.
            if self._preview_revert_timer is not None:
                self._preview_revert_timer.cancel()
                self._preview_revert_timer = None
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            dashboard_cfg["elements"] = elements
            self.cfg["dashboard"] = dashboard_cfg
            config_store.save_config(self.cfg)
            dashboard_theme.set_pending_dashboard_layout(elements=elements)
            # A `weather` element's location/units (see
            # default_weather_element()) are the one per-element field
            # that isn't purely cosmetic -- they drive weather.py's
            # shared background poll, so this needs its own live-apply
            # here too, same "no Stop/Start" deal as the layout/
            # background themselves. dashboard_theme.run()'s pending-
            # layout loop does the same thing for a layout edit made
            # while a different config-writer (app.py) is what's
            # actually driving the running theme.
            dashboard_theme.apply_weather_from_elements(elements)
            return dict(self.cfg["dashboard"])

    def preview_dashboard_elements(self, elements, background=None, duration=5.0):
        """Shows `elements` (and, if given, `background`) live on the
        running dashboard theme for `duration` seconds, then reverts
        both to whatever's actually saved -- the design canvas's
        "Preview on screen" button, for trying an edit on the real panel
        without committing to it the way Save layout/Save background
        does. Reuses the exact same live-apply path
        save_dashboard_elements()/save_dashboard_background() use
        (set_pending_dashboard_layout(), picked up by the render loop's
        next frame), just without ever touching self.cfg or
        config_store -- so if nothing else happens, the running theme
        quietly goes back to the last real Save on its own, and a page
        reload (which reads self.cfg, never the pending layout) was
        never showing anything different in the first place.

        `background` used to not be a parameter at all -- previewing
        only ever pushed elements, so previewing a freshly-loaded preset
        (or any unsaved background edit) showed the new layout over
        whatever background was still actually saved, not the one being
        tried. It's optional (None) rather than required because the
        design canvas also uses this for an elements-only preview (e.g.
        just nudging a gauge) where re-sending an unchanged background
        would be harmless but pointless.

        A no-op-looking call when the dashboard theme isn't actually
        running is intentional, not an error: set_pending_dashboard_layout()
        already handles "nothing's listening for this right now" by just
        queuing it, and there being no live panel to preview against
        isn't something the caller needs to special-case here -- the
        design canvas itself is what decides whether to offer this
        button based on whether the dashboard's running.

        A second preview call (or a real Save) before the timer fires
        cancels/replaces the pending revert rather than letting both
        timers eventually fire -- otherwise an earlier preview's revert
        could land after a *later* preview or a real save and stomp
        either one back to a stale "saved" snapshot taken before it."""
        if not isinstance(elements, list):
            raise ValueError("elements must be a list")
        if background is not None and not isinstance(background, dict):
            raise ValueError("background must be an object")
        with self._lock:
            if self._preview_revert_timer is not None:
                self._preview_revert_timer.cancel()
                self._preview_revert_timer = None
            dashboard_theme.set_pending_dashboard_layout(elements=elements, background=background)
            # Snapshotted now (not re-read from self.cfg inside the
            # timer callback) so a Save that lands *during* the preview
            # window still reverts to what was saved before THIS
            # preview started, not whatever the save changed it to --
            # save_dashboard_elements()/save_dashboard_background()
            # already cancel this timer outright in that case, but
            # keeping the snapshot self-contained means this method's
            # behavior doesn't depend on that ordering to stay correct.
            saved_elements = list(
                (self.cfg.get("dashboard") or {}).get("elements") or dashboard_theme.DEFAULT_ELEMENTS
            )
            # Only snapshotted (and only reverted) when a background was
            # actually previewed -- an elements-only preview has no
            # reason to touch the background either on the way in or
            # the way back out.
            saved_background = (
                dict((self.cfg.get("dashboard") or {}).get("background") or {})
                if background is not None
                else None
            )
            timer = threading.Timer(
                max(0.5, float(duration)),
                self._revert_dashboard_preview,
                args=(saved_elements, saved_background),
            )
            timer.daemon = True
            self._preview_revert_timer = timer
            timer.start()

    def _revert_dashboard_preview(self, saved_elements, saved_background=None):
        """Timer callback for preview_dashboard_elements() above --
        hands the running theme back whatever was actually saved before
        the preview started (elements always; background too, but only
        when this preview actually touched it -- see
        preview_dashboard_elements()'s own comment on `saved_background`
        being None otherwise). Clears self._preview_revert_timer first
        so a save/preview racing this exact moment doesn't cancel a
        timer object that's already done firing (Timer.cancel() on an
        already-fired timer is harmless, but leaving the stale reference
        around would make a later check think a revert is still pending
        when it's not)."""
        with self._lock:
            self._preview_revert_timer = None
            dashboard_theme.set_pending_dashboard_layout(elements=saved_elements, background=saved_background)

    def save_dashboard_background(self, background):
        """Persists the panel background (preset mode, color scheme,
        and/or custom image path) -- same merge-into-"dashboard" shape
        and same live-apply behavior as save_dashboard_elements() just
        above (it's baked into the same static image, so it rebakes
        alongside any pending elements change on the running theme's
        next frame). Doesn't validate image_path exists or mode/scheme
        are known keys -- dashboard_theme.py already falls back to the
        default background silently if the image can't be opened or a
        key is unrecognized, same tolerance app.py's own Tkinter picker
        has always relied on."""
        if not isinstance(background, dict):
            raise ValueError("background must be an object")
        with self._lock:
            # Same reasoning as save_dashboard_elements()'s own timer
            # cancel: a real Save here makes `background` the new source
            # of truth, so a still-pending preview revert (see
            # preview_dashboard_elements()) firing later would otherwise
            # stomp this save back to whatever was saved *before* it.
            if self._preview_revert_timer is not None:
                self._preview_revert_timer.cancel()
                self._preview_revert_timer = None
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            existing = dict(dashboard_cfg.get("background") or {})
            existing.update(background)
            dashboard_cfg["background"] = existing
            self.cfg["dashboard"] = dashboard_cfg
            config_store.save_config(self.cfg)
            dashboard_theme.set_pending_dashboard_layout(background=existing)
            return dict(self.cfg["dashboard"]["background"])

    def save_dashboard_now_playing(self, patch):
        """Persists the "nothing playing" placeholder settings -- the
        default album-art image and/or the message shown in place of a
        track title -- same merge-into-"dashboard" shape as
        save_dashboard_background(), but unlike the background these
        two are live settings dashboard_theme.py re-reads every frame
        (see set_default_art_path()/set_not_playing_message()), the
        same way app.py's Tkinter Dashboard tab has always applied them
        as you type, no Stop/Start needed -- so this applies them to
        the running dashboard_theme module immediately too, not just on
        the next Start/Apply. Only `default_art_path`/
        `not_playing_message` keys are meaningful here; anything else
        in `patch` is stored but ignored by the renderer, same
        tolerance every other merge-safe dashboard endpoint has."""
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        with self._lock:
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            dashboard_cfg.update(patch)
            self.cfg["dashboard"] = dashboard_cfg
            config_store.save_config(self.cfg)
            result = dict(self.cfg["dashboard"])
        if "default_art_path" in patch:
            dashboard_theme.set_default_art_path(patch.get("default_art_path") or None)
        if "not_playing_message" in patch:
            dashboard_theme.set_not_playing_message(patch.get("not_playing_message"))
        return result

    # save_dashboard_middle_content() (the old global weather on/off,
    # "spotify"/"weather"/"none" fixed to the middle column) is gone --
    # weather is now a movable/resizable `weather` element like any
    # other (see dashboard_theme.default_weather_element()), saved and
    # live-applied through save_dashboard_elements()/
    # apply_weather_from_elements() above, same as the now-playing
    # display's own equivalent conversion earlier.

    def save_dashboard_preset(self, name, elements, background=None):
        """`background`, if given, is that preset's OWN snapshot of the
        panel background at the moment it was saved -- the web UI's
        "Save current layout as preset" button sends its current
        background draft, so a preset restores the exact look it was
        saved with (background included) rather than just its element
        layout against whatever background happens to be configured
        globally when it's later loaded. `None` (the default, and what
        every preset saved before this stored) means "no background of
        its own" -- _dashboard_preset_thumbnails() and the web UI's own
        loadPreset() both fall back to the currently configured global
        background in that case, same as before this existed.

        Writes into `dashboard.presets`, which holds a person's own
        presets only. The app's own built-ins (dashboard_theme.
        BUILTIN_DASHBOARD_PRESETS) are read-only: saving under a
        built-in's exact name does NOT overwrite it, it saves a copy
        under a free derived name ("Fusion Core" -> "Fusion Core
        (custom)", see config_store.free_preset_name()), and the
        returned "name" says which one it actually used so the caller
        can report it rather than claiming a save that didn't happen
        where it said.

        That used to be the opposite: a same-named save shadowed the
        built-in from then on (config_store.resolve_dashboard_presets()
        preferred the saved copy), which is a strange bargain -- the
        only way to tweak a built-in also permanently destroyed your
        access to the original, and froze that slot against any future
        app update to it. Requested directly: "default themes shouldn't
        be modified, like if a user tries to modify them, we create a
        duplicate of it where the user does his modifications".

        A built-in that was previously deleted stays deleted through
        this (restore_dismissed_dashboard_presets() is what brings
        dismissed built-ins back now) -- saving a *copy* of one says
        nothing about wanting the original back in the picker, unlike
        the old overwrite, which by definition put something there
        under that exact name."""
        name = (name or "").strip()
        if not name:
            raise ValueError("preset name can't be empty")
        if not isinstance(elements, list):
            raise ValueError("elements must be a list")
        if background is not None and not isinstance(background, dict):
            raise ValueError("background must be an object")
        with self._lock:
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            presets = dict(dashboard_cfg.get("presets") or {})
            if name in dashboard_theme.BUILTIN_DASHBOARD_PRESETS:
                name = config_store.free_preset_name(
                    name, set(presets) | set(dashboard_theme.BUILTIN_DASHBOARD_PRESETS))
            presets[name] = {"elements": elements, "background": background}
            dashboard_cfg["presets"] = presets
            self.cfg["dashboard"] = dashboard_cfg
            config_store.save_config(self.cfg)
            merged = config_store.resolve_dashboard_presets(self.cfg)
            return {"name": name, "presets": merged,
                    "thumbnails": self._dashboard_preset_thumbnails(merged)}

    def delete_dashboard_preset(self, name):
        """Removes `name` from whatever's actually saved in this
        config (a person's own preset, or a saved customization of one
        of the app's built-ins). If `name` also happens to be one of
        the app's built-ins (dashboard_theme.BUILTIN_DASHBOARD_
        PRESETS), it's additionally recorded in `dashboard.
        dismissed_builtin_presets` -- otherwise, since a built-in isn't
        stored in `presets` to begin with (see config_store.
        resolve_dashboard_presets()), deleting a saved customization of
        one would just reveal the code-defined original again on the
        next merge, instead of the preset actually disappearing the
        way "delete" should."""
        with self._lock:
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            presets = dict(dashboard_cfg.get("presets") or {})
            presets.pop(name, None)
            dashboard_cfg["presets"] = presets
            if name in dashboard_theme.BUILTIN_DASHBOARD_PRESETS:
                dismissed = list(dashboard_cfg.get("dismissed_builtin_presets") or [])
                if name not in dismissed:
                    dismissed.append(name)
                dashboard_cfg["dismissed_builtin_presets"] = dismissed
            self.cfg["dashboard"] = dashboard_cfg
            config_store.save_config(self.cfg)
            presets = config_store.resolve_dashboard_presets(self.cfg)
            # The dismissed list rides along so the picker can offer
            # "restore built-ins" the moment one is deleted, without a
            # second round trip for the whole meta blob.
            return {"presets": presets, "thumbnails": self._dashboard_preset_thumbnails(presets),
                    "dismissed": list(dashboard_cfg.get("dismissed_builtin_presets") or [])}

    # ------------------------------------------------------------------ #
    # Sharing a preset with someone else.
    #
    # A preset is already just {elements, background} -- plain JSON --
    # so the hard part isn't the shape, it's the pictures. Any image in
    # it (the background, an image element, a clock face) is stored as
    # an absolute path into this machine's own managed image folder
    # (image_store.py), which means nothing at all on the machine it's
    # sent to. Export therefore inlines each referenced image as base64
    # and drops the path; import writes those bytes back into the
    # receiving machine's image store and rewrites the paths to point
    # at the new copies.
    #
    # Import deliberately does NOT trust an incoming `image_path`: a
    # preset file arrives from someone else, and a bare path in one
    # would otherwise let it point the panel at an arbitrary file on
    # this disk. Only images that travelled with the file (as base64)
    # or paths already inside this machine's own image store survive;
    # anything else is dropped to None, which every renderer here
    # already treats as "no image" rather than an error.
    # ------------------------------------------------------------------ #
    _IMAGE_HOLDERS = "image_path"

    @staticmethod
    def _preset_image_slots(preset):
        """Every dict in a preset that can carry an image: its
        background plus each element. Yielded as the dicts themselves
        so callers can rewrite them in place."""
        background = preset.get("background")
        if isinstance(background, dict):
            yield background
        for el in preset.get("elements") or []:
            if isinstance(el, dict):
                yield el

    def export_dashboard_preset(self, name):
        """A preset as a self-contained JSON object, ready to hand to
        someone else: `{"name", "preset": {"elements", "background"}}`
        with every referenced image inlined as `image_b64` (+
        `image_name` for its extension) in place of `image_path`.

        Reads through the merged view, so a built-in exports exactly as
        well as a saved one -- "send me the one you're using" shouldn't
        depend on where it happens to live."""
        presets = config_store.resolve_dashboard_presets(self.cfg)
        preset = presets.get(name)
        if preset is None:
            raise ValueError(f"no such preset: {name}")
        preset = json.loads(json.dumps(preset))  # deep copy; never mutate the live one
        for slot in self._preset_image_slots(preset):
            path = slot.get("image_path")
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    slot["image_b64"] = base64.b64encode(f.read()).decode("ascii")
                # Send the name without the 8-char content-hash prefix
                # image_store adds when it takes a copy: that prefix is
                # our storage detail, not part of the picture's name,
                # and leaving it on means each export/import round trip
                # stacks another one ("ab12_ab12_shot.png") and stores
                # a second identical copy instead of deduping onto the
                # first.
                slot["image_name"] = re.sub(r"^[0-9a-f]{8}_", "", os.path.basename(path))
            except OSError:
                # The picture is gone from this machine -- export the
                # layout anyway rather than failing the whole thing;
                # the recipient gets it with no image, same as they'd
                # get from a preset that never had one.
                pass
            slot["image_path"] = None
        return {"name": name, "preset": preset}

    def import_dashboard_preset(self, name, preset):
        """Saves a preset that came from a file (see
        export_dashboard_preset()) under `name`, materializing any
        inlined images into this machine's image store first.

        Validates the shape rather than trusting it: `elements` must be
        a list and `background`, if present, an object -- past that the
        renderers already skip element types and background modes they
        don't recognize, which is what lets a preset made by a newer
        (or just differently-configured) copy of the app still load
        here instead of erroring. The name goes through
        save_dashboard_preset(), so importing something called "Fusion
        Core" lands as a copy rather than overwriting the built-in."""
        if not isinstance(preset, dict):
            raise ValueError("preset must be an object")
        elements = preset.get("elements")
        if not isinstance(elements, list):
            raise ValueError("preset needs an \"elements\" list")
        background = preset.get("background")
        if background is not None and not isinstance(background, dict):
            raise ValueError("preset's \"background\" must be an object")
        preset = json.loads(json.dumps(preset))  # detach from the caller's dict
        for slot in self._preset_image_slots(preset):
            blob = slot.pop("image_b64", None)
            original = slot.pop("image_name", None) or "imported.png"
            path = slot.get("image_path")
            if blob:
                try:
                    slot["image_path"] = image_store.store_image_bytes(
                        base64.b64decode(blob), original)
                    continue
                except (ValueError, base64.binascii.Error):
                    # Corrupt or not-an-image payload: drop it and keep
                    # the rest of the layout, same as a missing file.
                    slot["image_path"] = None
                    continue
            # No image travelled with it: keep a path only if it's one
            # of ours already (re-importing a file exported here), and
            # never an arbitrary path chosen by whoever sent it.
            slot["image_path"] = path if (path and image_store.is_managed(path)) else None
        return self.save_dashboard_preset(name, preset.get("elements"), preset.get("background"))

    def restore_dismissed_dashboard_presets(self):
        """Puts every deleted built-in back in the picker by clearing
        `dashboard.dismissed_builtin_presets` -- the undo for deleting
        one.

        This exists because built-ins became read-only: deleting one is
        now the only thing a person can do TO a built-in (everything
        else copies), and an action with no way back isn't a choice,
        it's a trap. It used to have an accidental undo -- saving any
        preset under that exact name un-dismissed it -- which stopped
        being a thing when that save started creating a copy instead.

        Only touches the dismissed list: a person's own saved presets
        aren't involved, including one named "<built-in> (custom)" that
        came from customizing the built-in in the first place."""
        with self._lock:
            dashboard_cfg = dict(self.cfg.get("dashboard") or {})
            restored = list(dashboard_cfg.get("dismissed_builtin_presets") or [])
            if restored:
                dashboard_cfg["dismissed_builtin_presets"] = []
                self.cfg["dashboard"] = dashboard_cfg
                config_store.save_config(self.cfg)
            presets = config_store.resolve_dashboard_presets(self.cfg)
            return {"restored": restored, "presets": presets,
                    "thumbnails": self._dashboard_preset_thumbnails(presets)}

    def _dashboard_preset_thumbnails(self, presets, default_background=None):
        """Renders every saved preset's small preview picture (a
        `data:image/png;base64,...` URI, ready for an <img src=...>) --
        see dashboard_theme.render_preset_thumbnail()'s own docstring
        for why this reuses the real render pipeline instead of a
        lightweight mock. Each preset renders against its OWN saved
        background (see save_dashboard_preset()'s `background` param)
        if it has one, so e.g. a built-in preset's starfield or grid
        background actually shows up in its thumbnail rather than
        whatever background the panel happens to be configured with
        right now; a preset with no background of its own (`None` --
        every preset saved before this existed, and any preset saved
        without changing the background) falls back to
        `default_background`, which itself defaults to the currently
        configured global background.

        Accepts both preset shapes for whichever one `presets` actually
        holds: the current `{"elements": [...], "background": {...} or
        None}` dict, and the older bare-`elements`-list shape (in case
        anything still hands this one before config_store.py's
        migrate_dashboard_preset_shape() has run against it) -- a bare
        list is treated as `{"elements": <the list>, "background":
        None}`, identical to how that migration itself upgrades one.

        Called with the lock already held by save/delete above (cheap
        enough -- a handful of presets, each a small Pillow render --
        not to be worth releasing it for) and without the lock from
        dashboard_meta() (which only reads self.cfg once up front,
        outside its own `with` block, same as every other field it
        returns). A single bad/malformed saved preset (hand-edited
        app_config.json, say) logs and is skipped rather than taking
        down the whole picker -- every other preset's thumbnail still
        renders."""
        dashboard_cfg = self.cfg.get("dashboard") or {}
        if default_background is None:
            default_background = dict(dashboard_theme.DEFAULT_BACKGROUND, **(dashboard_cfg.get("background") or {}))
        thumbnails = {}
        for name, value in presets.items():
            try:
                if isinstance(value, list):
                    elements, own_background = value, None
                else:
                    elements, own_background = value.get("elements"), value.get("background")
                background = own_background if own_background else default_background
                img = dashboard_theme.render_preset_thumbnail(elements, background)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                thumbnails[name] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as e:  # noqa: BLE001 -- one bad preset shouldn't blank the whole picker
                self._log(f"(dashboard: couldn't render a thumbnail for preset {name!r}: {e})")
        return thumbnails

    def render_dashboard_live_preview(self, elements, background=None):
        """Renders `elements`/`background` right now, using this
        machine's actual current stats (dashboard_theme.render_live_
        preview()) -- backs the design canvas's polling "what am I
        currently editing" backdrop, so a freshly-loaded preset (or any
        unsaved edit) shows genuinely moving gauges/graphs instead of a
        frozen thumbnail or, worse, the *previous* theme's own real
        live frame with the new design's mockups smeared on top of it.

        `background` is optional the same way _dashboard_preset_
        thumbnails()' own `default_background` is -- None means "use
        whatever's actually configured right now" rather than requiring
        the caller (the design canvas always has a background draft of
        its own, staged or not, so this mainly matters for a stray
        elements-only call) to always pass one explicitly.

        No lock held here on purpose: this doesn't touch self.cfg or
        config_store at all, purely a read of current stats + a CPU-
        bound render, so there's nothing for a lock to protect and
        holding one would only block an unrelated save/preview/etc.
        landing on another thread for the render's duration."""
        if not isinstance(elements, list):
            raise ValueError("elements must be a list")
        if background is not None and not isinstance(background, dict):
            raise ValueError("background must be an object")
        if background is None:
            dashboard_cfg = self.cfg.get("dashboard") or {}
            background = dict(dashboard_theme.DEFAULT_BACKGROUND, **(dashboard_cfg.get("background") or {}))
        img = dashboard_theme.render_live_preview(elements, background)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def list_ports(self):
        """Scans for Hongtai-family panels right now (driver.
        find_hongtai_ports() -- matches on USB VID, so it finds any
        rebrand of this same hardware, not just XTRM Lab's), for the web
        UI's "Detect screens" button. Same underlying scan app.py's own
        "Refresh" button next to its port Combobox already uses -- this
        is that scan, exposed over HTTP for the headless controller.

        Each returned port's `value` is exactly what should be saved as
        `cfg["port"]` (a ScreenPort.label -- see _selected_port()'s
        docstring for why the human-readable label, not the raw device
        name, is what's persisted); `device`/`label` are split out
        separately so the frontend can build its own display text
        without parsing the combined label string. `auto_detect` is the
        sentinel value (config_store.AUTO_DETECT) for "let it pick
        automatically at connect time (only works if exactly one
        Hongtai-family panel is plugged in)", so the frontend doesn't
        need its own copy of that constant."""
        try:
            candidates = hongtai_screen.find_hongtai_ports()
        except Exception as e:  # noqa: BLE001 -- e.g. pyserial enumeration failing oddly
            self._log(f"(couldn't scan serial ports: {e})")
            candidates = []
        return {
            "ports": [
                {"value": c.label, "device": c.device, "description": c.description}
                for c in candidates
            ],
            "auto_detect": config_store.AUTO_DETECT,
        }

    def _selected_port(self):
        """app_config.json's "port" is a human-readable *label* (e.g.
        "COM3  (VID 33C3:7804 -- ...)  USB Serial Device (COM3)"), not
        an openable device path -- see ScreenPort.label in the driver.
        app.py's own _selected_port() never opens that string directly
        either: it rescans find_hongtai_ports() and looks up the
        matching candidate's real .device (e.g. "COM3") by comparing
        labels, because the only thing worth persisting across restarts
        is *which physical port the user picked*, not a device name
        that can shift across reboots/replugs. This does the same
        lookup, so a saved selection behaves identically whether it's
        driven from the Tkinter GUI or this headless controller.

        Returns None (auto-detect) if the saved label doesn't match any
        port currently plugged in -- same fallback app.py's
        _refresh_ports() does when the saved selection isn't in the
        current port list any more."""
        label = self.cfg.get("port", config_store.AUTO_DETECT)
        if not label or label == config_store.AUTO_DETECT:
            return None
        for candidate in hongtai_screen.find_hongtai_ports():
            if candidate.label == label:
                return candidate.device
        self._log(
            f"(saved port selection {label!r} doesn't match any port "
            f"plugged in right now -- falling back to auto-detect)"
        )
        return None

    # ------------------------------------------------------------------ #
    # start / stop / apply -- mirrors app.py's _on_start()/_on_stop()/
    # _on_apply()/_on_theme_finished(), minus every Tk widget touch.
    #
    # There is no "already running" guard on start() any more: calling
    # it while another theme is active is exactly what live-switching
    # means, and ScreenEngine.switch() handles interrupting whatever
    # was running and reusing the existing connection for the new one
    # instead of reopening the serial port. See screen_engine.py.
    # ------------------------------------------------------------------ #
    def start(self, theme_name=None):
        with self._lock:
            if theme_name is None:
                idx = self.cfg.get("active_tab", 0)
                if not (0 <= idx < len(config_store.THEME_TAB_ORDER)):
                    idx = 0
                theme_name = config_store.THEME_TAB_ORDER[idx]
            elif theme_name not in config_store.THEME_TAB_ORDER:
                raise ValueError(
                    f"Unknown theme {theme_name!r} -- expected one of "
                    f"{config_store.THEME_TAB_ORDER}")

            port = self._selected_port()
            brightness = self.cfg.get("brightness", 90)
            label, target, kwargs = theme_kwargs.build(theme_name, self.cfg, port, brightness)

            self.running_theme = label
            self.cfg["active_tab"] = config_store.THEME_TAB_ORDER.index(theme_name)
            self.cfg["auto_resume_tab"] = self.cfg["active_tab"]
            config_store.save_config(self.cfg)

        self.engine.switch(label, target, kwargs, port=port)
        return {"running_theme": label}

    def _on_screen_connected(self, screen):
        with self._lock:
            self.active_screen = screen
        screen.enable_frame_capture()

    def _on_screen_disconnected(self):
        """ScreenEngine fully closed the connection -- an explicit
        stop(), a switch/stop racing an in-flight error, or giving up
        after RECOVERY_ATTEMPTS. Whatever the cause, nothing is running
        any more, so there's nothing left to auto-resume on next
        launch either (an explicit stop() already cleared
        auto_resume_tab itself, so this is a no-op in that case)."""
        with self._lock:
            self.active_screen = None
            self.running_theme = None
            if self.cfg.get("auto_resume_tab") is not None:
                self.cfg["auto_resume_tab"] = None
                config_store.save_config(self.cfg)

    def _on_theme_finished(self, label):
        """A theme's run() returned on its own -- e.g. a non-looping
        video reaching its last frame -- rather than being interrupted
        by stop()/switch(). Purely informational (the engine already
        tore the connection down and fired _on_screen_disconnected by
        the time this runs); kept as its own hook so a UI could
        distinguish "finished" from "stopped" in the log if it wanted
        to."""
        self._log(f"{label} finished on its own.")

    def stop(self):
        with self._lock:
            self.cfg["auto_resume_tab"] = None
            config_store.save_config(self.cfg)
        self.engine.stop()

    def apply(self):
        """Re-reads the currently-active theme's settings from config
        and switches to it again -- same one-click "pick up my config
        changes" the Tkinter app's Apply button does. Under the old
        ThemeWorker-per-theme design this meant a full stop+reconnect;
        now it's just another switch() on the same connection, same as
        picking a different theme from the dropdown."""
        with self._lock:
            if self.running_theme is None:
                raise RuntimeError("nothing running to restart -- use start() instead")
            idx = self.cfg.get("active_tab", 0)
            if not (0 <= idx < len(config_store.THEME_TAB_ORDER)):
                idx = 0
            theme_name = config_store.THEME_TAB_ORDER[idx]
        return self.start(theme_name)

    def close(self):
        """Shuts the engine down and unblocks any open SSE
        subscribers -- call this when shutting the whole process down
        (see control_server.py's shutdown handling)."""
        with self._lock:
            self._closed = True
            subs = list(self._log_subscribers)
        self.engine.close()
        for q in subs:
            q.put(None)  # sentinel: tells an SSE handler to close its stream
