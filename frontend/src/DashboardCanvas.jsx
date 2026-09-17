import React, { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api.js";
import Collapsible from "./Collapsible.jsx";
import { StyledGaugePreview, StyledBoxPreview, resolveWidgetStyle, widgetFont } from "./StyledWidgets.jsx";

// The dashboard design canvas (ROADMAP.md Phase 5) -- drag/resize gauge
// elements over the live panel frame, edit their stat/color/opacity in a
// property panel, undo/redo, and save the result (or named presets of
// it) back to the backend. Built on top of Phase 4's element list
// (dashboard_theme.py's build_static_background()/render_frame()) --
// every element here is exactly the {id, type, stat, x, y, radius,
// rotation, color, opacity, z} shape that already round-trips through
// config, so there's no separate "canvas format" to convert to/from.
//
// Reference size matches dashboard_theme.py's REFERENCE_WIDTH/HEIGHT --
// x/y/radius are stored as fractions (of width/height/min(width,height))
// precisely so they scale to any panel resolution, but this canvas just
// needs *a* concrete size to draw an SVG viewBox in, and 960x480 is this
// panel's real resolution.
const REF_W = 960;
const REF_H = 480;

const ACCENT_CPU = "rgb(0, 220, 255)";
const ACCENT_GPU = "rgb(235, 45, 225)";

// Rotation is deliberately not exposed here -- dashboard_theme.py stores
// and migrates it but doesn't render it yet (see ROADMAP.md Phase 4's
// notes): a rotate handle in this canvas would visibly do nothing,
// which is worse than not offering it. It'll get a control once
// rendering support lands.

const SNAP_THRESHOLD = 0.018; // fraction-of-canvas distance to snap at
const MIN_RADIUS = 0.02;
const MAX_RADIUS = 0.45;
// A pointerdown+pointerup pair is a "click" in every user's mind, but a
// mouse/trackpad/touch almost never reports *zero* pixels of movement
// between the two -- there's always a stray pointermove or two carrying a
// couple pixels of jitter. Every pointermove used to run straight through
// to setElements()'s prev.map(...), which allocates a new array (and a new
// object for the dragged element) even when the computed x/y come out
// numerically identical to what was already there -- and `dirty` is a
// strict `elements !== savedElementsRef.current` reference check, so that
// alone was enough to flip it true, instantly showing "unsaved changes"
// and enabling Save layout on a plain click. Below this pixel distance
// (measured in real screen pixels from where the gesture started, before
// any snapping/offset math), a pointermove is treated as still part of a
// click and does nothing at all -- no setElements call, so `elements`
// keeps its exact prior reference and `dirty` stays false. Once the
// gesture crosses this distance even once, it's a real drag/resize for
// the rest of the gesture (see the `moved` flag on dragRef.current).
const MOVE_THRESHOLD_PX = 4;

// How often the canvas polls a fresh live-rendered preview (see
// editingPreviewUrl's own comment) while there's an unsaved edit. Each
// poll is a real server-side render (gauge drawing included, not just
// a cheap pixel copy the way the real panel's own frame poll is), so
// this stays well below the real panel's FRAME_POLL_MS (App.jsx) --
// still frequent enough that gauges/graphs read as "alive", not so
// frequent it's hammering the backend for a design-canvas convenience.
const EDITING_PREVIEW_POLL_MS = 1200;

function rgbToHex([r, g, b]) {
  return "#" + [r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("");
}

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return [255, 255, 255];
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
}

function accentFor(el) {
  if (el.color) return `rgb(${el.color[0]}, ${el.color[1]}, ${el.color[2]})`;
  return el.x < 0.5 ? ACCENT_CPU : ACCENT_GPU;
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function basename(path) {
  if (!path) return "";
  return path.split(/[\\/]/).pop();
}

// backgroundsEqual: plain structural comparison for `dirty` tracking
// below -- a background object is always a flat {mode, scheme,
// image_path} dict (never nested, never containing anything
// non-JSON-safe like a function or a Date), so JSON.stringify on both
// sides is a correct and cheap deep-equal here, same trick Save
// layout's own "has anything actually changed" check needs. Treats
// null/undefined as equal to each other (both "no background staged
// yet") without treating either as equal to a real object.
function backgroundsEqual(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

// A short human-readable label for the element list panel -- "what is
// this thing", not its full config. Mirrors the labels already drawn
// inline on the canvas itself (gauge's stat title, graph/image/media's
// placeholder text) so the list and the canvas always agree on what to
// call something.
// A text element bound to a stat (el.stat set to a STAT_DEFS key --
// see api.js/control_server.py's dashboard meta and dashboard_theme.
// py's _resolve_text_content(), which this mirrors) shows that stat's
// *live* reading instead of a fixed string -- the editor has no live
// reading of its own to show (that's the real backend frame, rendered
// behind this SVG when connected -- see canvas-frame's own comment),
// so this fills in "--" the same way a disconnected gauge would,
// wrapped in the element's own template ("{value}" by default, or
// something like "CPU {value}" / "{label}: {value}").
function textElementPreview(el, meta) {
  const statMeta = el.stat ? meta.stats[el.stat] : null;
  if (!statMeta) return (el.text || "").trim();
  const template = el.template || "{value}";
  try {
    return template.replace(/\{value\}/g, "--").replace(/\{label\}/g, statMeta.title || el.stat);
  } catch {
    return "--";
  }
}

function elementLabel(el, meta) {
  if (el.type === "gauge") return meta.stats[el.stat]?.title || el.stat;
  if (el.type === "graph") return meta.stats[el.stat]?.title || el.stat;
  if (el.type === "bar") return meta.stats[el.stat]?.title || el.stat;
  if (el.type === "text") {
    if (el.stat) return `${meta.stats[el.stat]?.title || el.stat} (live)`;
    return el.text?.trim() ? `"${el.text}"` : "(empty text)";
  }
  if (el.type === "image") return el.image_path ? basename(el.image_path) : "(no image picked)";
  if (el.type === "media") return "Now playing";
  if (el.type === "clock") return "Clock";
  if (el.type === "weather") return el.location?.trim() ? `Weather (${el.location})` : "Weather";
  return el.type;
}

// Short plain-text badges (not emoji, to match the rest of this UI's
// flat/monochrome look) shown next to each row in the element list.
const ELEMENT_BADGES = {
  gauge: "G",
  graph: "GR",
  bar: "BAR",
  text: "T",
  image: "IMG",
  media: "NP",
  clock: "CLK",
  weather: "WX",
};

// Always includes a short random suffix rather than just counting up
// from `existing.length` -- a plain counter is only unique *within
// whatever array this particular browser tab currently has loaded*,
// which isn't good enough if the backend's file changes underneath an
// open tab (e.g. a config migration runs while the canvas is still
// open from before it): two different stale/fresh views of "how many
// elements exist" can independently compute the exact same next id
// even though neither one has actually seen the other's elements. A
// duplicate id is exactly what caused one clock to render but not be
// selectable (React collapses duplicate keys; dashboard_theme.py's
// renderer doesn't dedupe at all, so both drew). The counter prefix is
// kept purely so the saved JSON still reads as "the 3rd gauge added",
// not for uniqueness -- the random suffix is what actually guarantees
// that.
function makeId(existing, prefix = "el") {
  const used = new Set(existing.map((el) => el.id));
  const n = existing.length + 1;
  let id = `${prefix}_${n}_${Math.random().toString(36).slice(2, 6)}`;
  while (used.has(id)) {
    id = `${prefix}_${n}_${Math.random().toString(36).slice(2, 6)}`;
  }
  return id;
}

// Default field shapes for each element type ROADMAP.md Phase 6 adds
// (gauge -- and its optional gradient `color2` -- already existed).
// Every type shares `id`/`type`/`x`/`y`/`z`/`opacity`; the rest is
// exactly the shape dashboard_theme.py's element handlers expect, so
// there's nothing to translate on save -- same "no separate canvas
// format" reasoning as the gauge elements already worked this way.
function makeElement(type, elements, meta) {
  const maxZ = elements.reduce((m, el) => Math.max(m, el.z ?? 0), -1);
  const base = { x: 0.5, y: 0.5, z: maxZ + 1, opacity: 1.0 };
  const statKeys = Object.keys(meta.stats);
  if (type === "gauge") {
    const used = new Set(elements.filter((e) => e.type === "gauge").map((e) => e.stat));
    const stat = statKeys.find((k) => !used.has(k)) || statKeys[0];
    return { id: makeId(elements, "gauge"), type: "gauge", stat, radius: 0.09,
             rotation: 0, color: null, color2: null, ...base };
  }
  if (type === "text") {
    return { id: makeId(elements, "text"), type: "text", text: "Label",
             font_size: 0.05, color: [255, 255, 255], align: "center", ...base };
  }
  if (type === "graph") {
    return { id: makeId(elements, "graph"), type: "graph", stat: statKeys[0],
             style: "line", color: [0, 220, 255], width: 0.22, height: 0.14,
             history_seconds: 20, ...base };
  }
  if (type === "bar") {
    // A linear meter for one stat's current value -- the same idea as
    // a gauge (a live reading against its own min/max) but drawn as a
    // horizontal fill bar instead of a circular ring, for lining
    // several stats up as a compact stack rather than spreading them
    // out as circles, or just for the look. Wider-than-tall by default
    // (unlike graph's roughly-square default box) since it's meant to
    // read as a single bar, not a plot -- see dashboard_theme.py's
    // _bar_box()/_draw_bar_dynamic() for how it actually renders.
    // `orientation` picks which way the fill runs (left-to-right vs.
    // bottom-to-top) -- see the Orientation control in the property
    // panel below, which also flips width/height when it's toggled so
    // a vertical bar defaults to tall-and-narrow instead of staying
    // wide-and-short. `show_knob` defaults off -- direct user feedback
    // was that the round handle from progress_bar_glow() (originally
    // built for the now-playing progress bar, where it's the only
    // indicator of position) just looks like a stray dot sitting on a
    // bar element, whose own fill already shows the value. `gradient`/
    // `gradient_colors`/`gradient_direction` start unset (flat
    // `color` fill, same as before) -- see the property panel's
    // Gradient fill controls below for what turning it on adds.
    return { id: makeId(elements, "bar"), type: "bar", stat: statKeys[0],
             color: [0, 220, 255], width: 0.26, height: 0.12, orientation: "horizontal",
             show_knob: false, gradient: false, gradient_direction: "horizontal", ...base };
  }
  if (type === "image") {
    // width/height here are just a placeholder box until a picture's
    // actually picked -- uploadImage() below replaces them with a box
    // that matches the picked image's own aspect ratio, since a
    // freshly-added element has no image yet to size itself from.
    return { id: makeId(elements, "image"), type: "image", image_path: "",
             width: 0.15, height: 0.15, fit: "contain", ...base };
  }
  if (type === "media") {
    // The Spotify now-playing widget (album art + track/artist +
    // progress bar) as a movable/resizable element -- see
    // dashboard_theme.py's _draw_media_element(). Generously sized by
    // default since it has to fit album art plus two lines of text
    // plus a progress bar stacked vertically. show_art/show_name/
    // show_time let each piece be switched off independently.
    return { id: makeId(elements, "media"), type: "media", width: 0.32, height: 0.52,
             show_art: true, show_name: true, show_time: true, ...base };
  }
  if (type === "clock") {
    // Mirrors default_clock_element() in dashboard_theme.py -- "digital"
    // with these values renders identically to the plain HH:MM:SS clock
    // this used to be the only option.
    return { id: makeId(elements, "clock"), type: "clock",
             font_size: 0.05, color: [235, 235, 242], show_seconds: true,
             face: "digital", hour_format: "24h", show_date: false,
             analog_style: "classic", radius: 0.12,
             image_path: "", width: 0.22, height: 0.22, fit: "contain", ...base };
  }
  if (type === "weather") {
    // Mirrors default_weather_element() in dashboard_theme.py -- a
    // current-conditions readout (weather.py, free/no-API-key) as its
    // own movable/resizable element, the same conversion "now playing"
    // already got from a fixed always-on middle-column display into
    // the `media` element above. Empty location by default, same as
    // the backend's factory -- shows a "set a location" placeholder
    // until one's filled in via the property panel. show_icon/
    // show_temp/show_description/show_details/show_location let each
    // piece be switched off independently, same as media's show_art/
    // show_name/show_time -- so it can be shrunk to just an icon, just
    // the temperature, etc. to sit alongside a now-playing element
    // without the two eating the whole panel between them.
    return { id: makeId(elements, "weather"), type: "weather", width: 0.28, height: 0.46,
             location: "", units: "celsius", show_icon: true, show_temp: true,
             show_description: true, show_details: true, show_location: true, ...base };
  }
  return null;
}

// Auto-fit height estimates for media/weather -- called whenever a
// show_* piece checkbox is toggled in the property panel, so the box's
// own height (and with it, the selection border drawn on the canvas)
// shrinks or grows along with whichever pieces are actually turned on,
// instead of staying at whatever size it happened to be dragged to
// before. Without this, turning a piece off left the border exactly
// where it was -- the *content* re-centered inside it (see
// dashboard_theme.py's _draw_media_element()/_draw_weather_element()
// docstrings for that fix), but the box itself, which is what you're
// actually looking at and lining up against other elements, didn't
// visibly shrink to match.
//
// These mirror those two functions' own content-height pre-measurement
// as closely as a browser-side estimate reasonably can -- but not
// exactly: dashboard_theme.py's real pass knows whether weather data
// has actually loaded (a "no data yet" element is much shorter -- just
// a wrapped message) and whether something's really playing (title/
// artist/duration might not exist), neither of which is knowable here
// before the theme is even running. So this always assumes the
// "fully populated" case -- every enabled piece has real content to
// show -- which is the right box size to aim for either way: it's
// what the element will actually look like once there's real data,
// and a "no data" element temporarily having some empty space under
// its short placeholder message is far less confusing than a box that
// never changes size when you flip a checkbox at all. Height only,
// not width -- these two elements stack their pieces vertically, so
// width doesn't grow/shrink with which pieces are on the way height
// does (an icon-only weather element still wants to be as wide as its
// icon, not as wide as the location name it no longer shows); a
// narrower box is still just a manual resize away.
function estimateWeatherHeight(el, refWidth = REF_W, refHeight = REF_H) {
  const boxW = Math.max(60, (el.width ?? 0.28) * refWidth);
  // Same width-based half of _draw_weather_element()'s
  // `icon_size = min(mid_w * 0.4, box_h * 0.32)` -- the height-based
  // half is dropped since box_h is exactly what's being solved for
  // here, not yet known.
  const iconSize = boxW * 0.4;
  let contentH = 0;
  if (el.show_icon ?? true) contentH += iconSize + 10;
  if (el.show_temp ?? true) contentH += 54;
  if (el.show_description ?? true) contentH += 26;
  if (el.show_details ?? true) contentH += 22;
  if (el.show_location ?? true) contentH += 20;
  // `+ 12` matches _weather_box()'s own margin on top of the exact
  // same content_h formula -- see that function's docstring for why
  // the box is now never allowed to end up shorter than this.
  return clamp((contentH + 12) / refHeight, 0.04, 0.9);
}

function estimateMediaHeight(el, refWidth = REF_W, refHeight = REF_H) {
  const boxW = Math.max(60, (el.width ?? 0.32) * refWidth);
  const showArt = el.show_art ?? true;
  const showName = el.show_name ?? true;
  const showTime = el.show_time ?? true;
  // _draw_media_element()'s `art_size = min(mid_w * 0.75, box_h *
  // art_frac)` -- again dropping the box_h-based cap for the same
  // circular-dependency reason as the weather icon above, so this
  // assumes the art gets its full width-based size rather than
  // whatever a fixed box height would have squeezed it down to.
  const artSize = showArt ? Math.max(24, boxW * 0.75) : 0;
  let contentH = showArt ? artSize + 12 : 0;
  if (showName) contentH += 24 + 22; // track line + artist line
  if (showTime) contentH += 26; // bar + gap to the time labels
  return clamp(contentH / refHeight, 0.04, 0.9);
}

// Reads a just-picked File's own pixel dimensions in the browser (no
// upload round-trip needed for this) -- used so a new image element
// starts out matching the picture's own aspect ratio instead of
// whatever generic default box was there before. Resolves null (never
// rejects) on anything that isn't decodable as an image, so a caller
// can just skip the auto-sizing rather than having to handle an error.
function readImageDimensions(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve({ width: img.naturalWidth, height: img.naturalHeight });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(null);
    };
    img.src = url;
  });
}

