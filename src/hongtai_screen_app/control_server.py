"""
control_server.py -- the backend's local HTTP control API (ROADMAP.md
Phase 2). Generalizes the driver's existing web-mirror server (a single
/frame.jpg + one HTML page, tied to whichever HongtaiScreen happens to
be connected) into the always-available API surface a UI process talks
to: state, config get/set, start/stop/apply, a live log stream, and the
frame feed -- all backed by one AppController instance (controller.py),
which has no Tkinter dependency and works whether or not a screen is
currently connected.

**Binds to 127.0.0.1 only, on purpose.** Unlike the opt-in, LAN-facing
web mirror (see hongtai_screen.py's enable_web_mirror()), this API can
change settings and drive the panel -- it must never be reachable from
the network. Nothing here overlaps with the LAN mirror; both can run at
the same time.

Uses only Python's built-in http.server, no extra dependencies -- same
approach the driver's own mirror server already takes. SSE (the log
stream) is plain HTTP/1.0: no Content-Length, the connection just
stays open and the handler keeps writing `data: ...` lines to it until
the client disconnects or the server shuts down.

**Also serves the built frontend (ROADMAP.md Phase 2b), same origin.**
Any GET that isn't one of the API routes above falls through to
frontend/dist/ (Vite's build output -- see frontend/vite.config.js).
This is deliberate, not just convenient: the whole point of the
Phase 2 architecture is a UI process that talks to this backend over
HTTP, and serving the UI from the same process/port it's already
calling means there's no cross-origin request to worry about in
production at all (dev-mode `npm run dev` uses Vite's own proxy
instead, see vite.config.js). If frontend/dist/ hasn't been built yet,
GET / falls back to the placeholder page below instead of erroring.
"""
import json
import mimetypes
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .controller import AppController
from .paths import _app_base_dir

DEFAULT_PORT = 8899

FRONTEND_DIST = os.path.join(_app_base_dir(), "frontend", "dist")


