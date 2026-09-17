"""
config_store.py -- app_config.json load/save, and the couple of constants
every UI needs to agree on (the auto-detect sentinel, the fixed theme-tab
order). Split out of app.py (Phase 1 of ROADMAP.md's v2.0 rewrite) --
none of this is Tkinter, and the future webview UI reads/writes the same
file through the same functions.
"""
import json

from .paths import CONFIG_PATH

AUTO_DETECT = "(auto-detect)"

# Tab order in the Notebook -- kept in one place since both --theme and
# the "Launch at Windows startup" registration need to map a theme name
# to the tab index _on_start() reads.
THEME_TAB_ORDER = ["dashboard", "video", "webpage", "clock"]


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 -- missing/corrupt config is fine, just start fresh
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:  # noqa: BLE001 -- best-effort, never block on this
        pass


def migrate_dashboard_elements(cfg):
    """One-time upgrade for a saved `dashboard.elements` list from
    before the clock and now-playing widgets became their own movable/
    resizable elements (dashboard_theme.py's default_clock_element()/
    default_media_element()). Mutates `cfg` in place and returns True
    if it changed anything -- the caller is responsible for persisting
    that with save_config() (both AppController.__init__ and App.
    __init__ call this right after load_config(), so every entry point
    gets the same upgraded config, whichever UI opens it first).

    Only a config that already has a CONCRETE `elements` list saved can
    go stale like this -- a slots-only config has nothing to migrate,
    because slots_to_elements() (theme_kwargs.resolve_dashboard_
    elements()'s fallback whenever `elements` hasn't been saved yet)
    already appends both of these fresh, every time it runs. So this
    only ever adds to an existing `elements` list, never creates one
    from scratch.

    Flagged done via `dashboard._migrated_elements_v1` so it runs
    exactly once per config -- without that flag, someone who
    deliberately deletes the clock or now-playing element from their
    canvas would just get it silently re-added on the next launch,
    which would defeat the point of letting them remove it."""
    d = cfg.get("dashboard")
    if not isinstance(d, dict):
        return False
    if d.get("_migrated_elements_v1"):
        return False

    elements = d.get("elements")
    if not isinstance(elements, list) or not elements:
        # No concrete layout saved yet -- slots_to_elements() already
        # includes a clock + now-playing element on this path, nothing
        # to migrate. Still flag it done so this check is O(1) on every
        # future load instead of re-inspecting an (absent) list.
        d["_migrated_elements_v1"] = True
        return True

    from .themes import dashboard_theme  # lazy: keep load_config() light for callers that don't need it

    has_clock = any(el.get("type") == "clock" for el in elements)
    has_media = any(el.get("type") == "media" for el in elements)
    # "spotify" was the old default/only always-on now-playing display,
    # fixed to the middle column -- MIDDLE_CONTENT_OPTIONS no longer
    # offers it (see dashboard_theme.py), so a config from before this
    # migration either has that literal value saved or, just as often,
    # never saved middle_content at all and got the "spotify" default
    # implicitly. Either way is the signal that this config was relying
    # on the now-removed static display and needs a real media element
    # in its place; anyone who had already chosen "weather" or "none"
    # deliberately didn't want it, so their choice is left alone.
    had_static_now_playing = (d.get("middle_content") or "spotify") == "spotify"

    if not has_clock:
        elements.append(dashboard_theme.default_clock_element())

    if not has_media and had_static_now_playing:
        elements.append(dashboard_theme.default_media_element())
        d["middle_content"] = "none"

    d["elements"] = elements
    d["_migrated_elements_v1"] = True
    return True


def migrate_dashboard_preset_shape(cfg):
    """One-time upgrade for saved `dashboard.presets` entries from
    before a preset could carry its own background: each value used to
    be a bare `elements` list, now it's `{"elements": [...],
    "background": {...} or None}` (see save_dashboard_preset()'s
    docstring) so loading a preset can restore the exact look it was
    saved with, not just its layout against whatever background
    happens to be configured globally right now. A bare-list entry is
    wrapped as `{"elements": <the list>, "background": None}` --
    `None` means "no background of its own", which is exactly what a
    pre-upgrade preset actually was, so this changes nothing about how
    it looks, only how it's stored.

    Mutates `cfg` in place and returns True if it changed anything --
    same caller contract as the other migrate_* functions in this
    module. Flagged via `dashboard._migrated_preset_shape_v1` so it
    runs exactly once."""
    d = cfg.get("dashboard")
    if not isinstance(d, dict):
        return False
    if d.get("_migrated_preset_shape_v1"):
        return False

    presets = d.get("presets")
    if isinstance(presets, dict):
        upgraded = {}
        for name, value in presets.items():
            if isinstance(value, list):
                upgraded[name] = {"elements": value, "background": None}
            else:
                upgraded[name] = value
        d["presets"] = upgraded

    # Always flags done (even if nothing needed upgrading, e.g. a fresh
    # config with no presets at all yet) -- same "flag it regardless"
    # pattern migrate_dashboard_elements() uses, so this check stays
    # O(1) on every future load instead of re-inspecting `presets`.
    d["_migrated_preset_shape_v1"] = True
    return True