const PREVIEW_SECONDS = 5;

// Shared "solid color, or a 2-4 stop gradient" property-panel control --
// originally built just for the bar element (see CHANGELOG.md), then
// pulled out into its own component so gauge/text/graph/clock's own
// color pickers could get the exact same treatment instead of each
// growing their own copy of this same checkbox+swatches logic. Reads
// and writes `selected.gradient` (bool), `selected.gradient_colors`
// (2-4 RGB triples), and `selected.gradient_direction` -- every backend
// renderer that supports a gradient (see dashboard_theme.py's
// _element_gradient_colors()) expects exactly this shape, so nothing
// element-type-specific belongs in here beyond which direction options
// make sense to offer (`directionOptions`) and what to seed a first-time
// gradient with (`defaultColor` -- the same fallback the plain solid
// The font-family picker shown for text and clock elements. The list
// itself comes from the backend (meta.fontFamilies -- dashboard_theme
// .FONT_FAMILIES), so adding a bundled face is a one-line change there
// and shows up here with no frontend edit at all. "default" is the
// OS's own sans, which is what every element saved before the bundled
// faces existed resolves to.
function FontFamilyControl({ selected, updateSelected, meta }) {
  const families = meta?.fontFamilies;
  if (!families) return null;
  return (
    <div className="row">
      <label className="grow">
        Font
        <select value={selected.font || ""}
                onChange={(e) => updateSelected({ font: e.target.value || null })}>
          <option value="">Use theme font</option>
          {Object.entries(families).map(([key, f]) => (
            <option key={key} value={key}>{f.label}</option>
          ))}
        </select>
      </label>
      <label className="row-inline">
        <input type="checkbox" checked={!!selected.bold}
               onChange={(e) => updateSelected({ bold: e.target.checked })} />
        Bold
      </label>
    </div>
  );
}

// The "chip" (plate) behind a text element -- a filled rounded tag
// auto-sized to the string, the label style the card-based built-in
// presets use ("TEMP" in a solid tag next to its value). Off unless a
// plate color is set, so an ordinary text element is unaffected.
function TextPlateControl({ selected, updateSelected }) {
  const on = !!selected.plate;
  return (
    <div className="row">
      <label className="row-inline">
        <input type="checkbox" checked={on}
               onChange={(e) => updateSelected(
                 e.target.checked
                   ? { plate: selected.plate || [236, 72, 153], plate_radius: selected.plate_radius ?? 0.3 }
                   : { plate: null })} />
        Label chip
      </label>
      {on && (
        <>
          <label>
            Chip color
            <input type="color" value={rgbToHex(selected.plate || [236, 72, 153])}
                   onChange={(e) => updateSelected({ plate: hexToRgb(e.target.value) })} />
          </label>
          <label>
            Roundness
            <input type="range" min={0} max={50}
                   value={Math.round((selected.plate_radius ?? 0.3) * 100)}
                   onChange={(e) => updateSelected({ plate_radius: Number(e.target.value) / 100 })} />
          </label>
        </>
      )}
    </div>
  );
}

