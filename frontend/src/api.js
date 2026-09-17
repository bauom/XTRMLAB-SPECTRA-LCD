// api.js -- the only file that knows the backend's actual endpoint
// shapes (control_server.py). Everything else in the app just calls
// these functions. Paths are relative ("/api/state", not a full
// origin) on purpose: in dev, Vite's proxy (vite.config.js) forwards
// them to the backend; in production, the backend serves this app's
// own build output, so "relative" already means "same origin, same
// port" with nothing to configure.

async function asJson(res) {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.error || `${res.status} ${res.statusText}`);
  }
  return body;
}

export function getState() {
  return fetch("/api/state").then(asJson);
}

export function getConfig() {
  return fetch("/api/config").then(asJson);
}

export function updateConfig(patch) {
  return fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  }).then(asJson);
}

// Brightness is split out from updateConfig() on purpose: the backend
// applies it live (no restart needed) the same way app.py's slider
// does, by pushing straight to the connected screen if one's running.
export function setBrightness(value) {
  return fetch("/api/brightness", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  }).then(asJson);
}

// "Detect screens" -- scans serial ports for a Hongtai-family panel
// (control_server.py/controller.py's list_ports(), same scan app.py's
// Tkinter Refresh button already uses). Each returned port's `value`
// is exactly what updateConfig({ port }) expects; `auto_detect` is the
// sentinel for "pick automatically at connect time" so this file
// doesn't need its own copy of that constant.
export function listPorts() {
  return fetch("/api/ports").then(asJson);
}

export function getSystem() {
  return fetch("/api/system").then(asJson);
}

export function setStartup(enabled) {
  return fetch("/api/startup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  }).then(asJson);
}

export function createShortcut() {
  return fetch("/api/shortcut", { method: "POST" }).then(asJson);
}

// Mirrors the official XTRM Lab app's "Keep playing when screen is
// off" setting -- see power_state.py's docstring. Applies immediately,
// no restart needed.
export function setKeepActiveWhenLocked(value) {
  return fetch("/api/keep_active_when_locked", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  }).then(asJson);
}

// Dashboard design canvas (ROADMAP.md Phase 5) -- a small surface of
// its own rather than going through updateConfig(), because that PATCHes
// the whole "dashboard" sub-object at once; these merge into it
// server-side instead, so saving a layout tweak can't accidentally wipe
// out web_port/enable_web/background/slots. See controller.py's
// dashboard_meta()/save_dashboard_elements() docstrings.
export function getDashboardMeta() {
  return fetch("/api/dashboard/meta").then(asJson);
}

export function uploadDashboardImage(file) {
  // Turns the picked File into a data: URL (FileReader), strips the
  // "data:image/png;base64," prefix, and posts the raw base64 to the
  // backend -- a browser file input can only ever hand back a file's
  // *content*, never a real filesystem path the way Tkinter's Browse
  // dialog can, so this is the actual "upload" half of "pick an
  // image": the backend copies the bytes into its own managed image
  // folder and hands back that copy's path.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("couldn't read the file"));
    reader.onload = () => {
      const dataUrl = reader.result;
      const comma = dataUrl.indexOf(",");
      const data_base64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
      fetch("/api/dashboard/upload_image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, data_base64 }),
      }).then(asJson).then(resolve, reject);
    };
    reader.readAsDataURL(file);
  });
}

// Points the canvas at the actual picked image (background, now-playing
// placeholder, an image/media element) instead of just its filename --
// see control_server.py's _handle_dashboard_image()/controller.py's
// read_dashboard_image(). `path` is always one this app itself handed
// back (from uploadDashboardImage() or dashboard/meta), never anything
// typed in directly, but it's still passed as a query param the
// backend re-validates (image_store.is_managed()) rather than trusted
// blindly.
export function dashboardImageUrl(path) {
  if (!path) return null;
  return `/api/dashboard/image?path=${encodeURIComponent(path)}`;
}