def _make_handler(controller: AppController):
    class ControlHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):  # noqa: A002 -- keep stdout clean
            pass

        # -- helpers ----------------------------------------------------
        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status, message):
            self._send_json(status, {"error": message})

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ValueError(f"invalid JSON body: {e}")
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        # -- routing ------------------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/state":
                    self._send_json(200, controller.state())
                elif path == "/api/config":
                    self._send_json(200, controller.get_config())
                elif path == "/api/system":
                    self._send_json(200, controller.system_info())
                elif path == "/api/ports":
                    self._send_json(200, controller.list_ports())
                elif path == "/api/dashboard/meta":
                    self._send_json(200, controller.dashboard_meta())
                elif path == "/api/logs/stream":
                    self._handle_log_stream()
                elif path == "/frame.jpg":
                    self._handle_frame()
                elif path == "/api/dashboard/image":
                    self._handle_dashboard_image()
                elif self._try_serve_static(path):
                    pass
                elif path == "/":
                    self._handle_index()
                else:
                    self._send_error_json(404, f"no such endpoint: {path}")
            except Exception as e:  # noqa: BLE001 -- surfaced to the caller either way
                self._send_error_json(500, str(e))

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    patch = self._read_json_body()
                    self._send_json(200, controller.update_config(patch))
                elif path == "/api/brightness":
                    body = self._read_json_body()
                    self._send_json(200, controller.set_brightness(body.get("value")))
                elif path == "/api/start":
                    body = self._read_json_body()
                    self._send_json(200, controller.start(theme_name=body.get("theme")))
                elif path == "/api/stop":
                    controller.stop()
                    self._send_json(200, {"ok": True})
                elif path == "/api/apply":
                    controller.apply()
                    self._send_json(200, {"ok": True})
                elif path == "/api/startup":
                    body = self._read_json_body()
                    self._send_json(200, controller.set_startup(bool(body.get("enabled"))))
                elif path == "/api/shortcut":
                    self._send_json(200, controller.create_desktop_shortcut())
                elif path == "/api/keep_active_when_locked":
                    body = self._read_json_body()
                    self._send_json(200, controller.set_keep_active_when_locked(body.get("value")))
                elif path == "/api/dashboard/upload_image":
                    body = self._read_json_body()
                    self._send_json(200, controller.upload_dashboard_image(
                        body.get("filename"), body.get("data_base64")))
                elif path == "/api/dashboard/elements":
                    body = self._read_json_body()
                    self._send_json(200, controller.save_dashboard_elements(body.get("elements")))
                elif path == "/api/dashboard/preview":
                    body = self._read_json_body()
                    controller.preview_dashboard_elements(
                        body.get("elements"), body.get("background"), body.get("duration", 5.0))
                    self._send_json(200, {"ok": True})
                elif path == "/api/dashboard/background":
                    body = self._read_json_body()
                    self._send_json(200, controller.save_dashboard_background(body.get("background")))
                elif path == "/api/dashboard/live_preview":
                    body = self._read_json_body()
                    image = controller.render_dashboard_live_preview(body.get("elements"), body.get("background"))
                    self._send_json(200, {"image": image})
                elif path == "/api/dashboard/now_playing":
                    body = self._read_json_body()
                    self._send_json(200, controller.save_dashboard_now_playing(body.get("patch")))
                elif path == "/api/dashboard/presets":
                    body = self._read_json_body()
                    result = controller.save_dashboard_preset(
                        body.get("name"), body.get("elements"), body.get("background"))
                    self._send_json(200, result)
                elif path == "/api/dashboard/presets/delete":
                    body = self._read_json_body()
                    result = controller.delete_dashboard_preset(body.get("name"))
                    self._send_json(200, result)
                elif path == "/api/dashboard/presets/export":
                    body = self._read_json_body()
                    self._send_json(200, controller.export_dashboard_preset(body.get("name")))
                elif path == "/api/dashboard/presets/import":
                    body = self._read_json_body()
                    self._send_json(200, controller.import_dashboard_preset(
                        body.get("name"), body.get("preset")))
                elif path == "/api/dashboard/presets/restore_builtins":
                    # No body: it's the one-shot undo for deleting
                    # built-in presets (which are read-only otherwise),
                    # so there's nothing to parameterize.
                    self._send_json(200, controller.restore_dismissed_dashboard_presets())
                else:
                    self._send_error_json(404, f"no such endpoint: {path}")
            except (ValueError, RuntimeError) as e:
                # A request-shape problem or an invalid state transition
                # (already running, nothing to restart, ...) -- the
                # caller's fault, not a server error.
                self._send_error_json(400, str(e))
            except Exception as e:  # noqa: BLE001
                self._send_error_json(500, str(e))

        # -- static frontend build (frontend/dist/) ------------------------
        def _try_serve_static(self, path):
            """Serves a file out of FRONTEND_DIST for `path`, returning
            True if it did. Returns False (without writing a response)
            whenever there's nothing to serve -- FRONTEND_DIST doesn't
            exist (frontend not built yet) or the requested file isn't
            in it -- so the caller falls through to its normal
            "/" -> placeholder / else -> 404 handling instead."""
            if not os.path.isdir(FRONTEND_DIST):
                return False
            rel = unquote(path.lstrip("/")) or "index.html"
            full = os.path.normpath(os.path.join(FRONTEND_DIST, rel))
            dist_root = os.path.normpath(FRONTEND_DIST)
            # Reject anything that escapes FRONTEND_DIST (e.g. "/../..").
            if full != dist_root and not full.startswith(dist_root + os.sep):
                return False
            if not os.path.isfile(full):
                return False
            content_type, _ = mimetypes.guess_type(full)
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        # -- individual endpoints ------------------------------------------
        def _handle_frame(self):
            screen = controller.active_screen
            data = screen.get_mirror_frame_jpeg() if screen is not None else None
            if data is None:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_dashboard_image(self):
            """Serves back a previously-picked image (background,
            now-playing placeholder, an image element) by its stored
            path, so the design canvas can show what was actually
            picked -- as a thumbnail on the file field, and as the
            element's real content on the canvas itself -- immediately,
            without needing Save/Start/Apply first. `?path=` is the
            managed path upload_dashboard_image()/dashboard_meta()
            already handed the frontend; controller.read_dashboard_image()
            refuses anything outside image_store's own managed folder."""
            qs = parse_qs(urlparse(self.path).query)
            path = (qs.get("path") or [None])[0]
            try:
                data, resolved = controller.read_dashboard_image(path)
            except ValueError as e:
                self._send_error_json(404, str(e))
                return
            content_type, _ = mimetypes.guess_type(resolved)
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_log_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = controller.subscribe_log()
            try:
                while True:
                    line = q.get()
                    if line is None:  # controller.close()'s shutdown sentinel
                        return
                    data = line.replace("\r", "").replace("\n", "\\n")
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                controller.unsubscribe_log(q)

        def _handle_index(self):
            # Only reached when frontend/dist/ doesn't exist yet (not
            # built, or a fresh checkout) -- _try_serve_static() above
            # handles "/" once a real build is present. Just enough to
            # confirm the API is up when opened in a browser by hand.
            body = (
                "<!doctype html><html><body style=\"font-family:sans-serif\">"
                "<h3>Hongtai Screen -- control API</h3>"
                "<p>This is the backend's local control API (127.0.0.1 only). "
                "The real frontend isn't wired up yet -- see ROADMAP.md Phase 2."
                "</p><ul>"
                "<li><a href=\"/api/state\">/api/state</a></li>"
                "<li><a href=\"/api/config\">/api/config</a></li>"
                "<li><a href=\"/api/dashboard/meta\">/api/dashboard/meta</a></li>"
                "<li><a href=\"/frame.jpg\">/frame.jpg</a></li>"
                "</ul></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # http.server calls handle_error() from deep inside its own
        # internals on any exception it catches itself (not the ones we
        # already catch above) -- same reasoning as the driver's
        # _MirrorServer.handle_error(): a client that hangs up mid-request
        # (closing an SSE tab, say) is normal, not worth logging.
        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

    return ControlHandler


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc_type, exc, _tb = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        print(f"(control API: error handling a request from {client_address}: "
              f"{exc_type.__name__ if exc_type else '?'}: {exc})")


def run(controller: AppController = None, port: int = DEFAULT_PORT, blocking: bool = True):
    """Starts the control API bound to 127.0.0.1:`port`. Pass an existing
    `controller` to share state with something else already running one
    (e.g. a future in-process caller); otherwise a fresh AppController is
    created. Returns (server, controller, thread) when `blocking=False`
    so a caller can stop it later (server.shutdown() + controller.close());
    with `blocking=True` (the default) this runs serve_forever() itself
    and only returns after shutdown() is called from another thread."""
    controller = controller or AppController()
    handler = _make_handler(controller)
    server = ControlServer(("127.0.0.1", port), handler)

    if not blocking:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, controller, thread

    try:
        print(f"Control API listening on http://127.0.0.1:{port}/ (localhost only)")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
    return server, controller, None