def resolve_dashboard_presets(cfg):
    """The preset picker's actual list: the app's own built-in presets
    (`dashboard_theme.BUILTIN_DASHBOARD_PRESETS` -- code, not saved
    data) merged with whatever this config has saved under `dashboard.
    presets` (a person's own presets, plus any built-in they've
    resaved under its own name to customize it -- see
    save_dashboard_preset()'s docstring).

    This used to work the other way around: a fresh config got the 6
    built-ins *copied* into `dashboard.presets` once (the old
    seed_builtin_dashboard_presets(), now gone) and from then on they
    were just ordinary saved data, indistinguishable from something a
    person typed in themselves. That meant a built-in was permanently
    frozen at whatever it looked like the moment it got copied in --
    an app update that improved one of the 6 (or added a 7th) would
    never reach an install that had already launched once, because
    app_config.json is the person's own data, not the app's, and
    nothing should be quietly rewriting it on their behalf. Resolving
    the merge fresh on every read instead means a built-in always
    reflects whatever this running app's own code defines it as --
    app_config.json only ever holds what a person actually chose to
    save.

    The two sides no longer collide by design: a built-in is read-only,
    and an attempt to save over one lands under a derived name instead
    (see AppController.save_dashboard_preset() and
    migrate_unshadow_builtin_presets() below). Saving over a built-in
    used to be *how* you customized it -- the saved copy shadowed the
    code-defined one from then on -- which quietly cost you the
    original: there was no way back to it, and an app update that
    improved that preset never reached you. A saved entry that still
    shares a built-in's name (only reachable by hand-editing
    app_config.json now) is still preferred here rather than ignored:
    hiding a person's own saved data because of a name clash would be
    worse than the clash itself.

    `dashboard.dismissed_builtin_presets` (a plain list of names, set
    by delete_dashboard_preset() -- see its own comment) removes a
    built-in that was deleted, so a delete stays a real choice with
    nothing to actually delete out of app_config.json (there was never
    a copy in there to remove); AppController.restore_dismissed_
    dashboard_presets() clears that list to bring them all back, which
    is the only way back now that resaving over the name no longer
    un-dismisses one."""
    d = cfg.get("dashboard") or {}
    dismissed = set(d.get("dismissed_builtin_presets") or [])

    from .themes import dashboard_theme  # lazy: keep load_config() light for callers that don't need it

    merged = {name: value for name, value in dashboard_theme.BUILTIN_DASHBOARD_PRESETS.items()
              if name not in dismissed}
    merged.update(d.get("presets") or {})
    return merged


def free_preset_name(base, taken, suffix="custom"):
    """A preset name derived from `base` that isn't already in `taken`:
    "Fusion Core" -> "Fusion Core (custom)", then "(custom 2)",
    "(custom 3)", ... Shared by the save-time guard against overwriting
    a built-in and by migrate_unshadow_builtin_presets() below, so a
    config migrated yesterday and a save made today land on the same
    naming rather than two near-identical conventions."""
    candidate = f"{base} ({suffix})"
    n = 2
    while candidate in taken:
        candidate = f"{base} ({suffix} {n})"
        n += 1
    return candidate


def migrate_unshadow_builtin_presets(cfg):
    """One-time fix for a config saved back when overwriting a built-in
    was allowed: any `dashboard.presets` entry whose name matches a
    current BUILTIN_DASHBOARD_PRESETS key is renamed to a free
    "<name> (custom)" (see free_preset_name()).

    Built-ins are read-only now -- saving over one creates a copy
    instead (AppController.save_dashboard_preset()) -- so an existing
    same-named saved entry is the last thing that can still shadow one.
    Renaming rather than deleting keeps both: the person's own edited
    version stays, under a name that says what it is, and the built-in
    it was covering reappears at whatever this app version defines it
    as.

    Runs after migrate_strip_redundant_builtin_presets(), which removes
    the *untouched* copies an old seeded install had -- so by the time
    this sees a collision, it's a real customization worth keeping, not
    a stale duplicate of the same thing.

    Mutates `cfg` in place and returns True if it changed anything --
    same caller contract as the other migrate_* functions here. Flagged
    via `dashboard._unshadowed_builtin_presets_v1` so it runs exactly
    once: a person is free to hand-edit a colliding name back in
    afterwards, and this shouldn't keep undoing that on every load."""
    d = cfg.get("dashboard")
    if not isinstance(d, dict):
        return False
    if d.get("_unshadowed_builtin_presets_v1"):
        return False
    d["_unshadowed_builtin_presets_v1"] = True

    presets = d.get("presets")
    if not isinstance(presets, dict) or not presets:
        return True

    from .themes import dashboard_theme  # lazy: keep load_config() light for callers that don't need it

    builtins = dashboard_theme.BUILTIN_DASHBOARD_PRESETS
    collisions = [name for name in presets if name in builtins]
    if not collisions:
        return True

    for name in collisions:
        taken = set(presets) | set(builtins)
        renamed = free_preset_name(name, taken)
        presets[renamed] = presets.pop(name)
        # A built-in that was both dismissed and shadowed was only ever
        # visible *as* the saved override; now that the override has
        # moved aside, leaving it dismissed would make the preset the
        # person was actually using disappear from the picker. Their
        # copy survives under the new name either way, but the built-in
        # they'd effectively un-deleted by saving over it should come
        # back with it.
        dismissed = d.get("dismissed_builtin_presets")
        if isinstance(dismissed, list) and name in dismissed:
            dismissed.remove(name)
    return True