export function saveDashboardElements(elements) {
  return fetch("/api/dashboard/elements", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ elements }),
  }).then(asJson);
}

export function previewDashboardElements(elements, background = null, duration = 5) {
  // Same shape as saveDashboardElements(), but the backend never
  // touches config_store for this one -- it just shows `elements` (and,
  // if given, `background`) live on the running dashboard theme for
  // `duration` seconds, then reverts both to whatever's actually saved
  // on its own (controller.py's preview_dashboard_elements()/
  // _revert_dashboard_preview()). Lets the design canvas offer "see
  // this on the real panel" without it being the same commitment as
  // Save layout / Save background. `background` used to not be sent at
  // all -- previewing only ever pushed elements, so a preview right
  // after loading a preset showed its layout over whatever background
  // was still actually saved, not the preset's own.
  return fetch("/api/dashboard/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ elements, background, duration }),
  }).then(asJson);
}

export function renderDashboardLivePreview(elements, background) {
  // A one-shot render of `elements`/`background` using this machine's
  // actual current stats (controller.py's render_dashboard_live_
  // preview() / dashboard_theme.render_live_preview()) -- returns
  // {image: "data:image/jpeg;base64,..."}, ready for an <img src=...>.
  // The design canvas polls this on an interval while there's an
  // unsaved edit the real live panel photo doesn't reflect yet, so
  // gauges/graphs keep visibly moving with real numbers instead of
  // freezing on whatever they showed the moment a preset was loaded.
  return fetch("/api/dashboard/live_preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ elements, background }),
  }).then(asJson);
}

export function saveDashboardBackground(background) {
  return fetch("/api/dashboard/background", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ background }),
  }).then(asJson);
}

export function saveDashboardNowPlaying(patch) {
  return fetch("/api/dashboard/now_playing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch }),
  }).then(asJson);
}

export function saveDashboardPreset(name, elements, background) {
  return fetch("/api/dashboard/presets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, elements, background }),
  }).then(asJson);
}

export function exportDashboardPreset(name) {
  // Returns {name, preset} with every image the preset references
  // inlined as base64 (controller.py's export_dashboard_preset()), so
  // the file the user saves works on someone else's machine instead of
  // pointing at paths only theirs has.
  return fetch("/api/dashboard/presets/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then(asJson);
}

export function importDashboardPreset(name, preset) {
  return fetch("/api/dashboard/presets/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, preset }),
  }).then(asJson);
}

export function restoreBuiltinDashboardPresets() {
  // Puts every deleted built-in preset back (controller.py's
  // restore_dismissed_dashboard_presets()). No arguments: built-ins
  // are read-only, so deleting one is the only state there is to undo.
  return fetch("/api/dashboard/presets/restore_builtins", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).then(asJson);
}

export function deleteDashboardPreset(name) {
  return fetch("/api/dashboard/presets/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then(asJson);
}

export function start(theme) {
  return fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(theme ? { theme } : {}),
  }).then(asJson);
}

export function stop() {
  return fetch("/api/stop", { method: "POST" }).then(asJson);
}

export function apply() {
  return fetch("/api/apply", { method: "POST" }).then(asJson);
}

// EventSource handles SSE reconnection itself; the caller just gets a
// stream of log lines and an unsubscribe function.
export function subscribeLogs(onLine) {
  const source = new EventSource("/api/logs/stream");
  source.onmessage = (ev) => onLine(ev.data);
  return () => source.close();
}

export const THEMES = ["dashboard", "video", "webpage", "clock"];

// The display label controller.py's state() reports as "running_theme"
// for each theme key -- see theme_kwargs.py's BUILDERS, which is where
// these come from ("Dashboard", "Video", "Webpage Mirror", "Clock").
// Used to figure out which THEMES key is actually running so the
// picker can show it, rather than whatever it happened to default to.
export const THEME_LABELS = {
  dashboard: "Dashboard",
  video: "Video",
  webpage: "Webpage Mirror",
  clock: "Clock",
};