// Color picker next to this one already uses for that element type).
function GradientFillControl({ selected, updateSelected, defaultColor = [0, 220, 255],
                                 secondColor = "#ff2ee0", showSolidColorWhenOff = true,
                                 directionOptions = [["horizontal", "Left → Right"], ["vertical", "Top → Bottom"]] }) {
  return (
    <>
      <div className="row">
        <label className="row-inline">
          <input
            type="checkbox"
            checked={!!selected.gradient}
            onChange={(e) => {
              const on = e.target.checked;
              if (!on) {
                updateSelected({ gradient: false });
                return;
              }
              // Turning gradient on for the first time seeds two stops
              // from whatever solid color was already in effect (so the
              // element doesn't visually jump the moment the checkbox
              // is ticked) plus a second, borrowed color -- an existing
              // gradient_colors list (from having turned this on
              // before, then off) is left alone rather than reset.
              const stops = (selected.gradient_colors && selected.gradient_colors.length >= 2)
                ? selected.gradient_colors
                : [selected.color || defaultColor, hexToRgb(secondColor)];
              updateSelected({ gradient: true, gradient_colors: stops });
            }}
          />
          Gradient fill
        </label>
        {!selected.gradient ? (
          // Some callers (gauge) already have their own separate solid-
          // color control with its own nullable "derive from position"
          // semantics that don't map onto this component's plain
          // `color` field -- showSolidColorWhenOff=false skips this
          // picker there so the two don't show a redundant/conflicting
          // second "Color" input right next to each other.
          showSolidColorWhenOff && (
            <label>
              Color
              <input type="color" value={rgbToHex(selected.color || defaultColor)}
                     onChange={(e) => updateSelected({ color: hexToRgb(e.target.value) })} />
            </label>
          )
        ) : (
          <label>
            Direction
            <select
              value={selected.gradient_direction || directionOptions[0][0]}
              onChange={(e) => updateSelected({ gradient_direction: e.target.value })}
            >
              {directionOptions.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      {selected.gradient && (
        <div className="row" style={{ flexWrap: "wrap", gap: "0.5em" }}>
          {(selected.gradient_colors || []).map((c, i) => (
            <span key={i} className="row-inline">
              <input
                type="color"
                value={rgbToHex(c)}
                onChange={(e) => {
                  const stops = [...selected.gradient_colors];
                  stops[i] = hexToRgb(e.target.value);
                  updateSelected({ gradient_colors: stops });
                }}
              />
              {selected.gradient_colors.length > 2 && (
                <button
                  type="button"
                  onClick={() => updateSelected({
                    gradient_colors: selected.gradient_colors.filter((_, j) => j !== i),
                  })}
                >
                  ✕
                </button>
              )}
            </span>
          ))}
          {(selected.gradient_colors || []).length < 4 && (
            <button
              type="button"
              onClick={() => updateSelected({
                gradient_colors: [...(selected.gradient_colors || []), [255, 255, 255]],
              })}
            >
              + Add color stop
            </button>
          )}
          <span className="hint">
            2-4 color stops, evenly spaced. Direction is independent of anything else this
            element's own orientation/layout controls -- pick whichever axis looks best.
          </span>
        </div>
      )}
    </>
  );
}

// The SVG-side counterpart to GradientFillControl above: given an
// element and a fallback (non-gradient) fill, returns the actual SVG
// `fill` value to use (either that fallback, or a `url(#id)` reference)
// plus the `<linearGradient>` def to render for it (or null if this
// element isn't using a gradient right now). Every element type that
// got a GradientFillControl in its property panel uses this so the
// on-canvas mockup shows the real gradient immediately instead of a
// flat swatch, matching how every other property on this canvas already
// previews live -- same reasoning bar's own mockup gradient was added
// for. `idPrefix` just needs to be unique per element *type* (element
// ids are already unique on their own, but two different types reusing
// the same literal string as a prefix would still collide if this ever
// runs on the same element for two purposes at once).
//
// Gauge's "diagonal" direction (its original, only-ever corner-to-corner
// sweep, kept as an option in its own property panel -- see
// _element_gauge_gradient() on the backend) doesn't have a clean SVG
// equivalent as a plain 0/1 axis, so it's approximated here as the same
// left-to-right gradient "horizontal" gets; the backend's cairo-based
// gauge renderer (the actual source of truth for what ships to the
// panel) still draws the real corner-to-corner sweep regardless of what
// this preview approximates.
function gradientFill(el, idPrefix, fallbackColor) {
  const stops = el.gradient ? (el.gradient_colors || []) : null;
  if (!stops || stops.length < 2) {
    return { fill: fallbackColor, defs: null };
  }
  const id = `${idPrefix}_${el.id}`;
  const vertical = el.gradient_direction === "vertical";
  const defs = (
    <linearGradient id={id} x1="0" y1="0" x2={vertical ? "0" : "1"} y2={vertical ? "1" : "0"}>
      {stops.map((c, i) => (
        <stop key={i} offset={`${(i / (stops.length - 1)) * 100}%`} stopColor={`rgb(${c[0]}, ${c[1]}, ${c[2]})`} />
      ))}
    </linearGradient>
  );
  return { fill: `url(#${id})`, defs };
}

export default function DashboardCanvas({ frameUrl, connected, dashboardRunning }) {
  const [meta, setMeta] = useState(null);
  const [elements, setElements] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [presetName, setPresetName] = useState("");
  // Which preset card's Delete button is armed, waiting for a second
  // click to confirm -- see the presets grid's render for why (a card
  // click loads it immediately, so a plain Delete button with no
  // confirmation step at all would be one misclick away from losing a
  // saved layout with no undo).
  const [presetPendingDelete, setPresetPendingDelete] = useState(null);
  // Which preset card's "..." action menu (Duplicate / Delete) is open,
  // if any -- collapsing those two actions behind a menu instead of two
  // always-visible full-width buttons is what leaves room for the full
  // preset name (see the preset-card-actions JSX below; long names were
  // getting clipped to "N...", "B...", etc. next to "Duplicate"/"Delete").
  const [presetMenuOpen, setPresetMenuOpen] = useState(null);
  const [guides, setGuides] = useState({ x: null, y: null });
  const [bgDraft, setBgDraft] = useState(null);
  const [bgStatus, setBgStatus] = useState(null);
  const [bgError, setBgError] = useState(null);
  const [npDraft, setNpDraft] = useState(null);
  const [npStatus, setNpStatus] = useState(null);
  const [npError, setNpError] = useState(null);
  const [uploadingId, setUploadingId] = useState(null); // "background" | "nowPlaying" | an element id, or null
  // Seconds remaining in an in-progress "Preview on screen" (see
  // previewLayout() below), or null when none is running -- drives the
  // button's own countdown label so it's obvious when the live panel is
  // about to revert back to whatever's actually saved.
  const [previewSecondsLeft, setPreviewSecondsLeft] = useState(null);
  // A live render of whatever's currently on this canvas -- elements
  // *and* background draft, both possibly unsaved -- using this
  // machine's actual current stats (controller.py's render_dashboard_
  // live_preview()), shown as the canvas's backdrop in place of the
  // live panel photo whenever there's an edit that photo doesn't
  // reflect yet. See the polling effect below for when it runs.
  //
  // Loading a preset used to leave the *old* live photo showing
  // underneath the new preset's SVG mockups -- since nothing had
  // actually been pushed to the panel yet -- which combined two
  // unrelated designs into one broken-looking mess (the old theme's
  // own baked-in text/gauges bleeding through the new one's mockup
  // overlay). A single static thumbnail fixed that mess but traded it
  // for a frozen picture -- gauges that don't move and a live reading
  // that's visibly stale within a second reads as "broken" in a
  // different way. Polling this on an interval is what keeps it
  // genuinely alive: same fix, just re-rendered with fresh numbers
  // instead of rendered once.
  const [editingPreviewUrl, setEditingPreviewUrl] = useState(null);
  // Bumped by saveLayout() on a successful save -- see the polling
  // effect below. Needed because saveLayout() marks things saved by
  // mutating savedElementsRef.current (a ref), not by changing
  // `elements` itself (`elements` is already the just-saved array,
  // that's the whole point) -- so `dirty` (elements !== savedElementsRef
  // .current) flips to false, but a ref mutation alone doesn't retrigger
  // an effect whose dependency array never actually changed. This is a
  // plain "something happened" signal for the effect to notice that.
  const [savedVersion, setSavedVersion] = useState(0);

  const historyRef = useRef([]);
  const futureRef = useRef([]);
  // What's currently saved on the backend, so `dirty` below can tell
  // an honest "you have unsaved changes" state from guesswork -- it
  // drives the Save layout button's enabled/disabled and attention
  // styling directly (see the toolbar JSX), rather than a separate
  // sentence of explanatory text next to it. Reference equality is
  // enough: commit()/undo()/redo()/load() always hand back a *new*
  // array, never mutate elements in place.
  const savedElementsRef = useRef(null);
  // Same idea as savedElementsRef, for the background -- lets `dirty`
  // (and Save layout, below) know whether bgDraft is still just what's
  // actually saved or a staged-but-unpersisted change (a loaded
  // preset's own background, or a hand-edited background field).
  // Compared structurally (backgroundsEqual()), not by reference: unlike
  // elements (always handed a fresh array by commit()/undo()/redo()),
  // updateBgDraft() spreads into a new object on every keystroke, so
  // reference equality here would read as "dirty" on every render.
  const savedBackgroundRef = useRef(null);
  const dragRef = useRef(null); // {id, mode: 'move'|'resize', beforeElements}
  const svgRef = useRef(null);
  // The hidden <input type="file"> behind the "Import theme" button.
  const importInputRef = useRef(null);

  const load = useCallback(() => {
    setError(null);
    api.getDashboardMeta().then(
      (m) => {
        setMeta(m);
        setElements(m.elements);
        savedElementsRef.current = m.elements;
        setBgDraft(m.background);
        savedBackgroundRef.current = m.background;
        setNpDraft(m.nowPlaying);
        historyRef.current = [];
        futureRef.current = [];
        setSelectedId(null);
      },
      (e) => setError(e.message)
    );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Keeps editingPreviewUrl fresh (see its own comment) while there's
  // an edit the real live panel photo doesn't reflect yet -- any
  // difference from `savedElementsRef.current` *or* `savedBackgroundRef
  // .current` covers a loaded preset exactly the same way it covers a
  // drag, a property edit, undo/redo, Reset to defaults, or a
  // background-only change (picking a different mode/scheme/photo with
  // no element touched at all -- elements alone used to miss this case
  // entirely, since `elements` stays the same reference until commit()
  // hands back a new one, so a background-only edit's live backdrop
  // silently kept showing the *old* background until Save background
  // was clicked). Skipped during an active "Preview on screen" countdown
  // (previewSecondsLeft !== null): the real panel is showing this
  // exact design for real during that window (previewLayout() just
  // pushed it), so the real live frame is already a more accurate
  // backdrop than a second render of the same thing done here.
  // `savedVersion` is in the dependency list purely so saveLayout()'s
  // save can retrigger this: it marks things saved by mutating
  // savedElementsRef.current/savedBackgroundRef.current (refs), not by
  // changing `elements`/`bgDraft` themselves (they're already the
  // just-saved values), and a ref mutation alone doesn't retrigger an
  // effect whose actual dependencies never changed -- without this, the
  // poll from before the save would just keep ticking forever with a
  // stale closure, never noticing `dirty` had gone false.
  useEffect(() => {
    const unsaved =
      elements &&
      (elements !== savedElementsRef.current || !backgroundsEqual(bgDraft, savedBackgroundRef.current));
    if (!unsaved || previewSecondsLeft !== null) {
      setEditingPreviewUrl(null);
      return;
    }
    let cancelled = false;
    const tick = () => {
      api.renderDashboardLivePreview(elements, bgDraft).then(
        (r) => {
          if (!cancelled) setEditingPreviewUrl(r.image);
        },
        () => {} // a failed poll just leaves the previous frame showing -- not worth a page-level error for a background convenience refresh
      );
    };
    tick();
    const id = setInterval(tick, EDITING_PREVIEW_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [elements, bgDraft, previewSecondsLeft, savedVersion]);

  // A preset card's Delete button arms on the first click and only
  // actually deletes on a second (see deletePreset()); auto-disarming
  // it after a few seconds means walking away from an armed card
  // doesn't leave a silent "one misclick from deleting this" trap for
  // whenever it's clicked next, for an unrelated reason, later.
  useEffect(() => {
    if (!presetPendingDelete) return;
    const t = setTimeout(() => setPresetPendingDelete(null), 3000);
    return () => clearTimeout(t);
  }, [presetPendingDelete]);

  // Close an open preset action menu on any click outside it (the menu
  // and its "..." toggle both carry data-preset-menu, so a click that
  // lands on either is left alone).
  useEffect(() => {
    if (!presetMenuOpen) return;
    const onDocClick = (e) => {
      if (e.target.closest("[data-preset-menu]")) return;
      setPresetMenuOpen(null);
    };
    document.addEventListener("pointerdown", onDocClick);
    return () => document.removeEventListener("pointerdown", onDocClick);
  }, [presetMenuOpen]);

  const commit = useCallback((next) => {
    setElements((prev) => {
      historyRef.current = [...historyRef.current, prev];
      if (historyRef.current.length > 100) historyRef.current.shift();
      return next;
    });
    futureRef.current = [];
  }, []);

  const undo = useCallback(() => {
    setElements((prev) => {
      const h = historyRef.current;
      if (h.length === 0) return prev;
      const previous = h[h.length - 1];
      historyRef.current = h.slice(0, -1);
      futureRef.current = [...futureRef.current, prev];
      return previous;
    });
  }, []);

  const redo = useCallback(() => {
    setElements((prev) => {
      const f = futureRef.current;
      if (f.length === 0) return prev;
      const next = f[f.length - 1];
      futureRef.current = f.slice(0, -1);
      historyRef.current = [...historyRef.current, prev];
      return next;
    });
  }, []);

  // Keyboard undo/redo -- only while this panel is mounted (the
  // Dashboard tab is selected), same scoping as everything else here.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const key = e.key.toLowerCase();
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (key === "y" || (key === "z" && e.shiftKey)) {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // Arrow-key nudging for the selected element -- dragging with a
  // mouse is the only way to reposition anything otherwise, which is
  // fiddly for pixel-level alignment (especially the small mini-gauges,
  // where a drag can easily overshoot). Plain arrow = 1% of the panel,
  // Shift+arrow = 5%, matching the field labels' own "X %"/"Y %" units
  // so the step sizes read the same way as the numeric inputs below.
  // Each press commits (via updateSelected -> commit) so Undo steps
  // through individual nudges, same as a drag does.
  const NUDGE_STEP = 0.01;
  const NUDGE_STEP_FAST = 0.05;
  useEffect(() => {
    const onKey = (e) => {
      if (!selectedId) return;
      if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
      // Don't hijack arrow keys while editing a text field, a <select>,
      // etc. -- those need normal cursor/selection behavior.
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      e.preventDefault();
      const step = e.shiftKey ? NUDGE_STEP_FAST : NUDGE_STEP;
      const el = elements?.find((it) => it.id === selectedId);
      if (!el) return;
      const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
      const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
      commit(elements.map((it) => (it.id === selectedId
        ? { ...it, x: clamp(it.x + dx, 0, 1), y: clamp(it.y + dy, 0, 1) }
        : it)));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, elements, commit]);

  // `rect` is optional -- pass the one snapshotted at the start of a
  // drag gesture (see onPointerDownGauge/onPointerDownHandle below)
  // rather than re-querying getBoundingClientRect() on every single
  // pointermove. That re-query used to be exactly what made a drag
  // track slightly wrong: selecting/moving an element flips `dirty`
  // true on its very first pointermove (see the `dirty` computation
  // above), which reveals the "* You have unsaved changes" hint line
  // above the canvas -- a real DOM reflow that shifts the canvas box
  // (and therefore this rect) down by however tall that line is, mid-
  // gesture. Every pointermove after the first was then computing its
  // fraction against a rect whose top had silently moved out from
  // under the still-held cursor, so the element drifted from the
  // cursor by that same shift instead of tracking it exactly. A static
  // page can still call this with no rect (initial mount, etc.); a
  // drag never should.
  const pointerToFraction = (e, rect) => {
    const r = rect || svgRef.current.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    return { x: clamp(x, 0, 1), y: clamp(y, 0, 1) };
  };

  const onPointerDownGauge = (el) => (e) => {
    e.stopPropagation();
    e.target.setPointerCapture(e.pointerId);
    setSelectedId(el.id);
    const rect = svgRef.current.getBoundingClientRect();
    // Recorded here (not just at drag time) so a move preserves *where
    // on the element* you actually grabbed it -- without this, the
    // very first pointermove snapped the element's x/y straight to the
    // cursor's own fraction, so clicking near a gauge's edge (rather
    // than dead center) made it visibly jump until that edge was under
    // the cursor instead of moving smoothly from wherever it already
    // was. offsetX/offsetY is the fixed gap between the cursor and the
    // element's center at the moment of the grab; onPointerMove below
    // subtracts it back out of every subsequent cursor position, so
    // the element tracks the cursor's *movement*, not its raw position.
    const { x: px, y: py } = pointerToFraction(e, rect);
    dragRef.current = {
      id: el.id, mode: "move", beforeElements: elements,
      offsetX: px - el.x, offsetY: py - el.y, rect,
      startClientX: e.clientX, startClientY: e.clientY, moved: false,
    };
  };

  // `axis` picks which dimension(s) a given handle drags -- "both" (the
  // original corner handle, still there for a quick freeform resize),
  // "width", or "height". Only graph/image/media elements have
  // independent width/height to begin with (a gauge resizes via one
  // `radius`, text via one `font_size`), so this is a no-op for those.
  const onPointerDownHandle = (el, axis = "both") => (e) => {
    e.stopPropagation();
    e.target.setPointerCapture(e.pointerId);
    setSelectedId(el.id);
    const rect = svgRef.current.getBoundingClientRect();
    dragRef.current = {
      id: el.id, mode: "resize", axis, beforeElements: elements, rect,
      startClientX: e.clientX, startClientY: e.clientY, moved: false,
    };
  };

  const onPointerMove = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (!drag.moved) {
      const dxScreen = e.clientX - drag.startClientX;
      const dyScreen = e.clientY - drag.startClientY;
      if (Math.sqrt(dxScreen * dxScreen + dyScreen * dyScreen) < MOVE_THRESHOLD_PX) {
        // Still within a click's worth of jitter -- see MOVE_THRESHOLD_PX
        // above. Do nothing: no setElements call means `elements` keeps
        // its exact prior reference, so `dirty` (a `!==` check) stays
        // false and Save layout doesn't light up for a plain click.
        return;
      }
      drag.moved = true;
    }
    const { x: px, y: py } = pointerToFraction(e, drag.rect);
    setElements((prev) => {
      const el = prev.find((it) => it.id === drag.id);
      if (!el) return prev;
      if (drag.mode === "move") {
        // pointerToFraction() already clamped px/py to [0, 1], but
        // subtracting the grab offset can push the result outside that
        // range (e.g. grabbing an element near its left edge and
        // dragging to x=0 would otherwise put its *center*, not the
        // grabbed edge, at a negative x) -- clamp again after.
        let nx = clamp(px - (drag.offsetX ?? 0), 0, 1);
        let ny = clamp(py - (drag.offsetY ?? 0), 0, 1);
        const others = prev.filter((it) => it.id !== drag.id);
        let snapX = null;
        let snapY = null;
        const xCandidates = [0.5, ...others.map((o) => o.x)];
        const yCandidates = [0.5, ...others.map((o) => o.y)];
        for (const c of xCandidates) {
          if (Math.abs(nx - c) < SNAP_THRESHOLD) {
            nx = c;
            snapX = c;
            break;
          }
        }
        for (const c of yCandidates) {
          if (Math.abs(ny - c) < SNAP_THRESHOLD) {
            ny = c;
            snapY = c;
            break;
          }
        }
        setGuides({ x: snapX, y: snapY });
        return prev.map((it) => (it.id === drag.id ? { ...it, x: nx, y: ny } : it));
      }
      // resize: gauge is a circle so one radius (a fraction of
      // min(REF_W, REF_H), matching how dashboard_theme.py resolves
      // it) does it, from the pixel distance between the element's
      // center and the pointer -- computed in pixel space first since
      // x/y and radius are fractions of different bases (width vs.
      // min(width, height)). Graph/image are rectangles with their own
      // independent width/height instead, resized the same way but on
      // each axis separately; text has no box at all, so its "handle"
      // scales font_size off the vertical drag distance instead.
      const dxPx = px * REF_W - el.x * REF_W;
      const dyPx = py * REF_H - el.y * REF_H;
      if (el.type === "graph" || el.type === "bar" || el.type === "image" || el.type === "media" || el.type === "weather") {
        // `axis` (set by which handle was grabbed -- see
        // onPointerDownHandle) picks whether this drag touches width,
        // height, or both -- e.g. dragging the right-edge handle
        // straight up shouldn't also shrink the height.
        const axis = drag.axis || "both";
        const patch = {};
        if (axis !== "height") patch.width = clamp((Math.abs(dxPx) * 2) / REF_W, 0.04, 0.9);
        if (axis !== "width") patch.height = clamp((Math.abs(dyPx) * 2) / REF_H, 0.04, 0.9);
        return prev.map((it) => (it.id === drag.id ? { ...it, ...patch } : it));
      }
      // A clock's resize handle means something different per face --
      // width/height for an image face (it's a picture box, same as an
      // image element), radius for an analog face (it's a circle, same
      // as a gauge), font_size for a digital face (it's just text,
      // same as a text element).
      if (el.type === "clock" && el.face === "image") {
        const axis = drag.axis || "both";
        const patch = {};
        if (axis !== "height") patch.width = clamp((Math.abs(dxPx) * 2) / REF_W, 0.04, 0.9);
        if (axis !== "width") patch.height = clamp((Math.abs(dyPx) * 2) / REF_H, 0.04, 0.9);
        return prev.map((it) => (it.id === drag.id ? { ...it, ...patch } : it));
      }
      if (el.type === "text" || (el.type === "clock" && (!el.face || el.face === "digital"))) {
        const font_size = clamp((Math.abs(dyPx) * 2) / REF_H, 0.02, 0.25);
        return prev.map((it) => (it.id === drag.id ? { ...it, font_size } : it));
      }
      const distPx = Math.sqrt(dxPx * dxPx + dyPx * dyPx);
      const radius = clamp(distPx / Math.min(REF_W, REF_H), MIN_RADIUS, MAX_RADIUS);
      return prev.map((it) => (it.id === drag.id ? { ...it, radius } : it));
    });
  };

  const endDrag = () => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    setGuides({ x: null, y: null });
    // If the gesture never crossed MOVE_THRESHOLD_PX, onPointerMove never
    // touched `elements` at all -- it's just a click/selection, not a
    // move/resize, so there's nothing to undo and pushing beforeElements
    // here would only clutter the undo stack with a no-op entry.
    if (!drag.moved) return;
    setElements((current) => {
      historyRef.current = [...historyRef.current, drag.beforeElements];
      if (historyRef.current.length > 100) historyRef.current.shift();
      futureRef.current = [];
      return current;
    });
  };

  const updateSelected = (patch) => {
    if (!selectedId || !elements) return;
    commit(elements.map((el) => (el.id === selectedId ? { ...el, ...patch } : el)));
  };

  const addElement = (type) => {
    if (!elements || !meta) return;
    const el = makeElement(type, elements, meta);
    if (!el) return;
    if (meta.widgetStyles?.[bgDraft?.widget_style]) {
      el.color = null;
      el.color2 = null;
      el.font = null;
      el.gradient = false;
    }
    commit([...elements, el]);
    setSelectedId(el.id);
  };

  const deleteSelected = () => {
    if (!selectedId || !elements) return;
    commit(elements.filter((el) => el.id !== selectedId));
    setSelectedId(null);
  };

  const centerHorizontally = () => updateSelected({ x: 0.5 });
  const centerVertically = () => updateSelected({ y: 0.5 });

  const bringToFront = () => {
    if (!selectedId || !elements) return;
    const maxZ = elements.reduce((m, el) => Math.max(m, el.z ?? 0), -1);
    updateSelected({ z: maxZ + 1 });
  };

  const sendToBack = () => {
    if (!selectedId || !elements) return;
    const minZ = elements.reduce((m, el) => Math.min(m, el.z ?? 0), 0);
    updateSelected({ z: minZ - 1 });
  };

  const resetToDefaults = () => {
    if (!meta) return;
    commit(meta.defaults);
    setSelectedId(null);
  };

  // Save layout now saves the *whole canvas* -- elements and, if it's
  // staged and different from what's actually saved, the background
  // too -- in one action. It used to only ever touch elements, leaving
  // a loaded preset's background sitting in bgDraft until a *second*,
  // separate "Save background" click down in the Background section.
  // Reported directly: "pressing a preset, and pressing save layout
  // doesn't update the background" -- exactly right, and the same
  // trap "Preview on screen" used to have before it was fixed to send
  // both together (see previewDashboardElements()'s own comment) --
  // Save layout just never got the matching fix at the time. A
  // background-only edit (no element touched) now saves correctly too,
  // instead of silently needing its own separate button press.
  const saveLayout = () => {
    const backgroundNeedsSaving = bgDraft && !backgroundsEqual(bgDraft, savedBackgroundRef.current);
    const saveElements = api.saveDashboardElements(elements);
    const saveBg = backgroundNeedsSaving ? api.saveDashboardBackground(bgDraft) : Promise.resolve(null);
    return Promise.all([saveElements, saveBg]).then(
      ([, savedBg]) => {
        savedElementsRef.current = elements;
        if (backgroundNeedsSaving) {
          savedBackgroundRef.current = savedBg;
          setBgDraft(savedBg);
          setBgStatus(null);
        }
        // Retriggers the live-preview polling effect (see its own
        // comment on why a plain ref mutation above isn't enough on its
        // own) so it notices `dirty` just went false, stops polling,
        // and the canvas goes back to the real live panel photo, which
        // is about to actually match this layout (and background) on
        // its own next frame.
        setSavedVersion((v) => v + 1);
        setStatus(
          backgroundNeedsSaving
            ? "Layout and background saved -- applies live, even while the dashboard is already running."
            : "Layout saved -- applies live, even while the dashboard is already running."
        );
      },
      (e) => setError(e.message)
    );
  };

  // "Preview on screen": shows whatever's currently on this canvas --
  // saved or not -- on the real panel for PREVIEW_SECONDS, then the
  // backend reverts it back to whatever's actually saved on its own
  // (controller.py's preview_dashboard_elements()/
  // _revert_dashboard_preview()). Requested directly: Save layout is a
  // real commitment (it's what a fresh Start/Apply, or anyone else
  // looking at the panel later, will keep showing), and the only way to
  // see an edit on the actual hardware before this was to save it,
  // decide you don't like it, and Undo -- annoying for something as
  // quick as "does this gradient direction actually look better than
  // the other one on the real screen". The countdown here is purely a
  // client-side display (setInterval ticking a local counter down) --
  // the backend's own revert timer is what actually matters and runs
  // independently of whether this tab stays open, so a closed tab or a
  // page reload mid-preview doesn't leave the panel stuck showing an
  // unsaved preview forever.
  const previewLayout = () => {
    if (previewSecondsLeft !== null) return; // one preview at a time from this tab
    // Sends bgDraft along with elements -- previewing a freshly-loaded
    // preset used to only push its *elements* live, leaving the panel's
    // actual background untouched until a separate "Save background"
    // click, so "Preview on screen" right after loading a preset showed
    // its layout over the *previous* preset's background. See
    // preview_dashboard_elements()'s own updated comment on the backend
    // side for how the revert now covers both.
    api.previewDashboardElements(elements, bgDraft, PREVIEW_SECONDS).then(
      () => {
        setPreviewSecondsLeft(PREVIEW_SECONDS);
        // No explicit "stop the live-preview backdrop" step needed --
        // the polling effect's own previewSecondsLeft dependency turns
        // it off on its next run, since the real live photo is about to
        // actually show this exact preview for real.
        const started = Date.now();
        const tick = () => {
          const remaining = PREVIEW_SECONDS - (Date.now() - started) / 1000;
          if (remaining <= 0) {
            setPreviewSecondsLeft(null);
          } else {
            setPreviewSecondsLeft(Math.ceil(remaining));
            setTimeout(tick, 250);
          }
        };
        setTimeout(tick, 250);
      },
      (e) => setError(e.message)
    );
  };

  // Shared by every "pick an image" field on this canvas (background,
  // now-playing placeholder, an image element) -- a browser file input
  // can only hand back the picked file's bytes, never a real
  // filesystem path, so this always goes through an actual upload:
  // the backend copies it into its own managed image folder
  // (image_store.py) and hands back that copy's path, which is what
  // actually gets saved. `uploadingId` just drives a "Uploading..."
  // label near whichever field is mid-upload.
  // `onStored` gets (path, dims) -- dims is the picked file's own
  // {width, height} in pixels (or null if it couldn't be read), read
  // client-side in parallel with the upload so the image element case
  // can size its box to match, see makeElement()'s comment.
  const uploadImage = (id, file, onStored, onError) => {
    if (!file) return;
    setUploadingId(id);
    Promise.all([api.uploadDashboardImage(file), readImageDimensions(file)]).then(
      ([r, dims]) => {
        setUploadingId(null);
        onStored(r.path, dims);
      },
      (e) => {
        setUploadingId(null);
        onError(e.message);
      }
    );
  };

  const updateBgDraft = (patch) => {
    setBgStatus(null);
    setBgDraft((prev) => ({ ...prev, ...patch }));
  };

  // Kept as its own button for a background-only change (no element
  // touched) made straight in the Background section, separately from
  // whatever's on the canvas -- Save layout above now also covers a
  // staged background, so the two buttons overlap on purpose rather
  // than one replacing the other.
  const saveBackground = () => {
    if (!bgDraft) return;
    setBgError(null);
    api.saveDashboardBackground(bgDraft).then(
      (bg) => {
        setBgDraft(bg);
        savedBackgroundRef.current = bg;
        // Retriggers the live-preview polling effect the same way
        // saveLayout()'s own savedVersion bump does -- a ref mutation
        // alone (savedBackgroundRef.current above) doesn't retrigger an
        // effect whose dependency array never actually changed, so
        // without this a background-only save would clear `dirty` but
        // leave the previous poll ticking on a stale closure.
        setSavedVersion((v) => v + 1);
        setBgStatus("Background saved -- applies live, even while the dashboard is already running.");
      },
      (e) => setBgError(e.message)
    );
  };

  const updateNpDraft = (patch) => {
    setNpStatus(null);
    setNpDraft((prev) => ({ ...prev, ...patch }));
  };

  const saveNowPlaying = () => {
    if (!npDraft) return;
    setNpError(null);
    api.saveDashboardNowPlaying({
      default_art_path: npDraft.default_art_path || null,
      not_playing_message: npDraft.not_playing_message || null,
    }).then(
      (dashboardCfg) => {
        setNpDraft((prev) => ({ ...prev, default_art_path: dashboardCfg.default_art_path,
                                 not_playing_message: dashboardCfg.not_playing_message }));
        setNpStatus("Saved -- applies live, even while the dashboard is already running.");
      },
      (e) => setNpError(e.message)
    );
  };

  // The app's own presets (dashboard_theme.BUILTIN_DASHBOARD_PRESETS,
  // sent along in meta) are read-only: they can be loaded, duplicated
  // and hidden, but never written over -- saving under one of their
  // names saves a copy instead. Used for the card badge, the
  // save-name warning, and the wording of a delete.
  const isBuiltinPreset = (name) => (meta?.builtinPresets || []).includes(name);

  const saveAsPreset = () => {
    const name = presetName.trim();
    if (!name) return;
    // Captures the current background draft alongside the layout, so
    // loading this preset back later restores the exact look it was
    // saved with -- not just the element positions against whatever
    // background happens to be configured at load time. See
    // AppController.save_dashboard_preset()'s own docstring.
    api.saveDashboardPreset(name, elements, bgDraft).then(
      (r) => {
        setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails }));
        setPresetName("");
        // The backend decides the final name: saving under a built-in's
        // name saves a copy ("Fusion Core (custom)") rather than
        // overwriting a read-only preset, so report what it actually
        // did instead of echoing what was typed.
        const saved = r.name || name;
        setStatus(saved === name
          ? `Saved preset "${saved}".`
          : `"${name}" is a built-in preset and can't be overwritten -- saved your version as "${saved}".`);
      },
      (e) => setError(e.message)
    );
  };

  const restoreBuiltins = () => {
    api.restoreBuiltinDashboardPresets().then(
      (r) => {
        setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails,
                           dismissedBuiltinPresets: [] }));
        setStatus(r.restored?.length
          ? `Restored ${r.restored.length} built-in preset${r.restored.length === 1 ? "" : "s"}.`
          : "No deleted built-in presets to restore.");
      },
      (e) => setError(e.message)
    );
  };

  const loadPreset = (name) => {
    const preset = meta?.presets?.[name];
    if (!preset) return;
    commit(preset.elements);
    setSelectedId(null);
    // A preset with its own saved background (see saveAsPreset() above)
    // stages it into the background draft too -- same "loaded, not yet
    // persisted" deal commit() already gives elements: "Save layout"/
    // "Save background" are still what actually pushes either one to
    // the physical panel. A preset with no background of its own
    // (every preset saved before this existed) leaves the current
    // background draft alone rather than clearing it to something.
    if (preset.background) setBgDraft(preset.background);
    // No explicit "show a preview" step needed beyond the commit()/
    // setBgDraft() above -- both just landed `elements`/`bgDraft` in a
    // state that differs from what's actually saved, and the
    // editingPreviewUrl polling effect picks up on exactly that (any
    // unsaved edit, not just a freshly-loaded preset) to start showing
    // a live-rendered backdrop of this new design in place of the live
    // panel photo, which still shows whatever the *previous* theme
    // looked like until an actual Save/Preview pushes this one.
    // Save layout alone now applies both (see its own comment) --
    // used to need a second, separate "Save background" click too,
    // which this message wrongly implied was required either way.
    setStatus(`Loaded preset "${name}" -- Save layout to apply.`);
  };

  // Starts a theme from nothing: saves an empty preset and drops the
  // canvas straight into editing it. Until this existed, every preset
  // was necessarily derived from another one -- Duplicate copies a
  // card, and "Save current layout as preset" bottles up whatever
  // happens to be on the canvas -- so "I want to build one of my own"
  // meant first dismantling somebody else's layout element by element.
  //
  // Deliberately empty rather than seeded with the default 8 gauges:
  // "Reset to defaults" in the toolbar above already puts that layout
  // on the canvas for anyone who wants to start from it, so seeding it
  // here would just make this a second, worse Duplicate. The one thing
  // it does carry over is the background draft currently in effect --
  // starting on a black rectangle would be a strange definition of
  // "blank", and the Background section right below changes it.
  const createNewTheme = () => {
    const existing = new Set(Object.keys(meta.presets || {}));
    let name = "New theme";
    let n = 2;
    while (existing.has(name)) {
      name = `New theme ${n}`;
      n += 1;
    }
    api.saveDashboardPreset(name, [], bgDraft).then(
      (r) => {
        const saved = r.name || name;
        setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails }));
        // Same two moves loadPreset() makes, minus the lookup: this
        // preset's elements are empty by construction and its
        // background is the draft that's already staged.
        commit([]);
        setSelectedId(null);
        // Prefilled so "Save as preset" updates this card rather than
        // spawning another one -- saving over your own preset
        // overwrites in place (built-ins are the ones that copy).
        setPresetName(saved);
        setStatus(`Started "${saved}" -- an empty theme. Add elements above, then Save layout to put it on the panel, or Save as preset to update this card.`);
      },
      (e) => setError(e.message)
    );
  };

  // Export writes the preset out as a .json the user can send to
  // someone else; the backend inlines its images first so the file
  // stands on its own (see export_dashboard_preset()). Downloading is
  // done the only way a browser can -- a Blob URL behind a synthetic
  // <a download> click.
  const exportPreset = (name) => {
    setPresetMenuOpen(null);
    api.exportDashboardPreset(name).then(
      (r) => {
        const blob = new Blob([JSON.stringify(r.preset, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${name.replace(/[^\w.-]+/g, "_").replace(/^_|_$/g, "") || "theme"}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Revoked on the next tick rather than immediately -- some
        // browsers cancel an in-flight download if the URL dies first.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        setStatus(`Exported "${name}" -- the file includes its background image, so it works as-is on another machine.`);
      },
      (e) => setError(e.message)
    );
  };

  // Import reads the picked .json here (a file input is the only way a
  // browser hands over file contents) and posts it; the backend
  // validates the shape, writes any inlined images into its own image
  // store, and ignores image paths that didn't travel with the file.
  const importPresetFile = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onerror = () => setError(`Couldn't read "${file.name}".`);
    reader.onload = () => {
      let parsed;
      try {
        parsed = JSON.parse(reader.result);
      } catch (err) {
        setError(`"${file.name}" isn't valid JSON: ${err.message}`);
        return;
      }
      // Name it after the file, since a preset file carries a layout,
      // not a name -- the backend uniquifies it if that's taken.
      // "nocturne_cathedral.json" reads better in the picker as
      // "Nocturne Cathedral", so separators become spaces and an
      // all-lowercase name gets title-cased. A name that already has
      // capitals ("GPU Monitor") is left exactly as the sender wrote
      // it rather than being "helpfully" mangled into "Gpu Monitor".
      let base = file.name.replace(/\.json$/i, "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
      if (base && base === base.toLowerCase()) {
        base = base.replace(/\b\w/g, (ch) => ch.toUpperCase());
      }
      base = base || "Imported theme";
      api.importDashboardPreset(base, parsed).then(
        (r) => {
          setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails }));
          setStatus(`Imported "${r.name || base}". Click its card to load it.`);
        },
        (e) => setError(`Couldn't import "${file.name}": ${e.message}`)
      );
    };
    reader.readAsText(file);
  };

  const duplicatePreset = (name) => {
    // Works on any card -- one of the app's own built-ins (dashboard_
    // theme.BUILTIN_DASHBOARD_PRESETS) exactly as well as something a
    // person saved themselves, since meta.presets is already the
    // merged view (config_store.resolve_dashboard_presets()) and this
    // just reads whatever's sitting there under `name`. Saving the
    // copy under a new name is a completely ordinary save -- there's
    // nothing built-in-specific about it -- so it's just
    // api.saveDashboardPreset() again, the same call "Save current
    // layout as preset" below already makes.
    const preset = meta?.presets?.[name];
    if (!preset) return;
    const existing = new Set(Object.keys(meta.presets || {}));
    let copyName = `${name} (copy)`;
    let n = 2;
    while (existing.has(copyName)) {
      copyName = `${name} (copy ${n})`;
      n += 1;
    }
    setPresetMenuOpen(null);
    api.saveDashboardPreset(copyName, preset.elements, preset.background).then(
      (r) => {
        setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails }));
        setStatus(`Duplicated "${name}" as "${copyName}".`);
      },
      (e) => setError(e.message)
    );
  };

  const deletePreset = (name) => {
    if (!name) return;
    // First click on a card's Delete button just arms it (see
    // presetPendingDelete's own comment) -- this is the second click,
    // the one that actually deletes. The menu stays open across the
    // confirm click so "Confirm?" has somewhere to render; it only
    // closes once the delete actually goes through.
    if (presetPendingDelete !== name) {
      setPresetPendingDelete(name);
      return;
    }
    setPresetPendingDelete(null);
    setPresetMenuOpen(null);
    api.deleteDashboardPreset(name).then(
      (r) => {
        setMeta((m) => ({ ...m, presets: r.presets, presetThumbnails: r.thumbnails,
                           dismissedBuiltinPresets: r.dismissed ?? m.dismissedBuiltinPresets }));
        setStatus(isBuiltinPreset(name)
          ? `Hid built-in preset "${name}" -- "Restore built-ins" below brings it back.`
          : `Deleted preset "${name}".`);
      },
      (e) => setError(e.message)
    );
  };

  if (!elements || !meta) {
    return (
      <section className="panel">
        <h2>Dashboard layout</h2>
        {error ? <p className="error">{error}</p> : <p className="hint">Loading layout…</p>}
      </section>
    );
  }

  const selected = elements.find((el) => el.id === selectedId) || null;
  const styledSelected = selected ? resolveWidgetStyle(selected, bgDraft, meta.widgetStyles) : null;
  const usesWidgetStyle = !!meta.widgetStyles?.[styledSelected?.widget_style];
  const ordered = elements.map((el) => resolveWidgetStyle(el, bgDraft, meta.widgetStyles))
    .sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
  // Includes a staged-but-unsaved background now too, not just
  // elements -- Save layout persists both together (see its own
  // comment), so the button's enabled/attention state needs to reflect
  // a background-only change exactly the same way it already reflects
  // an element-only one, instead of only lighting up for the latter.
  const dirty = elements !== savedElementsRef.current || !backgroundsEqual(bgDraft, savedBackgroundRef.current);
  // Forces every element's mockup to show, not just the selected one --
  // needed for Reset to defaults (and Undo/Redo landing on a state with
  // nothing selected), which touches every element at once and leaves
  // none of them selected, so `isSelected` alone wouldn't make any of
  // them show up-to-date. Gated on `!selectedId` so an ordinary drag or
  // property-panel edit -- which always has the element being edited
  // selected -- doesn't trip this: `isSelected` already covers that
  // element on its own, and this used to also force every *other*,
  // untouched element's mockup box/border to flash into view for the
  // whole gesture (a plain drag on one gauge lit up the image/media
  // elements' borders too), which read as those other elements getting
  // highlighted for no reason.
  // `&& !editingPreviewUrl` carves out the one case where forcing every
  // mockup on would be actively wrong instead of just unnecessary: with
  // a fresh live-rendered backdrop already showing every element in its
  // current position (updated on its own polling interval -- see
  // editingPreviewUrl's own comment), forcing the SVG copies on top too
  // would double-draw the same content a second time -- the same kind
  // of ghosting the showMockup gates elsewhere exist to prevent, just
  // against an accurate backdrop instead of a stale one this time.
  // There's a brief window right after a change lands, before the next
  // poll tick resolves, where this still falls back to forcing mockups
  // on over the stale real frame (self-heals within one
  // EDITING_PREVIEW_POLL_MS once editingPreviewUrl catches up).
  const forceAllMockups = dirty && !selectedId && !editingPreviewUrl;
  // Whatever's currently showing as .canvas-frame -- the real live
  // panel photo, or editingPreviewUrl standing in for it -- is an
  // accurate backdrop for the element mockups to defer to. Every
  // showMockup/showClockMockup gate below used to compute its own local
  // `connected && !!frameUrl` for this; kept as one shared value now
  // that there are two possible accurate backdrops instead of one, so
  // every gate treats them the same way rather than only some of them
  // learning about editingPreviewUrl.
  const hasAccurateBackdrop = !!editingPreviewUrl || (connected && !!frameUrl);

  return (
    <section className="panel">
      <h2>Dashboard layout</h2>
      <p className="hint">
        Drag an element to move it, click to select. A selected element with independent
        width/height (graph, bar, image, now-playing) gets three resize handles: the one on
        its right edge changes width only, the one on its bottom edge changes height only,
        and the corner changes both. Everything below updates live in this box as you edit --
        no need to save just to see it, though Save layout is what actually pushes it to the
        physical panel (instantly, if the dashboard's already running -- no need to
        Stop/Start). Not sure about a change yet? "Preview on screen" shows exactly what's
        on this canvas right now (saved or not) on the physical panel for a few seconds,
        then reverts back to whatever's actually saved -- no commitment, and Undo isn't
        needed either way.
        {connected ? " It's overlaid on the panel's live frame too." : " Start the Dashboard to also see it over the live frame."}
      </p>

      {/* Two groups, not one flat row: "add a thing" (plus undo/redo/
          reset, which act on the layout you're building, same
          category) on the left, and "commit the layout" on the right --
          space-between plus this being the toolbar's only two children
          is what pushes Save layout to the far right edge. It used to
          sit inline with the "+ Add X" buttons, reading as just another
          one of them, even though it's a fundamentally different kind
          of action (it's the one that actually reaches the physical
          panel) -- separating it, and disabling it outright while
          there's nothing to save (see `dirty` below) rather than
          relying on a separate "* you have unsaved changes" sentence
          to explain that, makes what it does and when it matters
          obvious from the button alone. */}
      <div className="canvas-toolbar">
        <div className="canvas-toolbar-group">
          <button onClick={() => addElement("gauge")}>+ Add gauge</button>
          <button onClick={() => addElement("text")}>+ Add text</button>
          <button onClick={() => addElement("graph")}>+ Add graph</button>
          <button onClick={() => addElement("bar")}>+ Add bar</button>
          <button onClick={() => addElement("image")}>+ Add image</button>
          <button onClick={() => addElement("media")}>+ Add now-playing</button>
          <button onClick={() => addElement("clock")}>+ Add clock</button>
          <button onClick={() => addElement("weather")}>+ Add weather</button>
          <button onClick={undo} disabled={historyRef.current.length === 0}>Undo</button>
          <button onClick={redo} disabled={futureRef.current.length === 0}>Redo</button>
          <button onClick={resetToDefaults}>Reset to defaults</button>
        </div>
        <div className="canvas-toolbar-group">
          <button
            onClick={previewLayout}
            disabled={!dashboardRunning || previewSecondsLeft !== null}
            title={
              !dashboardRunning
                ? "Start the Dashboard theme to preview on the real panel"
                : previewSecondsLeft !== null
                ? "Already previewing"
                : `Show the current (even unsaved) layout on the physical panel for ${PREVIEW_SECONDS}s, then revert`
            }
          >
            {previewSecondsLeft !== null ? `Previewing... ${previewSecondsLeft}s` : "Preview on screen"}
          </button>
          <button onClick={saveLayout} disabled={!dirty} className={dirty ? "btn-attention" : undefined}
                  title={dirty ? "Push these changes to the physical panel" : "No unsaved changes"}>
            {dirty ? "Save layout*" : "Save layout"}
          </button>
        </div>
      </div>

      {/* The canvas itself (the live preview -- it's what the panel
          actually looks like, dragged elements and all) and everything
          about the currently-selected element sit side by side here,
          not stacked -- this is the one screen where preview and
          config genuinely need to be looked at at the same time (drag
          something on the left, watch its numbers change on the
          right), so stacking them the way the rest of this page used
          to is exactly the scrolling problem this whole redesign is
          about. Background/presets stay below, full width -- those are
          occasional, not part of the moment-to-moment drag-and-tweak
          loop this split is for. */}
      <div className="canvas-layout">
        <div className="canvas-main">
          <div
            className="canvas-box"
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerLeave={endDrag}
          >
        {/* Backdrop for the SVG mockups below: normally this <img> is a
            snapshot of what the physical panel is showing *right now*
            -- kept visible and continuously refreshing regardless of
            `dirty`, so the canvas never drops to a plain black
            background just because there's an unsaved edit in
            progress. (An earlier version hid this photo while dirty, to
            stop it from reading as a stale preview after Reset to
            defaults -- but "stale photo behind live mockups" is a much
            smaller problem than "background goes black/disappears the
            moment you touch anything", so that trade got reversed. The
            mockups below now carry the "does this reflect my unsaved
            edit" job on their own -- via plain `isSelected` for the
            element actually being edited, and `forceAllMockups` (dirty
            with nothing selected) for a mass change like Reset to
            defaults -- instead of this image doing it by
            disappearing.)

            `editingPreviewUrl` overrides it whenever there's an edit
            (see its own comment) the live photo above doesn't reflect
            yet -- freshly loaded preset, drag, property change,
            whatever: the live photo would still be showing the
            *previous* design at that point, and layering the new
            design's forceAllMockups-driven overlay on top of that used
            to look like two designs smashed together, not "here's what
            I'm editing". A polled live render of the current design is
            an accurate, genuinely moving stand-in for both halves
            (background + elements) until an actual Save/Preview makes
            the live photo itself accurate, at which point
            editingPreviewUrl clears and this goes back to that photo. */}
        {editingPreviewUrl ? (
          <img className="canvas-frame" src={editingPreviewUrl} alt="Live preview of the current edit" />
        ) : (
          frameUrl && connected && (
            <img className="canvas-frame" src={frameUrl} alt="Live panel frame" />
          )
        )}
        <svg
          ref={svgRef}
          className="canvas-svg"
          viewBox={`0 0 ${REF_W} ${REF_H}`}
          onPointerDown={() => setSelectedId(null)}
        >
          {guides.x !== null && (
            <line x1={guides.x * REF_W} y1={0} x2={guides.x * REF_W} y2={REF_H} className="canvas-guide" />
          )}
          {guides.y !== null && (
            <line x1={0} y1={guides.y * REF_H} x2={REF_W} y2={guides.y * REF_H} className="canvas-guide" />
          )}
          {ordered.map((el) => {
            const isSelected = el.id === selectedId;

            if (el.type === "text") {
              const x = el.x * REF_W;
              const y = el.y * REF_H;
              const fontSize = Math.max(8, (el.font_size ?? 0.05) * REF_H);
              const solidColor = el.color ? `rgb(${el.color[0]}, ${el.color[1]}, ${el.color[2]})` : "#fff";
              const { fill: color, defs: gradDefs } = gradientFill(el, "textgrad", solidColor);
              const anchor = { left: "start", center: "middle", right: "end" }[el.align || "center"] || "middle";
              // A stat-bound element's real content is a live reading
              // this editor doesn't have (see textElementPreview()'s
              // own comment) -- "--" stands in so the box/handle sizes
              // and reads sensibly either way.
              const previewText = el.stat ? textElementPreview(el, meta) : (el.text || "(empty text)");
              const halfW = Math.max(24, (previewText.length * fontSize) / 3.2);
              // Every text element -- custom or stat-bound -- is already
              // baked into the live frame once connected (build_static_
              // background()/render_frame() on the backend), so drawing
              // this SVG copy on top of it unconditionally double-renders
              // the same text twice at the same spot. For a stat-bound
              // element that's a garbled overlap between the SVG's fixed
              // "--" placeholder (see textElementPreview()) and the real
              // live value underneath -- exactly the "NENET.GM/s" bug
              // reported against "Outrun Drive"'s "NET {value}" text. For
              // plain custom text the content matches, but slightly
              // different font rendering between the SVG and the backend
              // PNG still shows as a visible ghosting/blur, not a clean
              // overlap -- so both cases defer to the live frame here,
              // same as the showMockup / showClockMockup gates below for
              // box/image/media/weather and the clock (unlike those,
              // there's no "always show" exception for one sub-type here,
              // since text has nothing like graph's need for history data
              // the editor doesn't have).
              const overLiveFrame = hasAccurateBackdrop;
              const showMockup = isSelected || !overLiveFrame || forceAllMockups;
              return (
                <g key={el.id}>
                  {gradDefs && <defs>{gradDefs}</defs>}
                  {isSelected && (
                    <rect x={x - (anchor === "start" ? 4 : anchor === "end" ? halfW * 2 - 4 : halfW)}
                          y={y - fontSize * 0.8} width={halfW * 2} height={fontSize * 1.6}
                          fill="none" stroke="#ffd85e" strokeDasharray="4 3" />
                  )}
                  {showMockup ? (
                    <text x={x} y={y} textAnchor={anchor} dominantBaseline="middle"
                          fontSize={fontSize} fontFamily={widgetFont(el)} fontWeight={widgetFont(el) ? (el.bold ? 650 : 450) : undefined}
                          fill={color} opacity={el.opacity ?? 1}
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move", userSelect: "none" }}>
                      {previewText}
                    </text>
                  ) : (
                    // Live frame already shows this stat's real value here --
                    // keep an invisible hit-target so the element can still
                    // be picked up and dragged (selecting it flips
                    // showMockup back on, same as the box/image branch above).
                    <rect x={x - (anchor === "start" ? 4 : anchor === "end" ? halfW * 2 - 4 : halfW)}
                          y={y - fontSize * 0.8} width={halfW * 2} height={fontSize * 1.6}
                          fill="transparent"
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                  )}
                  {isSelected && (
                    <rect x={x + halfW - 7} y={y + fontSize * 0.8 - 7} width={14} height={14}
                          fill="#ffd85e" stroke="#fff" strokeWidth={1}
                          onPointerDown={onPointerDownHandle(el)} style={{ cursor: "ns-resize" }} />
                  )}
                </g>
              );
            }

            if (el.type === "graph" || el.type === "bar" || el.type === "image" || el.type === "media" || el.type === "weather") {
              const w = (el.width ?? (el.type === "media" ? 0.32 : el.type === "weather" ? 0.28 : el.type === "bar" ? 0.26 : 0.2)) * REF_W;
              // Exactly el.width/el.height, no forced minimum -- a
              // weather element's box is never grown to fit its
              // content (see dashboard_theme.py's _weather_box()
              // docstring for why an earlier version of this did grow
              // it, and why that made things worse: it made the real
              // on-panel footprint balloon past what these fields say
              // and overlap neighboring elements). The real render
              // instead scales the *content* down to fit whatever box
              // this is, so the mockup here staying exactly this size
              // is the accurate preview.
              const h = (el.height ?? (el.type === "media" ? 0.52 : el.type === "weather" ? 0.46 : el.type === "bar" ? 0.12 : 0.14)) * REF_H;
              const x0 = el.x * REF_W - w / 2;
              const y0 = el.y * REF_H - h / 2;
              const accent = meta.widgetStyles?.[el.widget_style] || el.type === "graph" || el.type === "bar" ? accentFor(el)
                : el.type === "media" ? ACCENT_GPU : el.type === "weather" ? "rgb(255, 200, 60)" : "rgb(150, 170, 200)";
              const label = el.type === "graph" || el.type === "bar" ? (meta.stats[el.stat]?.title || el.stat)
                : el.type === "media" ? "NOW PLAYING" : el.type === "weather" ? "WEATHER" : "PICK AN IMAGE BELOW";
              const imageUrl = el.type === "image" ? api.dashboardImageUrl(el.image_path) : null;
              const clipId = `clip_${el.id}`;
              // A bar or graph's mockup mirrors its real gradient fill
              // (rather than just the flat `accent` swatch every other
              // box type gets) so customizing a gradient's colors/
              // direction shows its actual look right away, same as
              // every other property already updates live in this
              // canvas.
              const { fill: boxFill, defs: boxGradDefs } =
                (el.type === "bar" || el.type === "graph") ? gradientFill(el, "boxgrad", accent) : { fill: accent, defs: null };
              // Mirrors dashboard_theme.py's _fit_into_box(): "contain"
              // (default) never crops, "cover" fills the box and crops
              // overflow, "stretch" ignores aspect ratio entirely --
              // same three SVG preserveAspectRatio values do the same
              // job here, so this preview matches what actually renders.
              const preserveAspectRatio =
                el.type === "image" && el.fit === "cover" ? "xMidYMid slice"
                : el.type === "image" && el.fit === "stretch" ? "none"
                : "xMidYMid meet";
              // Same "the live frame already shows the real thing"
              // reasoning as the clock face below: media/weather (and
              // an image element with a picture already picked) are
              // dynamic/baked content the live frame already draws at
              // this exact spot, so the tinted fill + border + label
              // mockup reads as an artifact sitting on top of the real
              // preview rather than an editor overlay -- only draw it
              // when there's no live frame to collide with, or once
              // selected (so you can always find/resize it). A plain
              // graph or bar (this mockup never attempts to draw their
              // actual bars/line/fill -- that needs live history/stat
              // data this editor doesn't have) always keeps its box,
              // connected or not -- it's the only thing showing where
              // one of those will be (a bar is different -- see the note
              // by showMockup below), unlike media/weather's box, which
              // duplicates a real "NOW PLAYING"/"WEATHER" label the
              // live frame already shows. An invisible hit-rect keeps
              // the whole footprint click/drag-able either way.
              //
              // A bar element used to be lumped in with graph here too
              // (always showing its box, connected or not), which is
              // exactly what a user reported as "the bar looks
              // highlighted by default" -- a saved, unselected bar kept
              // its tinted fill/border/label drawn right on top of the
              // real live photo forever, permanently looking selected.
              // Unlike a graph (whose mockup is a rough placeholder that
              // can't approximate a live line/history at all), a bar's
              // mockup is just its accent-colored fill/border/label at a
              // fixed demo fraction -- close enough to the real thing,
              // same as a gauge's demo needle, that there's no reason to
              // keep it drawn over an accurate live photo. So bar now
              // defers to the live frame exactly like gauge/clock/media/
              // weather do (mockup only when selected, disconnected, or
              // forceAllMockups), and only graph keeps the "always show"
              // behavior.
              //
              // `|| forceAllMockups` in showMockup matters too: the
              // live frame photo only ever shows what was last *saved*
              // (see the "Save layout" hint at the top of this panel,
              // and the canvas-frame <img> above), so right after
              // Reset to defaults (or an Undo/Redo landing on nothing
              // selected) that photo is known-stale relative to what's
              // on screen here, and nothing is selected to fall back
              // on for showing the update. Forcing every mockup to show
              // in just that case, on top of the (possibly stale)
              // photo, is what makes the reset visibly register
              // immediately even though the photo itself hasn't caught
              // up yet. It's deliberately NOT just `dirty` -- an
              // ordinary drag or checkbox edit is dirty too, but always
              // has the edited element selected, so `isSelected` alone
              // already covers it; `forceAllMockups` only kicking in
              // once nothing is selected keeps a plain drag on one
              // gauge from also lighting up every *other* element's
              // mockup box/border for the whole gesture. Once Save
              // layout is clicked, `dirty` (and so `forceAllMockups`)
              // goes false again and the mockup goes back to deferring
              // to the real frame.
              const overLiveFrame = hasAccurateBackdrop;
              // Graphs used to be exempt from all of this and draw their
              // mockup unconditionally, on the theory that the SVG
              // overlay can't plot the line itself so the box was the
              // only thing making the region locatable. That was wrong
              // twice over: the backdrop this defers to (the panel's
              // live frame, or the live-rendered preview of an unsaved
              // edit) draws the graph in full -- frame, title AND line
              // -- so there's nothing to locate that isn't already
              // there; and the mockup isn't a faint outline, it's a
              // gradient-filled box with the element's name across the
              // middle, so forcing it on top of that real render read
              // as the graph highlighting itself at random. Reported
              // exactly that way. The transparent hit rect below still
              // covers dragging when the mockup is hidden, and
              // `!overLiveFrame` still brings it back whenever there's
              // no accurate backdrop to defer to.
              const showMockup = isSelected || !overLiveFrame || forceAllMockups;
              const styledBox = !!meta.widgetStyles?.[el.widget_style] && ["bar", "media"].includes(el.type);
              return (
                <g key={el.id}>
                  {imageUrl && (
                    <clipPath id={clipId}>
                      <rect x={x0} y={y0} width={w} height={h} rx={4} />
                    </clipPath>
                  )}
                  {boxGradDefs && <defs>{boxGradDefs}</defs>}
                  {imageUrl ? (
                    // The actual picked image, shown here the moment
                    // it's uploaded -- not just once Saved/Started, see
                    // uploadImage()'s comment on why this can render
                    // immediately. Left visible either way (it's what
                    // the live frame itself shows once baked in, not
                    // an edit-only annotation).
                    <image href={imageUrl} x={x0} y={y0} width={w} height={h}
                           preserveAspectRatio={preserveAspectRatio} clipPath={`url(#${clipId})`}
                           opacity={el.opacity ?? 1}
                           onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                  ) : showMockup && styledBox ? (
                    <g>
                      <StyledBoxPreview el={el} x={x0} y={y0} w={w} h={h} title={label} />
                      <rect x={x0} y={y0} width={w} height={h} fill="transparent"
                            stroke={isSelected ? "#ffd85e" : "none"} strokeDasharray="6 3"
                            onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                    </g>
                  ) : showMockup ? (
                    <rect x={x0} y={y0} width={w} height={h}
                          fill={boxFill}
                          fillOpacity={boxGradDefs ? 0.55 * (el.opacity ?? 1) : 0.1 * (el.opacity ?? 1)}
                          stroke={accent} strokeOpacity={el.opacity ?? 1}
                          strokeWidth={isSelected ? 3 : 1.5}
                          strokeDasharray={isSelected ? "6 3" : undefined}
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                  ) : (
                    <rect x={x0} y={y0} width={w} height={h} fill="transparent"
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                  )}
                  {imageUrl && showMockup && (
                    <rect x={x0} y={y0} width={w} height={h} rx={4}
                          fill="none" stroke={isSelected ? "#ffd85e" : accent}
                          strokeOpacity={isSelected ? 1 : 0.6}
                          strokeWidth={isSelected ? 3 : 1.5}
                          strokeDasharray={isSelected ? "6 3" : undefined}
                          style={{ pointerEvents: "none" }} />
                  )}
                  {!imageUrl && showMockup && !styledBox && (
                    <text x={x0 + w / 2} y={y0 + h / 2} textAnchor="middle" dominantBaseline="middle"
                          fill="#fff" fontSize={12} style={{ pointerEvents: "none" }}>
                      {label}
                    </text>
                  )}
                  {isSelected && (
                    <>
                      {/* Right-edge handle: width only. */}
                      <rect x={x0 + w - 5} y={y0 + h / 2 - 7} width={10} height={14} rx={2}
                            fill={accent} stroke="#fff" strokeWidth={1}
                            onPointerDown={onPointerDownHandle(el, "width")}
                            style={{ cursor: "ew-resize" }}>
                        <title>Drag to resize width only</title>
                      </rect>
                      {/* Bottom-edge handle: height only. */}
                      <rect x={x0 + w / 2 - 7} y={y0 + h - 5} width={14} height={10} rx={2}
                            fill={accent} stroke="#fff" strokeWidth={1}
                            onPointerDown={onPointerDownHandle(el, "height")}
                            style={{ cursor: "ns-resize" }}>
                        <title>Drag to resize height only</title>
                      </rect>
                      {/* Corner handle: both at once. */}
                      <rect x={x0 + w - 7} y={y0 + h - 7} width={14} height={14}
                            fill={accent} stroke="#fff" strokeWidth={1}
                            onPointerDown={onPointerDownHandle(el, "both")}
                            style={{ cursor: "nwse-resize" }}>
                        <title>Drag to resize width and height together</title>
                      </rect>
                    </>
                  )}
                </g>
              );
            }

            if (el.type === "clock") {
              const x = el.x * REF_W;
              const y = el.y * REF_H;
              const color = el.color ? `rgb(${el.color[0]}, ${el.color[1]}, ${el.color[2]})` : "#fff";
              const face = el.face || "digital";
              // When this canvas is overlaid on an accurate backdrop
              // (the real live frame, or editingPreviewUrl standing in
              // for it while there's an unsaved edit -- see
              // hasAccurateBackdrop's own comment), that backdrop
              // already shows the panel's actual, real, ticking clock
              // at this exact spot -- drawing a mockup on top of it
              // reads as two different clocks fighting each other, not
              // as an editor overlay. So every face below only draws
              // its visible mockup when there's no accurate backdrop
              // under it to collide with; an invisible hit-shape the
              // same size/position keeps it clickable/draggable either
              // way, and the selection outline/handle are unaffected --
              // you can still always tell it's there and where it is
              // once it's selected. Same reasoning/pattern for all
              // three faces, just a different hit-shape each.
              const overLiveFrame = hasAccurateBackdrop;
              // `|| forceAllMockups` below (see the box-rendering branch
              // above for the full reasoning) makes the mockup keep
              // showing even while connected right after a mass change
              // like Reset to defaults, since the live frame photo
              // hasn't caught up to it yet and nothing is selected to
              // fall back on.
              const showClockMockup = !overLiveFrame || forceAllMockups;

              if (face === "analog") {
                const r = Math.max(10, (el.radius ?? 0.12) * Math.min(REF_W, REF_H));
                const style = el.analog_style || "classic";
                const ringColor = style === "classic" ? "#2a2a2e" : color;
                const faceFill = style === "neon" ? "#0c0e14" : style === "minimal" ? null : "#fafafc";
                const handColor = style === "classic" ? "#1e1e22" : color;
                // A static "ten past ten" hand position -- purely
                // decorative, not the actual time (same reasoning as
                // the digital face's fixed "12:34:56" sample below):
                // this is a layout editor, not a second ticking clock.
                const hourAng = (-60 * Math.PI) / 180;
                const minAng = (60 * Math.PI) / 180;
                const hx = x + Math.cos(hourAng) * r * 0.5;
                const hy = y + Math.sin(hourAng) * r * 0.5;
                const mx = x + Math.cos(minAng) * r * 0.72;
                const my = y + Math.sin(minAng) * r * 0.72;
                return (
                  <g key={el.id}>
                    {isSelected && (
                      <rect x={x - r - 4} y={y - r - 4} width={(r + 4) * 2} height={(r + 4) * 2}
                            fill="none" stroke="#ffd85e" strokeDasharray="4 3" />
                    )}
                    {!showClockMockup ? (
                      <circle cx={x} cy={y} r={r} fill="transparent"
                              onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }}>
                        <title>Clock (analog) -- showing the real live time from the panel behind it</title>
                      </circle>
                    ) : (
                      <g onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }}>
                        {faceFill && <circle cx={x} cy={y} r={r} fill={faceFill} opacity={el.opacity ?? 1} />}
                        <circle cx={x} cy={y} r={r} fill="none" stroke={ringColor}
                                strokeWidth={Math.max(1, r * 0.05)} opacity={el.opacity ?? 1} />
                        <line x1={x} y1={y} x2={hx} y2={hy} stroke={handColor}
                              strokeWidth={Math.max(2, r * 0.07)} strokeLinecap="round" opacity={el.opacity ?? 1} />
                        <line x1={x} y1={y} x2={mx} y2={my} stroke={handColor}
                              strokeWidth={Math.max(1.5, r * 0.045)} strokeLinecap="round" opacity={el.opacity ?? 1} />
                      </g>
                    )}
                    {isSelected && (
                      <rect x={x + r * 0.707 - 7} y={y + r * 0.707 - 7} width={14} height={14}
                            fill="#ffd85e" stroke="#fff" strokeWidth={1}
                            onPointerDown={onPointerDownHandle(el)} style={{ cursor: "nwse-resize" }} />
                    )}
                  </g>
                );
              }

              if (face === "image") {
                const w = (el.width ?? 0.22) * REF_W;
                const h = (el.height ?? 0.22) * REF_H;
                const x0 = x - w / 2;
                const y0 = y - h / 2;
                const imageUrl = el.image_path ? api.dashboardImageUrl(el.image_path) : null;
                const clipId = `clip_${el.id}`;
                const sample = el.show_seconds ?? true ? "12:34:56" : "12:34";
                return (
                  <g key={el.id}>
                    {imageUrl && (
                      <clipPath id={clipId}><rect x={x0} y={y0} width={w} height={h} rx={4} /></clipPath>
                    )}
                    {!showClockMockup ? (
                      <rect x={x0} y={y0} width={w} height={h} fill="transparent"
                            onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }}>
                        <title>Clock (custom image) -- showing the real live time from the panel behind it</title>
                      </rect>
                    ) : (
                      <>
                        {imageUrl ? (
                          <image href={imageUrl} x={x0} y={y0} width={w} height={h}
                                 preserveAspectRatio="xMidYMid meet" clipPath={`url(#${clipId})`}
                                 opacity={el.opacity ?? 1}
                                 onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                        ) : (
                          <rect x={x0} y={y0} width={w} height={h}
                                fill="rgb(150,170,200)" fillOpacity={0.1 * (el.opacity ?? 1)}
                                stroke="rgb(150,170,200)" strokeOpacity={el.opacity ?? 1}
                                strokeWidth={isSelected ? 3 : 1.5}
                                strokeDasharray={isSelected ? "6 3" : undefined}
                                onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                        )}
                        {!imageUrl && (
                          <text x={x} y={y0 + 14} textAnchor="middle" fill="#fff" fontSize={11}
                                style={{ pointerEvents: "none" }}>
                            PICK A CLOCK FACE IMAGE
                          </text>
                        )}
                        <text x={x} y={imageUrl ? y : y + h * 0.22} textAnchor="middle" dominantBaseline="middle"
                              fontSize={Math.max(10, h * 0.16)} fill={color} style={{ pointerEvents: "none" }}>
                          {sample}
                        </text>
                      </>
                    )}
                    {imageUrl && (
                      <rect x={x0} y={y0} width={w} height={h} rx={4} fill="none"
                            stroke={isSelected ? "#ffd85e" : "rgb(150,170,200)"}
                            strokeOpacity={isSelected ? 1 : 0.6}
                            strokeWidth={isSelected ? 3 : 1.5}
                            strokeDasharray={isSelected ? "6 3" : undefined}
                            style={{ pointerEvents: "none" }} />
                    )}
                    {isSelected && (
                      <>
                        <rect x={x0 + w - 5} y={y0 + h / 2 - 7} width={10} height={14} rx={2}
                              fill="rgb(150,170,200)" stroke="#fff" strokeWidth={1}
                              onPointerDown={onPointerDownHandle(el, "width")} style={{ cursor: "ew-resize" }} />
                        <rect x={x0 + w / 2 - 7} y={y0 + h - 5} width={14} height={10} rx={2}
                              fill="rgb(150,170,200)" stroke="#fff" strokeWidth={1}
                              onPointerDown={onPointerDownHandle(el, "height")} style={{ cursor: "ns-resize" }} />
                        <rect x={x0 + w - 7} y={y0 + h - 7} width={14} height={14}
                              fill="rgb(150,170,200)" stroke="#fff" strokeWidth={1}
                              onPointerDown={onPointerDownHandle(el, "both")} style={{ cursor: "nwse-resize" }} />
                      </>
                    )}
                  </g>
                );
              }

              // digital (default / back-compat)
              const fontSize = Math.max(8, (el.font_size ?? 0.05) * REF_H);
              const twelveHour = el.hour_format === "12h";
              const sample = (el.show_seconds ?? true)
                ? (twelveHour ? "12:34:56 PM" : "12:34:56")
                : (twelveHour ? "12:34 PM" : "12:34");
              const halfW = Math.max(24, (sample.length * fontSize) / 3.4);
              const { fill: clockColor, defs: clockGradDefs } = gradientFill(el, "clockgrad", color);
              return (
                <g key={el.id}>
                  {clockGradDefs && <defs>{clockGradDefs}</defs>}
                  {isSelected && (
                    <rect x={x - halfW} y={y - fontSize * 0.7} width={halfW * 2} height={fontSize * 1.4}
                          fill="none" stroke="#ffd85e" strokeDasharray="4 3" />
                  )}
                  {!showClockMockup ? (
                    <rect x={x - halfW} y={y - fontSize * 0.7} width={halfW * 2} height={fontSize * 1.4}
                          fill="transparent"
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }}>
                      <title>Clock -- showing the real live time from the panel behind it</title>
                    </rect>
                  ) : (
                    <text x={x} y={y} textAnchor="middle" dominantBaseline="middle"
                          fontSize={fontSize} fontFamily={widgetFont(el)} fill={clockColor} opacity={el.opacity ?? 1}
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move", userSelect: "none" }}>
                      {sample}
                    </text>
                  )}
                  {isSelected && (
                    <rect x={x + halfW - 7} y={y + fontSize * 0.7 - 7} width={14} height={14}
                          fill="#ffd85e" stroke="#fff" strokeWidth={1}
                          onPointerDown={onPointerDownHandle(el)} style={{ cursor: "ns-resize" }} />
                  )}
                </g>
              );
            }

            // gauge (the original element type) -- same "the live frame
            // already shows the real thing" reasoning as clock/media/
            // weather above: once connected, the live frame already
            // draws this exact gauge's real ring + live value at this
            // spot, so the mockup ring + static title text is a
            // duplicate sitting on top of it. Only draw the mockup when
            // there's no live frame to collide with, or once selected;
            // an invisible circle keeps the hit area click/drag-able
            // either way. `|| forceAllMockups` too, same as the other
            // element types above -- covers a mass change like Reset to
            // defaults (nothing selected, live frame photo not caught
            // up yet) without also lighting up every gauge's mockup
            // ring during an ordinary drag on some other element.
            const cx = el.x * REF_W;
            const cy = el.y * REF_H;
            const r = el.radius * Math.min(REF_W, REF_H);
            const accent = accentFor(el);
            const { fill: ringFill, defs: ringGradDefs } = gradientFill(el, "gaugegrad", accent);
            const title = meta.stats[el.stat]?.title || el.stat;
            const overLiveFrame = hasAccurateBackdrop;
            const showMockup = isSelected || !overLiveFrame || forceAllMockups;
            return (
              <g key={el.id}>
                {ringGradDefs && <defs>{ringGradDefs}</defs>}
                {showMockup && !!meta.widgetStyles?.[el.widget_style] ? (
                  <g>
                    <StyledGaugePreview el={el} title={title} cx={cx} cy={cy} r={r} />
                    <circle cx={cx} cy={cy} r={r+6} fill="transparent"
                            stroke={isSelected ? "#ffd85e" : "none"} strokeDasharray="6 3"
                            onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                  </g>
                ) : showMockup ? (
                  <circle
                    cx={cx}
                    cy={cy}
                    r={r}
                    fill={ringFill}
                    fillOpacity={0.12 * (el.opacity ?? 1)}
                    stroke={accent}
                    strokeOpacity={el.opacity ?? 1}
                    strokeWidth={isSelected ? 3 : 1.5}
                    strokeDasharray={isSelected ? "6 3" : undefined}
                    onPointerDown={onPointerDownGauge(el)}
                    style={{ cursor: "move" }}
                  />
                ) : (
                  <circle cx={cx} cy={cy} r={r} fill="transparent"
                          onPointerDown={onPointerDownGauge(el)} style={{ cursor: "move" }} />
                )}
                {showMockup && !meta.widgetStyles?.[el.widget_style] && (
                  <text x={cx} y={cy} textAnchor="middle" dominantBaseline="middle"
                        fill="#fff" fontSize={Math.max(10, r * 0.28)} style={{ pointerEvents: "none" }}>
                    {title}
                  </text>
                )}
                {isSelected && (
                  <rect
                    x={cx + r * 0.707 - 7} y={cy + r * 0.707 - 7} width={14} height={14}
                    fill={accent} stroke="#fff" strokeWidth={1}
                    onPointerDown={onPointerDownHandle(el)}
                    style={{ cursor: "nwse-resize" }}
                  />
                )}
              </g>
            );
          })}
        </svg>
          </div>
        </div>

        <div className="canvas-side">
          {/* Element list -- clicking directly on the canvas is fine
              when things are spread out, but small or fully-overlapped
              elements (a mini gauge, a now-playing box sitting on top
              of a gauge) are hard or impossible to grab precisely, and
              there's no way to even tell two overlapping things apart.
              This lists every element by name regardless of where it
              sits or what's on top of it, and clicking a row selects
              it exactly like clicking it on the canvas would. */}
          <ul className="element-list">
            {ordered.map((el) => (
              <li key={el.id}>
                <button
                  type="button"
                  className={el.id === selectedId ? "element-row selected" : "element-row"}
                  onClick={() => setSelectedId(el.id)}
                >
                  <span className="element-badge">{ELEMENT_BADGES[el.type] || "?"}</span>
                  <span className="element-label">{elementLabel(el, meta)}</span>
                  <span className="element-id">{el.id}</span>
                </button>
              </li>
            ))}
          </ul>

          {selected && (
        <div className="canvas-props">
          {["text", "clock", "gauge", "bar", "media", "graph"].includes(selected.type) && (
            <div className="row">
              <label>
                Widget appearance
                <select value={selected.widget_style || ""}
                        onChange={(e) => updateSelected({ widget_style: e.target.value || null })}>
                  <option value="">Use theme style</option>
                  <option value="default">Default</option>
                  {Object.entries(meta.widgetStyles || {}).map(([key, spec]) => (
                    <option key={key} value={key}>{spec.label}</option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {!!meta.widgetStyles?.[styledSelected.widget_style] && ["gauge", "bar", "media"].includes(selected.type) && (
            <>
              <FontFamilyControl selected={selected} updateSelected={updateSelected} meta={meta} />
              <div className="row">
                {[["color", "Accent"], ["ornament_color", "Metal"], ["text_color", "Text"]].map(([key, label]) => (
                  <label key={key}>{label}
                    <input type="color" value={rgbToHex(styledSelected[key])}
                           onChange={(e) => updateSelected({ [key]: hexToRgb(e.target.value) })} />
                  </label>
                ))}
                <button onClick={() => updateSelected({color: null, ornament_color: null, text_color: null, font: null})}>
                  Use theme colors and font
                </button>
              </div>
            </>
          )}
          {selected.type === "text" && (
            <>
              <div className="row">
                <label>
                  Source
                  <select
                    value={selected.stat ? "stat" : "custom"}
                    onChange={(e) =>
                      updateSelected(
                        e.target.value === "stat"
                          ? { stat: selected.stat || Object.keys(meta.stats)[0], template: selected.template || "{value}" }
                          : { stat: null }
                      )
                    }
                  >
                    <option value="custom">Custom text</option>
                    <option value="stat">Live stat</option>
                  </select>
                </label>
                <label>
                  Align
                  <select value={selected.align || "center"} onChange={(e) => updateSelected({ align: e.target.value })}>
                    <option value="left">Left</option>
                    <option value="center">Center</option>
                    <option value="right">Right</option>
                  </select>
                </label>
              </div>
              {selected.stat ? (
                <div className="row">
                  <label>
                    Stat
                    <select value={selected.stat} onChange={(e) => updateSelected({ stat: e.target.value })}>
                      {Object.entries(meta.stats).map(([key, s]) => (
                        <option key={key} value={key}>{s.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="grow">
                    Template
                    <input type="text" value={selected.template ?? "{value}"}
                           placeholder="{value}"
                           onChange={(e) => updateSelected({ template: e.target.value })} />
                  </label>
                </div>
              ) : (
                <div className="row">
                  <label className="grow">
                    Text
                    <input type="text" value={selected.text || ""}
                           onChange={(e) => updateSelected({ text: e.target.value })} />
                  </label>
                </div>
              )}
              {selected.stat && (
                <p className="hint">
                  Shows this stat's live reading, refreshed every frame -- e.g. "{"{value}"}" alone
                  shows just "42%"; "CPU {"{value}"}" or "{"{label}"}: {"{value}"}" adds your own
                  words around it.
                </p>
              )}
              <div className="row">
                <label>
                  Font size %
                  <input type="number" min={2} max={25} style={{ width: "5em" }}
                         value={Math.round((selected.font_size ?? 0.05) * 100)}
                         onChange={(e) => updateSelected({ font_size: clamp(Number(e.target.value) / 100, 0.02, 0.25) })} />
                </label>
                <label>
                  Opacity
                  <input type="range" min={20} max={100}
                         value={Math.round((selected.opacity ?? 1) * 100)}
                         onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
                </label>
              </div>
              <FontFamilyControl selected={selected} updateSelected={updateSelected} meta={meta} />
              <TextPlateControl selected={selected} updateSelected={updateSelected} />
              <GradientFillControl selected={selected} updateSelected={updateSelected}
                                    defaultColor={[255, 255, 255]} />
            </>
          )}

          {selected.type === "graph" && (
            <>
              <div className="row">
                <label>
                  Stat
                  <select value={selected.stat} onChange={(e) => updateSelected({ stat: e.target.value })}>
                    {Object.entries(meta.stats).map(([key, s]) => (
                      <option key={key} value={key}>{s.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Style
                  <select value={selected.style || "line"} onChange={(e) => updateSelected({ style: e.target.value })}>
                    <option value="line">Line</option>
                    <option value="bar">Bar</option>
                  </select>
                </label>
                <label>
                  History (seconds)
                  <input type="number" min={2} max={120} style={{ width: "5em" }}
                         value={selected.history_seconds ?? 20}
                         onChange={(e) => updateSelected({ history_seconds: Math.max(2, Number(e.target.value)) })} />
                </label>
              </div>
              <div className="row">
                <label>
                  Opacity
                  <input type="range" min={20} max={100}
                         value={Math.round((selected.opacity ?? 1) * 100)}
                         onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
                </label>
                {/* Both default on -- off is for a layout that labels
                    and frames the graph itself (the card-based presets
                    draw the heading and the plot well into their own
                    background art). */}
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_title !== false}
                         onChange={(e) => updateSelected({ show_title: e.target.checked })} />
                  Show title
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_frame !== false}
                         onChange={(e) => updateSelected({ show_frame: e.target.checked })} />
                  Show frame
                </label>
              </div>
              <GradientFillControl selected={selected} updateSelected={updateSelected} />
            </>
          )}

          {selected.type === "bar" && (
            <div className="row">
              <label>
                Stat
                <select value={selected.stat} onChange={(e) => updateSelected({ stat: e.target.value })}>
                  {Object.entries(meta.stats).map(([key, s]) => (
                    <option key={key} value={key}>{s.label}</option>
                  ))}
                </select>
              </label>
              <label>
                Orientation
                <select
                  value={selected.orientation || "horizontal"}
                  onChange={(e) => {
                    const orientation = e.target.value;
                    const patch = { orientation };
                    const w = selected.width ?? 0.26;
                    const h = selected.height ?? 0.12;
                    // Flip the box's own aspect to suit the new fill
                    // direction -- a vertical bar that's still wider
                    // than tall (or a horizontal one taller than wide)
                    // reads oddly, so swap width/height whenever the
                    // current box doesn't already fit the orientation
                    // being switched to. Only ever swaps once: if it's
                    // already the right shape (or square), this is a
                    // no-op.
                    if (orientation === "vertical" && w > h) {
                      patch.width = h;
                      patch.height = w;
                    } else if (orientation === "horizontal" && h > w) {
                      patch.width = h;
                      patch.height = w;
                    }
                    updateSelected(patch);
                  }}
                >
                  <option value="horizontal">Horizontal</option>
                  <option value="vertical">Vertical</option>
                </select>
              </label>
              <label>
                Opacity
                <input type="range" min={20} max={100}
                       value={Math.round((selected.opacity ?? 1) * 100)}
                       onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
              </label>
              {!usesWidgetStyle && <label className="row-inline">
                <input type="checkbox" checked={!!selected.show_knob}
                       onChange={(e) => updateSelected({ show_knob: e.target.checked })} />
                Show knob
              </label>}
              {/* Turn both off to use the bar as a bare meter and put
                  the label/reading in your own text elements instead
                  -- how the card-based presets lay out a "LOAD ... 42%"
                  row above a slim meter, in their own font. */}
              <label className="row-inline">
                <input type="checkbox" checked={selected.show_title !== false}
                       onChange={(e) => updateSelected({ show_title: e.target.checked })} />
                Show title
              </label>
              <label className="row-inline">
                <input type="checkbox" checked={selected.show_value !== false}
                       onChange={(e) => updateSelected({ show_value: e.target.checked })} />
                Show value
              </label>
            </div>
          )}

          {selected.type === "bar" && !usesWidgetStyle && (
            <GradientFillControl selected={selected} updateSelected={updateSelected} />
          )}

          {selected.type === "bar" && selected.gradient && !usesWidgetStyle && (
            <div className="row">
              <span className="hint">
                The color at any point on the bar stays put as the value changes, only how
                much of it is revealed moves. Direction is independent of Orientation
                above: a horizontal bar can still gradient top-to-bottom, and a vertical
                one left-to-right, if that reads better than matching the fill direction.
              </span>
            </div>
          )}

          {selected.type === "bar" && (
            <div className="row">
              <span className="hint">
                A linear meter for this stat's current value -- the same reading a gauge
                shows, just as a fill bar instead of a ring. No history here (that's what
                graph is for); this only ever shows the value right now. Orientation picks
                which way the fill runs -- left-to-right, or bottom-to-top.
              </span>
            </div>
          )}

          {selected.type === "image" && (
            <div className="row">
              <label className="grow">
                Image
                <input type="file" accept="image/*"
                       onChange={(e) => uploadImage(selected.id, e.target.files[0],
                         (path, dims) => {
                           const patch = { image_path: path };
                           if (dims && dims.width && dims.height) {
                             // Box the new picture at roughly its own on-screen
                             // footprint, matching ITS aspect ratio -- not the
                             // small square default (or whatever box a
                             // previous picture left behind), so a freshly
                             // picked image doesn't start out looking
                             // squished/cropped before anyone's touched the
                             // resize handles.
                             const target = 0.28;
                             const ratio = dims.width / dims.height;
                             if (ratio >= 1) {
                               patch.width = target;
                               patch.height = clamp((target * REF_W) / ratio / REF_H, 0.02, 0.9);
                             } else {
                               patch.height = target;
                               patch.width = clamp((target * REF_H) * ratio / REF_W, 0.02, 0.9);
                             }
                           }
                           updateSelected(patch);
                         }, setError)} />
              </label>
              <label>
                Fit
                <select value={selected.fit || "contain"} onChange={(e) => updateSelected({ fit: e.target.value })}>
                  <option value="contain">Contain (show the whole image)</option>
                  <option value="cover">Cover (fill the box, may crop)</option>
                  <option value="stretch">Stretch (fill exactly, may distort)</option>
                </select>
              </label>
              <label>
                Opacity
                <input type="range" min={20} max={100}
                       value={Math.round((selected.opacity ?? 1) * 100)}
                       onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
              </label>
              {selected.image_path && (
                <>
                  <img className="file-thumb" src={api.dashboardImageUrl(selected.image_path)} alt="" />
                  <span className="hint">{basename(selected.image_path)}</span>
                </>
              )}
              {uploadingId === selected.id && <span className="hint">Uploading…</span>}
            </div>
          )}

          {selected.type === "media" && (
            <>
              <div className="row">
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_art ?? true}
                         onChange={(e) => {
                           const patch = { show_art: e.target.checked };
                           patch.height = estimateMediaHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Cover art
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_name ?? true}
                         onChange={(e) => {
                           const patch = { show_name: e.target.checked };
                           patch.height = estimateMediaHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Track/artist name
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_time ?? true}
                         onChange={(e) => {
                           const patch = { show_time: e.target.checked };
                           patch.height = estimateMediaHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Progress/time
                </label>
              </div>
              <div className="row">
                <label>
                  Opacity
                  <input type="range" min={20} max={100}
                         value={Math.round((selected.opacity ?? 1) * 100)}
                         onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
                </label>
                <span className="hint">
                  Album art, track/artist, and playback progress -- movable and resizable, unlike
                  the old fixed middle-column display. Turn off whichever pieces you don't want
                  -- e.g. just the cover art, or just the time.
                </span>
              </div>

              {npDraft && (
                <div className="canvas-props">
                  <p className="hint">
                    "Nothing playing" placeholder -- shown in place of the album art/track
                    title whenever nothing's actually playing. Shared by every now-playing
                    element on the canvas, not just this one.
                  </p>
                  <div className="row">
                    <label className="grow">
                      Placeholder image
                      <input
                        type="file"
                        accept="image/*"
                        onChange={(e) => uploadImage("nowPlaying", e.target.files[0],
                          (path) => updateNpDraft({ default_art_path: path }), setNpError)}
                      />
                    </label>
                    {npDraft.default_art_path && (
                      <>
                        <img className="file-thumb" src={api.dashboardImageUrl(npDraft.default_art_path)} alt="" />
                        <span className="hint">{basename(npDraft.default_art_path)}</span>
                        <button type="button" onClick={() => updateNpDraft({ default_art_path: "" })}>Clear</button>
                      </>
                    )}
                    {uploadingId === "nowPlaying" && <span className="hint">Uploading…</span>}
                  </div>
                  <div className="row">
                    <label className="grow">
                      Message
                      <input
                        type="text"
                        value={npDraft.not_playing_message || ""}
                        onChange={(e) => updateNpDraft({ not_playing_message: e.target.value })}
                        placeholder={meta.widgetStyles?.[styledSelected.widget_style] ? "Awaiting Spotify" : npDraft.default_message}
                      />
                    </label>
                  </div>
                  <p className="hint">
                    Leave either blank to fall back to the default. Both apply live, even
                    while the dashboard is already running -- no need to Stop/Start.
                  </p>
                  <div className="row">
                    <button onClick={saveNowPlaying}>Save placeholder</button>
                  </div>
                  {npStatus && <p className="hint settings-saved">{npStatus}</p>}
                  {npError && <p className="error">{npError}</p>}
                </div>
              )}
            </>
          )}

          {selected.type === "weather" && (
            <>
              <div className="row">
                <label className="grow">
                  Location
                  <input
                    type="text"
                    value={selected.location || ""}
                    onChange={(e) => updateSelected({ location: e.target.value })}
                    placeholder="City, address, or lat,lon"
                  />
                </label>
                <label>
                  Units
                  <select value={selected.units || "celsius"}
                          onChange={(e) => updateSelected({ units: e.target.value })}>
                    {Object.entries(meta.weatherUnitOptions || {}).map(([key, label]) => (
                      <option key={key} value={key}>{label}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row">
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_icon ?? true}
                         onChange={(e) => {
                           const patch = { show_icon: e.target.checked };
                           patch.height = estimateWeatherHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Icon
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_temp ?? true}
                         onChange={(e) => {
                           const patch = { show_temp: e.target.checked };
                           patch.height = estimateWeatherHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Temperature
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_description ?? true}
                         onChange={(e) => {
                           const patch = { show_description: e.target.checked };
                           patch.height = estimateWeatherHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Description
                </label>
              </div>
              <div className="row">
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_details ?? true}
                         onChange={(e) => {
                           const patch = { show_details: e.target.checked };
                           patch.height = estimateWeatherHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Feels-like/humidity
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_location ?? true}
                         onChange={(e) => {
                           const patch = { show_location: e.target.checked };
                           patch.height = estimateWeatherHeight({ ...selected, ...patch });
                           updateSelected(patch);
                         }} />
                  Location name
                </label>
              </div>
              <div className="row">
                <label>
                  Opacity
                  <input type="range" min={20} max={100}
                         value={Math.round((selected.opacity ?? 1) * 100)}
                         onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
                </label>
                <span className="hint">
                  City, address, or "lat,lon" -- looked up via a free weather service (Open-Meteo,
                  no account/API key needed). Turn off whichever pieces you don't want -- e.g. just
                  the icon, or just the temperature -- to shrink this down, say to sit next to a
                  now-playing element. Applies once you Save layout below, even while the dashboard
                  is already running -- no need to Stop/Start.
                </span>
              </div>
            </>
          )}

          {selected.type === "clock" && (
            <>
              <div className="row">
                <label>
                  Face
                  <select value={selected.face || "digital"} onChange={(e) => updateSelected({ face: e.target.value })}>
                    {Object.entries(meta.clockFaces || { digital: "Digital", analog: "Analog", image: "Custom image" })
                      .map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                </label>
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_seconds ?? true}
                         onChange={(e) => updateSelected({ show_seconds: e.target.checked })} />
                  {selected.face === "analog" ? "Second hand" : "Show seconds"}
                </label>
                <label>
                  Opacity
                  <input type="range" min={20} max={100}
                         value={Math.round((selected.opacity ?? 1) * 100)}
                         onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })} />
                </label>
              </div>

              {(!selected.face || selected.face === "digital") && (
                <div className="row">
                  <label>
                    Format
                    <select value={selected.hour_format || "24h"} onChange={(e) => updateSelected({ hour_format: e.target.value })}>
                      {Object.entries(meta.clockHourFormats || { "24h": "24-hour", "12h": "12-hour (AM/PM)" })
                        .map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                    </select>
                  </label>
                  <label className="row-inline">
                    <input type="checkbox" checked={!!selected.show_date}
                           onChange={(e) => updateSelected({ show_date: e.target.checked })} />
                    Show date
                  </label>
                  <label>
                    Font size %
                    <input type="number" min={2} max={25} style={{ width: "5em" }}
                           value={Math.round((selected.font_size ?? 0.05) * 100)}
                           onChange={(e) => updateSelected({ font_size: clamp(Number(e.target.value) / 100, 0.02, 0.25) })} />
                  </label>
                </div>
              )}

              {(!selected.face || selected.face === "digital") && (
                <>
                  <FontFamilyControl selected={selected} updateSelected={updateSelected} meta={meta} />
                  <GradientFillControl selected={selected} updateSelected={updateSelected}
                                        defaultColor={[235, 235, 242]} />
                </>
              )}

              {selected.face === "analog" && (
                <div className="row">
                  <label>
                    Style
                    <select value={selected.analog_style || "classic"} onChange={(e) => updateSelected({ analog_style: e.target.value })}>
                      {Object.entries(meta.clockAnalogStyles || { classic: "Classic", minimal: "Minimal", neon: "Neon" })
                        .map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                    </select>
                  </label>
                  <label>
                    Color
                    <input type="color" value={rgbToHex(selected.color || [235, 235, 242])}
                           onChange={(e) => updateSelected({ color: hexToRgb(e.target.value) })} />
                  </label>
                  <label>
                    Radius %
                    <input type="number" min={2} max={45} style={{ width: "5em" }}
                           value={Math.round((selected.radius ?? 0.12) * 100)}
                           onChange={(e) => updateSelected({ radius: clamp(Number(e.target.value) / 100, MIN_RADIUS, MAX_RADIUS) })} />
                  </label>
                </div>
              )}

              {selected.face === "image" && (
                <>
                  <div className="row">
                    <label className="grow">
                      Clock face image
                      <input type="file" accept="image/*"
                             onChange={(e) => uploadImage(selected.id, e.target.files[0],
                               (path) => updateSelected({ image_path: path }), setError)} />
                    </label>
                    <label>
                      Fit
                      <select value={selected.fit || "contain"} onChange={(e) => updateSelected({ fit: e.target.value })}>
                        <option value="contain">Contain (show the whole image)</option>
                        <option value="cover">Cover (fill the box, may crop)</option>
                        <option value="stretch">Stretch (fill exactly, may distort)</option>
                      </select>
                    </label>
                    {selected.image_path && (
                      <>
                        <img className="file-thumb" src={api.dashboardImageUrl(selected.image_path)} alt="" />
                        <span className="hint">{basename(selected.image_path)}</span>
                      </>
                    )}
                    {uploadingId === selected.id && <span className="hint">Uploading…</span>}
                  </div>
                  <div className="row">
                    <label>
                      Time color
                      <input type="color" value={rgbToHex(selected.color || [235, 235, 242])}
                             onChange={(e) => updateSelected({ color: hexToRgb(e.target.value) })} />
                    </label>
                    <label>
                      Time font size %
                      <input type="number" min={2} max={25} style={{ width: "5em" }}
                             value={Math.round((selected.font_size ?? 0.05) * 100)}
                             onChange={(e) => updateSelected({ font_size: clamp(Number(e.target.value) / 100, 0.02, 0.25) })} />
                    </label>
                    <label className="row-inline">
                      <input type="checkbox" checked={!!selected.show_date}
                             onChange={(e) => updateSelected({ show_date: e.target.checked })} />
                      Show date
                    </label>
                  </div>
                  <p className="hint">
                    Pick any picture as the clock's background/skin -- a real clock face
                    graphic, a photo, a logo, anything. The time (and date, if turned on) is
                    drawn on top of it, with a shadow behind the text so it stays legible over
                    any picture.
                  </p>
                </>
              )}
            </>
          )}

          {(!selected.type || selected.type === "gauge") && (
            <>
              <div className="row">
                <label>
                  Stat
                  <select value={selected.stat} onChange={(e) => updateSelected({ stat: e.target.value })}>
                    {Object.entries(meta.stats).map(([key, s]) => (
                      <option key={key} value={key}>{s.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Opacity
                  <input
                    type="range" min={20} max={100}
                    value={Math.round((selected.opacity ?? 1) * 100)}
                    onChange={(e) => updateSelected({ opacity: Number(e.target.value) / 100 })}
                  />
                </label>
                {/* Off for a layout whose own heading already says
                    what the ring is -- see the card-based presets,
                    where the card is titled and the ring just shows
                    the reading. */}
                <label className="row-inline">
                  <input type="checkbox" checked={selected.show_title !== false}
                         onChange={(e) => updateSelected({ show_title: e.target.checked })} />
                  Show title
                </label>
              </div>
              {!usesWidgetStyle && <div className="row">
                <label className="row-inline">
                  <input
                    type="checkbox"
                    checked={selected.color != null}
                    onChange={(e) => updateSelected({ color: e.target.checked ? hexToRgb("#ffffff") : null })}
                  />
                  Custom color
                </label>
                {selected.color != null && (
                  <input
                    type="color"
                    value={rgbToHex(selected.color)}
                    onChange={(e) => updateSelected({ color: hexToRgb(e.target.value) })}
                  />
                )}
              </div>}
              {/* Gauge used to only ever offer exactly one extra color
                  (the old `color2` field, ROADMAP.md Phase 6) sweeping
                  the ring corner-to-corner -- upgraded to the same
                  shared 2-4 stop control every other element type's
                  gradient option now uses, with "Diagonal" kept as the
                  first/default direction so an existing dashboard's
                  color2 gauges (still read by the backend as a 2-stop
                  diagonal gradient -- see _element_gauge_gradient())
                  looks the same as before if re-saved through this UI.
                  showSolidColorWhenOff=false: the "Custom color"
                  checkbox+picker just above already covers the solid
                  case, including its "derive from left/right position"
                  default when left unchecked, which this component's
                  own plain `color` fallback doesn't know how to do. */}
              {!usesWidgetStyle && <GradientFillControl selected={selected} updateSelected={updateSelected}
                                    defaultColor={[0, 220, 255]} showSolidColorWhenOff={false}
                                    directionOptions={[
                                      ["diagonal", "Diagonal"],
                                      ["horizontal", "Left → Right"],
                                      ["vertical", "Top → Bottom"],
                                    ]} />}
              <div className="row">
                <label>
                  X %
                  <input type="number" min={0} max={100} style={{ width: "5em" }}
                         value={Math.round(selected.x * 100)}
                         onChange={(e) => updateSelected({ x: clamp(Number(e.target.value) / 100, 0, 1) })} />
                </label>
                <label>
                  Y %
                  <input type="number" min={0} max={100} style={{ width: "5em" }}
                         value={Math.round(selected.y * 100)}
                         onChange={(e) => updateSelected({ y: clamp(Number(e.target.value) / 100, 0, 1) })} />
                </label>
                <label>
                  Radius %
                  <input type="number" min={2} max={45} style={{ width: "5em" }}
                         value={Math.round(selected.radius * 100)}
                         onChange={(e) => updateSelected({ radius: clamp(Number(e.target.value) / 100, MIN_RADIUS, MAX_RADIUS) })} />
                </label>
              </div>
            </>
          )}

          {selected.type && selected.type !== "gauge" && (
            <div className="row">
              <label>
                X %
                <input type="number" min={0} max={100} style={{ width: "5em" }}
                       value={Math.round(selected.x * 100)}
                       onChange={(e) => updateSelected({ x: clamp(Number(e.target.value) / 100, 0, 1) })} />
              </label>
              <label>
                Y %
                <input type="number" min={0} max={100} style={{ width: "5em" }}
                       value={Math.round(selected.y * 100)}
                       onChange={(e) => updateSelected({ y: clamp(Number(e.target.value) / 100, 0, 1) })} />
              </label>
              {(selected.type === "graph" || selected.type === "bar" || selected.type === "image" || selected.type === "media"
                || selected.type === "weather" || (selected.type === "clock" && selected.face === "image")) && (
                <>
                  <label>
                    Width %
                    <input type="number" min={4} max={90} style={{ width: "5em" }}
                           value={Math.round((selected.width ?? (selected.type === "media" ? 0.32 : selected.type === "weather" ? 0.28 : selected.type === "bar" ? 0.26 : selected.type === "clock" ? 0.22 : 0.2)) * 100)}
                           onChange={(e) => updateSelected({ width: clamp(Number(e.target.value) / 100, 0.04, 0.9) })} />
                  </label>
                  <label>
                    Height %
                    <input type="number" min={selected.type === "bar" ? 1 : 4} max={90} style={{ width: "5em" }}
                           value={Math.round((selected.height ?? (selected.type === "media" ? 0.52 : selected.type === "weather" ? 0.46 : selected.type === "bar" ? 0.12 : selected.type === "clock" ? 0.22 : 0.14)) * 100)}
                           onChange={(e) => updateSelected({ height: clamp(Number(e.target.value) / 100, selected.type === "bar" ? 0.01 : 0.04, 0.9) })} />
                  </label>
                </>
              )}
            </div>
          )}

          <div className="row">
            <button onClick={centerHorizontally} title="Set X to 50% -- centers it left/right">Center horizontally</button>
            <button onClick={centerVertically} title="Set Y to 50% -- centers it top/bottom">Center vertically</button>
          </div>
          <div className="row">
            <button onClick={bringToFront}>Bring to front</button>
            <button onClick={sendToBack}>Send to back</button>
            <button onClick={deleteSelected}>Delete</button>
          </div>
        </div>
          )}
        </div>
      </div>

      {bgDraft && (
        <Collapsible id="dashboard-background" title="Background" defaultOpen={false} as="div" className="canvas-props">
          <div className="row">
            <label>
              Widget style
              <select value={bgDraft.widget_style || "default"}
                      onChange={(e) => updateBgDraft({ widget_style: e.target.value })}>
                <option value="default">Default</option>
                {Object.entries(meta.widgetStyles || {}).map(([key, spec]) => (
                  <option key={key} value={key}>{spec.label}</option>
                ))}
              </select>
            </label>
            <label>
              Style
              <select
                value={bgDraft.mode}
                onChange={(e) => updateBgDraft({ mode: e.target.value })}
              >
                {Object.entries(meta.backgroundPresets).map(([key, label]) => (
                  <option key={key} value={key}>{label}</option>
                ))}
              </select>
            </label>
            {!(meta.backgroundImageModes || ["image"]).includes(bgDraft.mode) && (
              <label>
                Color scheme
                <select
                  value={bgDraft.scheme}
                  onChange={(e) => updateBgDraft({ scheme: e.target.value })}
                >
                  {Object.entries(meta.backgroundSchemes).map(([key, s]) => (
                    <option key={key} value={key}>{s.label}</option>
                  ))}
                </select>
              </label>
            )}
          </div>
          {bgDraft.mode === "image" && (
            <div className="row">
              <label className="grow">
                Image
                <input
                  type="file"
                  accept="image/*"
                  onChange={(e) => uploadImage("background", e.target.files[0],
                    (path) => updateBgDraft({ image_path: path }), setBgError)}
                />
              </label>
              {bgDraft.image_path && (
                <>
                  <img className="file-thumb" src={api.dashboardImageUrl(bgDraft.image_path)} alt="" />
                  <span className="hint">{basename(bgDraft.image_path)}</span>
                </>
              )}
              {uploadingId === "background" && <span className="hint">Uploading…</span>}
            </div>
          )}
          <p className="hint">
            {bgDraft.mode === "image"
              ? "Pick an image on this PC -- it's copied into this app's own folder, so moving or deleting the original afterward won't break it. Falls back to the default background if none is set."
              : (meta.backgroundImageModes || []).includes(bgDraft.mode)
              ? "One of the app's own built-in pictures, darkened a bit so gauges/text stay readable over it."
              : "The color scheme tints the gradient and, for Grid/Starfield/Radial, the whole background."}
          </p>
          <div className="row">
            <button onClick={saveBackground}>Save background</button>
          </div>
          {bgStatus && <p className="hint settings-saved">{bgStatus}</p>}
          {bgError && <p className="error">{bgError}</p>}
        </Collapsible>
      )}

      <div className="preset-picker">
        <div className="preset-picker-head">
          <span className="preset-picker-label">Presets</span>
          <div className="preset-head-actions">
            {/* A file input is the only way a browser will hand over a
                file's contents, so the visible button just forwards to
                a hidden one. */}
            <input type="file" accept="application/json,.json" ref={importInputRef}
                   style={{ display: "none" }}
                   onChange={(e) => {
                     importPresetFile(e.target.files?.[0]);
                     e.target.value = "";  // so picking the same file twice still fires
                   }} />
            <button className="preset-new" onClick={() => importInputRef.current?.click()}
                    title="Import a theme someone shared with you (.json)">
              Import theme
            </button>
            <button className="preset-new" onClick={createNewTheme}
                    title="Start an empty theme and edit it here">
              + New theme
            </button>
          </div>
        </div>
        {Object.keys(meta.presets || {}).length === 0 ? (
          <p className="hint">
            No saved presets yet -- "+ New theme" starts an empty one, or build a layout
            above and save it below.
          </p>
        ) : (
          <div className="preset-grid">
            {Object.keys(meta.presets || {}).map((name) => {
              const thumb = meta.presetThumbnails?.[name];
              const armed = presetPendingDelete === name;
              const builtin = isBuiltinPreset(name);
              return (
                <div key={name} className="preset-card">
                  <button
                    className="preset-card-thumb"
                    onClick={() => loadPreset(name)}
                    title={`Load "${name}"`}
                  >
                    {thumb ? (
                      <img src={thumb} alt={`Preview of the "${name}" preset`} />
                    ) : (
                      // A saved-but-unrendered preset (e.g. its thumbnail
                      // failed to render -- see AppController.
                      // _dashboard_preset_thumbnails()) still gets a
                      // clickable card, just without a picture, rather
                      // than disappearing from the picker entirely.
                      <span className="preset-card-thumb-fallback">No preview</span>
                    )}
                  </button>
                  <div className="preset-card-footer">
                    <span className="preset-card-name"
                          title={builtin
                            ? `${name} -- built in, read-only. Editing it saves a copy.`
                            : name}>
                      {name}
                      {/* Marks a preset that belongs to the app rather
                          than to this config: it can be loaded,
                          duplicated and hidden, but never written
                          over. */}
                      {builtin && <span className="preset-card-badge" title="Built-in preset (read-only)">◆</span>}
                    </span>
                    <div className="preset-card-actions" data-preset-menu>
                      <button
                        className="preset-card-menu-toggle"
                        data-preset-menu
                        onClick={() => setPresetMenuOpen((open) => (open === name ? null : name))}
                        title="Preset actions"
                        aria-label={`Actions for "${name}"`}
                        aria-expanded={presetMenuOpen === name}
                      >
                        ⋯
                      </button>
                      {presetMenuOpen === name && (
                        <div className="preset-card-menu" data-preset-menu>
                          <button
                            className="preset-card-duplicate"
                            data-preset-menu
                            onClick={() => duplicatePreset(name)}
                            title={`Duplicate "${name}"`}
                          >
                            Duplicate
                          </button>
                          <button
                            className="preset-card-duplicate"
                            data-preset-menu
                            onClick={() => exportPreset(name)}
                            title={`Save "${name}" as a .json file you can share`}
                          >
                            Export
                          </button>
                          <button
                            className={`preset-card-delete${armed ? " confirm" : ""}`}
                            data-preset-menu
                            onClick={() => deletePreset(name)}
                            title={armed
                              ? "Click again to confirm"
                              : builtin
                                ? `Hide "${name}" -- it's built in, so this can be undone with "Restore built-ins"`
                                : `Delete "${name}"`}
                          >
                            {armed ? "Confirm?" : builtin ? "Hide" : "Delete"}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      {(meta.dismissedBuiltinPresets || []).length > 0 && (
        <div className="row">
          <span className="hint grow">
            {(meta.dismissedBuiltinPresets || []).length} built-in preset
            {(meta.dismissedBuiltinPresets || []).length === 1 ? " is" : "s are"} hidden.
          </span>
          <button onClick={restoreBuiltins}>Restore built-ins</button>
        </div>
      )}
      <div className="row">
        <label className="grow">
          Save current layout as preset
          <input type="text" value={presetName} onChange={(e) => setPresetName(e.target.value)}
                 placeholder="e.g. Streaming layout" />
        </label>
        <button onClick={saveAsPreset} disabled={!presetName.trim()}>Save as preset</button>
      </div>
      {/* Says so before the save rather than after: a built-in can't be
          overwritten, so this name will come back as a copy. */}
      {isBuiltinPreset(presetName.trim()) && (
        <p className="hint">
          "{presetName.trim()}" is a built-in preset -- built-ins are read-only, so this
          saves your version as a separate copy ("{presetName.trim()} (custom)") and leaves
          the original alone.
        </p>
      )}

      {status && <p className="hint settings-saved">{status}</p>}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