def migrate_strip_redundant_builtin_presets(cfg):
    """One-time cleanup for a config from before resolve_dashboard_
    presets() existed: back when a fresh install's `dashboard.presets`
    got the 6 built-ins *copied* into it (the old
    seed_builtin_dashboard_presets(), now gone -- see resolve_
    dashboard_presets()'s docstring for why), those copies are now
    redundant -- the merge supplies the same 6 from code on every read
    -- and worse, leaving them in `presets` would permanently freeze
    them at today's version instead of tracking future app updates,
    the exact problem the merge approach exists to avoid.

    Removes a `presets` entry only when BOTH its name matches a current
    `BUILTIN_DASHBOARD_PRESETS` key AND its content is still identical
    to that built-in (compared as JSON, ignoring key order) -- i.e.
    only an untouched copy. A same-named entry that's been edited
    (resaved after a rename, a moved gauge, a different background) is
    a real customization, not a stale copy, and is deliberately left in
    place -- it's exactly the "saved side wins" override resolve_
    dashboard_presets() is designed to keep.

    Mutates `cfg` in place and returns True if it changed anything --
    same caller contract as the other migrate_* functions in this
    module. Flagged via `dashboard._stripped_redundant_builtin_
    presets_v1` so it runs exactly once; a config with no `presets` at
    all (nothing to strip) still gets flagged so this stays O(1) on
    every later load."""
    d = cfg.get("dashboard")
    if not isinstance(d, dict):
        return False
    if d.get("_stripped_redundant_builtin_presets_v1"):
        return False
    d["_stripped_redundant_builtin_presets_v1"] = True

    presets = d.get("presets")
    if not isinstance(presets, dict) or not presets:
        return True

    from .themes import dashboard_theme  # lazy: keep load_config() light for callers that don't need it

    changed = False
    for name in list(presets.keys()):
        builtin = dashboard_theme.BUILTIN_DASHBOARD_PRESETS.get(name)
        if builtin is None:
            continue
        # Compare as JSON (round-tripped on both sides) rather than
        # `==` directly -- a saved preset's background dict may have
        # gone through the migrate_dashboard_preset_shape() bare-list
        # upgrade or a JSON round trip itself somewhere along the way,
        # which can leave e.g. tuples-vs-lists mismatches that are
        # equal in the shape that actually matters (their JSON form)
        # but not under Python's `==`.
        if json.dumps(presets[name], sort_keys=True) == json.dumps(builtin, sort_keys=True):
            del presets[name]
            changed = True
    if changed:
        d["presets"] = presets
    return True


def migrate_dashboard_weather_element(cfg):
    """One-time upgrade for a saved config that used the old global
    `middle_content` "weather" choice (dashboard_theme.py's now-removed
    MIDDLE_CONTENT_OPTIONS/set_middle_content()) -- converts it into a
    real `weather` element (dashboard_theme.default_weather_element())
    carrying that config's `weather_location`/`weather_units`, the same
    "the old global setting becomes a real element" move
    migrate_dashboard_elements() above already made for the old
    "spotify" middle_content choice and the now-playing display.

    Mutates `cfg` in place and returns True if it changed anything --
    same caller contract as migrate_dashboard_elements() (both
    AppController.__init__ and App.__init__ call this right after it).
    Flagged via `dashboard._migrated_weather_v1` so it runs exactly
    once -- without that flag, someone who deliberately removes the
    weather element from their canvas would just get it silently
    re-added on the next launch."""
    d = cfg.get("dashboard")
    if not isinstance(d, dict):
        return False
    if d.get("_migrated_weather_v1"):
        return False

    had_weather = d.get("middle_content") == "weather"
    elements = d.get("elements")
    has_weather = isinstance(elements, list) and any(el.get("type") == "weather" for el in elements)

    if had_weather and not has_weather:
        from .themes import dashboard_theme  # lazy: keep load_config() light for callers that don't need it

        weather_el = dashboard_theme.default_weather_element()
        weather_el["location"] = d.get("weather_location") or ""
        weather_el["units"] = d.get("weather_units") or "celsius"
        elements = list(elements) if isinstance(elements, list) else []
        elements.append(weather_el)
        d["elements"] = elements

    # `middle_content`/`weather_location`/`weather_units` are retired
    # either way -- weather.py's location/units now live on the element
    # itself (see dashboard_theme.apply_weather_from_elements()), and
    # anyone who had middle_content "none" just wasn't showing weather,
    # exactly as leaving it off the canvas now means.
    d.pop("middle_content", None)
    d.pop("weather_location", None)
    d.pop("weather_units", None)
    d["_migrated_weather_v1"] = True
    return True
