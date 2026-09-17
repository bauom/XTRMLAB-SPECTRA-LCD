"""
dashboard_theme.py -- a neon "cyberpunk dashboard" theme: separate
glowing CPU LOAD / CPU TEMP gauges on the left, GPU LOAD / GPU TEMP on
the right, and Spotify album art in the middle with playback progress
and the clock underneath it.

    python dashboard_theme.py               # auto-detects the port
    python dashboard_theme.py COM5           # explicit port
    python dashboard_theme.py --web-port 9000  # change the mirror's port
    python dashboard_theme.py --no-web      # disable the web mirror

Live web mirror: this also serves a webpage that mirrors whatever's
actually on the panel, in real time -- open the URL it prints (also
reachable from a phone on the same network) to watch the dashboard
without standing in front of the case. It's a literal mirror of the
exact frame being sent to the panel, not a separate re-implementation,
so there's nothing to keep in sync -- see enable_web_mirror() in
hongtai_screen.py, which any script here can use the same way.

Layout:

    +------------+----------------------+------------+
    |  CPU LOAD  |     [album art]      |  GPU LOAD  |
    |  (glowing  |    TRACK TITLE       |  (glowing  |
    |   ring)    |       artist         |   ring)    |
    +------------+  ===o=========== |  +------------+
    |  CPU TEMP  |    1:23 / 4:06        |  GPU TEMP  |
    |  (glowing  |       HH:MM:SS        |  (glowing  |
    |   ring)    |                       |   ring)    |
    +------------+----------------------+------------+

Each gauge is a full-circle neon dial (dim unlit track + a bright,
blurred/glowing filled arc + a lit needle) with major ticks at
0/25/50/75/100 and minor ticks every 5, on a dark background textured
with a faint hex grid and a handful of circuit-board trace lines for a
"cyberpunk panel" look instead of flat cards. The gauges themselves are
rendered with cairo (`pip install pycairo`) instead of hand-drawn PIL
polygons -- PIL's `draw.arc()` approximates a circle with straight
segments, which is what made the original gauges look faceted/jagged
up close; cairo draws true anti-aliased arcs and real gradients, so
the ring, the needle, and the glowing hub all look smooth at any size.
Only the glowing fill arc, needle, and value number are redrawn per
frame -- the ticks, dim track, background texture, and labels are
baked into a single static background image once at startup, which is
what keeps this fast enough to still hit a smooth refresh (see
"Refresh rate" below).

What each panel needs to work, and what happens if it's missing:

  - CPU (left): psutil, which you already have installed for
    demo_clock.py, for utilization. Temperature is a separate story on
    Windows -- see "CPU temperature" below, since psutil alone can't
    get it there.

  - GPU (right): needs an NVIDIA GPU + `pip install nvidia-ml-py`
    (that package's importable name is `pynvml`). If there's no NVIDIA
    GPU, or the driver library can't be found, that gauge just shows
    "N/A" instead of crashing the rest of the theme.

  - Spotify art + progress (middle): reads whatever is currently
    playing via Windows' own now-playing system (the same info the
    volume flyout / lock screen show) -- not the Spotify API, so
    there's no app to register or API key to get. Needs
    `pip install winsdk` and Windows 10 1809+. Works with the Spotify
    desktop app; if nothing is playing (or winsdk isn't installed), it
    shows a placeholder in place of album art instead, with no progress
    bar. That placeholder is a plain drawn icon by default -- pass
    --default-art to point it at your own image instead (nothing is
    bundled or assumed).
    That Windows call is a round trip to a system broker process and
    can take a second or more, so it's polled from its own background
    thread instead of the render loop -- the displayed position runs
    on its own free-running clock, anchored on the timestamp Windows
    attaches to each real update (not on whenever our poll happened to
    finish), and only snaps to the real value if the two disagree by
    more than ~1.2s (an actual pause, seek, or track change).

Refresh rate: the main loop targets 10Hz (redraws every ~100ms) and
times itself (like screen.run() does), sleeping only whatever's left
of that 100ms budget after everything else that frame needed -- CPU/GPU
reads, the SystemInfos.exe file read, rendering, and the JPEG
encode/send -- so one slow frame doesn't push every frame after it
late. 10Hz is a target, not a guarantee: a frame that's too large to
encode+transmit within ~100ms over the panel's 2 Mbaud link just makes
that one frame take longer, and the loop picks the pace back up on the
next one rather than trying to catch up.

CPU temperature (why it showed N/A):
    psutil.sensors_temperatures() is basically Linux-only -- Windows
    doesn't expose CPU temp through the API psutil uses at all, so
    "N/A" there wasn't a bug, it was Windows not offering the number.

    The XTRM lab app itself doesn't read it through Windows either --
    it ships its own helper, `SystemInfos.exe` (found inside the app's
    own install folder, under SDK/VC#/SystemInfos/.../Release/), which
    wraps CPUID's hardware SDK + a licensed HWiNFO sensor DLL and
    writes live sensor readings to a small file in %TEMP% once a
    second. This script spawns that exact helper itself and reads the
    same file -- no third-party monitoring tool needed, since the
    right tool was already installed on this machine the whole time.
    It's found automatically at the app's default install path; if
    yours is installed somewhere else, set SYSTEMINFOS_DIR below.
    Needs the script to run as Administrator the first time (same as
    the vendor app does) so its driver can load. If that helper can't
    be found or spawned, CPU temp falls back to psutil, which on
    Windows means it'll just show "--".

Install everything this theme can use:

    pip install pyserial pillow numpy pycairo psutil nvidia-ml-py winsdk

pycairo and numpy are required (they draw the gauges); the rest
degrade independently as described above.

You don't need all of them -- each stat degrades independently if its
dependency is missing.
"""

import argparse
import asyncio
from collections import deque
from copy import deepcopy
import functools
import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import cairo
except ImportError:  # pragma: no cover -- surfaced clearly at startup, see main()
    cairo = None

from ..driver.hongtai_screen import HongtaiScreen
from .. import weather
from .. import power_state
from ..paths import resource_path
from . import widget_styles

# ---------------------------------------------------------------- psutil ---

import psutil


CPU_UTIL_REFRESH = 0.5  # seconds -- CPU LOAD is *sampled* at 2Hz, independent
                         # of the main 10Hz render loop. psutil.cpu_percent()
                         # sampled every single frame was jittery/noisy at
                         # 10Hz (each call is just an instantaneous delta
                         # since the previous call, ~100ms apart), and simply
                         # holding that 2Hz reading flat for 5 frames then
                         # snapping to the next one made the needle visibly
                         # jump. Instead we glide from the previous sample to
                         # the new one linearly over the interval between
                         # samples, so the value returned to the renderer is
                         # slightly different every 10Hz frame and the needle
                         # moves smoothly instead of stepping.
_cpu_util_state = {"prev": None, "next": None, "sampled_at": 0.0}


def _get_cpu_util():
    now = time.time()
    state = _cpu_util_state
    if now - state["sampled_at"] >= CPU_UTIL_REFRESH:
        raw = psutil.cpu_percent(interval=None)
        state["prev"] = state["next"] if state["next"] is not None else raw
        state["next"] = raw
        state["sampled_at"] = now
    if state["next"] is None:
        return None
    if state["prev"] is None:
        return state["next"]
    frac = min(1.0, (now - state["sampled_at"]) / CPU_UTIL_REFRESH)
    return state["prev"] + (state["next"] - state["prev"]) * frac


def get_cpu_stats():
    """CPU load only now -- CPU temp used to live here too, but it's
    gone from this dashboard on purpose: the AIO panel shows CPU temp
    of its own, so duplicating it here was redundant (and see
    read_systeminfos()'s docstring for a real bug that used to make it
    show a frozen, wrong number half the time anyway)."""
    return {"util": _get_cpu_util()}


_ram_state = {"prev": None, "next": None, "sampled_at": 0.0}


def get_ram_percent():
    """Used-memory percentage, smoothed the same way _get_cpu_util() is
    (psutil's own number updates in coarse steps; interpolating between
    samples keeps the needle moving smoothly at this theme's 10Hz
    redraw rate instead of visibly stair-stepping)."""
    now = time.time()
    state = _ram_state
    if now - state["sampled_at"] >= CPU_UTIL_REFRESH:
        try:
            raw = psutil.virtual_memory().percent
        except Exception:  # noqa: BLE001
            return None
        state["prev"] = state["next"] if state["next"] is not None else raw
        state["next"] = raw
        state["sampled_at"] = now
    if state["next"] is None:
        return None
    if state["prev"] is None:
        return state["next"]
    frac = min(1.0, (now - state["sampled_at"]) / CPU_UTIL_REFRESH)
    return state["prev"] + (state["next"] - state["prev"]) * frac


def _pdh_cpu_performance_percent():
    """`\\Processor Information(_Total)\\% Processor Performance` -- how
    fast the CPU is actually running right now as a percentage of its
    *base* clock, read straight from Windows' performance-counter API
    (PDH) via ctypes. Goes above 100 when boosting, which is the whole
    point: multiply it by the base clock and you get the real current
    frequency, turbo included.

    Windows-only and deliberately dependency-free (ctypes, not pywin32
    or a WMI package). Any failure -- wrong OS, counter missing on an
    odd SKU, PDH refusing for any reason -- returns None so the caller
    falls back, rather than taking the whole stat down.

    A PDH counter needs two collections a moment apart to produce a
    rate, so the query is opened once and kept, and the first call
    after opening deliberately returns None (there's nothing to
    compare against yet)."""
    if sys.platform != "win32":
        return None
    global _pdh_state
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None

    try:
        pdh = ctypes.WinDLL("pdh.dll")
        if _pdh_state is None:
            query = wintypes.LPVOID()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                _pdh_state = False   # don't retry every frame once it's known-bad
                return None
            counter = wintypes.LPVOID()
            # The "English" variant so this keeps working on a
            # non-English Windows, where the localized counter name
            # would be something else entirely.
            if pdh.PdhAddEnglishCounterW(
                    query, r"\Processor Information(_Total)\% Processor Performance",
                    0, ctypes.byref(counter)) != 0:
                pdh.PdhCloseQuery(query)
                _pdh_state = False
                return None
            pdh.PdhCollectQueryData(query)   # priming sample
            _pdh_state = (pdh, query, counter)
            return None
        if _pdh_state is False:
            return None

        pdh, query, counter = _pdh_state
        if pdh.PdhCollectQueryData(query) != 0:
            return None

        class _FMT(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        value = _FMT()
        PDH_FMT_DOUBLE = 0x00000200
        if pdh.PdhGetFormattedCounterValue(
                counter, PDH_FMT_DOUBLE, None, ctypes.byref(value)) != 0:
            return None
        return value.doubleValue
    except Exception:  # noqa: BLE001
        _pdh_state = False
        return None


_pdh_state = None          # None = not opened yet, False = unavailable, else (pdh, query, counter)
_cpu_freq_state = {"value": None, "sampled_at": 0.0}
CPU_FREQ_REFRESH = 1.0     # seconds between real reads -- the clock is sampled, not smoothed


def get_cpu_freq_ghz():
    """Current CPU clock speed in GHz -- one of this theme's selectable
    stats (see STAT_DEFS' "cpu_freq" entry for its fixed gauge ceiling,
    CPU_FREQ_GAUGE_MAX_GHZ). CPU temp itself isn't shown here at all
    (see get_cpu_stats()'s docstring: the AIO panel already covers it),
    so this is a different number entirely, not a smaller version of
    the same one.

    This used to be `psutil.cpu_freq().current` alone, which on Windows
    is a lie a lot of the time: psutil reads CurrentMhz out of
    CallNtPowerInformation(ProcessorInformation), and on modern
    machines Windows just reports the *nominal* clock there -- so the
    stat sat on one fixed number forever (reported as "CPU Clock is
    always showing as 3.4G which isn't accurate", against a CPU whose
    vendor app was reading 5.5GHz at the same moment). It isn't a
    smoothing or rounding problem; the number never had the real clock
    in it to begin with.

    So, in order:

    1. The `% Processor Performance` performance counter (see
       _pdh_cpu_performance_percent()) against the base clock. This is
       the reading that actually tracks boost, and it's what Task
       Manager's own "Speed" field is derived from.
    2. psutil's `current`, if it's meaningfully different from
       `max` -- on Linux (and some Windows setups) it IS the live
       value, so it's a real answer there rather than a fallback.
    3. psutil's `current` regardless, as a last resort: a fixed number
       is still better than an empty gauge, and it's what this stat
       always showed before.

    Sampled at CPU_FREQ_REFRESH rather than per frame: PDH is cheap but
    not free, and a clock readout that updates once a second reads as
    steady rather than jittery."""
    now = time.time()
    state = _cpu_freq_state
    if state["value"] is not None and now - state["sampled_at"] < CPU_FREQ_REFRESH:
        return state["value"]

    try:
        freq = psutil.cpu_freq()
    except Exception:  # noqa: BLE001 -- not available on every platform
        freq = None

    value = None
    # Base clock: psutil's `max` is the nominal/marketing clock, which
    # is exactly what the performance counter is a percentage OF.
    base_mhz = getattr(freq, "max", None) or None
    percent = _pdh_cpu_performance_percent()
    if base_mhz and percent:
        value = (base_mhz * percent / 100.0) / 1000.0
    elif freq is not None:
        value = freq.current / 1000.0

    if value is not None:
        state["value"] = value
        state["sampled_at"] = now
    return state["value"] if value is None else value


_volume_state = {"value": None, "sampled_at": 0.0}
_volume_endpoint = None     # None = not tried, False = unavailable, else the IAudioEndpointVolume
VOLUME_REFRESH = 0.5        # seconds -- fast enough that nudging the volume key looks live


def _audio_endpoint():
    """The system's default playback device's volume interface (pycaw),
    or False if this machine can't provide one -- not Windows, pycaw
    not installed, or no output device at all.

    Cached because resolving the endpoint goes through COM device
    enumeration, which is far too heavy to redo at the render loop's
    rate; the interface itself stays valid and keeps reporting the
    current level as the user moves the slider."""
    global _volume_endpoint
    if _volume_endpoint is not None:
        return _volume_endpoint
    _volume_endpoint = False
    if sys.platform != "win32":
        return _volume_endpoint
    try:
        import comtypes
        from ctypes import POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        # The render loop is its own thread and pycaw's COM calls need
        # that thread initialized; harmless if something else already
        # did it.
        try:
            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        _volume_endpoint = cast(interface, POINTER(IAudioEndpointVolume))
    except Exception:  # noqa: BLE001 -- pycaw missing, no audio device, COM refusing
        _volume_endpoint = False
    return _volume_endpoint


def get_volume_percent():
    """The PC's master output volume, 0-100 -- the same number the
    Windows volume slider shows, and 0 while muted (a gauge reading 40%
    with nothing audible would be the wrong answer to "what's my
    volume").

    Windows-only in practice: it needs pycaw (see requirements.txt),
    which is an optional dependency, so a machine without it just gets
    None and the stat renders "--" like any other unavailable reading
    rather than breaking the theme.

    Uses the scalar level (what the slider shows), not the master
    *dB* level -- the two differ, and the scalar is the one a person
    recognizes as "my volume is at 40%"."""
    now = time.time()
    state = _volume_state
    if now - state["sampled_at"] < VOLUME_REFRESH:
        return state["value"]
    state["sampled_at"] = now

    endpoint = _audio_endpoint()
    if endpoint is False:
        state["value"] = None
        return None
    try:
        if endpoint.GetMute():
            state["value"] = 0.0
        else:
            state["value"] = max(0.0, min(100.0, endpoint.GetMasterVolumeLevelScalar() * 100.0))
    except Exception:  # noqa: BLE001 -- device unplugged/changed under us
        # Drop the cached interface so the next read re-resolves
        # whatever the default device is now.
        global _volume_endpoint
        _volume_endpoint = None
        state["value"] = None
    return state["value"]


def get_disk_usage_percent():
    """Used-space percentage of the system drive. No smoothing needed
    here unlike the other gauges -- disk usage barely moves between
    frames, so raw psutil output is already visually steady."""
    try:
        return psutil.disk_usage(os.path.abspath(os.sep)).percent
    except Exception:  # noqa: BLE001
        return None


NETWORK_GAUGE_MAX_MB_S = 20.0  # the gauge's 100% mark -- roughly 160Mbps
# combined up+down. A burst above this just pegs the needle at 100% while
# the printed number keeps showing the real value; tune to your own
# connection if 20MB/s is way off from what "full" looks like for you.
_NETWORK_SMOOTHING = 0.3  # 0-1, higher = follows raw jumps more closely
_net_state = {"prev_bytes": None, "prev_t": None, "smoothed": None}


def get_network_rate_mb_s():
    """Combined upload+download throughput in MB/s. Needs two samples to
    compute a rate at all (returns None on the very first call), and
    smooths the result a little -- a raw ~100ms-apart delta is jumpy
    enough to make the needle twitch distractingly otherwise."""
    try:
        counters = psutil.net_io_counters()
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    total = counters.bytes_sent + counters.bytes_recv
    state = _net_state
    prev_bytes, prev_t = state["prev_bytes"], state["prev_t"]
    state["prev_bytes"], state["prev_t"] = total, now
    if prev_bytes is None or now <= prev_t:
        return None
    rate = max(0.0, (total - prev_bytes) / (now - prev_t) / (1024 * 1024))
    state["smoothed"] = rate if state["smoothed"] is None else (
        state["smoothed"] + (rate - state["smoothed"]) * _NETWORK_SMOOTHING)
    return state["smoothed"]


DISK_IO_GAUGE_MAX_MB_S = 200.0  # same idea as NETWORK_GAUGE_MAX_MB_S's ceiling,
# just for local disk read+write instead of network -- tune to your drive
# (a fast NVMe can burst well past this; an old spinning disk far under it).
_DISK_IO_SMOOTHING = 0.3
_disk_io_state = {"prev_bytes": None, "prev_t": None, "smoothed": None}


def get_disk_io_mb_s():
    """Combined read+write throughput of the system's disks in MB/s --
    distinct from get_disk_usage_percent() (how full the drive is);
    this is how hard it's currently being hammered. Same two-sample/
    smoothing approach as get_network_rate_mb_s()."""
    try:
        counters = psutil.disk_io_counters()
    except Exception:  # noqa: BLE001
        return None
    if counters is None:
        return None
    now = time.time()
    total = counters.read_bytes + counters.write_bytes
    state = _disk_io_state
    prev_bytes, prev_t = state["prev_bytes"], state["prev_t"]
    state["prev_bytes"], state["prev_t"] = total, now
    if prev_bytes is None or now <= prev_t:
        return None
    rate = max(0.0, (total - prev_bytes) / (now - prev_t) / (1024 * 1024))
    state["smoothed"] = rate if state["smoothed"] is None else (
        state["smoothed"] + (rate - state["smoothed"]) * _DISK_IO_SMOOTHING)
    return state["smoothed"]


def get_swap_percent():
    """Used-swap percentage -- RAM's counterpart. No smoothing, same
    reasoning as get_disk_usage_percent(): it doesn't move fast enough
    between frames to need it."""
    try:
        return psutil.swap_memory().percent
    except Exception:  # noqa: BLE001
        return None


PROCESS_COUNT_MAX = 400.0  # gauge ceiling -- a "busy but normal" desktop
# usually sits well under this; tune it if your baseline process count
# runs a lot higher or lower.


def get_process_count():
    """Total running process count -- a rough, at-a-glance "how busy is
    this machine overall" number distinct from any single resource's
    load."""
    try:
        return float(len(psutil.pids()))
    except Exception:  # noqa: BLE001
        return None


_cpu_peak_state = {"prev": None, "next": None, "sampled_at": 0.0}


def get_cpu_load_peak_core():
    """Highest single core's utilization, smoothed the same way
    _get_cpu_util() smooths the overall average. On a many-core CPU the
    plain average (get_cpu_stats()) can look moderate while one core is
    actually pegged (a single-threaded task, for instance) -- this is
    the number that shows that."""
    now = time.time()
    state = _cpu_peak_state
    if now - state["sampled_at"] >= CPU_UTIL_REFRESH:
        try:
            per_core = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:  # noqa: BLE001
            return None
        if not per_core:
            return None
        raw = max(per_core)
        state["prev"] = state["next"] if state["next"] is not None else raw
        state["next"] = raw
        state["sampled_at"] = now
    if state["next"] is None:
        return None
    if state["prev"] is None:
        return state["next"]
    frac = min(1.0, (now - state["sampled_at"]) / CPU_UTIL_REFRESH)
    return state["prev"] + (state["next"] - state["prev"]) * frac


def get_battery_percent():
    """Battery charge percentage, if this machine reports one at all --
    a desktop tower usually won't (returns None, which just shows "--"
    like any other unavailable stat), but a laptop or a UPS psutil can
    see will."""
    try:
        battery = psutil.sensors_battery()
    except Exception:  # noqa: BLE001
        return None
    return battery.percent if battery else None


# ------------------------------------------------------ SystemInfos.exe ---
# The XTRM lab app's own bundled sensor helper. It writes a small JSON
# blob to %TEMP%\<name>.bin (4-byte little-endian length prefix, then
# UTF-8 JSON) about once a second, keyed by app name -- this is exactly
# what the vendor app itself reads to show CPU/GPU temperature. See the
# module docstring for how this was found.

SYSTEMINFOS_DIR = r"C:\Program Files\XTRM lab\resources\main\SDK\VC#\SystemInfos\vs2008\bin\x64\Release"
SYSTEMINFOS_EXE = os.path.join(SYSTEMINFOS_DIR, "SystemInfos.exe")
SYSTEMINFOS_APP_NAME = "XTRM_lab"  # must match the vendor app's package.json "name"
SYSTEMINFOS_SHM_PATH = os.path.join(tempfile.gettempdir(), f"{SYSTEMINFOS_APP_NAME}.bin")

_systeminfos_proc = None


def start_systeminfos():
    """Spawn the vendor's own sensor-reading helper in the background,
    the same way their app does. Safe to call even if it's already
    running (e.g. because the XTRM lab app is also open) -- if the
    output file is already being updated, we just don't bother
    launching a second copy."""
    global _systeminfos_proc
    if _systeminfos_proc is not None or not os.path.exists(SYSTEMINFOS_EXE):
        return

    try:
        if time.time() - os.path.getmtime(SYSTEMINFOS_SHM_PATH) < 3.0:
            return  # something is already feeding this file
    except OSError:
        pass

    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _systeminfos_proc = subprocess.Popen(
            [SYSTEMINFOS_EXE, SYSTEMINFOS_APP_NAME],
            cwd=SYSTEMINFOS_DIR,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        _systeminfos_proc = None


def stop_systeminfos():
    global _systeminfos_proc
    if _systeminfos_proc is not None:
        try:
            _systeminfos_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        _systeminfos_proc = None


def read_systeminfos():
    """Read+parse the current frame. Returns None if the helper isn't
    running yet, hasn't written anything, isn't installed at the
    expected path, or -- and this is the case that actually matters
    here -- HAS written something before but has since stopped updating
    it (e.g. it needs Administrator to load its sensor driver and this
    app isn't running elevated, so it wrote one valid frame at startup
    and then silently exited): without a freshness check, a stopped
    helper's last frame just sits on disk and reads back as if it were
    live forever, showing a frozen, increasingly-wrong number (the
    literal "CPU temp always at 41C" bug) instead of "no data". The file
    is meant to update about once a second either way, so anything more
    than a few seconds stale is treated as no reading at all."""
    try:
        if time.time() - os.path.getmtime(SYSTEMINFOS_SHM_PATH) > 3.0:
            return None
        with open(SYSTEMINFOS_SHM_PATH, "rb") as f:
            head = f.read(4)
            if len(head) < 4:
                return None
            n = struct.unpack("<I", head)[0]
            if n == 0 or n > 65532:
                return None
            return json.loads(f.read(n).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _sensor_value(frame, section_key, list_key):
    """Pull a value out of a SystemInfos frame the same way the vendor
    app does: frame[section_key][f"{list_key}_list"], preferring the
    sensor flagged "checked" and falling back to the first one."""
    if not frame:
        return None
    section = frame.get(section_key) or {}
    sensors = section.get(f"{list_key}_list") or []
    if not sensors:
        return None
    for s in sensors:
        if s.get("checked"):
            return s.get("value")
    return sensors[0].get("value")


# ----------------------------------------------------------------- pynvml --

try:
    import pynvml

    pynvml.nvmlInit()
    _gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    _GPU_OK = True
except Exception:  # noqa: BLE001 -- no nvidia GPU, no driver, lib missing, etc.
    _GPU_OK = False


def get_gpu_stats(sysinfo_frame=None):
    if _GPU_OK:
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(_gpu_handle).gpu
            temp = pynvml.nvmlDeviceGetTemperature(_gpu_handle, pynvml.NVML_TEMPERATURE_GPU)
            return {"util": util, "temp": temp}
        except Exception:  # noqa: BLE001
            pass

    # Fall back to the same SystemInfos.exe feed used for CPU temp --
    # this also covers non-NVIDIA GPUs, since it's not NVML-specific.
    util = _sensor_value(sysinfo_frame, "graphics", "utilization")
    temp = _sensor_value(sysinfo_frame, "graphics", "temperature")
    if util is None and temp is None:
        return None
    return {"util": util, "temp": temp}


def get_vram_percent():
    """GPU memory used, as a percentage -- one of the two gauges flanking
    the clock. Only available when pynvml found an NVIDIA GPU (_GPU_OK
    below); None otherwise, which just draws that gauge's dim track."""
    if not _GPU_OK:
        return None
    try:
        mem = pynvml.nvmlDeviceGetMemoryInfo(_gpu_handle)
        return mem.used / mem.total * 100
    except Exception:  # noqa: BLE001
        return None


GPU_POWER_MAX_W = 350.0  # gauge ceiling -- a reasonable high-end-card TDP;
# tune it down for a lower-power card so the needle actually uses the dial.


def get_gpu_power_w():
    """Live GPU power draw in watts -- pynvml-only (no SystemInfos.exe
    fallback; the vendor feed this theme otherwise falls back to for
    non-NVIDIA GPUs doesn't expose this), so this is None on anything
    but an NVIDIA card, same graceful "--" as any other missing stat."""
    if not _GPU_OK:
        return None
    try:
        return pynvml.nvmlDeviceGetPowerUsage(_gpu_handle) / 1000.0
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------- winsdk --

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winsdk.windows.storage.streams import Buffer, InputStreamOptions

    _MEDIA_OK = True
except Exception:  # noqa: BLE001 -- not on Windows, or winsdk not installed
    _MEDIA_OK = False

# Cache the last decoded album art by (title, artist) so we don't
# re-decode a JPEG every single poll for a track that hasn't changed.
_art_cache_key = None
_art_cache_img = None

# The session manager is a broker-process proxy, not a per-poll query --
# Microsoft's own guidance is to request it once and reuse it, calling
# .get_sessions() on it as often as you like. This used to call
# MediaManager.request_async() fresh on every single poll (once a
# second, for as long as the theme runs -- hours, if left open
# overnight) instead, which is the likely cause of a slow, real memory
# leak reported after leaving the app running overnight: each call
# round-trips to the system's media broker and constructs a new WinRT
# projection object graph (manager, session list, properties, timeline,
# playback info); WinRT objects are COM-reference-counted underneath
# Python's own refcounting, and that native side doesn't reliably get
# released just because the Python wrapper falls out of scope on a
# background asyncio loop that's never actually idle. At ~1 poll/sec,
# a leak of only ~45KB per call -- entirely plausible for an
# unreleased COM object graph -- adds up to the ~2GB reported after a
# single overnight run. Caching the manager removes that whole extra
# round trip and object graph from every poll but one; if native
# session/property objects are still the culprit even with this fix,
# the next thing to try is dropping `session`/`props`/`timeline`/
# `playback` more aggressively (e.g. `del` before returning) so nothing
# outlives the function on this event loop's periodic tick.
_media_manager = None


async def _get_media_manager():
    global _media_manager
    if _media_manager is None:
        _media_manager = await MediaManager.request_async()
    return _media_manager


def _winrt_datetime_to_epoch(dt):
    """winsdk maps Windows.Foundation.DateTime to a Python datetime;
    treat it as UTC (Windows always reports it in UTC, but the object
    doesn't always come back tz-aware) and convert to a plain epoch
    timestamp comparable with time.time()."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


async def _get_media_info_async():
    manager = await _get_media_manager()

    session = None
    for s in manager.get_sessions():
        if "spotify" in (s.source_app_user_model_id or "").lower():
            session = s
            break
    # Deliberately NOT falling back to manager.get_current_session() when
    # no Spotify session is found -- that returns whatever Windows
    # considers the "current" now-playing session system-wide, which is
    # very often a browser tab with a media session registered (a
    # YouTube embed, a page with a <video>/<audio> element, even one
    # that isn't actually playing) rather than Spotify. That's why a
    # random webpage's title could show up here with Spotify closed --
    # this theme is specifically a Spotify display, so no Spotify
    # session found means "not playing" rather than "show whatever else
    # Windows has".
    if session is None:
        return None

    info = {
        "title": None, "artist": None, "art": None,
        "position": None, "duration": None, "playing": False,
    }

    global _art_cache_key, _art_cache_img
    try:
        props = await session.try_get_media_properties_async()
        info["title"] = props.title or None
        info["artist"] = props.artist or None

        key = (info["title"], info["artist"])
        if key == _art_cache_key and _art_cache_img is not None:
            info["art"] = _art_cache_img
        else:
            thumb_ref = props.thumbnail
            if thumb_ref is not None:
                stream = await thumb_ref.open_read_async()
                size = stream.size
                if size:
                    buf = Buffer(size)
                    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
                    data = bytes(bytearray(buf))
                    art = Image.open(io.BytesIO(data)).convert("RGB")
                    info["art"] = art
                    _art_cache_key, _art_cache_img = key, art
    except Exception:  # noqa: BLE001
        pass

    try:
        timeline = session.get_timeline_properties()
        if timeline.position is not None:
            info["position"] = timeline.position.total_seconds()
        if timeline.end_time is not None and timeline.start_time is not None:
            info["duration"] = (timeline.end_time - timeline.start_time).total_seconds()
        # Windows stamps *when* that position value was actually valid --
        # use that as our interpolation anchor instead of whenever our
        # own poll happened to finish. Position only changes when the
        # source app pushes a fresh timeline update (not on every poll),
        # so anchoring on our own poll time made the estimate drift
        # forward between real updates and then jump back once it
        # drifted too far -- anchoring on the real timestamp fixes both
        # the drift and the systematic lag from the WinRT round trip.
        if timeline.last_updated_time is not None:
            info["position_as_of"] = _winrt_datetime_to_epoch(timeline.last_updated_time)
    except Exception:  # noqa: BLE001
        pass

    try:
        playback = session.get_playback_info()
        info["playing"] = playback.playback_status == PlaybackStatus.PLAYING
    except Exception:  # noqa: BLE001
        pass

    return info


# The WinRT call above is a real round trip to a system broker process
# and can easily take a second or more -- calling it inline from the
# render loop was the actual cause of the sluggish refresh / progress
# jumping by more than a second at a time (the loop always slept a
# further 1s *on top* of however long that call took). It's polled from
# a dedicated background thread instead, with the render loop reading
# whatever the latest snapshot is and locally extrapolating the
# playback position between polls so it still ticks smoothly once a
# second even if the underlying poll itself is slower than that.

_media_lock = threading.Lock()
_media_latest = None  # dict with an extra "_polled_at" timestamp
_media_thread_started = False


def _media_poll_worker(poll_interval=1.0):
    global _media_latest
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            result = loop.run_until_complete(_get_media_info_async())
        except Exception:  # noqa: BLE001
            result = None
        if result is not None:
            result["_polled_at"] = time.time()
        with _media_lock:
            _media_latest = result
        time.sleep(poll_interval)


def start_media_polling():
    global _media_thread_started
    if _media_thread_started or not _MEDIA_OK:
        return
    _media_thread_started = True
    threading.Thread(target=_media_poll_worker, daemon=True).start()


# Our own free-running clock for the progress bar: it ticks forward by
# real elapsed time every render frame regardless of how often (or how
# slowly) the background poll above actually completes -- like a
# stopwatch we started at the last known position. Whenever a fresh
# poll disagrees with where that stopwatch predicted we'd be by more
# than SYNC_TOLERANCE (a pause, seek, skip, or a real timeline update),
# we snap straight to the real value instead of drifting toward it.
SYNC_TOLERANCE = 1.2  # seconds of disagreement before we snap instead of drift

_progress_sync = {"track_key": None, "pos": None, "synced_at": None, "duration": None}


def get_media_info():
    """Latest media snapshot, non-blocking. `position` is our own
    ticking estimate of "right now", resynced against the real poll
    whenever the two disagree -- see SYNC_TOLERANCE above."""
    with _media_lock:
        snap = _media_latest
    if not snap:
        return None
    media = dict(snap)

    raw_pos = media.get("position")
    if raw_pos is None:
        return media

    now = time.time()
    # Anchor on Windows' own "as of" timestamp for this position value
    # when we have it -- it only changes when the source app actually
    # pushes a new timeline update, not on every poll -- and fall back
    # to our own poll time if it's missing for some reason.
    polled_at = media.get("position_as_of") or media.get("_polled_at", now)
    track_key = (media.get("title"), media.get("artist"))

    playing = media.get("playing")

    global _progress_sync
    if _progress_sync["track_key"] != track_key:
        # New track (or the very first poll) -- nothing to compare
        # against yet, so just adopt the real value outright.
        _progress_sync = {"track_key": track_key, "pos": raw_pos, "synced_at": polled_at,
                           "duration": media.get("duration")}
    elif not playing:
        # Paused: there's no extrapolation to protect, so just always
        # trust the real value directly rather than only on a big
        # mismatch -- this is what makes the frozen position land on the
        # actual moment playback stopped instead of whatever stale point
        # we last resynced from.
        _progress_sync["pos"] = raw_pos
        _progress_sync["synced_at"] = polled_at
        _progress_sync["duration"] = media.get("duration")
    else:
        predicted = _progress_sync["pos"] + (polled_at - _progress_sync["synced_at"])
        if abs(predicted - raw_pos) > SYNC_TOLERANCE:
            _progress_sync["pos"] = raw_pos
            _progress_sync["synced_at"] = polled_at
        _progress_sync["duration"] = media.get("duration")

    # Only run the stopwatch forward while something is actually playing.
    # It used to always add (now - synced_at), even while paused -- so a
    # real pause looked like the position kept climbing on its own (our
    # local clock ticking with nothing to anchor it) until the next poll
    # noticed the mismatch and snapped it back, which read as "jumping".
    # While paused, just hold at the exact synced position instead.
    if playing:
        display_pos = _progress_sync["pos"] + (now - _progress_sync["synced_at"])
    else:
        display_pos = _progress_sync["pos"]

    duration = _progress_sync["duration"]
    if duration:
        display_pos = min(display_pos, duration)
    media["position"] = max(0.0, display_pos)
    return media


# ---------------------------------------------------------------- drawing --
# A "cyberpunk panel" look: near-black background with a faint hex grid
# and circuit traces, full-circle neon gauges (dim track + a glowing
# filled arc + a lit needle), and a glowing frame around the album art.
# The static parts (background texture, dim gauge tracks, tick marks,
# labels) are baked into one image once at startup by
# build_static_background(); render_frame() only redraws the parts that
# actually change every second on top of a copy of it.

BG_TOP = (7, 6, 13)
BG_BOTTOM = (13, 8, 20)
PANEL_BORDER = (120, 40, 170)

ACCENT_CPU = (0, 220, 255)     # electric cyan
ACCENT_GPU = (235, 45, 225)    # neon magenta
ACCENT_MID = (225, 60, 235)    # violet-magenta, ties the middle column together

TICKS = (0, 25, 50, 75, 100)
# A near-full-circle sweep with a small gap at the bottom, in PIL's
# angle convention (0 = 3 o'clock, clockwise).
GAUGE_START = 125
GAUGE_END = 425

# The 4 big gauges' stat/title/range/format are no longer hardcoded to
# CPU/GPU load -- each of the 4 slots (top-left, bottom-left, top-right,
# bottom-right) independently picks any one of these (see app.py's
# Dashboard tab for the dropdowns, and run()'s `slots=` kwarg). Accent
# color is NOT part of this registry on purpose: it stays tied to which
# *column* a slot is in (left=cyan, right=magenta) regardless of which
# stat currently occupies it, so the two columns keep reading as
# "CPU-side" / "GPU-side" even after reassigning what's actually shown.
#
# Every gauge on the panel -- the 4 big ones AND the 4 smaller ones (the
# two flanking the album art, the two flanking the clock) -- picks from
# this same registry; there's nothing special about which stats can go
# in a "big" vs "small" slot. See SLOT_KINDS below for where each named
# slot actually sits and how big it is.
CPU_FREQ_GAUGE_MAX_GHZ = 6.0  # a fixed ceiling, same idea as NETWORK_GAUGE_MAX_MB_S --
# tune it if your CPU's boost clock is way above (or well below) this.
STAT_DEFS = {
    "cpu_load": {"label": "CPU Load", "title": "CPU LOAD", "min": 0, "max": 100,
                 "fmt": lambda v: f"{v:.0f}%"},
    "gpu_load": {"label": "GPU Load", "title": "GPU LOAD", "min": 0, "max": 100,
                 "fmt": lambda v: f"{v:.0f}%"},
    "ram": {"label": "RAM Usage", "title": "RAM", "min": 0, "max": 100,
            "fmt": lambda v: f"{v:.0f}%"},
    "network": {"label": "Network", "title": "NETWORK", "min": 0, "max": NETWORK_GAUGE_MAX_MB_S,
                "fmt": lambda v: f"{v:.1f}M/s"},
    "gpu_temp": {"label": "GPU Temp", "title": "GPU TEMP", "min": 0, "max": 100,
                 "fmt": lambda v: f"{v:.0f}°"},
    "cpu_freq": {"label": "CPU Freq", "title": "CPU FREQ", "min": 0, "max": CPU_FREQ_GAUGE_MAX_GHZ,
                 "fmt": lambda v: f"{v:.1f}G"},
    "disk_usage": {"label": "Disk Usage", "title": "DISK", "min": 0, "max": 100,
                   "fmt": lambda v: f"{v:.0f}%"},
    "vram_usage": {"label": "VRAM Usage", "title": "VRAM", "min": 0, "max": 100,
                   "fmt": lambda v: f"{v:.0f}%"},
    "swap": {"label": "Swap Usage", "title": "SWAP", "min": 0, "max": 100,
             "fmt": lambda v: f"{v:.0f}%"},
    "disk_io": {"label": "Disk Activity", "title": "DISK I/O", "min": 0, "max": DISK_IO_GAUGE_MAX_MB_S,
                "fmt": lambda v: f"{v:.0f}M/s"},
    "gpu_power": {"label": "GPU Power", "title": "GPU PWR", "min": 0, "max": GPU_POWER_MAX_W,
                  "fmt": lambda v: f"{v:.0f}W"},
    "process_count": {"label": "Processes", "title": "PROCESSES", "min": 0, "max": PROCESS_COUNT_MAX,
                       "fmt": lambda v: f"{v:.0f}"},
    "cpu_load_peak": {"label": "CPU Load (Peak Core)", "title": "CPU PEAK", "min": 0, "max": 100,
                       "fmt": lambda v: f"{v:.0f}%"},
    "battery": {"label": "Battery", "title": "BATTERY", "min": 0, "max": 100,
                "fmt": lambda v: f"{v:.0f}%"},
    # The PC's master output volume -- the one stat here that isn't
    # about load or heat, and the only one a person changes on purpose
    # rather than watches. Reads 0 while muted (see
    # get_volume_percent()), and "--" on a machine that can't report it
    # at all, same as any other unavailable sensor.
    "volume": {"label": "Volume", "title": "VOLUME", "min": 0, "max": 100,
               "fmt": lambda v: f"{v:.0f}%"},
}
# 15 stats, 8 slots -- deliberately more of the former than the latter
# (see STAT_DEFS' own comment above about how it's registered) so
# picking a layout is a real choice, not just "which of exactly 8
# things goes in the one slot it fits."
DEFAULT_SLOTS = {
    "top_left": "cpu_load", "bottom_left": "ram",
    "top_right": "gpu_load", "bottom_right": "network",
    "left_secondary": "cpu_freq", "right_secondary": "gpu_temp",
    "left_mini": "disk_usage", "right_mini": "vram_usage",
}
# Which of the 8 slots are "big" (the 4 main gauges), "secondary" (the
# pair flanking the album art), or "mini" (the pair flanking the clock)
# -- drives gauge size and value-font choice generically in
# build_static_background()/render_frame() instead of hardcoding each
# slot by name.
SLOT_KINDS = {
    "top_left": "big", "bottom_left": "big", "top_right": "big", "bottom_right": "big",
    "left_secondary": "secondary", "right_secondary": "secondary",
    "left_mini": "mini", "right_mini": "mini",
}

def gauge_layout(cx, cy, radius):
    ring_w = max(6, radius * 0.11)
    pad = int(ring_w * 3 + 24)
    size = int(radius * 2 + pad * 2)
    return {"cx": cx, "cy": cy, "radius": radius, "ring_w": ring_w, "pad": pad, "size": size}


def _gauge_box(g):
    """Top-left corner to paste a gauge tile at so it lands centered on
    (cx, cy) -- both the static and dynamic tiles for a given gauge are
    the same size, so this box is shared by both."""
    half = g["size"] / 2
    return (int(g["cx"] - half), int(g["cy"] - half))


# ---------------------------------------------------------------- Phase 4 --
# elements: slots -> elements (ROADMAP.md Phase 4).
#
# DEFAULT_SLOTS/SLOT_KINDS above are still what app.py's Tkinter Dashboard
# tab reads/writes (its 8 named dropdowns) and are kept exactly as they
# were -- nothing here changes that UI or its config shape. What changes
# is the *renderer*: build_static_background()/render_frame() no longer
# compute 8 fixed gauge positions from a formula keyed by slot name; they
# walk an arbitrary list of "elements" instead, each with its own
# position/size/color/opacity (and a stored-but-not-yet-rendered rotation,
# see draw_gauge_static()'s call site below) rather than one of exactly 3
# hardcoded "kinds". This is what actually unblocks a future drag/resize
# design canvas (Phase 5) -- there's now a real per-gauge x/y/radius to
# drag, instead of a name that only ever meant one of 8 fixed spots.
#
# slots_to_elements() is the bridge: it runs the exact same geometry
# formula build_static_background() used to compute inline (now pulled
# out into _slot_geometry() below) at a fixed REFERENCE_WIDTH/HEIGHT
# matching this panel's real resolution, and expresses each gauge's
# resulting center/radius as a *fraction* of that reference size instead
# of a formula. That's what "the 8 slots map onto 8 default elements"
# (ROADMAP.md's migration bullet) means in code: DEFAULT_ELEMENTS below
# IS that migration, computed once at import time rather than needing a
# separate migration script or a config schema version bump -- an old
# app_config.json with only "slots" (or nothing at all) still renders
# pixel-identically, because theme_kwargs.py falls back to
# slots_to_elements(cfg["dashboard"].get("slots")) whenever
# cfg["dashboard"] has no "elements" key yet. Only a future design canvas
# actually writing custom elements ever changes what's stored.
REFERENCE_WIDTH = 960
REFERENCE_HEIGHT = 480

# A gauge element is rendered with full tick labels + its title tucked
# inside the ring (the old "big" look) once its baked radius is at least
# this fraction of min(width, height); smaller than that gets the compact
# "title above the ring, no tick labels" look (the old "secondary"/"mini"
# look, which is really just "small" -- see the continuous label-gap
# formula at its call site instead of a second threshold). 0.13 sits
# between DEFAULT_ELEMENTS' big gauges (~0.17) and its secondary/mini
# ones (~0.075-0.089) at REFERENCE_WIDTH/HEIGHT, so the default layout's
# look doesn't change at all -- this just replaces a fixed "kind" string
# with something derived from an element's actual size, which is the
# whole point of elements having real geometry instead of a slot name.
BIG_GAUGE_RADIUS_FRACTION = 0.13


def _slot_geometry(width, height):
    """The pure geometry half of what build_static_background() used to
    compute inline: where each of the 8 named slots' gauges sit and how
    big they are, at a given panel size -- no drawing, no stat/accent
    lookup. Returns {slot_key: gauge_layout(...)}. Used today only by
    slots_to_elements() (to build DEFAULT_ELEMENTS and to migrate an old
    "slots"-only config), not by the render path itself any more."""
    margin = int(width * 0.015)
    col_w = int(width * 0.235)
    gauge_area_top = int(height * 0.07)
    gauge_area_bottom = int(height * 0.93)
    gap = int(height * 0.03)
    row_h = (gauge_area_bottom - gauge_area_top - gap) / 2
    radius = min(col_w * 0.5, row_h * 0.5) * 0.82

    def col_gauges(col_cx):
        top = gauge_layout(col_cx, gauge_area_top + row_h / 2, radius)
        bot = gauge_layout(col_cx, gauge_area_top + row_h + gap + row_h / 2, radius)
        return top, bot

    cpu_cx = margin + col_w / 2 + int(width * 0.015)
    gpu_cx = width - margin - col_w / 2 - int(width * 0.015)

    top_left, bottom_left = col_gauges(cpu_cx)
    top_right, bottom_right = col_gauges(gpu_cx)

    mid_x0 = margin + col_w + int(width * 0.03)
    mid_x1 = width - margin - col_w - int(width * 0.03)
    mid_cx = (mid_x0 + mid_x1) / 2

    art_size = int(min((mid_x1 - mid_x0) * 0.62, height * 0.42))
    art_right = mid_cx + art_size / 2
    art_left = mid_cx - art_size / 2
    lean = 0.68

    gauge_visible_left = top_right["cx"] - top_right["radius"] - top_right["ring_w"] / 2 - 25
    gauge_visible_right = top_left["cx"] + top_left["radius"] + top_left["ring_w"] / 2 + 25
    secondary_radius = top_right["radius"] * 0.52
    secondary_positions = {
        "right_secondary": gauge_layout(
            art_right + (gauge_visible_left - art_right) * lean,
            (top_right["cy"] + bottom_right["cy"]) / 2, secondary_radius),
        "left_secondary": gauge_layout(
            art_left + (gauge_visible_right - art_left) * lean,
            (top_left["cy"] + bottom_left["cy"]) / 2, secondary_radius),
    }

    clock_cy = int(height * 0.885)
    mini_radius = secondary_radius * 0.85
    mini_offset = (mid_x1 - mid_x0) * 0.26
    mini_positions = {
        "left_mini": gauge_layout(mid_cx - mini_offset, clock_cy, mini_radius),
        "right_mini": gauge_layout(mid_cx + mini_offset, clock_cy, mini_radius),
    }

    return {
        "top_left": top_left, "bottom_left": bottom_left,
        "top_right": top_right, "bottom_right": bottom_right,
        **secondary_positions, **mini_positions,
    }


# Same margin/mid-column arithmetic build_static_background() uses for
# its layout, just computed once here (at REFERENCE_WIDTH/HEIGHT) so a
# fresh clock/now-playing element starts out in exactly the spot each
# has always occupied -- the old fixed clock position and the old fixed
# Spotify column, respectively. Used by slots_to_elements() below (so
# every freshly-derived layout gets both) and by config_store's
# one-time elements migration (so an *existing* saved layout that
# predates these two element types gets them added at the same spot).
_DEFAULT_MARGIN = int(REFERENCE_WIDTH * 0.015)
_DEFAULT_COL_W = int(REFERENCE_WIDTH * 0.235)
_DEFAULT_MID_X0 = _DEFAULT_MARGIN + _DEFAULT_COL_W + int(REFERENCE_WIDTH * 0.03)
_DEFAULT_MID_X1 = REFERENCE_WIDTH - _DEFAULT_MARGIN - _DEFAULT_COL_W - int(REFERENCE_WIDTH * 0.03)
_DEFAULT_CLOCK_X = (_DEFAULT_MID_X0 + _DEFAULT_MID_X1) / 2 / REFERENCE_WIDTH
_DEFAULT_CLOCK_Y = int(REFERENCE_HEIGHT * 0.885) / REFERENCE_HEIGHT
_DEFAULT_MEDIA_X = (_DEFAULT_MID_X0 + _DEFAULT_MID_X1) / 2 / REFERENCE_WIDTH
_DEFAULT_MEDIA_Y = 0.42
_DEFAULT_MEDIA_W = 0.32
_DEFAULT_MEDIA_H = 0.52
_DEFAULT_WEATHER_X = (_DEFAULT_MID_X0 + _DEFAULT_MID_X1) / 2 / REFERENCE_WIDTH
_DEFAULT_WEATHER_Y = 0.42
_DEFAULT_WEATHER_W = 0.28
_DEFAULT_WEATHER_H = 0.46


def default_clock_element():
    """A fresh clock element dict at the old fixed clock spot -- a
    factory (not a shared constant) so every caller gets its own dict
    to mutate freely. Used by slots_to_elements() (so any layout
    derived from `slots` includes a clock) and by config_store's
    one-time migration for a pre-existing saved `elements` list that
    has none yet."""
    return {
        "id": "clock", "type": "clock",
        "x": _DEFAULT_CLOCK_X, "y": _DEFAULT_CLOCK_Y,
        "font_size": 24 / REFERENCE_HEIGHT,  # matches Fonts.time = load_font(24)
        "color": None, "opacity": 1.0, "show_seconds": True,
        # Clock customization (face/hour_format/show_date/analog_style/
        # radius/image_path/width/height) -- see CLOCK_FACES' comment
        # above _draw_clock_element(). "digital" with these values
        # reproduces exactly the old (and only) look, so an existing
        # saved clock element that predates these keys still renders
        # identically -- _draw_clock_element()/the property panel both
        # fall back to the same defaults via .get() either way.
        "face": "digital", "hour_format": "24h", "show_date": False,
        "analog_style": "classic", "radius": 0.12,
        "image_path": None, "width": 0.22, "height": 0.22,
        "z": 100,
    }


def default_media_element():
    """A fresh now-playing element dict at the old fixed Spotify-column
    spot -- see default_clock_element()'s docstring for why this is a
    factory function rather than a shared dict/constant."""
    return {
        "id": "now_playing", "type": "media",
        "x": _DEFAULT_MEDIA_X, "y": _DEFAULT_MEDIA_Y,
        "width": _DEFAULT_MEDIA_W, "height": _DEFAULT_MEDIA_H,
        "opacity": 1.0, "show_art": True, "show_name": True, "show_time": True,
        "z": 101,
    }


def default_weather_element():
    """A fresh weather element dict at the old fixed middle-column spot
    -- see default_clock_element()'s docstring for why this is a
    factory function rather than a shared dict/constant. Unlike the
    clock/media elements, NOT appended by slots_to_elements() -- the
    old middle_content default was "none" (opt-in), and this preserves
    that: weather only ever appears on the canvas once someone clicks
    "+ Add weather", same as any other element type. `location`/`units`
    are per-element (weather.py's actual lookup is still one shared
    background poll, same as before -- see run()'s
    apply_weather_from_elements()), empty/celsius by default so a
    freshly-added element shows the "set a location" placeholder
    (_draw_weather_element()) until someone fills one in.

    `show_icon`/`show_temp`/`show_description`/`show_details`/
    `show_location` (each default True) let each piece be switched off
    independently -- same "pick which pieces you actually want" deal
    as `show_art`/`show_name`/`show_time` on default_media_element(),
    so a weather element can be shrunk down to just an icon, just the
    temperature, or any other combination (e.g. for sitting next to a
    compact now-playing element without the two together eating the
    whole panel)."""
    return {
        "id": "weather", "type": "weather",
        "x": _DEFAULT_WEATHER_X, "y": _DEFAULT_WEATHER_Y,
        "width": _DEFAULT_WEATHER_W, "height": _DEFAULT_WEATHER_H,
        "location": "", "units": "celsius", "opacity": 1.0,
        "show_icon": True, "show_temp": True, "show_description": True,
        "show_details": True, "show_location": True,
        "z": 102,
    }


def slots_to_elements(slots=None):
    """Converts the old slot-based picks (`slots`, same shape as
    DEFAULT_SLOTS -- any of its 8 keys mapped to a STAT_DEFS key, missing
    entries falling back to DEFAULT_SLOTS) into the new element-list
    format, using _slot_geometry()'s positions at REFERENCE_WIDTH/HEIGHT
    expressed as fractions so they still scale correctly to whatever
    size the connected panel actually reports. Element `id`s are kept as
    the original slot names purely so a saved layout stays readable and
    a repeat migration (nothing has switched to "elements" yet) is
    idempotent -- nothing currently depends on the id being a slot name.

    Also appends a default clock and now-playing element (see
    default_clock_element()/default_media_element() above) -- this is
    the path every fresh/slots-only config takes (theme_kwargs.
    resolve_dashboard_elements() calls this whenever `dashboard.elements`
    hasn't been saved yet), so both widgets are there from the start
    with no separate migration needed. A saved `elements` list is the
    only thing that CAN go stale here, which is what config_store's
    one-time migration handles."""
    slots = dict(DEFAULT_SLOTS, **(slots or {}))
    positions = _slot_geometry(REFERENCE_WIDTH, REFERENCE_HEIGHT)
    base = min(REFERENCE_WIDTH, REFERENCE_HEIGHT)
    elements = []
    for z, (slot_key, g) in enumerate(positions.items()):
        elements.append({
            "id": slot_key,
            "type": "gauge",
            "stat": slots[slot_key],
            "x": g["cx"] / REFERENCE_WIDTH,
            "y": g["cy"] / REFERENCE_HEIGHT,
            "radius": g["radius"] / base,
            "rotation": 0.0,   # stored/migrated, not yet rendered -- see
                               # build_static_background()'s call site
            "color": None,     # None -> derive from x-position, see _element_accent()
            "opacity": 1.0,
            "z": z,
        })
    elements.append(default_clock_element())
    elements.append(default_media_element())
    return elements


DEFAULT_ELEMENTS = slots_to_elements()


# The app's own built-in presets (ROADMAP.md Phase 6 addendum) -- pure
# code, never copied into anyone's app_config.json. config_store.
# resolve_dashboard_presets() merges this dict with whatever's actually
# saved in a given config on every read, so the picker always reflects
# whatever this running app's code currently defines, and an update
# that improves one (or adds a new one, the way the last 4 below were
# added after the first 6 shipped) reaches every install immediately.
# Saving over a built-in's own name customizes it (the saved copy wins
# the merge); deleting a pure built-in records its name in
# `dashboard.dismissed_builtin_presets` instead, since there's no copy
# in `presets` to actually remove -- see resolve_dashboard_presets()'s
# own docstring for the full mechanism.
#
# Each is `{"elements": [...], "background": {...}}` -- the shape
# save_dashboard_preset() stores for every preset (old installs' bare-
# list presets are migrated to this same shape by config_store.py too
# -- see migrate_dashboard_preset_shape()), so a built-in preset looks
# and loads exactly like a hand-saved one, background included.
#
# Colors were picked and the whole layout of each preset iterated by
# actually rendering it through render_preset_thumbnail() and looking
# at the result (the same function the picker itself uses) rather than
# guessing coordinates blind -- the first pass clipped gauge titles at
# the edges and had a couple of caption/title text collisions, both
# fixed by nudging positions until a real render came out clean.
#
#   Neon Horizon      -- futuristic: a starfield HUD, cyan/magenta
#                        gradient gauges, a live network graph and a
#                        gradient-filled VRAM bar.
#   Bubblegum         -- cute: soft pink/lavender/mint pastel gauges
#                        and rounded knob-bars on a purple gradient,
#                        a minimal analog clock, a friendly caption.
#   Panic Mode        -- funny: over-dramatic captions ("BRAIN USAGE",
#                        "SNACK STORAGE", "WILL TO LIVE") relabeling
#                        perfectly ordinary stats on a red grid.
#   Mission Control   -- informative: as many stats as reasonably fit
#                        at once -- 8 gauges, a CPU history graph, a
#                        GPU power bar, date-showing clock -- on a
#                        clean grayscale grid.
#   Midnight Minimal  -- a big minimal analog clock and almost nothing
#                        else, for anyone who wants the panel to be
#                        calm rather than busy.
#   Arcade RGB        -- a rainbow gradient shared across a bar-style
#                        graph, a gradient bar and two gradient gauges,
#                        neon analog clock, starfield background.
#
# The 4 below were added later, each built to showcase one of the
# bundled "photo" backgrounds (BUNDLED_BACKGROUND_IMAGES) rather than
# a procedural one, and deliberately sparser than the 6 above -- a
# photo background is already visually busy, so piling on gauges the
# way Mission Control does would just fight it for attention. Two of
# them (Northern Lights, City Nights) use a stat-bound text element
# (`"stat"` + `"template"`, see _resolve_text_content()) instead of
# only gauges/bars, doubling as an example layout for that too.
#
#   Northern Lights   -- the aurora background, cyan/purple gradient
#                        gauges, a live "RAM NN%" text reading.
#   Deep Space        -- the nebula background, magenta/blue gradient
#                        gauges, a gradient disk-usage bar.
#   Outrun Drive      -- the synthwave background, kept sparse (two
#                        small gauges low in the corners, a live "NET"
#                        reading) so the sun/grid picture stays the
#                        star of the show.
#   City Nights       -- the bokeh background, just a big minimal
#                        analog clock and two live text readings.
#
# Circuit Bloom is different from all 10 above: it isn't an original
# design, it's a real user's own saved layout ("my preset") plus their
# own uploaded background photo, shipped as a built-in at their request
# so it ships with every install instead of staying local to their
# machine. See its own comment just above its entry below.
#
# Cherry Blossom and Petal Dream, right after it, are a different kind
# of departure: the first two built-ins with zero gauges at all --
# every stat is a plain text row (label + value, `"stat"` +
# `"template"`, same mechanism Northern Lights/City Nights introduced)
# over a new pastel floral photo background, requested as a pair
# styled after a photo of a "girly" fan-controller readout. See their
# own shared comment just above their entries below for what carried
# over from that reference and what didn't.
BUILTIN_DASHBOARD_PRESETS = {
    "Neon Horizon": {
        "background": {
            "mode": "starfield",
            "scheme": "blue",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.15,
                "y": 0.32,
                "radius": 0.155,
                "rotation": 0.0,
                "color": (80, 220, 255),
                "color2": (150, 90, 255),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.85,
                "y": 0.32,
                "radius": 0.155,
                "rotation": 0.0,
                "color": (255, 90, 220),
                "color2": (150, 90, 255),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "g_ram",
                "type": "gauge",
                "stat": "ram",
                "x": 0.15,
                "y": 0.76,
                "radius": 0.12,
                "rotation": 0.0,
                "color": (80, 220, 255),
                "color2": (150, 90, 255),
                "opacity": 1.0,
                "z": 3,
            },
            {
                "id": "g_temp",
                "type": "gauge",
                "stat": "gpu_temp",
                "x": 0.85,
                "y": 0.76,
                "radius": 0.12,
                "rotation": 0.0,
                "color": (255, 90, 220),
                "color2": (150, 90, 255),
                "opacity": 1.0,
                "z": 4,
            },
            {
                "id": "g_net",
                "type": "graph",
                "stat": "network",
                "x": 0.5,
                "y": 0.45,
                "width": 0.3,
                "height": 0.2,
                "color": (120, 220, 255),
                "style": "line",
                "history_seconds": 20,
                "opacity": 1.0,
                "z": 5,
                "gradient": True,
                "gradient_colors": [(80, 220, 255), (255, 90, 220)],
                "gradient_direction": "horizontal",
            },
            {
                "id": "b_vram",
                "type": "bar",
                "stat": "vram_usage",
                "x": 0.5,
                "y": 0.685,
                "width": 0.3,
                "height": 0.05,
                "color": (0, 220, 255),
                "orientation": "horizontal",
                "show_knob": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 6,
                "gradient_colors": [(80, 220, 255), (150, 90, 255), (255, 90, 220)],
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.86,
                "font_size": 0.055,
                "color": (160, 220, 255),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "digital",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "classic",
                "radius": 0.12,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 100,
            },
        ],
    },
    "Bubblegum": {
        "background": {
            "mode": "solid",
            "scheme": "purple",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.17,
                "y": 0.34,
                "radius": 0.145,
                "rotation": 0.0,
                "color": (255, 158, 203),
                "color2": (255, 214, 240),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_ram",
                "type": "gauge",
                "stat": "ram",
                "x": 0.83,
                "y": 0.34,
                "radius": 0.145,
                "rotation": 0.0,
                "color": (179, 157, 255),
                "color2": (214, 196, 255),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.5,
                "y": 0.76,
                "radius": 0.115,
                "rotation": 0.0,
                "color": (157, 255, 207),
                "color2": (214, 255, 232),
                "opacity": 1.0,
                "z": 3,
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.34,
                "font_size": 0,
                "color": (255, 214, 240),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "analog",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "minimal",
                "radius": 0.135,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 4,
            },
            {
                "id": "b_batt",
                "type": "bar",
                "stat": "battery",
                "x": 0.17,
                "y": 0.7,
                "width": 0.26,
                "height": 0.045,
                "color": (255, 158, 203),
                "orientation": "horizontal",
                "show_knob": True,
                "gradient": False,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 5,
            },
            {
                "id": "b_disk",
                "type": "bar",
                "stat": "disk_usage",
                "x": 0.83,
                "y": 0.7,
                "width": 0.26,
                "height": 0.045,
                "color": (179, 157, 255),
                "orientation": "horizontal",
                "show_knob": True,
                "gradient": False,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 6,
            },
        ],
    },
    "Panic Mode": {
        "background": {
            "mode": "grid",
            "scheme": "crimson",
            "image_path": None,
        },
        "elements": [
            {
                "id": "cap_cpu",
                "type": "text",
                "text": "BRAIN USAGE",
                "x": 0.17,
                "y": 0.145,
                "font_size": 0.026,
                "color": (255, 200, 160),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.17,
                "y": 0.33,
                "radius": 0.145,
                "rotation": 0.0,
                "color": (255, 140, 60),
                "color2": None,
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "cap_gpu",
                "type": "text",
                "text": "SWEAT LEVEL",
                "x": 0.83,
                "y": 0.145,
                "font_size": 0.026,
                "color": (255, 200, 160),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 3,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_temp",
                "x": 0.83,
                "y": 0.33,
                "radius": 0.145,
                "rotation": 0.0,
                "color": (255, 90, 90),
                "color2": None,
                "opacity": 1.0,
                "z": 4,
            },
            {
                "id": "cap_ram",
                "type": "text",
                "text": "SNACK STORAGE",
                "x": 0.5,
                "y": 0.46,
                "font_size": 0.026,
                "color": (255, 200, 160),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 5,
            },
            {
                "id": "b_ram",
                "type": "bar",
                "stat": "ram",
                "x": 0.5,
                "y": 0.615,
                "width": 0.34,
                "height": 0.06,
                "color": (255, 140, 60),
                "orientation": "horizontal",
                "show_knob": True,
                "gradient": False,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 6,
            },
            {
                "id": "cap_net",
                "type": "text",
                "text": "PANIC SIGNAL",
                "x": 0.17,
                "y": 0.655,
                "font_size": 0.024,
                "color": (255, 200, 160),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 7,
            },
            {
                "id": "g_net",
                "type": "gauge",
                "stat": "network",
                "x": 0.17,
                "y": 0.9,
                "radius": 0.088,
                "rotation": 0.0,
                "color": (255, 90, 90),
                "color2": None,
                "opacity": 1.0,
                "z": 8,
            },
            {
                "id": "cap_batt",
                "type": "text",
                "text": "WILL TO LIVE",
                "x": 0.83,
                "y": 0.655,
                "font_size": 0.024,
                "color": (255, 200, 160),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 9,
            },
            {
                "id": "g_batt",
                "type": "gauge",
                "stat": "battery",
                "x": 0.83,
                "y": 0.9,
                "radius": 0.088,
                "rotation": 0.0,
                "color": (255, 140, 60),
                "color2": None,
                "opacity": 1.0,
                "z": 10,
            },
        ],
    },
    "Mission Control": {
        "background": {
            "mode": "grid",
            "scheme": "mono",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.105,
                "y": 0.235,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (120, 200, 255),
                "color2": None,
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.895,
                "y": 0.235,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (255, 150, 120),
                "color2": None,
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "g_ram",
                "type": "gauge",
                "stat": "ram",
                "x": 0.105,
                "y": 0.46,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (120, 200, 255),
                "color2": None,
                "opacity": 1.0,
                "z": 3,
            },
            {
                "id": "g_vram",
                "type": "gauge",
                "stat": "vram_usage",
                "x": 0.895,
                "y": 0.46,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (255, 150, 120),
                "color2": None,
                "opacity": 1.0,
                "z": 4,
            },
            {
                "id": "g_disk",
                "type": "gauge",
                "stat": "disk_usage",
                "x": 0.105,
                "y": 0.685,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (180, 180, 190),
                "color2": None,
                "opacity": 1.0,
                "z": 5,
            },
            {
                "id": "g_swap",
                "type": "gauge",
                "stat": "swap",
                "x": 0.895,
                "y": 0.685,
                "radius": 0.082,
                "rotation": 0.0,
                "color": (180, 180, 190),
                "color2": None,
                "opacity": 1.0,
                "z": 6,
            },
            {
                "id": "g_hist",
                "type": "graph",
                "stat": "cpu_load",
                "x": 0.5,
                "y": 0.33,
                "width": 0.44,
                "height": 0.22,
                "color": (120, 200, 255),
                "style": "line",
                "history_seconds": 30,
                "opacity": 1.0,
                "z": 7,
            },
            {
                "id": "b_gpu_power",
                "type": "bar",
                "stat": "gpu_power",
                "x": 0.5,
                "y": 0.565,
                "width": 0.44,
                "height": 0.045,
                "color": (255, 150, 120),
                "orientation": "horizontal",
                "show_knob": False,
                "gradient": False,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 8,
            },
            {
                "id": "cap_freq",
                "type": "text",
                "text": "CPU FREQ",
                "x": 0.5,
                "y": 0.645,
                "font_size": 0.022,
                "color": (150, 155, 165),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 9,
            },
            {
                "id": "g_freq",
                "type": "gauge",
                "stat": "cpu_freq",
                "x": 0.5,
                "y": 0.75,
                "radius": 0.075,
                "rotation": 0.0,
                "color": (120, 200, 255),
                "color2": None,
                "opacity": 1.0,
                "z": 10,
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.105,
                "y": 0.9,
                "font_size": 0.03,
                "color": (200, 205, 215),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "classic",
                "radius": 0.12,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 11,
            },
            {
                "id": "cap_net2",
                "type": "text",
                "text": "NET",
                "x": 0.895,
                "y": 0.855,
                "font_size": 0.022,
                "color": (150, 155, 165),
                "align": "center",
                "bold": False,
                "opacity": 1.0,
                "z": 12,
            },
            {
                "id": "g_net2",
                "type": "gauge",
                "stat": "network",
                "x": 0.895,
                "y": 0.9,
                "radius": 0.06,
                "rotation": 0.0,
                "color": (180, 180, 190),
                "color2": None,
                "opacity": 1.0,
                "z": 13,
            },
        ],
    },
    "Midnight Minimal": {
        "background": {
            "mode": "solid",
            "scheme": "mono",
            "image_path": None,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.42,
                "font_size": 0,
                "color": (230, 230, 235),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "analog",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "minimal",
                "radius": 0.22,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 0,
            },
            {
                "id": "b_batt",
                "type": "bar",
                "stat": "battery",
                "x": 0.5,
                "y": 0.9,
                "width": 0.22,
                "height": 0.03,
                "color": (120, 125, 135),
                "orientation": "horizontal",
                "show_knob": False,
                "gradient": False,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 2,
            },
        ],
    },
    "Arcade RGB": {
        "background": {
            "mode": "starfield",
            "scheme": "crimson",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.17,
                "y": 0.34,
                "radius": 0.15,
                "rotation": 0.0,
                "color": (255, 60, 60),
                "color2": (255, 220, 60),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.83,
                "y": 0.34,
                "radius": 0.15,
                "rotation": 0.0,
                "color": (60, 180, 255),
                "color2": (200, 60, 255),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "g_net",
                "type": "graph",
                "stat": "network",
                "x": 0.5,
                "y": 0.47,
                "width": 0.3,
                "height": 0.19,
                "color": (60, 255, 120),
                "style": "bar",
                "history_seconds": 20,
                "opacity": 1.0,
                "z": 3,
                "gradient": True,
                "gradient_colors": [(255, 60, 60), (255, 220, 60), (60, 255, 120), (60, 180, 255), (200, 60, 255)],
                "gradient_direction": "horizontal",
            },
            {
                "id": "b_ram",
                "type": "bar",
                "stat": "ram",
                "x": 0.5,
                "y": 0.695,
                "width": 0.34,
                "height": 0.055,
                "color": (0, 220, 255),
                "orientation": "horizontal",
                "show_knob": True,
                "gradient": True,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 4,
                "gradient_colors": [(255, 60, 60), (255, 220, 60), (60, 255, 120), (60, 180, 255), (200, 60, 255)],
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.87,
                "font_size": 0.05,
                "color": (60, 255, 180),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "analog",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "neon",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 100,
            },
        ],
    },
    # The 4 below each showcase one of the bundled "photo" backgrounds
    # (BUNDLED_BACKGROUND_IMAGES) -- added after the original 6, which
    # all use a procedural background (starfield/grid/solid), so a
    # fresh install's picker also demonstrates the photo backgrounds
    # without anyone having to build a layout for one from scratch.
    # Deliberately sparser than the original 6: a photo background is
    # already visually busy on its own, so piling on gauges the way
    # Mission Control does would just fight it for attention. Two of
    # these (Northern Lights, City Nights) also use a stat-bound text
    # element (`"stat"` + `"template"`, see _resolve_text_content())
    # instead of only gauges/bars for a live reading, doubling as an
    # example layout for that.
    "Northern Lights": {
        "background": {
            "mode": "aurora",
            "scheme": "purple",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.17,
                "y": 0.42,
                "radius": 0.14,
                "rotation": 0.0,
                "color": (90, 230, 200),
                "color2": (140, 120, 255),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.83,
                "y": 0.42,
                "radius": 0.14,
                "rotation": 0.0,
                "color": (120, 200, 255),
                "color2": (170, 110, 255),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "t_ram",
                "type": "text",
                "stat": "ram",
                "template": "RAM {value}",
                "x": 0.5,
                "y": 0.63,
                "font_size": 0.04,
                "color": (210, 235, 255),
                "align": "center",
                "bold": False,
                "opacity": 0.95,
                "z": 3,
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.86,
                "font_size": 0.05,
                "color": (220, 240, 235),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 100,
            },
        ],
    },
    "Deep Space": {
        "background": {
            "mode": "nebula",
            "scheme": "purple",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_temp",
                "x": 0.17,
                "y": 0.42,
                "radius": 0.14,
                "rotation": 0.0,
                "color": (255, 120, 200),
                "color2": (140, 110, 255),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.83,
                "y": 0.42,
                "radius": 0.14,
                "rotation": 0.0,
                "color": (120, 160, 255),
                "color2": (200, 110, 255),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "b_disk",
                "type": "bar",
                "stat": "disk_usage",
                "x": 0.5,
                "y": 0.66,
                "width": 0.32,
                "height": 0.05,
                "color": (190, 130, 255),
                "orientation": "horizontal",
                "show_knob": True,
                "gradient": True,
                "gradient_direction": "horizontal",
                "opacity": 1.0,
                "z": 3,
                "gradient_colors": [(120, 160, 255), (190, 130, 255), (255, 120, 200)],
            },
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.87,
                "font_size": 0.05,
                "color": (230, 215, 250),
                "opacity": 1.0,
                "show_seconds": True,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 100,
            },
        ],
    },
    "Outrun Drive": {
        "background": {
            "mode": "synthwave",
            "scheme": "crimson",
            "image_path": None,
        },
        "elements": [
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.14,
                "y": 0.83,
                "radius": 0.11,
                "rotation": 0.0,
                "color": (255, 90, 170),
                "color2": (255, 210, 110),
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.86,
                "y": 0.83,
                "radius": 0.11,
                "rotation": 0.0,
                "color": (255, 90, 170),
                "color2": (255, 210, 110),
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "t_net",
                "type": "text",
                "stat": "network",
                "template": "NET {value}",
                "x": 0.5,
                "y": 0.83,
                "font_size": 0.035,
                "color": (255, 235, 210),
                "align": "center",
                "bold": False,
                "opacity": 0.95,
                "z": 3,
            },
        ],
    },
    "City Nights": {
        "background": {
            "mode": "bokeh",
            "scheme": "blue",
            "image_path": None,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.42,
                "font_size": 0.05,
                "color": (235, 235, 245),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "analog",
                "hour_format": "24h",
                "show_date": False,
                "analog_style": "minimal",
                "radius": 0.19,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "z": 0,
            },
            {
                "id": "t_cpu",
                "type": "text",
                "stat": "cpu_load",
                "template": "CPU {value}",
                "x": 0.28,
                "y": 0.88,
                "font_size": 0.032,
                "color": (190, 205, 225),
                "align": "center",
                "bold": False,
                "opacity": 0.9,
                "z": 2,
            },
            {
                "id": "t_gpu",
                "type": "text",
                "stat": "gpu_temp",
                "template": "GPU {value}",
                "x": 0.72,
                "y": 0.88,
                "font_size": 0.032,
                "color": (190, 205, 225),
                "align": "center",
                "bold": False,
                "opacity": 0.9,
                "z": 3,
            },
        ],
    },
    # "Circuit Bloom" -- shipped at the user's own request: it's their
    # personal "my preset" layout (an 8-gauge full-stats board they'd
    # built and saved themselves) plus the magenta/teal circuit-board
    # photo they'd set as their background at the time, bundled here as
    # a built-in so a fresh install starts with it as an option too.
    # Every gauge below is a verbatim copy of their saved layout (same
    # ids, stats, positions, radii, z-order); none of them set an
    # explicit "color", so they fall back to the standard left-half/
    # right-half cyan/magenta split via _element_accent(), same as in
    # their own config.
    "Circuit Bloom": {
        "background": {
            "mode": "circuit",
            "scheme": "blue",
            "image_path": None,
        },
        "elements": [
            {
                "id": "top_left",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.14635416666666667,
                "y": 0.2765625,
                "radius": 0.17040625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 0,
            },
            {
                "id": "bottom_left",
                "type": "gauge",
                "stat": "ram",
                "x": 0.14635416666666667,
                "y": 0.7213541666666666,
                "radius": 0.17040625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 1,
            },
            {
                "id": "top_right",
                "type": "gauge",
                "stat": "gpu_load",
                "x": 0.8536458333333333,
                "y": 0.2765625,
                "radius": 0.17040625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 2,
            },
            {
                "id": "bottom_right",
                "type": "gauge",
                "stat": "network",
                "x": 0.8536458333333333,
                "y": 0.7213541666666666,
                "radius": 0.17040625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 3,
            },
            {
                "id": "right_secondary",
                "type": "gauge",
                "stat": "gpu_temp",
                "x": 0.6951461114583334,
                "y": 0.49895833333333334,
                "radius": 0.08861125,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 4,
            },
            {
                "id": "left_secondary",
                "type": "gauge",
                "stat": "cpu_freq",
                "x": 0.3048538885416667,
                "y": 0.49895833333333334,
                "radius": 0.08861125,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 5,
            },
            {
                "id": "left_mini",
                "type": "gauge",
                "stat": "disk_usage",
                "x": 0.384625,
                "y": 0.8833333333333333,
                "radius": 0.0753195625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 6,
            },
            {
                "id": "right_mini",
                "type": "gauge",
                "stat": "vram_usage",
                "x": 0.615375,
                "y": 0.8833333333333333,
                "radius": 0.0753195625,
                "rotation": 0.0,
                "color": None,
                "opacity": 1.0,
                "z": 7,
            },
        ],
    },
    # "Cherry Blossom" and "Petal Dream" -- requested as a pair: "two
    # app presets, that uses text field stats ... something girly",
    # modeled after a photo the user sent of a pastel floral fan-
    # controller readout (a script-style title, soft floral corner
    # flourishes, and CPU/GPU stats laid out as two columns of plain
    # label+value text rows rather than gauges). Neither the reference
    # photo's specific artwork/character nor its exact field set is
    # reproduced here -- its own "CPU Temp"/"CPU Power"/"GPU Freq"
    # rows don't have matching STAT_DEFS entries in this app (there's
    # no CPU-side temp/power sensor reading or a GPU freq one wired up)
    # -- what's carried over is the *style*: an all-text stat readout
    # (deliberately zero gauges, unlike every other built-in above) in
    # a two-column layout, over a new pastel floral background
    # (BUNDLED_BACKGROUND_IMAGES -- see generate_backgrounds.py's
    # make_cherry_blossom()/make_lavender_bloom(), original generated
    # art, not a copy of anything). Both use every stat this app can
    # actually read for CPU/GPU/RAM/VRAM/disk/swap between them so the
    # two presets don't just re-skin the same field list.
    # ---------------------------------------------------------------
    # The three "flagship" presets, built after a round of feedback
    # that the existing ones read as hobby projects next to the
    # commercial LCD themes the user had in hand: every stat block
    # lives in a real card/panel drawn into the background art at the
    # same coordinates (see generate_backgrounds.py), the type is set
    # in the app's own bundled display faces rather than whatever
    # generic sans the OS has (FONT_FAMILIES), labels sit in solid
    # chips (`plate`), and the meters carry the theme's gradient
    # instead of a flat accent. None of them draws its own name on the
    # panel -- that was the other half of the same feedback.
    #
    #   Fusion Core    -- dark card dashboard, color sweep behind matte
    #                     panels, rings + gradient meters + history
    #                     graphs, modeled on modern all-in-one monitor
    #                     software.
    #   Neon Pulse     -- acid yellow / magenta / cyan, hard diagonals,
    #                     chip labels, heavy display type.
    #   Crimson Strike -- red / black / bone, comic halftone and
    #                     slashes, outlined readout panels.
    # ---------------------------------------------------------------
    "Fusion Core": {
        "background": {
            "mode": "fusion",
            "scheme": "blue",
            "image_path": None,
            "border": "none",
            "dim": 0,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.25,
                "y": 0.15,
                "font_size": 0.105,
                "color": (238, 242, 250),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "font": "poppins",
            },
            {
                "id": "h_cpu",
                "type": "text",
                "text": "CPU",
                "x": 0.552,
                "y": 0.12,
                "font_size": 0.05,
                "color": (238, 242, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "g_cpu",
                "type": "gauge",
                "stat": "cpu_load",
                "x": 0.608,
                "y": 0.3,
                "radius": 0.098,
                "rotation": 0.0,
                "color": (34, 211, 238),
                "color2": (99, 102, 241),
                "show_title": False,
                "opacity": 1.0,
            },
            {
                "id": "c_cpu",
                "type": "text",
                "text": "LOAD",
                "x": 0.608,
                "y": 0.428,
                "font_size": 0.03,
                "color": (148, 163, 184),
                "align": "center",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l0",
                "type": "text",
                "text": "CLOCK",
                "x": 0.665,
                "y": 0.15,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "cpu_v0",
                "type": "text",
                "stat": "cpu_freq",
                "template": "{value}",
                "x": 0.945,
                "y": 0.15,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b0",
                "type": "bar",
                "stat": "cpu_freq",
                "x": 0.805,
                "y": 0.192,
                "width": 0.28,
                "height": 0.022,
                "color": (34, 211, 238),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(34, 211, 238), (99, 102, 241)],
                "opacity": 1.0,
            },
            {
                "id": "cpu_l1",
                "type": "text",
                "text": "PEAK",
                "x": 0.665,
                "y": 0.25,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "cpu_v1",
                "type": "text",
                "stat": "cpu_load_peak",
                "template": "{value}",
                "x": 0.945,
                "y": 0.25,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b1",
                "type": "bar",
                "stat": "cpu_load_peak",
                "x": 0.805,
                "y": 0.292,
                "width": 0.28,
                "height": 0.022,
                "color": (34, 211, 238),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(34, 211, 238), (99, 102, 241)],
                "opacity": 1.0,
            },
            {
                "id": "cpu_l2",
                "type": "text",
                "text": "TASKS",
                "x": 0.665,
                "y": 0.35,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "cpu_v2",
                "type": "text",
                "stat": "process_count",
                "template": "{value}",
                "x": 0.945,
                "y": 0.35,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b2",
                "type": "bar",
                "stat": "process_count",
                "x": 0.805,
                "y": 0.392,
                "width": 0.28,
                "height": 0.022,
                "color": (34, 211, 238),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(34, 211, 238), (99, 102, 241)],
                "opacity": 1.0,
            },
            {
                "id": "gr_cpu",
                "type": "graph",
                "stat": "cpu_load",
                "x": 0.75,
                "y": 0.523,
                "width": 0.385,
                "height": 0.086,
                "color": (34, 211, 238),
                "style": "line",
                "history_seconds": 60,
                "show_title": False,
                "show_frame": False,
                "opacity": 1.0,
            },
            {
                "id": "h_gpu",
                "type": "text",
                "text": "GPU",
                "x": 0.052,
                "y": 0.395,
                "font_size": 0.05,
                "color": (238, 242, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "g_gpu",
                "type": "gauge",
                "stat": "gpu_temp",
                "x": 0.108,
                "y": 0.575,
                "radius": 0.098,
                "rotation": 0.0,
                "color": (236, 72, 153),
                "color2": (251, 146, 60),
                "show_title": False,
                "opacity": 1.0,
            },
            {
                "id": "c_gpu",
                "type": "text",
                "text": "TEMP",
                "x": 0.108,
                "y": 0.703,
                "font_size": 0.03,
                "color": (148, 163, 184),
                "align": "center",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.165,
                "y": 0.425,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "gpu_v0",
                "type": "text",
                "stat": "gpu_load",
                "template": "{value}",
                "x": 0.445,
                "y": 0.425,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b0",
                "type": "bar",
                "stat": "gpu_load",
                "x": 0.305,
                "y": 0.467,
                "width": 0.28,
                "height": 0.022,
                "color": (236, 72, 153),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(236, 72, 153), (251, 146, 60)],
                "opacity": 1.0,
            },
            {
                "id": "gpu_l1",
                "type": "text",
                "text": "POWER",
                "x": 0.165,
                "y": 0.525,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "gpu_v1",
                "type": "text",
                "stat": "gpu_power",
                "template": "{value}",
                "x": 0.445,
                "y": 0.525,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b1",
                "type": "bar",
                "stat": "gpu_power",
                "x": 0.305,
                "y": 0.567,
                "width": 0.28,
                "height": 0.022,
                "color": (236, 72, 153),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(236, 72, 153), (251, 146, 60)],
                "opacity": 1.0,
            },
            {
                "id": "gpu_l2",
                "type": "text",
                "text": "VRAM",
                "x": 0.165,
                "y": 0.625,
                "font_size": 0.036,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "gpu_v2",
                "type": "text",
                "stat": "vram_usage",
                "template": "{value}",
                "x": 0.445,
                "y": 0.625,
                "font_size": 0.042,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b2",
                "type": "bar",
                "stat": "vram_usage",
                "x": 0.305,
                "y": 0.667,
                "width": 0.28,
                "height": 0.022,
                "color": (236, 72, 153),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(236, 72, 153), (251, 146, 60)],
                "opacity": 1.0,
            },
            {
                "id": "gr_gpu",
                "type": "graph",
                "stat": "gpu_load",
                "x": 0.25,
                "y": 0.86,
                "width": 0.385,
                "height": 0.086,
                "color": (236, 72, 153),
                "style": "line",
                "history_seconds": 60,
                "show_title": False,
                "show_frame": False,
                "opacity": 1.0,
            },
            {
                "id": "m_l0",
                "type": "text",
                "text": "RAM",
                "x": 0.552,
                "y": 0.7,
                "font_size": 0.034,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "m_v0",
                "type": "text",
                "stat": "ram",
                "template": "{value}",
                "x": 0.74,
                "y": 0.7,
                "font_size": 0.04,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "m_b0",
                "type": "bar",
                "stat": "ram",
                "x": 0.646,
                "y": 0.74,
                "width": 0.188,
                "height": 0.02,
                "color": (168, 85, 247),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(168, 85, 247), (236, 72, 153)],
                "opacity": 1.0,
            },
            {
                "id": "m_l1",
                "type": "text",
                "text": "SWAP",
                "x": 0.775,
                "y": 0.7,
                "font_size": 0.034,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "m_v1",
                "type": "text",
                "stat": "swap",
                "template": "{value}",
                "x": 0.95,
                "y": 0.7,
                "font_size": 0.04,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "m_b1",
                "type": "bar",
                "stat": "swap",
                "x": 0.8625,
                "y": 0.74,
                "width": 0.175,
                "height": 0.02,
                "color": (168, 85, 247),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(168, 85, 247), (236, 72, 153)],
                "opacity": 1.0,
            },
            {
                "id": "m_l2",
                "type": "text",
                "text": "DISK",
                "x": 0.552,
                "y": 0.84,
                "font_size": 0.034,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "m_v2",
                "type": "text",
                "stat": "disk_usage",
                "template": "{value}",
                "x": 0.74,
                "y": 0.84,
                "font_size": 0.04,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "m_b2",
                "type": "bar",
                "stat": "disk_usage",
                "x": 0.646,
                "y": 0.88,
                "width": 0.188,
                "height": 0.02,
                "color": (168, 85, 247),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(168, 85, 247), (236, 72, 153)],
                "opacity": 1.0,
            },
            {
                "id": "m_l3",
                "type": "text",
                "text": "NET",
                "x": 0.775,
                "y": 0.84,
                "font_size": 0.034,
                "color": (148, 163, 184),
                "align": "left",
                "bold": False,
                "font": "poppins",
                "opacity": 1.0,
            },
            {
                "id": "m_v3",
                "type": "text",
                "stat": "network",
                "template": "{value}",
                "x": 0.95,
                "y": 0.84,
                "font_size": 0.04,
                "color": (241, 245, 249),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "m_b3",
                "type": "bar",
                "stat": "network",
                "x": 0.8625,
                "y": 0.88,
                "width": 0.175,
                "height": 0.02,
                "color": (168, 85, 247),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(168, 85, 247), (236, 72, 153)],
                "opacity": 1.0,
            },
        ],
    },
    "Neon Pulse": {
        "background": {
            "mode": "neon",
            "scheme": "crimson",
            "image_path": None,
            "border": "none",
            "dim": 0,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.145,
                "y": 0.115,
                "font_size": 0.072,
                "color": (18, 18, 22),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "font": "chakra",
            },
            {
                "id": "h_cpu",
                "type": "text",
                "text": "CPU",
                "x": 0.49,
                "y": 0.12,
                "font_size": 0.115,
                "color": (18, 18, 22),
                "align": "center",
                "bold": True,
                "font": "anton",
                "opacity": 1.0,
            },
            {
                "id": "h_gpu",
                "type": "text",
                "text": "GPU",
                "x": 0.82,
                "y": 0.12,
                "font_size": 0.115,
                "color": (18, 18, 22),
                "align": "center",
                "bold": True,
                "font": "anton",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.36,
                "y": 0.345,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (34, 226, 236),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v0",
                "type": "text",
                "stat": "cpu_load",
                "template": "{value}",
                "x": 0.62,
                "y": 0.345,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l1",
                "type": "text",
                "text": "CLOCK",
                "x": 0.36,
                "y": 0.5,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (34, 226, 236),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v1",
                "type": "text",
                "stat": "cpu_freq",
                "template": "{value}",
                "x": 0.62,
                "y": 0.5,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l2",
                "type": "text",
                "text": "PEAK",
                "x": 0.36,
                "y": 0.655,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (34, 226, 236),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v2",
                "type": "text",
                "stat": "cpu_load_peak",
                "template": "{value}",
                "x": 0.62,
                "y": 0.655,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l3",
                "type": "text",
                "text": "RAM",
                "x": 0.36,
                "y": 0.81,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (34, 226, 236),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v3",
                "type": "text",
                "stat": "ram",
                "template": "{value}",
                "x": 0.62,
                "y": 0.81,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.69,
                "y": 0.345,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (250, 232, 26),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v0",
                "type": "text",
                "stat": "gpu_load",
                "template": "{value}",
                "x": 0.95,
                "y": 0.345,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l1",
                "type": "text",
                "text": "TEMP",
                "x": 0.69,
                "y": 0.5,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (250, 232, 26),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v1",
                "type": "text",
                "stat": "gpu_temp",
                "template": "{value}",
                "x": 0.95,
                "y": 0.5,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l2",
                "type": "text",
                "text": "POWER",
                "x": 0.69,
                "y": 0.655,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (250, 232, 26),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v2",
                "type": "text",
                "stat": "gpu_power",
                "template": "{value}",
                "x": 0.95,
                "y": 0.655,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l3",
                "type": "text",
                "text": "VRAM",
                "x": 0.69,
                "y": 0.81,
                "font_size": 0.046,
                "color": (16, 16, 20),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (250, 232, 26),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v3",
                "type": "text",
                "stat": "vram_usage",
                "template": "{value}",
                "x": 0.95,
                "y": 0.81,
                "font_size": 0.068,
                "color": (250, 250, 252),
                "align": "right",
                "bold": True,
                "font": "chakra",
                "opacity": 1.0,
            },
        ],
    },
    "Crimson Strike": {
        "background": {
            "mode": "crimson",
            "scheme": "crimson",
            "image_path": None,
            "border": "none",
            "dim": 0,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.145,
                "y": 0.072,
                "font_size": 0.072,
                "color": (246, 244, 240),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "font": "bebas",
            },
            {
                "id": "h_cpu",
                "type": "text",
                "text": "CPU",
                "x": 0.49,
                "y": 0.12,
                "font_size": 0.1,
                "color": (246, 244, 240),
                "align": "center",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "h_gpu",
                "type": "text",
                "text": "GPU",
                "x": 0.82,
                "y": 0.12,
                "font_size": 0.1,
                "color": (246, 244, 240),
                "align": "center",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.36,
                "y": 0.345,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v0",
                "type": "text",
                "stat": "cpu_load",
                "template": "{value}",
                "x": 0.62,
                "y": 0.345,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l1",
                "type": "text",
                "text": "CLOCK",
                "x": 0.36,
                "y": 0.5,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v1",
                "type": "text",
                "stat": "cpu_freq",
                "template": "{value}",
                "x": 0.62,
                "y": 0.5,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l2",
                "type": "text",
                "text": "PEAK",
                "x": 0.36,
                "y": 0.655,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v2",
                "type": "text",
                "stat": "cpu_load_peak",
                "template": "{value}",
                "x": 0.62,
                "y": 0.655,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "cpu_l3",
                "type": "text",
                "text": "RAM",
                "x": 0.36,
                "y": 0.81,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v3",
                "type": "text",
                "stat": "ram",
                "template": "{value}",
                "x": 0.62,
                "y": 0.81,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.69,
                "y": 0.345,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v0",
                "type": "text",
                "stat": "gpu_load",
                "template": "{value}",
                "x": 0.95,
                "y": 0.345,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l1",
                "type": "text",
                "text": "TEMP",
                "x": 0.69,
                "y": 0.5,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v1",
                "type": "text",
                "stat": "gpu_temp",
                "template": "{value}",
                "x": 0.95,
                "y": 0.5,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l2",
                "type": "text",
                "text": "POWER",
                "x": 0.69,
                "y": 0.655,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v2",
                "type": "text",
                "stat": "gpu_power",
                "template": "{value}",
                "x": 0.95,
                "y": 0.655,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
            {
                "id": "gpu_l3",
                "type": "text",
                "text": "VRAM",
                "x": 0.69,
                "y": 0.81,
                "font_size": 0.046,
                "color": (250, 248, 246),
                "align": "left",
                "bold": True,
                "font": "chakra",
                "plate": (206, 20, 34),
                "plate_radius": 0.2,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v3",
                "type": "text",
                "stat": "vram_usage",
                "template": "{value}",
                "x": 0.95,
                "y": 0.81,
                "font_size": 0.06,
                "color": (246, 244, 240),
                "align": "right",
                "bold": True,
                "font": "archivo",
                "opacity": 1.0,
            },
        ],
    },
    "Cherry Blossom": {
        "background": {
            "mode": "cherry",
            "scheme": "crimson",
            "image_path": None,
            "border": "none",
            "dim": 0,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.068,
                "font_size": 0.062,
                "color": (120, 54, 86),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "font": "poppins",
            },
            {
                "id": "cpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.058,
                "y": 0.275,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v0",
                "type": "text",
                "stat": "cpu_load",
                "template": "{value}",
                "x": 0.458,
                "y": 0.275,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b0",
                "type": "bar",
                "stat": "cpu_load",
                "x": 0.258,
                "y": 0.337,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "cpu_l1",
                "type": "text",
                "text": "CLOCK",
                "x": 0.058,
                "y": 0.447,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v1",
                "type": "text",
                "stat": "cpu_freq",
                "template": "{value}",
                "x": 0.458,
                "y": 0.447,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b1",
                "type": "bar",
                "stat": "cpu_freq",
                "x": 0.258,
                "y": 0.509,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "cpu_l2",
                "type": "text",
                "text": "PEAK",
                "x": 0.058,
                "y": 0.619,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v2",
                "type": "text",
                "stat": "cpu_load_peak",
                "template": "{value}",
                "x": 0.458,
                "y": 0.619,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b2",
                "type": "bar",
                "stat": "cpu_load_peak",
                "x": 0.258,
                "y": 0.681,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "cpu_l3",
                "type": "text",
                "text": "RAM",
                "x": 0.058,
                "y": 0.791,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v3",
                "type": "text",
                "stat": "ram",
                "template": "{value}",
                "x": 0.458,
                "y": 0.791,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b3",
                "type": "bar",
                "stat": "ram",
                "x": 0.258,
                "y": 0.853,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "gpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.543,
                "y": 0.275,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v0",
                "type": "text",
                "stat": "gpu_load",
                "template": "{value}",
                "x": 0.943,
                "y": 0.275,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b0",
                "type": "bar",
                "stat": "gpu_load",
                "x": 0.743,
                "y": 0.337,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "gpu_l1",
                "type": "text",
                "text": "TEMP",
                "x": 0.543,
                "y": 0.447,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v1",
                "type": "text",
                "stat": "gpu_temp",
                "template": "{value}",
                "x": 0.943,
                "y": 0.447,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b1",
                "type": "bar",
                "stat": "gpu_temp",
                "x": 0.743,
                "y": 0.509,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "gpu_l2",
                "type": "text",
                "text": "POWER",
                "x": 0.543,
                "y": 0.619,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v2",
                "type": "text",
                "stat": "gpu_power",
                "template": "{value}",
                "x": 0.943,
                "y": 0.619,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b2",
                "type": "bar",
                "stat": "gpu_power",
                "x": 0.743,
                "y": 0.681,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
            {
                "id": "gpu_l3",
                "type": "text",
                "text": "VRAM",
                "x": 0.543,
                "y": 0.791,
                "font_size": 0.036,
                "color": (255, 246, 250),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (236, 106, 152),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v3",
                "type": "text",
                "stat": "vram_usage",
                "template": "{value}",
                "x": 0.943,
                "y": 0.791,
                "font_size": 0.05,
                "color": (96, 42, 70),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b3",
                "type": "bar",
                "stat": "vram_usage",
                "x": 0.743,
                "y": 0.853,
                "width": 0.385,
                "height": 0.018,
                "color": (246, 122, 162),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(246, 122, 162), (250, 178, 138)],
                "opacity": 1.0,
                "track_color": (244, 214, 226),
            },
        ],
    },
    "Petal Dream": {
        "background": {
            "mode": "lavender",
            "scheme": "crimson",
            "image_path": None,
            "border": "none",
            "dim": 0,
        },
        "elements": [
            {
                "id": "clk",
                "type": "clock",
                "x": 0.5,
                "y": 0.068,
                "font_size": 0.062,
                "color": (72, 60, 118),
                "opacity": 1.0,
                "show_seconds": False,
                "face": "digital",
                "hour_format": "24h",
                "show_date": True,
                "analog_style": "minimal",
                "radius": 0.1,
                "image_path": None,
                "width": 0.22,
                "height": 0.22,
                "font": "poppins",
            },
            {
                "id": "cpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.058,
                "y": 0.275,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v0",
                "type": "text",
                "stat": "cpu_load",
                "template": "{value}",
                "x": 0.458,
                "y": 0.275,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b0",
                "type": "bar",
                "stat": "cpu_load",
                "x": 0.258,
                "y": 0.337,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "cpu_l1",
                "type": "text",
                "text": "CLOCK",
                "x": 0.058,
                "y": 0.447,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v1",
                "type": "text",
                "stat": "cpu_freq",
                "template": "{value}",
                "x": 0.458,
                "y": 0.447,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b1",
                "type": "bar",
                "stat": "cpu_freq",
                "x": 0.258,
                "y": 0.509,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "cpu_l2",
                "type": "text",
                "text": "PEAK",
                "x": 0.058,
                "y": 0.619,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v2",
                "type": "text",
                "stat": "cpu_load_peak",
                "template": "{value}",
                "x": 0.458,
                "y": 0.619,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b2",
                "type": "bar",
                "stat": "cpu_load_peak",
                "x": 0.258,
                "y": 0.681,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "cpu_l3",
                "type": "text",
                "text": "RAM",
                "x": 0.058,
                "y": 0.791,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "cpu_v3",
                "type": "text",
                "stat": "ram",
                "template": "{value}",
                "x": 0.458,
                "y": 0.791,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "cpu_b3",
                "type": "bar",
                "stat": "ram",
                "x": 0.258,
                "y": 0.853,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "gpu_l0",
                "type": "text",
                "text": "LOAD",
                "x": 0.543,
                "y": 0.275,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v0",
                "type": "text",
                "stat": "gpu_load",
                "template": "{value}",
                "x": 0.943,
                "y": 0.275,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b0",
                "type": "bar",
                "stat": "gpu_load",
                "x": 0.743,
                "y": 0.337,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "gpu_l1",
                "type": "text",
                "text": "TEMP",
                "x": 0.543,
                "y": 0.447,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v1",
                "type": "text",
                "stat": "gpu_temp",
                "template": "{value}",
                "x": 0.943,
                "y": 0.447,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b1",
                "type": "bar",
                "stat": "gpu_temp",
                "x": 0.743,
                "y": 0.509,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "gpu_l2",
                "type": "text",
                "text": "POWER",
                "x": 0.543,
                "y": 0.619,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v2",
                "type": "text",
                "stat": "gpu_power",
                "template": "{value}",
                "x": 0.943,
                "y": 0.619,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b2",
                "type": "bar",
                "stat": "gpu_power",
                "x": 0.743,
                "y": 0.681,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
            {
                "id": "gpu_l3",
                "type": "text",
                "text": "VRAM",
                "x": 0.543,
                "y": 0.791,
                "font_size": 0.036,
                "color": (250, 248, 255),
                "align": "left",
                "bold": True,
                "font": "poppins_bold",
                "plate": (139, 110, 226),
                "plate_radius": 0.5,
                "plate_pad": 0.5,
                "opacity": 1.0,
            },
            {
                "id": "gpu_v3",
                "type": "text",
                "stat": "vram_usage",
                "template": "{value}",
                "x": 0.943,
                "y": 0.791,
                "font_size": 0.05,
                "color": (52, 44, 92),
                "align": "right",
                "bold": True,
                "font": "poppins_bold",
                "opacity": 1.0,
            },
            {
                "id": "gpu_b3",
                "type": "bar",
                "stat": "vram_usage",
                "x": 0.743,
                "y": 0.853,
                "width": 0.385,
                "height": 0.018,
                "color": (150, 120, 232),
                "orientation": "horizontal",
                "show_knob": False,
                "show_title": False,
                "show_value": False,
                "gradient": True,
                "gradient_direction": "horizontal",
                "gradient_colors": [(150, 120, 232), (128, 196, 216)],
                "opacity": 1.0,
                "track_color": (224, 220, 244),
            },
        ],
    },
}


# A dark cathedral backdrop leaves the center clear for Spotify's live
# Windows media session. Shared gothic styling supplies the typefaces,
# engraved dials, pointed meters, and ornamental media frame.
# Build the repeated rows from one small factory to keep their spacing,
# labels, and stat bindings consistent when this preset is edited.
def _nocturne_stat_row(stat, label, side, row):
    y = (0.61, 0.73, 0.85)[row]
    x = 0.075 if side == "left" else 0.925
    return [
        {
            "id": f"nocturne_{side}_{stat}_reading", "type": "text",
            "stat": stat, "template": f"{label}  {{value}}",
            "x": x, "y": y, "font_size": 0.036,
            "color": (239, 229, 218),
            "align": "left" if side == "left" else "right",
            "bold": True,
            "plate": (9, 7, 12), "plate_opacity": 0.82,
            "plate_pad": 0.32,
            "opacity": 1.0, "z": 20 + row,
        },
        {
            "id": f"nocturne_{side}_{stat}_meter", "type": "bar",
            "stat": stat, "x": 0.18 if side == "left" else 0.82,
            "y": round(y + 0.047, 3), "width": 0.205, "height": 0.018,
            "orientation": "horizontal",
            "show_knob": False, "show_title": False, "show_value": False,
            "opacity": 1.0,
            "z": 30 + row,
        },
    ]


BUILTIN_DASHBOARD_PRESETS["Nocturne Cathedral"] = {
    "background": {
        "mode": "nocturne", "scheme": "crimson", "image_path": None,
        "border": [132, 112, 106], "dim": 0.28, "widget_style": "gothic",
    },
    "elements": [
        {
            "id": "nocturne_title", "type": "text", "text": "Nocturne",
            "x": 0.5, "y": 0.075, "font_size": 0.09,
            "color": (232, 220, 210), "align": "center", "bold": True,
            "font": "unifraktur", "opacity": 1.0, "z": 1,
        },
        {
            "id": "nocturne_music_label", "type": "text", "text": "NOW PLAYING",
            "x": 0.5, "y": 0.15, "font_size": 0.027,
            "color": (181, 82, 96), "align": "center", "bold": True,
            "opacity": 1.0, "z": 2,
        },
        {
            "id": "nocturne_cpu_dial", "type": "gauge", "stat": "cpu_load",
            "x": 0.18, "y": 0.345, "radius": 0.145,
            "opacity": 1.0, "z": 3,
        },
        {
            "id": "nocturne_gpu_dial", "type": "gauge", "stat": "gpu_load",
            "x": 0.82, "y": 0.345, "radius": 0.145,
            "opacity": 1.0, "z": 4,
        },
        *[el for row, (stat, label) in enumerate((
            ("ram", "RAM"),
            ("cpu_freq", "CLOCK"), ("disk_usage", "DISK"),
        )) for el in _nocturne_stat_row(stat, label, "left", row)],
        *[el for row, (stat, label) in enumerate((
            ("gpu_temp", "TEMP"),
            ("vram_usage", "VRAM"), ("network", "NET"),
        )) for el in _nocturne_stat_row(stat, label, "right", row)],
        {
            "id": "nocturne_spotify", "type": "media",
            "x": 0.5, "y": 0.47, "width": 0.315, "height": 0.59,
            "show_art": True, "show_name": True, "show_time": True,
            "opacity": 1.0, "z": 50,
        },
        {
            "id": "nocturne_clock", "type": "clock",
            "x": 0.5, "y": 0.855, "font_size": 0.056,
            "color": (232, 220, 210), "show_seconds": False,
            "show_date": True, "face": "digital", "hour_format": "24h",
            "opacity": 1.0, "z": 51,
        },
    ],
}


def _styled_dashboard_preset(style, title, background, prefix):
    """Keep the music and useful readings in the same readable arrangement.

    Only geometry is shared. Font, metalwork, needles, meters, media frame,
    and palette are supplied by the selected style, with normal overrides.
    """
    preset = deepcopy(BUILTIN_DASHBOARD_PRESETS["Nocturne Cathedral"])
    preset["background"] = {**background, "widget_style": style, "image_path": None}
    for el in preset["elements"]:
        el["id"] = el["id"].replace("nocturne", prefix)
        el.pop("color", None)
        el.pop("font", None)
        if el.get("plate"):
            el["plate"] = widget_styles.STYLES[style]["defaults"]["face_color"]
        if el["id"] == f"{prefix}_title":
            el.update(text=title, font_size=0.052 if style == "cyberpunk" else 0.047)
        if el["id"] == f"{prefix}_music_label":
            el["color"] = widget_styles.STYLES[style]["defaults"]["color"]
        if style == "cyberpunk" and el.get("type") == "text" and el.get("stat"):
            el["font_size"] = 0.029
    return preset


BUILTIN_DASHBOARD_PRESETS["Neon Ronin"] = _styled_dashboard_preset(
    "cyberpunk", "NEON // RONIN",
    {"mode": "ronin", "scheme": "blue", "border": [33, 224, 238], "dim": 0.26}, "ronin")
BUILTIN_DASHBOARD_PRESETS["Arcane Observatory"] = _styled_dashboard_preset(
    "high_fantasy", "Arcane Observatory",
    {"mode": "arcane", "scheme": "emerald", "border": [191, 157, 88], "dim": 0.3}, "arcane")



def _element_accent(el):
    """An element's gauge color: an explicit `color` (r, g, b) tuple if
    it has one, otherwise the old left-column-cyan/right-column-magenta
    split -- generalized from "slot name contains 'left'/'right'" (which
    no longer exists once a gauge is just an x/y position) to "which
    half of the panel its center sits in", which reproduces the exact
    same result for all of DEFAULT_ELEMENTS."""
    if el.get("color") is not None:
        return tuple(el["color"])
    return ACCENT_CPU if el["x"] < 0.5 else ACCENT_GPU


def _element_accent2(el):
    """A gauge's optional second ring color (ROADMAP.md Phase 6) -- None
    means "no gradient, single accent color" (today's look, unchanged);
    set only by an explicit `color2` on the element, never derived like
    `_element_accent()`'s left/right fallback is, since there's no
    sensible default second color to guess at. Superseded by
    `_element_gauge_gradient()` below (which folds this in as a
    fallback) for anything that wants more than exactly 2 stops or a
    direction other than the original fixed diagonal sweep -- kept
    as its own function since it's still exactly what an *old* saved
    gauge with just `color2` set (no `gradient`/`gradient_colors`)
    needs, with no migration required."""
    color2 = el.get("color2")
    return tuple(color2) if color2 else None


def _element_gauge_gradient(el):
    """A gauge ring's gradient stops + direction, or (None, "diagonal")
    for a plain single-accent ring. Prefers the newer `gradient_colors`
    (2-4 stops, the same field text/graph/bar all use) + `gradient_
    direction` ("diagonal"/"horizontal"/"vertical") over the older,
    gauge-only `color2` (exactly one second stop, always swept corner-
    to-corner) -- an existing dashboard saved before gradient_colors
    existed still renders exactly the same via the color2 fallback,
    with `_element_accent(el)` (the ring's own first/base color) as the
    implied first stop, same as before."""
    if el.get("gradient"):
        stops = _element_gradient_colors(el)
        if stops:
            return stops, el.get("gradient_direction", "diagonal")
    color2 = _element_accent2(el)
    if color2 is not None:
        return [_element_accent(el), color2], "diagonal"
    return None, "diagonal"


def _gauge_gradient_endpoints(cx, cy, radius, direction):
    """The two (x, y) points a cairo.LinearGradient sweeps between, for
    a gauge ring of the given center/radius -- "horizontal" (left-to-
    right) and "vertical" (top-to-bottom) match the same two options
    the bar/text/graph elements' own gradient direction picker offers;
    "diagonal" (the default, and the only option that ever existed
    before gauge got more than exactly 2 stops) is the original fixed
    corner-to-corner sweep, kept as its own case rather than folded into
    "horizontal" so an existing `color2` gauge's look never shifts under
    it."""
    if direction == "horizontal":
        return cx - radius, cy, cx + radius, cy
    if direction == "vertical":
        return cx, cy - radius, cx, cy + radius
    return cx - radius, cy - radius, cx + radius, cy + radius


# ---------------------------------------------------------------- Phase 6 --
# Non-gauge elements: text labels, custom images, and stat history
# graphs. All three share the gauge elements' x/y (a center point, as a
# fraction of width/height) but not `radius` -- a graph/image also
# needs its own width/height (fractions of width/height respectively,
# same normalization idea as radius being a fraction of
# min(width, height)); text sizes itself from `font_size` (a fraction
# of height) instead of a box. Every one of these is fully static
# EXCEPT a graph's plotted history, which is why text/image are drawn
# once in build_static_background() (baked into the background image,
# same as the gauge tracks/titles) while a graph's box+title bakes in
# there too but its bars/line are redrawn every frame in render_frame()
# from whatever history render_frame() is handed -- see run()'s history
# deque and _draw_graph_dynamic()'s docstring.
def _element_color(el, key="color", default=(225, 226, 236)):
    value = el.get(key)
    return tuple(value) if value else default


def _element_gradient_colors(el, key="gradient_colors"):
    """An element's multi-stop gradient fill colors, or None if there
    aren't at least 2 of them -- a single stop isn't a gradient, and
    this is meant to be checked with a plain truthiness test (`if
    fill_colors:`) at every call site rather than each one re-checking
    length itself. Started out just for the bar element; text, graph,
    the digital clock face, and gauge (via _element_gauge_gradient(),
    which also folds in the older single-`color2` gauge field) all read
    this the same way now -- every one of them only draws a gradient at
    all when `el["gradient"]` is truthy, same as the bar element."""
    value = el.get(key)
    if not value or len(value) < 2:
        return None
    return [tuple(c) for c in value]


def _gradient_text(img, xy, text, font, anchor, gradient_colors, direction, opacity=1.0):
    """Draws `text` filled with a multi-stop gradient instead of a flat
    color -- PIL's own draw.text() only takes one flat `fill`, so this
    renders the text as a plain white-on-transparent alpha mask (an "L"
    image the same size as `img`) and uses it as the alpha channel for a
    full-image-sized gradient, same masking idea _bar_fill_subtile()
    uses for the bar element's fill. Composited straight onto `img`,
    same "paste an RGBA layer" pattern every other dynamic element here
    uses.

    The gradient is sized to the *whole image*, not just this text's own
    small bounding box, so a "vertical" gradient looks the same slice of
    color at the same height across every element that uses one -- two
    separate text elements a little apart vertically both gradient
    top-to-bottom consistently, rather than each restarting its own
    gradient from scratch within its own tiny box (which would make two
    short labels look like a repeating pattern instead of one coherent
    gradient across the panel)."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor)
    grad = _linear_gradient_multi(img.size[0], img.size[1], gradient_colors, direction).convert("RGBA")
    grad.putalpha(mask)
    if opacity < 1.0:
        grad = _apply_tile_opacity(grad, opacity)
    img.paste(grad, (0, 0), grad)


def _resolve_text_content(el, stats=None):
    """The string a text element actually shows: either whatever was
    typed into `el["text"]` (a free-standing label), or, when `el
    ["stat"]` names a STAT_DEFS key, that stat's *current* formatted
    reading -- e.g. a text element bound to "cpu_load" with the default
    template shows "42%" and tracks the real value every frame, instead
    of a person having to fake it with a hardcoded "CPU: 42%" string
    that immediately goes stale. `el["template"]` (default "{value}")
    can wrap that reading in more of its own text -- "CPU {value}" or
    "{label}: {value}" -- via str.format, so a bound element can still
    read as a proper label instead of a bare number. A `template` with
    neither placeholder, or one with an unknown one, just falls back to
    the bare value string rather than raising, since a bad template
    shouldn't crash the whole render.

    `stats` is the same flat dict every other dynamic element (gauge,
    bar) reads its live value from; missing/None here means "no live
    reading available" (build_static_background()'s own call, or a
    stopped panel with no polled value yet), which renders as "--",
    same as a gauge's own dim/no-needle state for a missing stat."""
    stat_key = el.get("stat")
    stat_def = STAT_DEFS.get(stat_key) if stat_key else None
    if stat_def is not None:
        value = (stats or {}).get(stat_key)
        value_str = stat_def["fmt"](value) if value is not None else "--"
        template = el.get("template") or "{value}"
        try:
            return template.format(value=value_str, label=stat_def["label"])
        except (KeyError, IndexError, ValueError):
            return value_str
    return (el.get("text") or "").strip()


def _text_element_is_dynamic(el):
    """True when a text element is bound to a live stat (see
    _resolve_text_content()) and so needs redrawing every frame instead
    of being baked into the static background once -- the same
    dynamic/static split every other element type already has (gauge/
    graph/bar redrawn per-frame, text/image normally baked once)."""
    return bool(el.get("stat")) and el.get("stat") in STAT_DEFS


def _draw_text_plate(img, el, text, font, pos, anchor, size_px, opacity):
    """The filled rounded "chip" behind a text element, when `el
    ["plate"]` gives it a color -- the label-plate look every one of the
    reference themes this was built for leans on ("TEMP"/"USAGE"/
    "POWER" sitting in solid colored tags next to their values, rather
    than bare text floating on the background).

    Sized from the text's own measured bbox rather than a fixed box, so
    a chip always fits its string exactly at whatever font/size the
    element uses -- which is the whole reason this is drawn here per
    element instead of being painted into the background art, where it
    would have to be hand-measured against a string it can't see and
    would drift the moment the text or font changed.

    `plate_pad` (fraction of the font's pixel size, default 0.42) is the
    breathing room around the text, `plate_radius` (fraction of the
    chip's *height*, default 0.28 -- 0 for hard corners, 0.5 for a full
    pill) the corner rounding. Drawn on its own RGBA layer whenever it's
    translucent so the chip composites over the background properly
    instead of punching a hole in it."""
    plate = _element_color(el, key="plate", default=None)
    if plate is None:
        return
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = draw.textbbox(pos, text, font=font, anchor=anchor)
    pad_x = size_px * float(el.get("plate_pad", 0.42))
    pad_y = size_px * float(el.get("plate_pad", 0.42)) * 0.62
    box = [left - pad_x, top - pad_y, right + pad_x, bottom + pad_y]
    radius = max(0, (box[3] - box[1]) * float(el.get("plate_radius", 0.28)))
    plate_opacity = float(el.get("plate_opacity", 1.0)) * opacity
    if el.get("widget_style") in widget_styles.STYLES:
        layer = Image.new("RGBA", img.size)
        d = ImageDraw.Draw(layer)
        x0, y0, x1, y1 = box
        cut = min(4, (y1-y0)/4)
        points = [(x0+cut, y0), (x1-cut, y0), (x1, y0+cut), (x1, y1-cut),
                  (x1-cut, y1), (x0+cut, y1), (x0, y1-cut), (x0, y0+cut)]
        d.polygon(points, fill=(*plate, max(0, min(255, round(255*plate_opacity)))))
        d.line([(x0+cut, y1), (x1-cut, y1)], fill=(*widget_styles.color(el, "ornament_color"), round(90*opacity)))
        img.paste(layer, (0, 0), layer)
        return
    if plate_opacity >= 1.0:
        rounded_rect(draw, box, radius=radius, fill=plate)
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    rounded_rect(ImageDraw.Draw(layer), box, radius=radius,
                 fill=(*plate, max(0, min(255, int(255 * plate_opacity)))))
    img.paste(layer, (0, 0), layer)


def _draw_text_element(img, el, width, height, stats=None):
    """A text label -- either a free-standing string typed into the
    canvas, or (see _resolve_text_content()) a live stat reading. A
    free-standing label is baked into the static background since it
    never changes frame to frame (same reasoning as the gauge titles
    above); a stat-bound one is instead redrawn here every frame by
    render_frame(), with `stats` passed through so its reading stays
    current -- see _text_element_is_dynamic() for which is which.

    `el["gradient"]` + `el["gradient_colors"]` (2-4 stops) + `el
    ["gradient_direction"]` fill the text with a gradient instead of a
    flat color, via _gradient_text()'s masking approach -- everything
    else about the element (font, size, alignment, opacity) is
    unaffected either way.

    `el["font"]` (a FONT_FAMILIES key) picks one of the app's bundled
    display faces; `el["plate"]` draws a filled rounded "chip" behind
    the text, auto-sized to the string -- see _draw_text_plate()."""
    text = _resolve_text_content(el, stats)
    if not text:
        return
    size_px = max(8, int(el.get("font_size", 0.05) * height))
    font = _cached_scaled_font(size_px, bool(el.get("bold", False)), el.get("font"))
    color = _element_color(el)
    anchor = {"left": "lm", "center": "mm", "right": "rm"}.get(el.get("align", "center"), "mm")
    x, y = el["x"] * width, el["y"] * height
    opacity = el.get("opacity", 1.0)
    _draw_text_plate(img, el, text, font, (x, y), anchor, size_px, opacity)
    gradient_colors = _element_gradient_colors(el) if el.get("gradient") else None
    if gradient_colors:
        _gradient_text(img, (x, y), text, font, anchor, gradient_colors,
                        el.get("gradient_direction", "horizontal"), opacity)
        return
    if opacity >= 1.0:
        # Common case: draw straight onto the (opaque) background,
        # same as every other piece of static text in this theme --
        # no need for the extra layer/composite round-trip below.
        ImageDraw.Draw(img).text((x, y), text, font=font, fill=color, anchor=anchor)
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x, y), text, font=font, fill=(*color, 255), anchor=anchor)
    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


def _fit_into_box(src, w, h, mode):
    """Scales `src` (any PIL image) into a `w`x`h` RGBA canvas per
    `mode`:

    - "contain" (the default): the whole image is always visible,
      scaled down/up to fit inside the box and centered, with
      transparent padding on whichever axis doesn't fill exactly (like
      CSS `object-fit: contain`). Nothing is ever cropped.
    - "cover": scaled up just enough to fill the box completely on both
      axes, cropping whatever overflows (like CSS `object-fit: cover`
      -- this was this function's only behavior before `fit` existed).
    - "stretch": resized to exactly `w`x`h`, ignoring the image's own
      aspect ratio -- can distort it, but fills the box exactly with
      nothing cropped or padded.
    """
    if mode == "stretch":
        return src.resize((w, h), Image.LANCZOS)
    if mode == "cover":
        return ImageOps.fit(src, (w, h), method=Image.LANCZOS)
    # contain
    src_ratio = src.width / max(1, src.height)
    box_ratio = w / max(1, h)
    if src_ratio > box_ratio:
        new_w, new_h = w, max(1, round(w / src_ratio))
    else:
        new_h, new_w = h, max(1, round(h * src_ratio))
    scaled = src.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(scaled, ((w - new_w) // 2, (h - new_h) // 2), scaled)
    return canvas


# Clock customization -- originally just one hardcoded digital format,
# now three selectable "faces" (el["face"]) each with their own extra
# options, picked per-element the same way a gauge picks a stat:
#
#   "digital" (default -- every clock element saved before this existed
#     is one) -- the time as text. Used to be just an HH:MM:SS/HH:MM
#     toggle (`show_seconds`); now also has `hour_format` (24h/12h,
#     with AM/PM in 12h) and an optional `show_date` line underneath.
#   "analog" -- a procedurally drawn round face with hour/minute/second
#     hands, in one of ANALOG_CLOCK_STYLES below. `radius` sizes it,
#     same fraction-of-min(width,height) convention as a gauge's.
#   "image" -- a user-supplied PNG (`image_path`, uploaded the exact
#     same way an image element's picture is) as a decorative clock
#     face/skin, fit into its `width`/`height` box, with the digital
#     time (same formatting options as the "digital" face) drawn on top
#     of it -- a shadow behind the text keeps it legible over any
#     picture without having to know its colors ahead of time.
#
# All three are redrawn every frame (never baked into the static
# background), same as the original digital-only clock always was --
# see this function's own docstring below for why.
DIGITAL_CLOCK_HOUR_FORMATS = {
    "24h": "24-hour",
    "12h": "12-hour (AM/PM)",
}

ANALOG_CLOCK_STYLES = {
    "classic": "Classic -- white face, black ticks and hands",
    "minimal": "Minimal -- thin ring, no ticks, just hands",
    "neon": "Neon -- dark face, glowing hands in the clock's color",
}

CLOCK_FACES = {
    "digital": "Digital",
    "analog": "Analog",
    "image": "Custom image",
}


def _clock_time_format(el):
    """The strftime() format a digital time readout uses -- shared by
    the "digital" face and the time overlay the "image" face draws on
    top of its picture, so both respect the same hour_format/
    show_seconds options."""
    twelve_hour = el.get("hour_format", "24h") == "12h"
    fmt = ("%I" if twelve_hour else "%H") + ":%M"
    if el.get("show_seconds", True):
        fmt += ":%S"
    if twelve_hour:
        fmt += " %p"
    return fmt


def _draw_clock_element(img, el, width, height, fonts):
    """A free-standing clock element -- like _draw_text_element() above,
    but showing the current time instead of a fixed string. Unlike
    every other element type here, this used to be the ONE thing
    render_frame() always drew unconditionally at a hardcoded spot
    (see its own docstring); it's now just another element, addable/
    movable/removable/resizable/re-colorable from the canvas like
    everything else. Redrawn every frame (never baked into the static
    background) since its content changes every second, same reasoning
    as a graph's plotted line or the media element's progress bar.
    Dispatches on `el["face"]` -- see the comment above CLOCK_FACES."""
    face = el.get("face", "digital")
    if face == "analog":
        _draw_analog_clock_face(img, el, width, height)
        return
    if face == "image":
        _draw_image_clock_face(img, el, width, height)
        return

    x, y = el["x"] * width, el["y"] * height
    size_px = max(8, int(el.get("font_size", 24 / REFERENCE_HEIGHT) * height))
    family = el.get("font")
    font = _cached_scaled_font(size_px, bool(el.get("bold", False)), family)
    color = _element_color(el, default=(235, 235, 242))
    time_str = datetime.datetime.now().strftime(_clock_time_format(el))
    opacity = el.get("opacity", 1.0)
    show_date = bool(el.get("show_date"))
    date_str = datetime.datetime.now().strftime("%a, %b %d") if show_date else None
    # The date line inherits the clock's own font family (it's the same
    # element, just a second line), only smaller and never bolded.
    date_font = _cached_scaled_font(max(7, int(size_px * 0.45)), False, family) if show_date else None
    if el.get("widget_style") in widget_styles.STYLES:
        ornament = Image.new("RGBA", img.size)
        widget_styles.rule(ImageDraw.Draw(ornament), x-size_px*2.5, x+size_px*2.5,
                           y-size_px*.95, (*widget_styles.color(el, "ornament_color"), round(180*opacity)), el["widget_style"])
        img.paste(ornament, (0, 0), ornament)

    # `el["gradient"]` (2-4 stops, same fields text/graph/bar/gauge all
    # use) fills the time -- and date, if shown, a touch dimmer, same
    # 210-vs-255 alpha split the flat-color path below already used --
    # with a gradient instead of a flat color. Drawn fresh every frame
    # (this whole element is, since the time itself changes every
    # second -- see this function's own docstring), unlike
    # _draw_text_element()'s gradient, which only ever needs to render
    # once since a plain text element's string is static.
    gradient_colors = _element_gradient_colors(el) if el.get("gradient") else None
    if gradient_colors:
        mask = Image.new("L", img.size, 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.text((x, y), time_str, font=font, fill=255, anchor="mm")
        if show_date:
            mdraw.text((x, y + size_px * 0.75), date_str, font=date_font, fill=210, anchor="ma")
        grad = _linear_gradient_multi(
            img.size[0], img.size[1], gradient_colors, el.get("gradient_direction", "horizontal")
        ).convert("RGBA")
        grad.putalpha(mask)
        grad = _apply_tile_opacity(grad, opacity)
        img.paste(grad, (0, 0), grad)
        return

    if opacity >= 1.0 and not show_date:
        ImageDraw.Draw(img).text((x, y), time_str, font=font, fill=color, anchor="mm")
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((x, y), time_str, font=font, fill=(*color, 255), anchor="mm")
    if show_date:
        draw.text((x, y + size_px * 0.75), date_str, font=date_font, fill=(*color, 210), anchor="ma")
    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


def _draw_analog_clock_face(img, el, width, height):
    """A procedurally-drawn round clock face -- ticks plus hour/minute/
    (optionally) second hands, all computed from the actual system
    time every frame. `el["analog_style"]` (see ANALOG_CLOCK_STYLES)
    picks the palette/decoration; `el["color"]` tints the hands/ticks
    (and, for "neon", the glow) rather than being a fixed part of any
    one style, so the same three styles still work with whatever accent
    color the rest of the layout uses. Drawn on its own RGBA layer and
    composited once so `opacity` applies to the whole face uniformly,
    same approach every other semi-transparent element here uses."""
    cx, cy = el["x"] * width, el["y"] * height
    radius = max(10.0, el.get("radius", 0.12) * min(width, height))
    style = el.get("analog_style", "classic")
    color = _element_color(el, default=(235, 235, 242))
    opacity = el.get("opacity", 1.0)
    now = datetime.datetime.now()

    hour_angle = math.radians((now.hour % 12 + now.minute / 60) * 30 - 90)
    minute_angle = math.radians((now.minute + now.second / 60) * 6 - 90)
    second_angle = math.radians(now.second * 6 - 90)

    if style == "neon":
        face_fill = (12, 14, 20, 235)
        ring_color = (*color, 255)
        tick_color = (*color, 190)
        hour_color = (*color, 255)
        minute_color = (*color, 255)
        second_color = (255, 90, 90, 255)
        ring_width = max(2, round(radius * 0.05))
    elif style == "minimal":
        face_fill = None
        ring_color = (*color, 150)
        tick_color = None
        hour_color = (*color, 255)
        minute_color = (*color, 220)
        second_color = (*color, 160)
        ring_width = max(1, round(radius * 0.02))
    else:  # classic
        face_fill = (250, 250, 252, 235)
        ring_color = (40, 40, 46, 255)
        tick_color = (40, 40, 46, 210)
        hour_color = (30, 30, 34, 255)
        minute_color = (30, 30, 34, 255)
        second_color = (200, 40, 40, 255)
        ring_width = max(2, round(radius * 0.035))

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    if face_fill:
        draw.ellipse(bbox, fill=face_fill)
    draw.ellipse(bbox, outline=ring_color, width=ring_width)

    if tick_color:
        for i in range(12):
            ang = math.radians(i * 30 - 90)
            major = i % 3 == 0
            outer_r = radius * 0.92
            inner_r = radius * (0.78 if major else 0.85)
            x1, y1 = cx + outer_r * math.cos(ang), cy + outer_r * math.sin(ang)
            x2, y2 = cx + inner_r * math.cos(ang), cy + inner_r * math.sin(ang)
            draw.line([(x1, y1), (x2, y2)], fill=tick_color,
                      width=max(1, round(radius * (0.045 if major else 0.02))))

    def hand(angle, length_frac, width_frac, fill):
        length = radius * length_frac
        half_w = radius * width_frac
        px, py = math.cos(angle), math.sin(angle)
        nx, ny = -py, px
        tip = (cx + px * length, cy + py * length)
        base_l = (cx + nx * half_w, cy + ny * half_w)
        base_r = (cx - nx * half_w, cy - ny * half_w)
        tail = (cx - px * length * 0.15, cy - py * length * 0.15)
        draw.polygon([base_l, tip, base_r, tail], fill=fill)

    hand(hour_angle, 0.5, 0.045, hour_color)
    hand(minute_angle, 0.72, 0.03, minute_color)
    if el.get("show_seconds", True):
        length = radius * 0.8
        tail_len = radius * 0.18
        tip = (cx + math.cos(second_angle) * length, cy + math.sin(second_angle) * length)
        tail = (cx - math.cos(second_angle) * tail_len, cy - math.sin(second_angle) * tail_len)
        draw.line([tail, tip], fill=second_color, width=max(1, round(radius * 0.02)))

    center_r = max(2, round(radius * 0.05))
    draw.ellipse([cx - center_r, cy - center_r, cx + center_r, cy + center_r], fill=hour_color)

    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


# One-slot-per-path cache for a custom clock face image, same idea as
# _default_art_cache above -- avoids re-decoding the same PNG from disk
# 10 times a second just because the clock face is redrawn every frame
# (its background PICTURE never changes frame to frame even though the
# time text on top of it does). Not size-bounded since realistically
# there's at most a small handful of distinct clock-face elements in
# any one layout, nowhere near enough distinct paths to matter.
_clock_face_cache = {}


def _load_clock_face_image(path):
    if path not in _clock_face_cache:
        try:
            _clock_face_cache[path] = Image.open(path).convert("RGBA")
        except Exception:  # noqa: BLE001 -- bad/missing/corrupt file
            _clock_face_cache[path] = False
    return _clock_face_cache[path] or None


def _draw_image_clock_face(img, el, width, height):
    """The "image" clock face: a user-supplied picture (`image_path`,
    uploaded the same way as an image element's -- see image_store.py)
    as a decorative background/skin, fit into its `width`/`height` box
    (same _fit_into_box() convention as an image element), with the
    digital time drawn on top of it at its own center. A dark shadow
    behind the light-colored time text (and date, if `show_date`) keeps
    it readable over any picture without needing to know its colors
    ahead of time; falls back to just the time on a transparent
    background if the picture can't be loaded, same tolerance every
    other image-backed element here has for a bad/missing path."""
    cx, cy = el["x"] * width, el["y"] * height
    w = max(4, int(el.get("width", 0.22) * width))
    h = max(4, int(el.get("height", 0.22) * height))
    opacity = el.get("opacity", 1.0)

    src = _load_clock_face_image(el.get("image_path")) if el.get("image_path") else None
    if src is not None:
        fitted = _fit_into_box(src, w, h, el.get("fit", "contain"))
        fitted = _apply_tile_opacity(fitted, opacity)
        img.paste(fitted, (int(cx - w / 2), int(cy - h / 2)), fitted)

    size_px = max(8, int(el.get("font_size", 24 / REFERENCE_HEIGHT) * height))
    font = load_font(size_px, bold=bool(el.get("bold", False)))
    color = _element_color(el, default=(235, 235, 242))
    time_str = datetime.datetime.now().strftime(_clock_time_format(el))
    shadow = max(1, size_px // 16)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((cx + shadow, cy + shadow), time_str, font=font, fill=(0, 0, 0, 180), anchor="mm")
    draw.text((cx, cy), time_str, font=font, fill=(*color, 255), anchor="mm")
    if el.get("show_date"):
        date_str = datetime.datetime.now().strftime("%a, %b %d")
        date_font = load_font(max(7, int(size_px * 0.45)))
        date_y = cy + size_px * 0.75
        draw.text((cx + shadow, date_y + shadow), date_str, font=date_font, fill=(0, 0, 0, 160), anchor="ma")
        draw.text((cx, date_y), date_str, font=date_font, fill=(*color, 230), anchor="ma")
    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


def _draw_image_element(img, el, width, height):
    """A user-supplied photo/logo dropped onto the layout as its own
    positioned element, fit into its box per `el["fit"]` (see
    _fit_into_box() -- "contain" by default, so a freshly-picked image
    is never cropped or stretched just because its box's aspect ratio
    doesn't match the picture's own) and alpha-composited at its own
    opacity. Baked into the static background since it's a fixed
    picture, not a live reading -- a bad/missing/unreadable path is
    skipped silently rather than erroring the whole theme out, same
    tolerance the background image and app.py's Tkinter background
    picker already have."""
    path = el.get("image_path")
    if not path:
        return
    try:
        src = Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 -- bad/missing file, corrupt image, etc.
        return
    w = max(4, int(el.get("width", 0.15) * width))
    h = max(4, int(el.get("height", 0.15) * height))
    fitted = _fit_into_box(src, w, h, el.get("fit", "contain"))
    fitted = _apply_tile_opacity(fitted, el.get("opacity", 1.0))
    cx, cy = el["x"] * width, el["y"] * height
    img.paste(fitted, (int(cx - w / 2), int(cy - h / 2)), fitted)


def _graph_box(el, width, height):
    """A graph element's pixel rect, from its center x/y and its own
    width/height (fractions of width/height, not of min(width,height) --
    a graph is a rectangle, not a circle, so there's no reason to tie
    its two dimensions together the way a gauge's single `radius`
    does)."""
    w = max(20.0, el.get("width", 0.22) * width)
    h = max(14.0, el.get("height", 0.14) * height)
    cx, cy = el["x"] * width, el["y"] * height
    x0, y0 = cx - w / 2, cy - h / 2
    return {"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h, "w": w, "h": h, "cx": cx, "cy": cy}


def _draw_graph_static(img, el, box, fonts):
    """The part of a history graph that doesn't change frame to frame:
    a dim border box and the bound stat's title above it -- the bars/
    line themselves are redrawn every frame in render_frame() (see
    _draw_graph_dynamic()) since they plot values that change.

    `el["show_title"]` (default True) turns the title off, and
    `el["show_frame"]` (default True) the border box, for a layout that
    labels and frames the graph itself -- a themed background card with
    its own heading, say -- and just wants the plotted line dropped
    into it rather than a second, differently-styled label/box drawn on
    top of its own."""
    draw = ImageDraw.Draw(img)
    accent = _element_color(el, default=ACCENT_CPU)
    if el.get("show_frame", True):
        rounded_rect(draw, [box["x0"], box["y0"], box["x1"], box["y1"]], radius=6,
                     outline=dim_color(accent, 0.7), width=1)
    if not el.get("show_title", True):
        return
    stat_def = STAT_DEFS.get(el.get("stat"))
    title = stat_def["title"] if stat_def else str(el.get("stat", "")).upper()
    gothic = el.get("widget_style") in widget_styles.STYLES
    draw.text((box["cx"], box["y0"] - (22 if gothic else 10)), title,
              font=_cached_scaled_font(11, True, el.get("font")) if gothic else fonts.small_title,
              fill=widget_styles.color(el, "text_color") if gothic else (225, 226, 236), anchor="mb")


def _draw_graph_tile(el, box, values, accent, gradient_colors=None, gradient_direction="horizontal"):
    """Renders the actual bars/line for one frame onto a fresh RGBA
    tile the size of the graph's box, from `values` (oldest first,
    matching the deque run() maintains -- see its own comment) -- a
    missing/None sample just leaves a gap instead of drawing a false
    zero, same "don't lie about missing data" rule draw_gauge_dynamic_
    tile follows for a gauge with no reading.

    `gradient_colors` (2-4 stops) draws the bars/line with a gradient
    across the tile instead of a flat `accent`, same fields the bar
    element's own gradient uses. Unlike the bar element's fill (which
    has to anchor its gradient to a *virtual full track* so a value
    change doesn't visibly shift colors around -- see
    _bar_fill_subtile()'s docstring), a graph tile is simpler: every
    pixel of it is always either "part of a bar/line right now" or not,
    with no partially-revealed region to keep stable, so the whole
    tile's own gradient can just be built fresh at the tile's own size
    every frame and used as-is -- built normally with a flat `accent`
    first (to get the actual bars/line shape and their existing per-
    pixel alpha, 210 for bars, 230 for the line, both unchanged), then
    the gradient's colors swapped in through that same alpha as a mask,
    same technique _bar_fill_subtile() uses for its own rounded-end
    mask."""
    w, h = max(1, int(box["w"])), max(1, int(box["h"]))
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    stat_def = STAT_DEFS.get(el.get("stat"))
    min_v = stat_def["min"] if stat_def else 0
    max_v = stat_def["max"] if stat_def else 100
    span = max(1e-6, max_v - min_v)
    pad = 3
    plot_w, plot_h = max(1, w - pad * 2), max(1, h - pad * 2)
    n = len(values)
    if n == 0:
        return tile

    def y_of(v):
        frac = max(0.0, min(1.0, (v - min_v) / span))
        return pad + (1 - frac) * plot_h

    if el.get("style") == "bar":
        bar_w = plot_w / n
        for i, v in enumerate(values):
            if v is None:
                continue
            y = y_of(v)
            x0 = pad + i * bar_w
            draw.rectangle([x0, y, x0 + max(1.0, bar_w * 0.75), pad + plot_h], fill=(*accent, 210))
    else:
        prev = None
        for i, v in enumerate(values):
            x = pad + (i / max(1, n - 1)) * plot_w
            if v is None:
                prev = None
                continue
            point = (x, y_of(v))
            if prev is not None:
                draw.line([prev, point], fill=(*accent, 230), width=2)
            prev = point

    if gradient_colors:
        grad = _linear_gradient_multi(w, h, gradient_colors, gradient_direction).convert("RGBA")
        grad.putalpha(tile.split()[3])
        return grad
    return tile


def _draw_graph_dynamic(img, el, box, values, accent, gradient_colors=None, gradient_direction="horizontal"):
    """Redraws one graph element's bars/line for the current frame and
    composites it into the (already-static) background copy, respecting
    the element's own opacity -- same `_apply_tile_opacity()` +
    `img.paste(tile, ..., tile)` pattern every other dynamic element in
    this theme uses."""
    tile = _draw_graph_tile(el, box, values, accent, gradient_colors, gradient_direction)
    tile = _apply_tile_opacity(tile, el.get("opacity", 1.0))
    img.paste(tile, (int(box["x0"]), int(box["y0"])), tile)


def _bar_box(el, width, height):
    """A bar element's pixel rect, from its center x/y and its own
    width/height -- same idea as _graph_box(), just wider-than-tall by
    default (see makeElement()'s "bar" case on the frontend) since it
    defaults to reading as a single horizontal meter, not a plot. The
    box itself doesn't care about `el["orientation"]` -- see
    _draw_bar_dynamic() for how that picks the fill direction within
    whatever rect this returns."""
    w = max(20.0, el.get("width", 0.26) * width)
    h = max(14.0, el.get("height", 0.12) * height)
    cx, cy = el["x"] * width, el["y"] * height
    x0, y0 = cx - w / 2, cy - h / 2
    return {"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h, "w": w, "h": h, "cx": cx, "cy": cy}


def _draw_bar_static(img, el, box, fonts):
    """The part of a bar element that doesn't change frame to frame:
    the bound stat's title above it -- same split as _draw_graph_static()
    above, just for a single current-value linear meter (see
    _draw_bar_dynamic()'s docstring for why this exists alongside
    gauge/graph) instead of a scrolling history."""
    if not el.get("show_title", True):
        # Same opt-out as the graph's (see _draw_graph_static()): a
        # layout that draws its own label -- e.g. a text element to the
        # bar's left in the theme's own font, the way the reference
        # dashboards this was built for lay out a "Load ... 6%" row
        # above the meter -- wants only the meter drawn here, not a
        # second centered title in a different face on top of it.
        return
    draw = ImageDraw.Draw(img)
    stat_def = STAT_DEFS.get(el.get("stat"))
    title = stat_def["title"] if stat_def else str(el.get("stat", "")).upper()
    gothic = el.get("widget_style") in widget_styles.STYLES
    draw.text((box["cx"], box["y0"] - (22 if gothic else 10)), title,
              font=_cached_scaled_font(11, True, el.get("font")) if gothic else fonts.small_title,
              fill=widget_styles.color(el, "text_color") if gothic else (225, 226, 236), anchor="mb")


def _draw_bar_dynamic(img, el, box, value, min_v, max_v, accent, font_value, value_fmt):
    """Redraws one bar element's fill + current-value text for this
    frame. A linear alternative to `gauge`'s circular ring for the same
    kind of stat (a live value against its own min/max) -- for anyone
    who'd rather line several stats up as a compact vertical stack of
    bars than spread them out as circles, or just prefers the look.
    Reuses progress_bar_glow() (the same filled-track-plus-glowing-knob
    look `_draw_media_element()`'s playback bar already draws) rather
    than inventing a second bar-drawing routine.

    `value` may be None (the stat isn't available yet, or this frame) --
    same "don't lie about missing data" rule draw_gauge_dynamic()
    follows for a gauge with no reading: the track (and title, baked in
    by _draw_bar_static() above) still draws, so the element's
    footprint is always visible and locatable, but the fill stays empty
    and the value reads "--" instead of guessing zero.

    `el["orientation"]` ("horizontal", the default, or "vertical")
    picks which way the fill runs -- left-to-right within the box's
    full width, or bottom-to-top within its full height -- independent
    of the box's own width/height fields (the frontend's Orientation
    control nudges those to match when it's toggled, but nothing here
    requires it). The bar's own thickness is capped the same way either
    orientation: min(the box's short axis * 0.55, 22px), so a bar
    dragged very wide/tall doesn't turn into a slab.

    `el["show_knob"]` (default False -- user feedback on the first
    version of this element was that the round white handle just
    looked out of place, since the bar's own fill already shows exactly
    where the value is without it) toggles progress_bar_glow()'s round
    handle. `el["gradient"]` + `el["gradient_colors"]` (2-4 RGB tuples)
    + `el["gradient_direction"]` ("horizontal"/"vertical") pick a
    multi-stop gradient fill instead of the flat `accent` color --
    `accent` (still whatever `el["color"]` says, or the default cyan/
    magenta split) keeps tinting the empty track and the glow regardless
    of whether the fill itself is flat or a gradient."""
    tile = Image.new("RGBA", img.size, (0, 0, 0, 0))
    span = max(1e-6, max_v - min_v)
    fraction = 0.0 if value is None else max(0.0, min(1.0, (value - min_v) / span))
    vertical = el.get("orientation") == "vertical"
    show_knob = bool(el.get("show_knob", False))
    gradient_colors = _element_gradient_colors(el) if el.get("gradient") else None
    gradient_direction = el.get("gradient_direction", "horizontal")
    # progress_bar_glow() builds its own Image tiles from these
    # (w + 20, h + 20) -- PIL needs actual ints there, same reason
    # _draw_media_element()'s own bar_w/bar_h (this same helper's other
    # caller) are int()'d rather than left as the plain floats every
    # other box dimension in this file is.
    if vertical:
        bar_w = int(min(box["w"] * 0.55, 22))
        bar_h = int(box["h"])
        bar_x = int(box["cx"] - bar_w / 2)
        bar_y = int(box["y0"])
    else:
        bar_w = int(box["w"])
        bar_h = int(min(box["h"] * 0.55, 22))
        bar_x = int(box["x0"])
        bar_y = int(box["cy"] - bar_h / 2)
    # knob_scale=0.8, well under progress_bar_glow()'s 1.7 default --
    # this bar's ~22px track is much thicker than the now-playing
    # progress bar's 6px one that default was tuned for, and 1.7 would
    # blow the knob up to a ~75px ball (see that function's own
    # docstring) that dwarfed the bar and swamped the title/value text
    # around it. 0.8 keeps the knob a bit larger than the track itself
    # (still reads as a "handle" -- picked visually, not derived) rather
    # than ballooning past it several times over.
    progress_bar_glow(tile, bar_x, bar_y, bar_w, bar_h, fraction, accent, vertical=vertical, knob_scale=0.8,
                       show_knob=show_knob, fill_colors=gradient_colors, fill_direction=gradient_direction,
                       track_color=_element_color(el, key="track_color", default=None))
    if not el.get("show_value", True):
        # The counterpart to `show_title` (see _draw_bar_static()): a
        # layout that puts the reading in its own stat-bound *text*
        # element -- its own font, color and placement -- wants this
        # bar to be purely the meter, not the meter plus a second copy
        # of the same number in the theme's fallback white sans.
        tile = _apply_tile_opacity(tile, el.get("opacity", 1.0))
        img.paste(tile, (0, 0), tile)
        return
    draw = ImageDraw.Draw(tile)
    value_str = value_fmt(value) if value is not None else "--"
    if vertical:
        # Rotate the horizontal layout's placement 90°: the horizontal
        # bar's value text sits just past the bar's far end along its
        # own thickness axis (bar_x + bar_w, bar_y - 6 -- to the right
        # of the bar, above it). The vertical bar's fill/thickness axes
        # are swapped, so its value text goes to the right of the bar,
        # pinned to the bar's *top* -- NOT below it: the knob can land
        # anywhere from y (full) to y+h (empty), and "below the bar" is
        # exactly where it sits at low/idle values, the common case, so
        # putting the text there collided with the knob constantly.
        # Pinning to the top instead only risks a collision near 100%
        # fill, same rare-case trade the horizontal layout already
        # accepts (its own text can sit right on top of the knob once
        # the bar's nearly full).
        draw.text((bar_x + bar_w + 6, bar_y), value_str, font=font_value,
                  fill=(255, 255, 255), anchor="lt")
    else:
        draw.text((bar_x + bar_w, bar_y - 6), value_str, font=font_value,
                  fill=(255, 255, 255), anchor="rb")
    tile = _apply_tile_opacity(tile, el.get("opacity", 1.0))
    img.paste(tile, (0, 0), tile)


def _apply_tile_opacity(tile, opacity):
    """Scales an RGBA gauge tile's alpha channel by `opacity` (0-1).
    A no-op copy at opacity=1.0 rather than skipping the call entirely,
    so callers don't need their own branch for the common case."""
    if opacity >= 1.0:
        return tile
    r, g, b, a = tile.split()
    a = a.point(lambda v: int(v * max(0.0, opacity)))
    return Image.merge("RGBA", (r, g, b, a))

# The panel background is its own small registry, same idea as
# STAT_DEFS -- see _build_background_image() for what each mode
# actually draws, and app.py's Dashboard tab for the picker + file
# browse button. "image" needs a real file at image_path to do
# anything; missing/unreadable silently falls back to "default"
# rather than erroring the whole theme out. "solid" used to just look
# like flat black -- BG_TOP/BG_BOTTOM (still used elsewhere as plain
# matte-fill colors, see fit_album_art()/placeholder_art()) are both
# extremely dark on purpose, so they read fine as a base *under* the
# hex grid/circuit texture but had basically no visible gradient of
# their own once that texture was stripped away. BACKGROUND_COLOR_
# SCHEMES below replaces that with an actual pick of gradients that
# read as a gradient with no texture at all.
BACKGROUND_PRESETS = {
    "default": "Default (hex grid + circuits)",
    "grid": "Simple grid",
    "starfield": "Starfield",
    "radial": "Radial glow",
    "solid": "Plain gradient",
    "aurora": "Aurora Glow (image)",
    "nebula": "Deep Nebula (image)",
    "synthwave": "Synthwave Sunset (image)",
    "bokeh": "Bokeh Night (image)",
    "circuit": "Circuit Bloom (image)",
    "cherry": "Cherry Blossom (image)",
    "lavender": "Lavender Bloom (image)",
    "fusion": "Fusion Core (card chassis)",
    "neon": "Neon Pulse (card chassis)",
    "crimson": "Crimson Strike (card chassis)",
    "nocturne": "Nocturne Cathedral (image)",
    "ronin": "Neon Ronin (image)",
    "arcane": "Arcane Observatory (image)",
    "image": "Custom image",
}
# The four "(image)" entries above aren't a user's own photo (that's
# "image", with its own image_path) -- they're pre-rendered pictures
# shipped with the app itself (assets/backgrounds/*.jpg, generated by
# scripts/generate_backgrounds.py, a one-off authoring script, not run
# by the app), for people who want something that looks like an actual
# picture instead of this theme's own flat-gradient/line-art modes
# (grid/starfield/radial/solid) without having to go find and upload
# one themselves. BUNDLED_BACKGROUND_IMAGES maps each to its filename;
# _build_background_image() resolves that to a real path via
# _resource_path() and then renders it through the exact same
# cover-fit + darken code path a user's own uploaded "image" does, so
# it's legible under gauges/text the same way. Add a new one by
# dropping a fresh assets/backgrounds/<name>.jpg and one more entry in
# both dicts below -- no renderer changes needed.
BUNDLED_BACKGROUND_IMAGES = {
    "aurora": "aurora.jpg",
    "nebula": "nebula.jpg",
    "synthwave": "synthwave.jpg",
    "bokeh": "bokeh.jpg",
    "circuit": "circuit_bloom.jpg",
    "cherry": "cherry_blossom.jpg",
    "lavender": "lavender_bloom.jpg",
    "nocturne": "nocturne_cathedral.jpg",
    "ronin": "neon_ronin.jpg",
    "arcane": "arcane_observatory.jpg",
    # The three "chassis" backgrounds -- not pictures the layout sits
    # on but the cards/panels it sits *in*, drawn at the same
    # fraction-of-panel coordinates as their presets' elements (see
    # generate_backgrounds.py's own comment). They ship with "dim": 0
    # in their presets since they're already drawn at final contrast.
    "fusion": "fusion_core.jpg",
    "neon": "neon_pulse.jpg",
    "crimson": "crimson_strike.jpg",
}
# Applies to every mode above except "image" and the bundled-image
# modes (which are the photo itself) -- "default"'s hex grid and
# circuit traces keep their own fixed tint regardless of scheme
# (they're meant to read as CPU/GPU-side wiring, not as a color
# choice), but the gradient underneath them, and the starfield/grid/
# radial modes entirely, all use whichever scheme is picked here.
BACKGROUND_COLOR_SCHEMES = {
    "purple": {"label": "Purple", "top": (24, 14, 46), "bottom": (5, 4, 11)},
    "blue": {"label": "Ocean Blue", "top": (8, 28, 52), "bottom": (2, 5, 12)},
    "crimson": {"label": "Crimson", "top": (46, 10, 18), "bottom": (10, 3, 5)},
    "emerald": {"label": "Emerald", "top": (8, 42, 26), "bottom": (2, 8, 5)},
    "mono": {"label": "Monochrome", "top": (34, 34, 36), "bottom": (6, 6, 7)},
}
DEFAULT_SCHEME = "purple"
DEFAULT_BACKGROUND = {"mode": "default", "scheme": DEFAULT_SCHEME, "image_path": None}


def dim_color(color, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in color)


# Display fonts shipped with the app (assets/fonts/*.ttf, all SIL OFL
# 1.1 -- see assets/fonts/LICENSES.md), selectable per text/clock
# element via `el["font"]`. Added because the whole *look* of a theme
# lives in its typography: this theme could only ever draw in whatever
# generic UI sans happened to be installed (DejaVu Sans on Linux, Arial
# on Windows), which is why every preset read as "system readout" no
# matter how its colors or layout were arranged. These are the display
# faces the built-in presets are actually designed around -- a heavy
# geometric, a squared-off techno face, an angular "cyber" one, two
# condensed posters, and a clean modern UI sans.
#
# "default" deliberately maps to None/None: an element with no `font`
# (i.e. everything saved before this existed) resolves to exactly the
# old system-font candidate list below, so nothing that already exists
# re-renders differently.
FONT_FAMILIES = {
    "unifraktur": {"label": "UnifrakturCook (blackletter)",
                    "regular": "UnifrakturCook-Bold.ttf", "bold": "UnifrakturCook-Bold.ttf"},
    "cinzel": {"label": "Cinzel (engraved serif)",
                "regular": "Cinzel.ttf", "bold": "Cinzel.ttf"},
    "default": {"label": "Default (system sans)", "regular": None, "bold": None},
    "poppins": {"label": "Poppins (modern UI)",
                "regular": "Poppins-Regular.ttf", "bold": "Poppins-SemiBold.ttf"},
    "poppins_bold": {"label": "Poppins Bold",
                      "regular": "Poppins-SemiBold.ttf", "bold": "Poppins-Bold.ttf"},
    "orbitron": {"label": "Orbitron (techno)",
                  "regular": "Orbitron-Bold.ttf", "bold": "Orbitron-Black.ttf"},
    "chakra": {"label": "Chakra Petch (cyber)",
                "regular": "ChakraPetch-Bold.ttf", "bold": "ChakraPetch-Bold.ttf"},
    "bebas": {"label": "Bebas Neue (condensed)",
               "regular": "BebasNeue-Regular.ttf", "bold": "BebasNeue-Regular.ttf"},
    "anton": {"label": "Anton (heavy display)",
               "regular": "Anton-Regular.ttf", "bold": "Anton-Regular.ttf"},
    "archivo": {"label": "Archivo Black (poster)",
                 "regular": "ArchivoBlack-Regular.ttf", "bold": "ArchivoBlack-Regular.ttf"},
}


def load_font(size, bold=False, family=None):
    """`bold=True` specifically picks a bold font FILE (arial.ttf itself
    has no weight axis PIL can fake convincingly), so it needs its own
    candidate list -- "arial.ttf" bumped to bold is still just arial.ttf
    to PIL. Windows ships arialbd.ttf/segoeuib.ttf alongside the regular
    weights, which is what actually renders bold there.

    `family` (a FONT_FAMILIES key) picks one of the app's own bundled
    display faces instead, resolved to a real file through
    resource_path() the same way a bundled background image is -- so it
    works identically from source and from a PyInstaller build. An
    unknown family, or a bundled file that somehow can't be opened,
    falls through to the system candidates below rather than raising:
    a theme with a missing font should render in a plain sans, not
    fail to render at all."""
    spec = FONT_FAMILIES.get(family or "default")
    if spec:
        filename = spec["bold"] if bold else spec["regular"]
        if filename:
            try:
                font = ImageFont.truetype(resource_path("fonts", filename), size)
                if family == "cinzel":
                    font.set_variation_by_axes([650 if bold else 450])
                return font
            except Exception:  # noqa: BLE001 -- missing/corrupt bundled font
                pass
    if bold:
        candidates = ("DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "Arial Bold.ttf")
    else:
        candidates = ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf")
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


@functools.lru_cache(maxsize=256)
def _cached_scaled_font(size, bold=False, family=None):
    """A cached wrapper around load_font() for callers that need a font
    at a size computed on the fly, frame after frame --
    _draw_weather_element()'s fit-to-box scaling, and every text/clock
    element now that those resolve a per-element `font` family (which
    means hitting disk for a bundled .ttf, not just the OS font cache).
    `Fonts` (the theme's normal font set, built once when a theme
    starts) doesn't need this since it only ever calls load_font()
    directly at startup; this is for the case where the size (or now
    the family) varies per element and can't be known ahead of time, so
    it has to be resolved fresh -- caching keyed on the exact (rounded)
    size + weight + family keeps that from re-hitting disk/FreeType on
    every single frame for an element that isn't changing."""
    return load_font(size, bold=bold, family=family)


def rounded_rect(draw, box, radius, **kwargs):
    try:
        draw.rounded_rectangle(box, radius=radius, **kwargs)
    except AttributeError:  # very old Pillow without rounded_rectangle
        draw.rectangle(box, **kwargs)


def glow_paste(base_img, tile, box, blur=7, glow_alpha=0.55):
    """Composite `tile` (an RGBA image) onto base_img at `box`, with a
    soft blurred bloom underneath a crisp sharp copy on top."""
    glow = tile.filter(ImageFilter.GaussianBlur(blur))
    r, g, b, a = glow.split()
    a = a.point(lambda v: int(v * glow_alpha))
    glow = Image.merge("RGBA", (r, g, b, a))
    base_img.paste(glow, box, glow)
    base_img.paste(tile, box, tile)


def draw_hex_grid(draw, width, height, size=24, color=(64, 52, 96), width_px=1):
    # Brighter + thicker than the original (30, 24, 46) at 1px polygon
    # outline -- that contrast against the near-black background was
    # subtle enough on a monitor and got crushed further by the panel's
    # small size, JPEG compression, and viewing angle, to the point of
    # being basically invisible on the actual hardware. Drawn as
    # explicit line segments (rather than draw.polygon(), which has no
    # width control) so the line width can be bumped for visibility.
    hex_w = math.sqrt(3) * size
    row_h = 1.5 * size
    row = 0
    y = -size
    while y < height + size:
        x_offset = (hex_w / 2) if row % 2 else 0
        x = -hex_w + x_offset
        while x < width + hex_w:
            pts = [
                (x + size * math.cos(math.radians(60 * i - 30)),
                 y + size * math.sin(math.radians(60 * i - 30)))
                for i in range(6)
            ]
            pts.append(pts[0])
            draw.line(pts, fill=color, width=width_px, joint="curve")
            x += hex_w
        y += row_h
        row += 1


def draw_circuit_traces(draw, width, height, seed=7, count=16):
    rng = random.Random(seed)
    # Also brightened (was 0.22) and drawn 2px wide (was 1px) for the
    # same reason as the hex grid above -- too faint to read on the
    # actual panel.
    palette = [dim_color(ACCENT_CPU, 0.4), dim_color(ACCENT_GPU, 0.4)]
    for _ in range(count):
        x, y = rng.randint(0, width), rng.randint(0, height)
        color = rng.choice(palette)
        pts = [(x, y)]
        for _ in range(rng.randint(2, 4)):
            if rng.random() < 0.5:
                x += rng.choice((-1, 1)) * rng.randint(20, 70)
            else:
                y += rng.choice((-1, 1)) * rng.randint(20, 70)
            pts.append((x, y))
        draw.line(pts, fill=color, width=2)
        for px, py in (pts[0], pts[-1]):
            draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=color)



def _surface_to_pil(surface):
    """cairo hands back premultiplied-alpha ARGB32 (and BGRA byte order
    on a little-endian machine); PIL composites straight-alpha, so a
    naive wrap of the raw buffer double-darkens every anti-aliased edge
    pixel. Unpremultiply through numpy instead of doing it byte-by-byte
    in Python, since this runs once per gauge per frame."""
    surface.flush()
    w, h = surface.get_width(), surface.get_height()
    stride = surface.get_stride()
    arr = np.ndarray(shape=(h, stride), dtype=np.uint8, buffer=surface.get_data()).copy()
    arr = arr[:, : w * 4].reshape(h, w, 4)
    b, g_, r, a = (arr[..., i].astype(np.float32) for i in range(4))
    af = np.where(a == 0, 1.0, a) / 255.0
    rgb = np.dstack([np.clip(ch / af, 0, 255) for ch in (r, g_, b)]).astype(np.uint8)
    out = np.dstack([rgb, a.astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def _cairo_rgba(color, alpha=1.0):
    return (color[0] / 255, color[1] / 255, color[2] / 255, alpha)


def draw_gauge_static(g, accent, accent2=None, gradient_colors=None, gradient_direction="diagonal"):
    """The parts that never change: the dim track ring (with a soft drop
    shadow and a gradient sweep for a bit of depth), its crisp edge
    lines, and major/minor tick marks -- all real anti-aliased cairo
    arcs instead of PIL's straight-segment `draw.arc()`. Returns an RGBA
    tile sized g['size']; paste it at _gauge_box(g).

    `gradient_colors` (2-4 stops) + `gradient_direction` supersede the
    older `accent2` (still accepted for a direct 2-color call, but every
    real caller now goes through _element_gauge_gradient(), which
    already folds an old `color2`-only element into this same shape) --
    cairo's own add_color_stop_rgba() takes an arbitrary offset per
    stop, so unlike the raster (numpy/PIL) gradient helpers used
    elsewhere in this file, no separate multi-stop implementation is
    needed here at all: just loop and add stops evenly spaced 0..1."""
    radius, ring_w, size = g["radius"], g["ring_w"], g["size"]
    cx = cy = size / 2
    a0, a1 = math.radians(GAUGE_START), math.radians(GAUGE_END)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_source_rgba(0, 0, 0, 0.35)
    ctx.set_line_width(ring_w + 3)
    ctx.arc(cx, cy + 1.5, radius, a0, a1)
    ctx.stroke()

    # Track ring opacity was tuned back when the background behind it was
    # much darker/flatter -- once the hex grid + circuit traces got
    # brightened for visibility, that same low alpha let the busier
    # background bleed straight through the ring, making it look washed
    # out/low-opacity instead of like a solid dim dial. Bumped up
    # (0.22/0.10 -> 0.55/0.34) so the ring reads as a ring regardless of
    # what's behind it, and the edge lines/minor ticks got the same
    # treatment.
    stops = gradient_colors if gradient_colors and len(gradient_colors) >= 2 else (
        [accent, accent2] if accent2 is not None else None
    )
    gx0, gy0, gx1, gy1 = _gauge_gradient_endpoints(cx, cy, radius, gradient_direction)
    track_grad = cairo.LinearGradient(gx0, gy0, gx1, gy1)
    if stops:
        # A user-picked gradient (ROADMAP.md Phase 6, later widened past
        # exactly 2 colors) -- the ring sweeps across every stop instead
        # of the single-color near-to-far fade below, same alpha (0.6,
        # splitting the difference between the single-color version's
        # 0.55/0.34 so no stop looks washed out against its neighbors).
        n = len(stops)
        for i, c in enumerate(stops):
            track_grad.add_color_stop_rgba(i / (n - 1), *_cairo_rgba(c, 0.6))
    else:
        track_grad.add_color_stop_rgba(0, *_cairo_rgba(accent, 0.55))
        track_grad.add_color_stop_rgba(1, *_cairo_rgba(accent, 0.34))
    ctx.set_source(track_grad)
    ctx.set_line_width(ring_w)
    ctx.arc(cx, cy, radius, a0, a1)
    ctx.stroke()

    ctx.set_source_rgba(*_cairo_rgba(accent, 0.6))
    ctx.set_line_width(1)
    ctx.arc(cx, cy, radius + ring_w / 2, a0, a1)
    ctx.stroke()
    ctx.arc(cx, cy, radius - ring_w / 2, a0, a1)
    ctx.stroke()

    ctx.set_line_cap(cairo.LINE_CAP_BUTT)
    for t in range(0, 101, 5):
        ang = math.radians(GAUGE_START + (GAUGE_END - GAUGE_START) * t / 100)
        cosA, sinA = math.cos(ang), math.sin(ang)
        major = t in TICKS
        r1 = radius - ring_w / 2 - 2
        r2 = radius - ring_w / 2 - (13 if major else 8)
        ctx.set_source_rgba(*_cairo_rgba(accent, 0.95 if major else 0.55))
        ctx.set_line_width(2.2 if major else 1.4)
        ctx.move_to(cx + r1 * cosA, cy + r1 * sinA)
        ctx.line_to(cx + r2 * cosA, cy + r2 * sinA)
        ctx.stroke()

    return _surface_to_pil(surface)


def draw_tick_labels(draw, g, font_tick):
    """The 0/25/50/75/100 numbers -- kept as PIL text (drawn straight
    onto the static background) rather than cairo, since PIL's font
    rendering is already what the rest of this theme's text uses."""
    cx, cy, radius, ring_w = g["cx"], g["cy"], g["radius"], g["ring_w"]
    for t in TICKS:
        ang = math.radians(GAUGE_START + (GAUGE_END - GAUGE_START) * t / 100)
        cosA, sinA = math.cos(ang), math.sin(ang)
        tx, ty = cx + (radius + ring_w / 2 + 15) * cosA, cy + (radius + ring_w / 2 + 15) * sinA
        draw.text((tx, ty), str(t), font=font_tick, fill=(140, 142, 158), anchor="mm")


def draw_gauge_dynamic_tile(g, value, min_v, max_v, accent, accent2=None,
                             gradient_colors=None, gradient_direction="diagonal"):
    """The parts that change every frame: the lit value arc, the needle,
    and the glowing hub, plus a blurred glow pass underneath them.
    Returns an RGBA tile sized g['size'] (paste at _gauge_box(g)), or
    None if value is None -- callers should leave the dim static track
    showing with no needle at all rather than pointing at "0", which
    would read as a real (if low) reading instead of "no data".

    `gradient_colors`/`gradient_direction` -- see draw_gauge_static()'s
    docstring; the lit value arc fades across the same stops the static
    ring does (falling back through `accent2` to a plain accent fade,
    same as before) so the two halves of the ring always agree."""
    if value is None:
        return None

    radius, ring_w, size = g["radius"], g["ring_w"], g["size"]
    cx = cy = size / 2
    a0 = math.radians(GAUGE_START)
    stops = gradient_colors if gradient_colors and len(gradient_colors) >= 2 else None
    gx0, gy0, gx1, gy1 = _gauge_gradient_endpoints(cx, cy, radius, gradient_direction)

    pct = max(0.0, min(100.0, (value - min_v) / (max_v - min_v) * 100.0))
    val_angle = math.radians(GAUGE_START + (GAUGE_END - GAUGE_START) * pct / 100)

    needle_len = radius - ring_w * 0.35
    base_w = ring_w * 0.34
    tip = (cx + needle_len * math.cos(val_angle), cy + needle_len * math.sin(val_angle))
    perp = val_angle + math.pi / 2
    bx, by = math.cos(perp) * base_w, math.sin(perp) * base_w
    back = ring_w * 0.4
    backx, backy = cx - back * math.cos(val_angle), cy - back * math.sin(val_angle)
    hub_r = ring_w * 0.62
    dim_accent = tuple(int(c * 0.55) for c in accent)
    dark_accent = tuple(int(c * 0.4) for c in accent)

    def paint(ctx, solid):
        """Draws the value arc + needle + hub. `solid` fills everything
        flat white-on-accent (used to build the glow-source silhouette);
        otherwise it's the real gradient-shaded version drawn on top."""
        if pct > 0:
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            if solid:
                ctx.set_source_rgba(*_cairo_rgba(accent, 1))
            else:
                grad = cairo.LinearGradient(gx0, gy0, gx1, gy1)
                if stops:
                    n = len(stops)
                    for i, c in enumerate(stops):
                        grad.add_color_stop_rgba(i / (n - 1), *_cairo_rgba(c, 1))
                else:
                    grad.add_color_stop_rgba(0, *_cairo_rgba(dim_accent, 1))
                    # The lit value arc fades toward accent2 if one's set
                    # (matching the static ring's own accent->accent2
                    # sweep above), otherwise the original single-accent
                    # fade.
                    grad.add_color_stop_rgba(1, *_cairo_rgba(accent2 if accent2 is not None else accent, 1))
                ctx.set_source(grad)
            ctx.set_line_width(ring_w)
            ctx.arc(cx, cy, radius, a0, val_angle)
            ctx.stroke()

        if solid:
            ctx.set_source_rgba(*_cairo_rgba(accent, 1))
        else:
            needle_grad = cairo.LinearGradient(cx, cy, tip[0], tip[1])
            needle_grad.add_color_stop_rgba(0, 1, 1, 1, 0.95)
            needle_grad.add_color_stop_rgba(1, *_cairo_rgba(accent, 1))
            ctx.set_source(needle_grad)
        ctx.move_to(backx + bx, backy + by)
        ctx.line_to(tip[0], tip[1])
        ctx.line_to(backx - bx, backy - by)
        ctx.close_path()
        ctx.fill()

        if solid:
            ctx.set_source_rgba(*_cairo_rgba(accent, 1))
        else:
            hub_grad = cairo.RadialGradient(cx - hub_r * 0.3, cy - hub_r * 0.3, hub_r * 0.1, cx, cy, hub_r)
            hub_grad.add_color_stop_rgba(0, 1, 1, 1, 1)
            hub_grad.add_color_stop_rgba(0.5, *_cairo_rgba(accent, 1))
            hub_grad.add_color_stop_rgba(1, *_cairo_rgba(dark_accent, 1))
            ctx.set_source(hub_grad)
        ctx.arc(cx, cy, hub_r, 0, 2 * math.pi)
        ctx.fill()
        if not solid:
            ctx.set_source_rgba(*_cairo_rgba(accent, 0.8))
            ctx.set_line_width(1.2)
            ctx.arc(cx, cy, hub_r, 0, 2 * math.pi)
            ctx.stroke()

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    paint(cairo.Context(surface), solid=False)
    tile = _surface_to_pil(surface)

    glow_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    paint(cairo.Context(glow_surface), solid=True)
    glow = _surface_to_pil(glow_surface).filter(ImageFilter.GaussianBlur(radius * 0.045 + 3))
    r, g_, b, al = glow.split()
    al = al.point(lambda v: int(v * 0.6))
    glow = Image.merge("RGBA", (r, g_, b, al))

    out = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    out.alpha_composite(glow)
    out.alpha_composite(tile)
    return out


def draw_gauge_dynamic(img, g, value, min_v, max_v, accent, font_value, value_fmt, accent2=None,
                        gradient_colors=None, gradient_direction="diagonal"):
    tile = draw_gauge_dynamic_tile(g, value, min_v, max_v, accent, accent2, gradient_colors, gradient_direction)
    if tile is not None:
        img.alpha_composite(tile, _gauge_box(g)) if img.mode == "RGBA" else img.paste(tile, _gauge_box(g), tile)

    draw = ImageDraw.Draw(img)
    cx, cy, radius = g["cx"], g["cy"], g["radius"]
    value_str = value_fmt(value) if value is not None else "--"
    draw.text((cx, cy + radius * 0.32), value_str, font=font_value, fill=(255, 255, 255), anchor="mm")


def fit_album_art(art_img, size, radius):
    """Cover-fit: scale so the image fully fills the square art frame
    (no letterbox bars), then center-crop whichever dimension overflows.
    (Used to be a contain-fit -- thumbnail() + pad -- which left black
    bars above/below or left/right of any art that wasn't already
    square.)"""
    img = art_img.copy()
    src_w, src_h = img.size
    scale = max(size / src_w, size / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left, top = (new_w - size) // 2, (new_h - size) // 2
    img = img.crop((left, top, left + size, top + size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    rounded = Image.new("RGB", (size, size), BG_TOP)
    rounded.paste(img, (0, 0), mask)
    return rounded


def placeholder_art(size, radius):
    canvas = Image.new("RGB", (size, size), (18, 15, 26))
    draw = ImageDraw.Draw(canvas)
    r = size * 0.15
    cx, cy = size / 2, size / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(120, 90, 150), width=3)
    draw.ellipse([cx - r * 0.28, cy - r * 0.28, cx + r * 0.28, cy + r * 0.28], fill=(120, 90, 150))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    rounded = Image.new("RGB", (size, size), BG_TOP)
    rounded.paste(canvas, (0, 0), mask)
    return rounded


# Shown in place of album art whenever nothing is playing (or Spotify/
# winsdk data isn't available at all). No file is bundled or assumed by
# default -- this theme ships with no personal image baked in; set
# DEFAULT_ART_PATH (or pass --default-art / the GUI's file picker) to
# point at whatever image you want, or leave it unset and the plain
# drawn placeholder below is used instead. Loaded once and cached since
# it doesn't change frame to frame; falls back to the drawn placeholder
# if the path is unset, missing, or fails to load, so this never crashes
# over a bad/missing file.
DEFAULT_ART_PATH = None
_default_art_cache = None  # PIL Image once loaded, or False if load failed
_default_art_cache_path = None  # which path _default_art_cache was loaded from


def set_default_art_path(path):
    """Point the "nothing playing" placeholder at a specific image file
    (or None to go back to the plain drawn placeholder)."""
    global DEFAULT_ART_PATH
    DEFAULT_ART_PATH = path


def _load_default_art():
    global _default_art_cache, _default_art_cache_path
    if DEFAULT_ART_PATH is None:
        return None
    if _default_art_cache_path != DEFAULT_ART_PATH:
        try:
            _default_art_cache = Image.open(DEFAULT_ART_PATH).convert("RGB")
        except Exception:  # noqa: BLE001
            _default_art_cache = False
        _default_art_cache_path = DEFAULT_ART_PATH
    return _default_art_cache or None


def default_art(size, radius):
    img = _load_default_art()
    if img is not None:
        return fit_album_art(img, size, radius)
    return placeholder_art(size, radius)


def art_glow_frame(size, radius, accent, inset=7):
    """A soft neon border tile sized around the album art, drawn just
    outside its edges (not on top of it) so pasting the opaque art
    image afterward doesn't cover most of the stroke."""
    pad = inset + 20
    tile = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle([pad - inset, pad - inset, pad + size - 1 + inset, pad + size - 1 + inset],
                         radius=radius + inset, outline=accent + (255,), width=3)
    return tile, pad


# Shown (wrapped across as many lines as it needs) in place of a track
# title whenever nothing is currently playing. Same live-settable
# pattern as DEFAULT_ART_PATH/set_default_art_path() just above --
# `_not_playing_message` is what render_frame() actually reads every
# frame (falling back to DEFAULT_NOT_PLAYING_MESSAGE when it's None),
# so editing it, like the placeholder image, takes effect on the very
# next frame instead of needing a Stop/Start.
DEFAULT_NOT_PLAYING_MESSAGE = "Life is like a door never trust a cow because the sun can't swim"
_not_playing_message = None


def set_not_playing_message(message):
    global _not_playing_message
    message = (message or "").strip()
    _not_playing_message = message or None


def get_not_playing_message():
    return _not_playing_message or DEFAULT_NOT_PLAYING_MESSAGE


# Weather (weather.py -- a free, no-API-key lookup, just a place name)
# used to be a "middle_content" choice: a global on/off pinned to the
# fixed middle column, exactly like the now-playing display used to be
# a global "spotify" middle_content choice before it became the movable/
# resizable `media` element (see default_media_element()/
# _draw_media_element()). Weather has now had the exact same
# conversion -- see default_weather_element()/_draw_weather_element()
# below -- so MIDDLE_CONTENT_OPTIONS/_middle_content/set_middle_content()/
# get_middle_content() are gone entirely: add or remove weather from the
# canvas like anything else, independently of anything else drawn there.


# Layout (elements) and background used to only take effect on the next
# Start/Apply -- build_static_background() bakes gauge rings, graph/
# image/text boxes and their titles into one static image once, purely
# for performance (see that function's docstring), so the running
# render loop kept using the old bake until the whole theme was
# stopped and restarted. That's a reasonable trade-off for a layout
# tweak saved for later, but it reads as an outright bug the moment
# someone drags an element on the live design canvas: the canvas's SVG
# overlay jumps to the new spot immediately (it's just React state)
# while the real panel behind it -- and the "It's overlaid on the
# panel's live frame too" preview, which mirrors the real panel --
# keeps showing the old bake, i.e. the element visually appears to
# duplicate itself. Same complaint applies to "Reset to defaults":
# resetting the canvas's own state works fine, but the *live* panel
# kept showing whatever (including any since-removed image elements)
# was baked in at the last Start/Apply until one happened again.
#
# Layout/background are now live-appliable the same way
# DEFAULT_ART_PATH/_not_playing_message already are:
# controller.py's save_dashboard_elements()/save_dashboard_background()
# call set_pending_dashboard_layout() below (mirroring
# set_default_art_path() etc.), and the render loop in run() takes
# whatever's pending at the top of its next frame and rebakes with it
# -- a full rebake, not an attempt to patch the existing image, since a
# bake is cheap relative to a 100ms frame budget and "elements added,
# removed, resized, restyled" is too open-ended to patch incrementally
# anyway.
_pending_layout = None  # {"elements": [...], "background": {...}} or None; either key may be absent
_pending_layout_lock = threading.Lock()


def set_pending_dashboard_layout(elements=None, background=None):
    """Queues a live layout and/or background update for the running
    dashboard render loop to pick up on its very next frame. Either
    argument can be omitted (left as None) to leave that half of the
    layout alone -- e.g. saving just the background doesn't also force
    a stale copy of `elements` to be re-applied. A no-op if the
    dashboard theme isn't actually running right now; the queued value
    just sits here until the next time it is (or is silently replaced
    by a newer call before that happens)."""
    global _pending_layout
    with _pending_layout_lock:
        current = dict(_pending_layout or {})
        if elements is not None:
            current["elements"] = elements
        if background is not None:
            current["background"] = background
        _pending_layout = current


def _take_pending_dashboard_layout():
    """Atomically reads and clears whatever's queued -- called once per
    frame by run()'s render loop. Returns None (not {}) when nothing's
    pending, so the caller can use a plain `if pending:` check."""
    global _pending_layout
    with _pending_layout_lock:
        pending, _pending_layout = _pending_layout, None
        return pending or None


# Accent color per weather.categorize() icon category -- picked to read
# clearly at a glance (warm yellow for clear, blue for rain, and so on)
# rather than matching any particular icon-pack convention, since these
# are hand-drawn PIL shapes, not a bundled icon set.
_WEATHER_ICON_ACCENTS = {
    "clear": (255, 200, 60),
    "cloudy": (170, 185, 215),
    "fog": (170, 185, 215),
    "rain": (80, 170, 255),
    "snow": (225, 235, 250),
    "storm": (200, 90, 255),
}


def _weather_icon_tile(category, size):
    """A small glowing vector icon for one of weather.categorize()'s
    broad categories -- simple filled PIL shapes (a circle+rays for
    "clear", overlapping ellipses for a cloud, plus rain/snow/lightning
    marks below it) rather than a bundled icon set, consistent with the
    rest of this theme (gauges, the hex-grid background) being drawn,
    not loaded from image assets. Returns (RGBA tile, accent color) --
    the tile is meant to be glow_paste()'d the same way album art's
    border glow is."""
    accent = _WEATHER_ICON_ACCENTS.get(category, _WEATHER_ICON_ACCENTS["cloudy"])
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    cx = size / 2

    def cloud(cy_frac, scale):
        r = size * 0.16 * scale
        base_y = size * cy_frac
        d.ellipse([cx - r * 2.1, base_y - r * 0.6, cx - r * 0.3, base_y + r * 1.3], fill=accent + (255,))
        d.ellipse([cx - r * 0.8, base_y - r * 1.3, cx + r * 1.1, base_y + r * 1.1], fill=accent + (255,))
        d.ellipse([cx + r * 0.2, base_y - r * 0.5, cx + r * 2.0, base_y + r * 1.3], fill=accent + (255,))
        d.rectangle([cx - r * 2.0, base_y, cx + r * 1.9, base_y + r * 1.2], fill=accent + (255,))

    if category == "clear":
        cy = size / 2
        r = size * 0.22
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=accent + (255,))
        for i in range(8):
            ang = i * math.pi / 4
            x1, y1 = cx + math.cos(ang) * r * 1.35, cy + math.sin(ang) * r * 1.35
            x2, y2 = cx + math.cos(ang) * r * 1.75, cy + math.sin(ang) * r * 1.75
            d.line([x1, y1, x2, y2], fill=accent + (255,), width=max(2, int(size * 0.02)))
    elif category in ("cloudy", "fog"):
        cloud(0.52, 1.0)
        if category == "fog":
            for yf in (0.80, 0.90):
                d.line([cx - size * 0.32, size * yf, cx + size * 0.32, size * yf],
                       fill=accent + (200,), width=max(2, int(size * 0.025)))
    elif category == "rain":
        cloud(0.42, 0.9)
        for dx in (-0.16, 0.0, 0.16):
            x = cx + size * dx
            d.line([x, size * 0.66, x - size * 0.03, size * 0.82],
                   fill=accent + (255,), width=max(2, int(size * 0.025)))
    elif category == "snow":
        cloud(0.42, 0.9)
        for dx in (-0.16, 0.0, 0.16):
            x, y = cx + size * dx, size * 0.76
            s = size * 0.05
            d.line([x - s, y, x + s, y], fill=accent + (255,), width=2)
            d.line([x, y - s, x, y + s], fill=accent + (255,), width=2)
            d.line([x - s * 0.7, y - s * 0.7, x + s * 0.7, y + s * 0.7], fill=accent + (255,), width=2)
            d.line([x - s * 0.7, y + s * 0.7, x + s * 0.7, y - s * 0.7], fill=accent + (255,), width=2)
    else:  # "storm"
        cloud(0.38, 0.9)
        bolt = [cx + size * 0.04, size * 0.60, cx - size * 0.06, size * 0.74,
                cx + size * 0.02, size * 0.74, cx - size * 0.08, size * 0.92]
        d.line(bolt, fill=accent + (255,), width=max(2, int(size * 0.03)), joint="curve")
    return tile, accent


def truncate(draw, text, font, max_w):
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def wrap_text(draw, text, font, max_w, max_lines=None):
    """Greedy word-wrap into lines that each fit max_w -- unlike
    truncate(), nothing is cut off/ellipsized; every word in `text`
    ends up somewhere (unless max_lines is hit, in which case the
    remainder is dropped rather than overflowing)."""
    words = text.split()
    lines = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_w or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
            if max_lines is not None and len(lines) >= max_lines:
                return lines
    if line:
        lines.append(line)
    if max_lines is not None:
        lines = lines[:max_lines]
    return lines


def fmt_mmss(seconds):
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _bar_fill_subtile(full_w, full_h, direction, offset_x, offset_y, sub_w, sub_h, radius, accent, fill_colors):
    """Builds the `sub_w x sub_h` RGBA tile that gets pasted (and glow-
    blurred) as the *currently filled* portion of a bar -- either a
    flat `accent` fill (the original look, `fill_colors` is None) or a
    slice of a `full_w x full_h` multi-stop gradient taken at
    (offset_x, offset_y). Slicing from a *virtual full-size* gradient
    rather than building one sized to just the current fill is what
    keeps a given spot on the bar's own color fixed as the value (and
    so the visible fraction) changes over time -- only how much of the
    gradient is revealed moves, not which colors are where, same as the
    plain accent fill already behaved. The rounded-rect mask is always
    built at the actual `sub_w x sub_h` size, though (not cropped from
    a full-size mask), so the visible end of the fill still gets a
    proper rounded cap exactly like the original flat-fill version --
    cropping the mask too would leave a hard-edged cut instead. `radius`
    is clamped to half of whichever of `sub_w`/`sub_h` is smaller before
    drawing -- a caller passing the *track's* fixed corner radius (half
    its thickness) works fine while the fill is at least that thick,
    but at a low value the filled sliver can be thinner than the radius
    itself, and an unclamped radius there either draws wrong (older
    Pillow) or, on newer Pillow that clamps internally, still isn't
    guaranteed to match exactly how caller code below decides whether
    to draw at all -- clamping here once, ourselves, means this always
    produces a sane pill/round shape no matter how thin the sliver is,
    down to a single filled pixel."""
    radius = max(0.0, min(radius, sub_w / 2, sub_h / 2))
    if fill_colors and len(fill_colors) >= 2:
        grad_full = _linear_gradient_multi(full_w, full_h, fill_colors, direction)
        arr = np.array(grad_full)
        sub_arr = arr[offset_y:offset_y + sub_h, offset_x:offset_x + sub_w]
        color_img = Image.fromarray(sub_arr, "RGB").convert("RGBA")
    else:
        color_img = Image.new("RGBA", (sub_w, sub_h), accent + (255,))
    mask = Image.new("L", (sub_w, sub_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, sub_w - 1, sub_h - 1], radius=radius, fill=255)
    color_img.putalpha(mask)
    return color_img


def progress_bar_glow(img, x, y, w, h, fraction, accent, vertical=False, knob_scale=1.7,
                       show_knob=True, fill_colors=None, fill_direction="horizontal",
                       track_color=None):
    """Draws the filled-track-plus-glowing-knob meter used by both the
    now-playing progress bar (always horizontal, a thin 6px track) and
    the bar element (either orientation, a much thicker ~22px track --
    see _draw_bar_dynamic()). `vertical=True` just swaps which axis the
    fill travels along -- bottom-to-top instead of left-to-right -- and
    which axis the rounded ends/knob sit across; the track rect itself
    is still exactly [x, y, x+w, y+h] either way, so callers don't need
    to swap w/h themselves to get a tall-and-narrow vertical track vs.
    a wide-and-short horizontal one.

    `knob_scale` is the knob's radius as a multiple of the track's own
    thickness -- 1.7 (the original, still the default so the now-
    playing progress bar's look is unchanged) reads fine at that bar's
    thin 6px track, where it works out to a ~20px knob, but the exact
    same multiplier against the bar element's thicker ~22px track blows
    the knob up to a ~75px ball that dwarfs the track it's sitting on
    and swamps the title/value text around it. Callers with a thicker
    track should pass a smaller `knob_scale` to keep the knob reading
    as a knob rather than a blob.

    `show_knob=False` skips the round white handle entirely -- added
    because a thicker bar with its own fill color already showing
    exactly where the value is doesn't need the extra dot the way the
    now-playing progress bar's plain single-color fill does; the
    now-playing bar doesn't pass this, so its knob is unaffected.

    `fill_colors` (2+ RGB tuples) draws the filled portion as a multi-
    stop gradient instead of a flat `accent` color -- see
    _bar_fill_subtile() for how it stays anchored across the whole bar
    as the fraction changes. `fill_direction` ("horizontal" or
    "vertical") is which screen axis the *gradient* runs across,
    independent of `vertical` (which picks which axis the *value*
    fills along) -- a horizontal bar can have a top-to-bottom gradient
    and a vertical bar a left-to-right one, if that's the look wanted.
    `accent` is still what tints the empty track and the glow bloom
    either way, unless `track_color` overrides the track: the empty
    track is otherwise always dim_color(accent, 0.22), which is a
    near-black bar -- right on this theme's usual near-black
    background, but a heavy dark slab on a light-background theme
    (the floral pair), where the empty part of a meter should read
    as a pale channel instead."""
    draw = ImageDraw.Draw(img)
    fraction = max(0.0, min(1.0, fraction))

    if vertical:
        rounded_rect(draw, [x, y, x + w, y + h], radius=w / 2, fill=track_color or dim_color(accent, 0.22))
        fill_h = int(h * fraction)
        # Used to require fill_h > w (the track's own thickness) before
        # drawing anything at all, because _bar_fill_subtile's rounded-
        # rect mask used to choke on a radius (w/2) bigger than the
        # sliver's own height -- which made any value under roughly
        # "thickness / track length" (e.g. ~15% for a typical bar)
        # render as a completely empty track, indistinguishable from
        # 0%, until the value climbed past that threshold. Now that
        # _bar_fill_subtile clamps its own radius, any nonzero fill
        # draws -- down to a small round dot at the very bottom for a
        # tiny fraction, instead of nothing.
        if fill_h > 0:
            sub = _bar_fill_subtile(w, h, fill_direction, 0, h - fill_h, w, fill_h, w / 2, accent, fill_colors)
            tile = Image.new("RGBA", (w + 20, h + 20), (0, 0, 0, 0))
            tile.paste(sub, (10, 10 + (h - fill_h)), sub)
            glow_paste(img, tile, (x - 10, y - 10), blur=5, glow_alpha=0.5)

        if show_knob:
            knob_y = y + h - fill_h
            knob_r = w * knob_scale
            draw = ImageDraw.Draw(img)
            draw.ellipse([x + w / 2 - knob_r, knob_y - knob_r, x + w / 2 + knob_r, knob_y + knob_r],
                         fill=(255, 255, 255))
    else:
        rounded_rect(draw, [x, y, x + w, y + h], radius=h / 2, fill=track_color or dim_color(accent, 0.22))
        fill_w = int(w * fraction)
        # Same fix as the vertical branch above, mirrored: this used to
        # require fill_w > h before drawing anything.
        if fill_w > 0:
            sub = _bar_fill_subtile(w, h, fill_direction, 0, 0, fill_w, h, h / 2, accent, fill_colors)
            tile = Image.new("RGBA", (w + 20, h + 20), (0, 0, 0, 0))
            tile.paste(sub, (10, 10), sub)
            glow_paste(img, tile, (x - 10, y - 10), blur=5, glow_alpha=0.5)

        if show_knob:
            knob_x = x + fill_w
            knob_r = h * knob_scale
            draw = ImageDraw.Draw(img)
            draw.ellipse([knob_x - knob_r, y + h / 2 - knob_r, knob_x + knob_r, y + h / 2 + knob_r],
                         fill=(255, 255, 255))


class Fonts:
    def __init__(self):
        self.gauge_title = load_font(15)
        self.gauge_value = load_font(26)
        self.small_title = load_font(11)   # the compact GPU-temp gauge's title
        self.small_value = load_font(15)   # and its value -- gauge_value is too big for it
        self.tick = load_font(12)
        self.time = load_font(24)
        self.track = load_font(19)
        self.artist = load_font(15, bold=True)
        self.progress = load_font(15)
        self.message = load_font(22)  # the "nothing playing" message -- bigger, meant to be read
        self.weather_temp = load_font(48)
        self.weather_desc = load_font(18)
        self.weather_detail = load_font(14)
        self.weather_location = load_font(14, bold=True)


def _linear_gradient(width, height, top_color, bottom_color):
    """Vectorized top-to-bottom gradient via numpy -- the original
    per-pixel Python double loop (~460k iterations at 960x480) worked
    but there's no reason to pay that cost now that this runs for every
    mode, including ones with no texture drawn on top to hide banding."""
    t = np.linspace(0, 1, height, dtype=np.float32).reshape(height, 1, 1)
    top = np.array(top_color, dtype=np.float32).reshape(1, 1, 3)
    bottom = np.array(bottom_color, dtype=np.float32).reshape(1, 1, 3)
    arr = np.broadcast_to(top + (bottom - top) * t, (height, width, 3))
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _linear_gradient_multi(width, height, colors, direction="horizontal"):
    """A linear gradient across 2 or more evenly-spaced color stops, in
    either screen direction -- generalizes _linear_gradient() (kept
    as-is for its own callers, which only ever need a fixed top-to-
    bottom two-stop gradient) for the bar element's customizable fill,
    which needed both "more than two colors" and "not just top-to-
    bottom" (see _draw_bar_dynamic() and progress_bar_glow()).
    `direction="horizontal"` varies left-to-right across `width` and is
    constant down `height`; `"vertical"` is the reverse. A single color
    (len(colors) == 1) is just a flat fill, handled the same way rather
    than special-cased, so a caller can always call this uniformly."""
    cols = np.array(colors, dtype=np.float32)
    n = len(cols)
    if n == 1:
        arr = np.broadcast_to(cols[0].reshape(1, 1, 3), (height, width, 3))
        return Image.fromarray(arr.astype(np.uint8), "RGB")
    axis_len = height if direction == "vertical" else width
    # `t` walks 0..n-1 across the axis; `lo`/`frac` pick which pair of
    # adjacent stops each position falls between and how far along that
    # one segment it is -- same idea as a multi-stop CSS/SVG gradient,
    # just done as one vectorized numpy pass instead of per-pixel.
    t = np.linspace(0, 1, axis_len, dtype=np.float32) * (n - 1)
    lo = np.clip(np.floor(t).astype(np.int32), 0, n - 2)
    frac = (t - lo).reshape(-1, 1)
    seg = cols[lo] + (cols[lo + 1] - cols[lo]) * frac  # (axis_len, 3)
    if direction == "vertical":
        arr = np.broadcast_to(seg.reshape(height, 1, 3), (height, width, 3))
    else:
        arr = np.broadcast_to(seg.reshape(1, width, 3), (height, width, 3))
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _radial_gradient(width, height, center_color, edge_color):
    """A soft glow centered on the panel, fading to `edge_color` at the
    corners -- vectorized the same way as _linear_gradient() above."""
    yy, xx = np.mgrid[0:height, 0:width]
    cx, cy = width / 2, height / 2
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    t = np.clip(dist / math.hypot(cx, cy), 0, 1)[..., np.newaxis]
    center = np.array(center_color, dtype=np.float32)
    edge = np.array(edge_color, dtype=np.float32)
    arr = center + (edge - center) * t
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def draw_starfield(draw, width, height, seed=11, count=140):
    """A scattering of small static "stars" -- deterministic (fixed
    seed) so the pattern doesn't visibly change between Starts, same as
    the hex grid/circuit traces it stands in for as a lighter-weight,
    less "circuit board" alternative texture."""
    rng = random.Random(seed)
    for _ in range(count):
        x, y = rng.randint(0, width), rng.randint(0, height)
        r = rng.choice((0.6, 0.6, 0.8, 1.0, 1.4))
        b = rng.randint(110, 230)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(b, b, min(255, b + 15)))


def draw_simple_grid(draw, width, height, spacing=44, color=(70, 76, 100)):
    """A plain rectangular grid -- a calmer alternative to the hex grid
    for anyone who wants *some* structure without the full "circuit
    board" look."""
    for x in range(0, width, spacing):
        draw.line([(x, 0), (x, height)], fill=color, width=1)
    for y in range(0, height, spacing):
        draw.line([(0, y), (width, y)], fill=color, width=1)


def _build_background_image(width, height, background):
    """The part of the panel background that varies by BACKGROUND_PRESETS
    mode -- everything else in build_static_background() (border, gauges)
    is drawn on top of whatever this returns.

    "default": a gradient (see BACKGROUND_COLOR_SCHEMES) plus the
    hex-grid + circuit-trace texture. "grid"/"starfield": the same
    gradient with a lighter-weight texture instead. "radial": a glow
    centered on the panel rather than a top-to-bottom gradient. "solid":
    just the gradient, no texture at all. "image": a user-supplied
    photo, cover-fit to the panel and darkened -- the gauges/text here
    are all designed to sit on a near-black background, so a bright
    photo behind them unmodified would wreck their legibility; a bad or
    missing path (or an unreadable file) just falls back to "default"
    silently rather than failing the whole theme. "aurora"/"nebula"/
    "synthwave"/"bokeh" (BUNDLED_BACKGROUND_IMAGES): the app's own
    shipped pictures, resolved to a real file below and then rendered
    through that exact same "image" cover-fit + darken path."""
    mode = (background or {}).get("mode", "default")
    image_path = (background or {}).get("image_path")

    bundled_name = BUNDLED_BACKGROUND_IMAGES.get(mode)
    if bundled_name:
        mode = "image"
        image_path = resource_path("backgrounds", bundled_name)

    if mode == "image" and image_path:
        try:
            src = Image.open(image_path).convert("RGB")
            img = ImageOps.fit(src, (width, height), method=Image.LANCZOS)
            # `background["dim"]` (0-1) is how far the picture gets
            # blended toward black before anything is drawn on top.
            # 0.45 stays the default because that's what an arbitrary
            # *photo* needs to sit under bright gauges/text without
            # wrecking their legibility. A background designed FOR this
            # panel is a different case: the card-style themes below
            # draw their own dark panels at exactly the contrast they
            # want, and darkening those again just greys out a design
            # that was already correct, so those ship with dim: 0.
            dim = (background or {}).get("dim")
            dim = 0.45 if dim is None else max(0.0, min(1.0, float(dim)))
            if dim <= 0:
                return img
            overlay = Image.new("RGB", (width, height), (0, 0, 0))
            return Image.blend(img, overlay, dim)
        except Exception:  # noqa: BLE001 -- bad/missing file, corrupt image, etc.
            mode = "default"

    scheme = BACKGROUND_COLOR_SCHEMES.get((background or {}).get("scheme"),
                                            BACKGROUND_COLOR_SCHEMES[DEFAULT_SCHEME])
    top_color, bottom_color = scheme["top"], scheme["bottom"]

    if mode == "radial":
        return _radial_gradient(width, height, top_color, bottom_color)

    img = _linear_gradient(width, height, top_color, bottom_color)
    if mode == "default":
        draw = ImageDraw.Draw(img)
        draw_hex_grid(draw, width, height)
        draw_circuit_traces(draw, width, height)
    elif mode == "grid":
        draw_simple_grid(ImageDraw.Draw(img), width, height)
    elif mode == "starfield":
        draw_starfield(ImageDraw.Draw(img), width, height)
    # mode == "solid": the gradient above, with nothing drawn over it.
    return img


def build_static_background(width, height, fonts, elements=None, background=None):
    """Everything that doesn't change frame to frame: the background
    (see _build_background_image()/BACKGROUND_PRESETS), the panel
    border, and every gauge element's dim track/ticks/title. Returns
    (image, layout).

    `elements` is a list of dicts in the shape slots_to_elements()
    produces (id, type, stat, x, y, radius, rotation, color, opacity, z
    -- x/y/radius as fractions of width/height/min(width, height), so
    the same list renders correctly at any panel resolution); defaults
    to DEFAULT_ELEMENTS if not given. Only `type: "gauge"` elements are
    drawn -- an unrecognized type is skipped rather than erroring, so a
    future element type (Phase 6) added by a newer canvas doesn't crash
    an older renderer reading the same config. `background` maps "mode"
    (a BACKGROUND_PRESETS key), "scheme" (a BACKGROUND_COLOR_SCHEMES
    key), and "image_path"; missing entries fall back to
    DEFAULT_BACKGROUND. Both are baked into this static image, so
    changing either mid-stream needs a fresh call to this (i.e. a
    Stop/Start, or the GUI's Apply button) to take effect -- same as
    every other "needs a restart" setting in this theme.
    """
    elements = DEFAULT_ELEMENTS if elements is None else elements
    background = dict(DEFAULT_BACKGROUND, **(background or {}))
    elements = [widget_styles.resolve_element(el, background) for el in elements]

    img = _build_background_image(width, height, background)
    draw = ImageDraw.Draw(img)

    # The panel's own frame. Fixed purple for every theme until now,
    # which fought any preset whose palette wasn't purple-ish (it read
    # as chrome bolted on around the design rather than part of it).
    # `background["border"]` now overrides it per theme: an [r, g, b]
    # to recolor it, or "none"/False to drop it entirely for a design
    # that draws its own framing (the card-based ones do). Missing =
    # the original purple, so every existing preset is untouched.
    margin = int(width * 0.015)  # also the inset other layout math below works from
    border = (background or {}).get("border", "default")
    if border not in ("none", False):
        border_color = tuple(border) if isinstance(border, (list, tuple)) else dim_color(PANEL_BORDER, 0.7)
        if background.get("widget_style") in widget_styles.STYLES:
            widget_styles.draw_frame(img, border_color, background["widget_style"])
        else:
            rounded_rect(draw, [margin, margin, width - margin, height - margin],
                         radius=10, outline=border_color, width=2)

    base = min(width, height)
    resolved = {}
    ordered = sorted(elements, key=lambda el: el.get("z", 0))
    for el in ordered:
        etype = el.get("type", "gauge")
        if etype == "gauge":
            g = gauge_layout(el["x"] * width, el["y"] * height, el["radius"] * base)
            resolved[el["id"]] = g
            accent = _element_accent(el)
            gauge_gradient, gauge_gradient_dir = _element_gauge_gradient(el)
            title = STAT_DEFS[el["stat"]]["title"]
            if el.get("widget_style") in widget_styles.STYLES:
                widget_styles.draw_gauge_static(img, el, g, title, _cached_scaled_font)
                continue
            tile = draw_gauge_static(g, accent, gradient_colors=gauge_gradient, gradient_direction=gauge_gradient_dir)
            tile = _apply_tile_opacity(tile, el.get("opacity", 1.0))
            # `rotation` is stored and round-trips through config/
            # migration, but isn't actually applied to the drawing yet
            # -- the needle and value text drawn per-frame in
            # render_frame() would need to rotate in lockstep with the
            # ring for a rotated gauge to look right, and nothing can
            # set a non-zero rotation until Phase 5's canvas exists
            # anyway. DEFAULT_ELEMENTS' rotation is always 0, so this
            # doesn't change today's output.
            img.paste(tile, _gauge_box(g), tile)
            if not el.get("show_title", True):
                # Same opt-out the bar and graph elements have (see
                # _draw_bar_static()): a card-style layout that already
                # heads its own panel -- and sets that heading in its
                # own display face -- doesn't want a second label in
                # the theme's fallback sans stacked above the ring.
                continue
            if g["radius"] >= base * BIG_GAUGE_RADIUS_FRACTION:
                # Full tick labels + a title tucked inside the ring,
                # same as the old "big" slots.
                draw_tick_labels(draw, g, fonts.tick)
                draw.text((g["cx"], g["cy"] - g["radius"] * 0.42), title, font=fonts.gauge_title,
                           fill=(225, 226, 236), anchor="mm")
            else:
                # Compact: title above the ring, no tick labels -- gap
                # scales with the gauge's own radius (continuous,
                # replacing the old "secondary" vs "mini" kind
                # distinction) rather than a second hardcoded threshold;
                # see BIG_GAUGE_RADIUS_FRACTION's comment for how this
                # was calibrated to match the old look.
                label_gap = g["radius"] * 0.37
                draw.text((g["cx"], g["cy"] - g["radius"] - label_gap), title, font=fonts.small_title,
                           fill=(225, 226, 236), anchor="mm")
        elif etype == "text":
            # A stat-bound text element (_text_element_is_dynamic()) is
            # redrawn every frame in render_frame() instead, so its
            # reading stays live -- baking today's value in here would
            # freeze it at whatever the panel happened to read at
            # Start/Apply time.
            if not _text_element_is_dynamic(el):
                _draw_text_element(img, el, width, height)
        elif etype == "image":
            _draw_image_element(img, el, width, height)
        elif etype == "graph":
            box = _graph_box(el, width, height)
            resolved[el["id"]] = box
            _draw_graph_static(img, el, box, fonts)
        elif etype == "bar":
            box = _bar_box(el, width, height)
            resolved[el["id"]] = box
            _draw_bar_static(img, el, box, fonts)
        # Any other/unrecognized type is skipped rather than erroring --
        # see this function's own docstring for why (a newer canvas's
        # element type shouldn't crash an older renderer).

    col_w = int(width * 0.235)
    mid_x0 = margin + col_w + int(width * 0.03)
    mid_x1 = width - margin - col_w - int(width * 0.03)
    clock_cy = int(height * 0.885)

    layout = {
        "elements": elements,
        "resolved": resolved,
        "clock_cy": clock_cy,
        "mid_x0": mid_x0, "mid_x1": mid_x1, "mid_w": mid_x1 - mid_x0,
    }
    return img, layout


def _weather_content_height(el, box_w):
    """This weather element's *enabled* pieces' natural (unscaled)
    combined height -- the same stack _draw_weather_element() draws
    (icon, temp, description, details, location) at their normal, full
    size, assuming the "has data" case since a box's size can't depend
    on whether a weather lookup happens to have succeeded by the time a
    given frame renders. Purely a sizing input for
    _draw_weather_element()'s own fit-to-box scaling (see that
    function's docstring) -- this does NOT get used to resize the box
    itself; the box is always exactly what `el["width"]`/`el["height"]`
    say, full stop, so an element never grows into whatever's placed
    next to it. Icon size here is only capped by the box's own *width*
    (mid_w * 0.4), not its height, since this same value also feeds
    _draw_weather_element()'s scale-factor calculation, which needs a
    height-independent baseline to compare the box's actual height
    against. `estimateWeatherHeight()` on the frontend mirrors this
    exact formula, purely as a starting suggestion when a show_*
    checkbox is toggled -- not a floor, either."""
    icon_size = box_w * 0.4
    content_h = 0.0
    if el.get("show_icon", True):
        content_h += icon_size + 10
    if el.get("show_temp", True):
        content_h += 54
    if el.get("show_description", True):
        content_h += 26
    if el.get("show_details", True):
        content_h += 22
    if el.get("show_location", True):
        content_h += 20
    return content_h


def _weather_box(el, width, height):
    """A weather element's pixel box, from its center x/y and its own
    width/height -- same idea as _media_box()/_graph_box(). The box is
    exactly what `el["width"]`/`el["height"]` say (same 60px floor
    every other resizable element box uses) -- it is never grown to fit
    the content (an earlier version of this function did that, and it
    backfired: growing a box to fit every enabled piece at full size
    routinely made it balloon well past what its own width/height
    fields said, overlapping whatever the element next to it was, which
    read as just as broken as the overflow it was meant to fix, only
    now the box's own resize handles lied about its actual footprint).
    See _draw_weather_element()'s docstring for how content instead
    scales itself down to fit whatever box this returns, so nothing
    overflows *or* forces the box bigger."""
    w = max(60.0, el.get("width", _DEFAULT_WEATHER_W) * width)
    h = max(60.0, el.get("height", _DEFAULT_WEATHER_H) * height)
    cx, cy = el["x"] * width, el["y"] * height
    x0, y0 = cx - w / 2, cy - h / 2
    return {"x0": x0, "y0": y0, "w": w, "h": h, "cx": cx, "cy": cy}


def _draw_weather_element(img, el, box, fonts):
    """Current-conditions readout (weather.py) -- an icon, the
    temperature, a one-line description, and a couple of secondary
    details, all centered in the element's own box. Movable/resizable,
    like _draw_media_element() -- see that function's docstring and
    default_weather_element()'s for the history here (this used to be
    a global "middle_content" choice pinned to a fixed column). Any
    missing piece (no location set yet, a lookup that hasn't completed,
    a geocoding failure) degrades to a short centered message instead
    of a half-drawn readout, the same tolerant-fallback style as
    _draw_media_element()'s "nothing playing" placeholder. Fully
    dynamic (weather.py's background poll can update `info` at any
    time) so, like media, this is redrawn here in render_frame() every
    frame rather than baked into the static background.

    `show_icon`/`show_temp`/`show_description`/`show_details`/
    `show_location` (each default True) let each piece be switched off
    independently -- see default_weather_element()'s docstring.
    Whichever pieces are on stack top-to-bottom in that same order,
    vertically centered as a block in the box (see the content_h
    pre-measurement below) rather than always starting flush with the
    box's top edge -- same "top-anchoring left a growing gap under the
    content, so two boxes lined up edge-to-edge didn't line up their
    visible content" reasoning as _draw_media_element()'s docstring.
    Turning a piece off still doesn't leave a gap where it used to be.

    The whole stack (icon size, every font size, every row's height)
    scales down together -- by the same `scale` factor -- whenever the
    enabled pieces' natural, full-size combined height would be taller
    than the box actually is (see `scale` below). This is deliberately
    a scale-to-fit, not a grow-the-box-to-fit: an earlier version of
    this function's box (_weather_box()) grew itself to whatever the
    content needed, which routinely made a small element's real
    on-panel footprint balloon well past its own width/height fields
    and overlap whatever sat next to it -- exactly the kind of overlap
    this element itself is trying not to cause. Scaling the content
    down instead means the box is always exactly what its width/height
    fields say (same as every other resizable element), and the
    content simply never exceeds it -- a very small box just reads a
    compact readout instead of a full-size one, rather than either
    overflowing or forcing its neighbors to move."""
    mid_cx, mid_w, box_h = box["cx"], box["w"], box["h"]
    opacity = el.get("opacity", 1.0)
    show_icon = el.get("show_icon", True)
    show_temp = el.get("show_temp", True)
    show_description = el.get("show_description", True)
    show_details = el.get("show_details", True)
    show_location = el.get("show_location", True)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    info = weather.get_weather()
    icon_size_natural = min(mid_w * 0.4, box_h * 0.32)
    has_data = bool(info) and not info.get("error") and info.get("temperature") is not None

    if not has_data:
        # No icon/numbers to show either way -- but a text message only
        # makes sense if at least one text piece is actually switched
        # on (an icon-only element with no data yet just stays blank
        # rather than filling its small box with a paragraph). Capped
        # to however many lines actually fit the box, same "never
        # overflow, just show less" principle as the has-data path
        # below -- a message that's still cut off after wrapping to
        # width just loses its tail rather than spilling past box_h.
        icon_gap = int(icon_size_natural // 3)
        message_lines = []
        if show_temp or show_description or show_details or show_location:
            if not (el.get("location") or "").strip():
                message = "Set a location in this element's properties to show weather here"
            else:
                message = (info or {}).get("error") or "Weather unavailable"
            max_lines = max(1, int((box_h - icon_gap) // 28))
            message_lines = wrap_text(draw, message, fonts.message, mid_w - 16, max_lines=max_lines)

        content_h = (icon_gap + 28 * len(message_lines)) if message_lines else 0
        y = int(box["y0"] + max(0, (box_h - content_h) / 2))
        if message_lines:
            y += icon_gap
            for line in message_lines:
                draw.text((mid_cx, y), line, font=fonts.message, fill=(200, 190, 220), anchor="ma")
                y += 28
    else:
        units_symbol = "°F" if info.get("units") == "fahrenheit" else "°C"
        description = (info.get("description") or "") if show_description else ""
        detail_bits = []
        if show_details:
            if info.get("feels_like") is not None:
                detail_bits.append(f"Feels {round(info['feels_like'])}{units_symbol}")
            if info.get("humidity") is not None:
                detail_bits.append(f"{round(info['humidity'])}% humidity")
        location_text = info.get("location_name") if (show_location and info.get("location_name")) else None

        # Pre-measure the enabled (and actually present -- a
        # description/detail/location line with no data to show
        # doesn't reserve space) pieces' *natural*, full-size combined
        # height, then scale everything down together if that's taller
        # than the box -- see this function's own docstring.
        content_h_natural = 0.0
        if show_icon:
            content_h_natural += icon_size_natural + 10
        if show_temp:
            content_h_natural += 54
        if description:
            content_h_natural += 26
        if detail_bits:
            content_h_natural += 22
        if location_text:
            content_h_natural += 20

        # 0.55 is a floor, not a target -- below that, text stops being
        # legible on a 960x480 panel, so a box too small even for the
        # scaled-down minimum just quietly clips rather than shrinking
        # into unreadable soup.
        scale = 1.0
        if content_h_natural > box_h and content_h_natural > 0:
            scale = max(0.55, box_h / content_h_natural)

        icon_size = int(icon_size_natural * scale)
        row_icon = int(round((icon_size_natural + 10) * scale)) if show_icon else 0
        row_temp = int(round(54 * scale))
        row_desc = int(round(26 * scale))
        row_details = int(round(22 * scale))
        row_location = int(round(20 * scale))

        if scale >= 0.999:
            # The common case (content already fits): reuse the
            # pre-built Fonts instance exactly as before, byte-for-byte
            # the same output as prior to this scaling logic existing.
            font_temp, font_desc, font_details, font_location = (
                fonts.weather_temp, fonts.weather_desc, fonts.weather_detail, fonts.weather_location)
        else:
            font_temp = _cached_scaled_font(max(10, int(48 * scale)))
            font_desc = _cached_scaled_font(max(9, int(18 * scale)))
            font_details = _cached_scaled_font(max(8, int(14 * scale)))
            font_location = _cached_scaled_font(max(8, int(14 * scale)), bold=True)

        content_h = 0
        if show_icon:
            content_h += row_icon
        if show_temp:
            content_h += row_temp
        if description:
            content_h += row_desc
        if detail_bits:
            content_h += row_details
        if location_text:
            content_h += row_location

        y = int(box["y0"] + max(0, (box_h - content_h) / 2))

        if show_icon:
            tile, accent = _weather_icon_tile(info.get("icon", "cloudy"), icon_size)
            icon_x = int(mid_cx - icon_size / 2)
            glow_paste(layer, tile, (icon_x, y), blur=8, glow_alpha=0.5)
            layer.paste(tile, (icon_x, y), tile)
            y += row_icon

        if show_temp:
            temp = info.get("temperature")
            draw.text((mid_cx, y), f"{round(temp)}{units_symbol}", font=font_temp,
                       fill=(238, 238, 244), anchor="ma")
            y += row_temp

        if description:
            draw.text((mid_cx, y), description, font=font_desc, fill=(210, 202, 230), anchor="ma")
            y += row_desc

        if detail_bits:
            draw.text((mid_cx, y), "  ·  ".join(detail_bits), font=font_details,
                       fill=(170, 165, 190), anchor="ma")
            y += row_details

        if location_text:
            draw.text((mid_cx, y + 4), location_text.upper(), font=font_location,
                       fill=(150, 145, 175), anchor="ma")

    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


def _media_box(el, width, height):
    """A media (now-playing) element's pixel box, from its center x/y
    and its own width/height -- same idea as _graph_box()/_element's
    image sizing, just for the widget that used to be pinned to a fixed
    middle column and always-on (the old "spotify" middle_content
    option, since removed entirely). Kept generous
    by default (see makeElement()'s default shape on the frontend, and
    default_media_element() above) since it has to fit album art *and*
    two lines of text *and* a progress bar stacked vertically."""
    w = max(60.0, el.get("width", 0.32) * width)
    h = max(60.0, el.get("height", 0.52) * height)
    cx, cy = el["x"] * width, el["y"] * height
    x0, y0 = cx - w / 2, cy - h / 2
    return {"x0": x0, "y0": y0, "w": w, "h": h, "cx": cx, "cy": cy}


def _draw_media_element(img, el, box, media, fonts):
    """The Spotify now-playing widget (album art + track/artist +
    progress bar), but as its own movable/resizable element instead of
    being pinned to the fixed middle column -- for anyone who wants it
    somewhere other than dead center, or wants it alongside a `weather`
    element (also its own movable/resizable element -- see
    default_weather_element()) rather than instead of it. Fully
    dynamic (playback position advances every frame, same as a graph's
    plotted line) so it's redrawn here in render_frame(), never baked
    into the static background the way text/image elements are.

    `show_art`/`show_name`/`show_time` (each default True) let each of
    the three pieces -- cover art, track/artist text, progress bar -- be
    switched off independently, since not everyone wants all three
    (e.g. just the art, or just a compact time readout with no cover
    taking up space). Whichever pieces are on stack top-to-bottom in
    that same order, vertically centered as a block in the box (see
    the content_h pre-measurement below) rather than always starting
    flush with the box's top edge -- top-anchoring left a growing gap
    under the content whenever the box was taller than the content
    needed (the common case: the default box is generously sized to
    fit every piece, so turning pieces off, or just not filling a tall
    box, left dead space at the bottom), which meant lining up two
    elements' boxes edge-to-edge didn't actually line up their visible
    content -- the whole point of dragging boxes next to each other.
    Turning a piece off still doesn't leave a gap where it used to be,
    same as before."""
    if el.get("widget_style") in widget_styles.STYLES:
        widget_styles.draw_media(img, el, box, media, _cached_scaled_font,
                                _load_default_art(), _not_playing_message or "Awaiting Spotify")
        return
    mid_cx, mid_w, box_h = box["cx"], box["w"], box["h"]
    opacity = el.get("opacity", 1.0)
    show_art = el.get("show_art", True)
    show_name = el.get("show_name", True)
    show_time = el.get("show_time", True)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    title = artist = None
    position = duration = None
    if media:
        title, artist = media.get("title"), media.get("artist")
        position, duration = media.get("position"), media.get("duration")

    # However many of the other two pieces are also on, sizes the art
    # off the box's remaining space so all of them fit -- art-only
    # (both others off) lets it use nearly the whole box. Computed once
    # up front (rather than inside the `if show_art` block below) since
    # the pre-measurement pass right after needs it too.
    art_frac = 0.9 if not (show_name or show_time) else 0.55
    art_size = int(max(24, min(mid_w * 0.75, box_h * art_frac))) if show_art else 0

    # Pre-measure exactly how tall the enabled pieces are actually
    # going to render (same deltas the drawing pass below applies), so
    # the whole stack can start centered in the box instead of glued to
    # its top -- see this function's docstring.
    content_h = (art_size + 12) if show_art else 0
    not_playing_lines = None
    if show_name:
        if title:
            content_h += 24 + (22 if artist else 0)
        elif _MEDIA_OK:
            not_playing_lines = wrap_text(draw, get_not_playing_message(), fonts.message, mid_w - 12)
            content_h += 24 * len(not_playing_lines)
    if show_time and title and duration:
        content_h += 26  # bar_y offset (4) + bar_h (6) + gap to the time labels (16)

    y = int(box["y0"] + max(0, (box_h - content_h) / 2))
    bottom = box["y0"] + box_h

    if show_art:
        art = None
        if media and media.get("art") is not None:
            art = fit_album_art(media["art"], art_size, radius=14)
        if art is None:
            art = default_art(art_size, radius=14)

        art_x = int(mid_cx - art_size / 2)
        art_y = int(y)
        glow_tile, glow_pad = art_glow_frame(art_size, 14, ACCENT_MID)
        glow_paste(layer, glow_tile, (art_x - glow_pad, art_y - glow_pad), blur=8, glow_alpha=0.55)
        # `art` comes back as plain RGB (fit_album_art()/default_art()),
        # so -- unlike the fixed-column version, which pastes straight
        # onto an opaque background and needs no mask -- pasting it onto
        # this transparent RGBA layer needs an explicit opaque mask, or
        # the art would come through with alpha 0 (invisible) once
        # opacity is applied below.
        art_rgba = art.convert("RGBA")
        layer.paste(art_rgba, (art_x, art_y), art_rgba)
        y = art_y + art_size + 12

    if show_name:
        if title:
            track_line = truncate(draw, title.upper(), fonts.track, mid_w - 12)
            draw.text((mid_cx, y), track_line, font=fonts.track, fill=(238, 238, 244), anchor="ma")
            y += 24
            if artist:
                artist_line = truncate(draw, artist, fonts.artist, mid_w - 12)
                draw.text((mid_cx, y), artist_line, font=fonts.artist, fill=(200, 192, 220), anchor="ma")
                y += 22
        elif _MEDIA_OK:
            for line in not_playing_lines:
                if y > bottom:
                    break
                draw.text((mid_cx, y), line, font=fonts.message, fill=(200, 190, 220), anchor="ma")
                y += 24

    if show_time and title and duration and y + 20 <= bottom:
        bar_w = int(mid_w * 0.85)
        bar_h = 6
        bar_x = int(mid_cx - bar_w / 2)
        bar_y = y + 4
        fraction = max(0.0, min(1.0, (position or 0.0) / duration))
        progress_bar_glow(layer, bar_x, bar_y, bar_w, bar_h, fraction, ACCENT_MID)
        y = bar_y + bar_h + 16
        if y <= bottom:
            draw.text((bar_x, y), fmt_mmss(position), font=fonts.progress,
                       fill=(170, 165, 190), anchor="lm")
            draw.text((bar_x + bar_w, y), fmt_mmss(duration), font=fonts.progress,
                       fill=(170, 165, 190), anchor="rm")

    layer = _apply_tile_opacity(layer, opacity)
    img.paste(layer, (0, 0), layer)


def render_frame(background, layout, width, height, fonts, stats, media, history=None):
    """`stats` is a flat dict keyed by STAT_DEFS key -- any key can be
    missing or None, which just draws that gauge's dim track with no
    needle and "--" (see draw_gauge_dynamic_tile). Every gauge element
    reads from this same dict via its own `stat`, since any stat can be
    bound to any element; `layout["resolved"]` (built by
    build_static_background(), keyed by element id) is where each
    element's actual on-panel position/size ended up.

    `history` (ROADMAP.md Phase 6) is a dict of element id -> a
    sequence of that element's bound stat's recent values, oldest
    first -- only graph elements read it, and only run()'s loop
    actually maintains one (see its own comment on the per-element
    deques it keeps across frames); every other caller either omits it
    entirely or passes an empty dict, which just draws each graph's
    static border with nothing plotted inside it yet."""
    img = background.copy()
    base = min(width, height)
    history = history or {}

    # Sorted by `z` here for the same reason build_static_background()
    # sorts before baking text/image elements into the background image
    # -- without it, Bring to front/Send to back only ever affected
    # text/image (the only element types that are fully static and so
    # actually go through that sorted loop), while every dynamic
    # element (gauge, graph, bar, media, clock, weather -- everything
    # redrawn fresh every frame right here) was always drawn back-to-
    # front in whatever order it happened to sit in `elements`,
    # regardless of `z`. That's most of what a real dashboard is made
    # of, so it read as the buttons doing nothing at all.
    for el in sorted(layout["elements"], key=lambda el: el.get("z", 0)):
        etype = el.get("type", "gauge")
        if etype == "gauge":
            g = layout["resolved"][el["id"]]
            stat = STAT_DEFS[el["stat"]]
            if el.get("widget_style") in widget_styles.STYLES:
                widget_styles.draw_gauge_dynamic(img, el, g, stats.get(el["stat"]), stat, _cached_scaled_font)
                continue
            accent = _element_accent(el)
            gauge_gradient, gauge_gradient_dir = _element_gauge_gradient(el)
            big = g["radius"] >= base * BIG_GAUGE_RADIUS_FRACTION
            value_font = fonts.gauge_value if big else fonts.small_value
            draw_gauge_dynamic(img, g, stats.get(el["stat"]), stat["min"], stat["max"],
                                accent, value_font, stat["fmt"],
                                gradient_colors=gauge_gradient, gradient_direction=gauge_gradient_dir)
        elif etype == "graph":
            box = layout["resolved"].get(el["id"])
            if box is None:
                continue
            accent = _element_color(el, default=ACCENT_CPU)
            graph_gradient = _element_gradient_colors(el) if el.get("gradient") else None
            _draw_graph_dynamic(img, el, box, history.get(el["id"], ()), accent,
                                 graph_gradient, el.get("gradient_direction", "horizontal"))
        elif etype == "bar":
            box = layout["resolved"].get(el["id"])
            if box is None:
                continue
            stat_def = STAT_DEFS.get(el.get("stat"))
            if stat_def is None:
                continue
            if el.get("widget_style") in widget_styles.STYLES:
                widget_styles.draw_bar(img, el, box, stats.get(el["stat"]), stat_def, _cached_scaled_font)
                continue
            accent = _element_color(el, default=ACCENT_CPU)
            _draw_bar_dynamic(img, el, box, stats.get(el["stat"]), stat_def["min"], stat_def["max"],
                               accent, fonts.small_value, stat_def["fmt"])
        elif etype == "media":
            box = _media_box(el, width, height)
            _draw_media_element(img, el, box, media, fonts)
        elif etype == "clock":
            _draw_clock_element(img, el, width, height, fonts)
        elif etype == "weather":
            box = _weather_box(el, width, height)
            _draw_weather_element(img, el, box, fonts)
        elif etype == "text" and _text_element_is_dynamic(el):
            _draw_text_element(img, el, width, height, stats)
        # A free-standing (non-stat-bound) text element, and every
        # image element, are fully static -- baked into `background`
        # already, nothing to redraw here.

    return img


# Fixed, plausible demo values used only for render_preset_thumbnail()
# below -- never a real hardware/psutil reading. Picked mid-range
# rather than 0 or blank so every gauge/bar in a thumbnail actually
# shows a visible fill instead of looking broken or empty; fixed
# (not randomized) so the same preset always renders the same
# thumbnail rather than jittering on every page load.
_THUMBNAIL_STATS = {
    "cpu_load": 42, "gpu_load": 55, "gpu_temp": 58, "ram": 61,
    "network": 12.4, "cpu_freq": 3.6, "disk_usage": 47, "vram_usage": 38,
    "swap": 5, "disk_io": 8.2, "gpu_power": 95, "process_count": 210,
    "cpu_load_peak": 68, "battery": 80, "volume": 45,
}

_thumbnail_fonts_cache = None


def _thumbnail_fonts():
    """A single shared Fonts() instance (loading fonts from disk isn't
    free) reused across every render_preset_thumbnail() call in this
    process -- fine to share since Fonts() is read-only once built and
    a thumbnail render never mutates it, same reasoning run()'s own
    `fonts = Fonts()` (built once per Start, not per frame) already
    relies on."""
    global _thumbnail_fonts_cache
    if _thumbnail_fonts_cache is None:
        _thumbnail_fonts_cache = Fonts()
    return _thumbnail_fonts_cache


def render_preset_thumbnail(elements, background, width=480, height=240):
    """Renders a small preview image of `elements` on `background` --
    what actually shows in the web UI's preset picker (DashboardCanvas.
    jsx) so a person can see what a saved preset looks like before
    loading it, instead of picking a name blind out of a dropdown.

    Deliberately reuses the exact same build_static_background()/
    render_frame() pipeline the real panel renders through, rather than
    a separate lightweight mock -- a thumbnail that drew gauges/bars/
    text some other way could quietly drift from what Start/Apply
    actually shows, which would make the picker actively misleading
    instead of merely absent. The only inputs a thumbnail needs that a
    live render doesn't: `_THUMBNAIL_STATS` (fixed, plausible numbers)
    stand in for psutil/GPU readings no panel connection or hardware
    poll is needed to produce here; `media=None` reuses the theme's own
    existing "nothing playing" placeholder rendering rather than a
    separate fake-track mock; and each graph element gets a short
    synthetic wave instead of an empty history deque, so its line
    actually shows rather than rendering as a flat, empty box.

    Renders internally at REFERENCE_WIDTH/HEIGHT (960x480), then resizes
    down to `width`x`height` (480x240 by default -- half that) at the
    end, rather than building straight at the small size: every
    element's x/y/radius/etc. is stored as a *fraction* of the
    reference size (see REFERENCE_WIDTH/HEIGHT's own comment) and
    scales cleanly either way, but `Fonts()` is a set of fixed *pixel*
    sizes calibrated for the reference resolution (`gauge_title =
    load_font(15)`, not "15 * some scale factor") -- rendering straight
    onto a half-size canvas kept those fonts at their full absolute
    size, so a gauge's title text came out roughly twice as big
    relative to its own ring as on the real panel, clipping off its
    first couple of letters ("CPU LOAD" as "PU LOAD"). Rendering at the
    reference size first and resizing the *finished image* down keeps
    every proportion (font-to-gauge included) identical to a real
    render, at the cost of one extra (cheap) resize step."""
    fonts = _thumbnail_fonts()
    bg_image, layout = build_static_background(REFERENCE_WIDTH, REFERENCE_HEIGHT, fonts, elements, background)
    history = {}
    for el in elements:
        if el.get("type") != "graph":
            continue
        n = 12
        base_val = _THUMBNAIL_STATS.get(el.get("stat")) or 50
        history[el["id"]] = deque(
            (max(0.0, min(100.0, base_val + 15 * math.sin(i / 2))) for i in range(n)),
            maxlen=n,
        )
    img = render_frame(bg_image, layout, REFERENCE_WIDTH, REFERENCE_HEIGHT, fonts, _THUMBNAIL_STATS, None, history)
    if (width, height) != (REFERENCE_WIDTH, REFERENCE_HEIGHT):
        img = img.resize((width, height), Image.LANCZOS)
    return img


def render_live_preview(elements, background, width=REFERENCE_WIDTH, height=REFERENCE_HEIGHT):
    """Same render_frame() pipeline as render_preset_thumbnail() just
    above, but with this machine's actual CURRENT stats (the same
    psutil/SystemInfos.exe/pynvml/winsdk calls run()'s own render loop
    makes every frame) in place of _THUMBNAIL_STATS' fixed illustrative
    numbers.

    This is what backs the design canvas's "preview what I'm currently
    editing" backdrop (controller.py's render_dashboard_live_preview())
    for whenever there's an unsaved edit the real live panel photo
    doesn't reflect yet: a static thumbnail rendered once when a preset
    is first loaded was a genuine improvement over the broken double-
    exposure it replaced (see ROADMAP.md), but it's still a frozen
    picture -- needles that don't move and a live network/CPU reading
    that's visibly wrong within a second reads as "this is broken", not
    "this hasn't been saved yet". Called repeatedly (the canvas polls
    it on an interval, same idea as the real live frame's own polling)
    with genuinely fresh numbers each time is what makes gauges/graphs
    actually move again while still showing the *new* design instead of
    the old one.

    Deliberately does NOT touch history.py-style persistent state for a
    graph element's trend line -- each call is a one-shot render with no
    memory of the last one (this function has no session/request
    concept to hang that state off safely), so a graph here shows a
    synthetic wave *around the current live value*, the same technique
    render_preset_thumbnail() uses around its fixed one, rather than a
    real accumulating trend. It still visibly shifts level from poll to
    poll as the real stat changes -- just not a continuous line the way
    the actual running panel's own graph (which does keep real
    history) does.

    start_systeminfos()/start_media_polling() are idempotent (safe to
    call even if the dashboard theme -- or a different one entirely --
    already has them running) and are called here so GPU/media stats
    are actually available even when this is invoked while nothing (or
    some other theme) is running; the first call or two right after the
    design canvas opens may still come back with a None GPU/media
    reading until SystemInfos.exe's helper process has written its
    first frame, same cold-start gap the real panel has."""
    start_systeminfos()
    start_media_polling()
    fonts = _thumbnail_fonts()
    bg_image, layout = build_static_background(width, height, fonts, elements, background)
    sysinfo_frame = read_systeminfos()
    media = get_media_info()
    gpu = get_gpu_stats(sysinfo_frame)
    stats = {
        "cpu_load": get_cpu_stats()["util"],
        "gpu_load": gpu["util"] if gpu else None,
        "gpu_temp": gpu["temp"] if gpu else None,
        "ram": get_ram_percent(),
        "network": get_network_rate_mb_s(),
        "cpu_freq": get_cpu_freq_ghz(),
        "disk_usage": get_disk_usage_percent(),
        "vram_usage": get_vram_percent(),
        "swap": get_swap_percent(),
        "disk_io": get_disk_io_mb_s(),
        "gpu_power": get_gpu_power_w(),
        "process_count": get_process_count(),
        "cpu_load_peak": get_cpu_load_peak_core(),
        "battery": get_battery_percent(),
        "volume": get_volume_percent(),
    }
    history = {}
    for el in elements:
        if el.get("type") != "graph":
            continue
        n = 12
        base_val = stats.get(el.get("stat")) or 50
        history[el["id"]] = deque(
            (max(0.0, min(100.0, base_val + 15 * math.sin(i / 2))) for i in range(n)),
            maxlen=n,
        )
    return render_frame(bg_image, layout, width, height, fonts, stats, media, history)


def apply_weather_from_elements(elements):
    """Points weather.py's background poll at whichever `weather`
    element is on the canvas (the first one, if more than one -- the
    poll loop is one shared lookup, same as media polling is one shared
    "now playing" query even though a `media` element could in theory
    be duplicated too), or clears it if there's none. Called once at
    startup and again every time a live layout edit lands (see run()'s
    loop below) so editing an existing weather element's location/units
    from the design canvas applies without a Stop/Start, the same as
    every other live-appliable dashboard setting. set_location()/
    set_units() are both no-ops when the value hasn't actually changed,
    so calling this on every layout update (whether or not weather
    changed) is cheap."""
    weather_el = next((el for el in elements if el.get("type") == "weather"), None)
    weather.set_location(weather_el.get("location") if weather_el else None)
    weather.set_units((weather_el.get("units") if weather_el else None) or "celsius")


def run(port=None, web_port=8765, enable_web=True, default_art_path=None,
        not_playing_message=None,
        brightness=90, slots=None, elements=None, background=None,
        stop_event=None, log=print, screen_factory=HongtaiScreen, on_connected=None, screen=None):
    """Runs the dashboard until stop_event is set (or forever, if
    stop_event is None -- the CLI entry point below relies on Ctrl+C /
    KeyboardInterrupt instead in that case). Pulled out of main() so a
    GUI can start/stop this theme in a background thread instead of
    only being usable from the command line.

    Weather (weather.py) and the now-playing display are both separate,
    independently movable/resizable elements (see default_weather_
    element()/default_media_element()) -- not a global on/off this
    function takes a kwarg for. A weather element's own `location`/
    `units` fields drive weather.py's background poll via
    apply_weather_from_elements(), called below and again on every
    live layout edit.

    `elements` (ROADMAP.md Phase 4) is the new way to lay the gauges
    out -- a list of dicts in slots_to_elements()'s shape, with their
    own x/y/radius/color/opacity instead of one of 8 fixed named spots.
    If not given, it's derived from `slots` instead (see below), so
    passing neither still renders today's default layout exactly as
    before.

    `slots` is the old way (app.py's Tkinter Dashboard tab still reads/
    writes this shape, and still calls this with `slots=`, never
    `elements=` -- it's unaffected by any of this): picks which stat
    each of the 4 big gauges (plus the 4 smaller ones) shows -- see
    STAT_DEFS/DEFAULT_SLOTS/SLOT_KINDS. Defaults to DEFAULT_SLOTS if not
    given (or only partially given), and is ignored entirely if
    `elements` is given.

    `background` picks the panel background -- see BACKGROUND_PRESETS/
    DEFAULT_BACKGROUND and build_static_background()'s docstring;
    defaults to DEFAULT_BACKGROUND if not given (or only partially
    given, e.g. just {"mode": "solid"}).

    `screen_factory` exists purely so a GUI can hand in an already-built
    HongtaiScreen (e.g. if it wants to connect once and let the person
    switch themes without reopening the serial port each time); the CLI
    just uses the default, which is the class itself.

    `on_connected(screen)`, if given, is called once right after connect()
    -- this is how a GUI gets a live reference to the connected screen so
    things like the brightness slider can apply instantly (screen.set_
    brightness() is safe to call from another thread; see the write lock
    in hongtai_screen.py) instead of only taking effect on the next Start.

    `screen`, if given, is an ALREADY-CONNECTED HongtaiScreen to render
    onto directly -- `port`/`screen_factory`/`on_connected` are all
    ignored in that case, and this function does not close it when it
    returns. See demo_clock.py's run() docstring for the full
    explanation (screen_engine.py's live theme-switching relies on this).
    """
    if cairo is None:
        log("This theme needs pycairo to draw the gauges, and it isn't installed")
        log("for whichever Python is running this script:")
        log("    pip install pycairo numpy")
        log("(pycairo installs from a prebuilt wheel on Windows -- no separate")
        log(" Cairo library install needed. If you have more than one Python on")
        log(" this machine, e.g. a venv, make sure you install into the same one")
        log(" you're using to run this script.)")
        return

    set_default_art_path(default_art_path)
    set_not_playing_message(not_playing_message)
    weather.start_polling()

    owns_screen = screen is None
    if owns_screen:
        screen = screen_factory(port)
        info = screen.connect()
        log(f"Connected: {info.width}x{info.height}, firmware {info.version}")
        if on_connected is not None:
            on_connected(screen)
    else:
        info = screen.info
    screen.set_brightness(brightness)

    if enable_web:
        screen.enable_web_mirror(port=web_port, log=log)

    start_systeminfos()
    start_media_polling()
    if not os.path.exists(SYSTEMINFOS_EXE):
        log(f"  (SystemInfos.exe not found at {SYSTEMINFOS_DIR} -- GPU temp may be limited)")
    if not _GPU_OK:
        log("  (no NVIDIA GPU / nvidia-ml-py not available -- falling back to SystemInfos.exe for GPU stats)")
    if not _MEDIA_OK:
        log("  (winsdk not available -- install it for Spotify album art: pip install winsdk)")

    if elements is None:
        elements = slots_to_elements(slots)
    apply_weather_from_elements(elements)

    fonts = Fonts()
    bg_image, layout = build_static_background(info.width, info.height, fonts, elements, background)

    log("Streaming dashboard at 10Hz. Press Ctrl+C to stop." if stop_event is None
        else "Streaming dashboard at 10Hz.")
    target_period = 0.1  # 10Hz

    # One rolling deque per graph element (ROADMAP.md Phase 6), oldest
    # sample first -- render_frame() only reads these, it never appends
    # to them, since it has no idea how much wall-clock time actually
    # elapsed between frames (a busy frame can run long) but this loop
    # does. `history_seconds` is a per-element setting (like everything
    # else about it) so one graph can show a longer trend than another;
    # maxlen is computed from it and target_period rather than assuming
    # every frame lands exactly on schedule.
    history = {
        el["id"]: deque(maxlen=max(2, int(el.get("history_seconds", 20) / target_period)))
        for el in elements if el.get("type") == "graph"
    }

    # Occasional RSS log line -- purely diagnostic, to make a real memory
    # leak (reported after leaving this theme running overnight) visible
    # without needing a separate profiler attached ahead of time. Once
    # every 10 minutes is often enough to see a trend over a multi-hour
    # run without spamming the log; psutil.Process().memory_info() is
    # cheap (no per-frame cost worth worrying about at this interval).
    _mem_log_interval = 600.0
    _last_mem_log = time.time()
    try:
        _this_process = psutil.Process()
    except Exception:  # noqa: BLE001
        _this_process = None

    try:
        while stop_event is None or not stop_event.is_set():
            frame_start = time.time()

            if _this_process is not None and frame_start - _last_mem_log >= _mem_log_interval:
                _last_mem_log = frame_start
                try:
                    rss_mb = _this_process.memory_info().rss / (1024 * 1024)
                    log(f"  (memory: {rss_mb:.0f} MB RSS -- if this keeps climbing over "
                        f"several hours rather than leveling off, that's a real leak)")
                except Exception:  # noqa: BLE001
                    pass

            # Pick up a live layout/background edit from the design
            # canvas, if one's queued -- see set_pending_dashboard_
            # layout()'s docstring. Checked every frame (not just while
            # unpaused) so a change made while the panel's paused is
            # already baked in and ready the moment it resumes, rather
            # than showing stale content for one extra frame after
            # waking up.
            pending_layout = _take_pending_dashboard_layout()
            if pending_layout:
                if "elements" in pending_layout:
                    elements = pending_layout["elements"]
                    apply_weather_from_elements(elements)
                if "background" in pending_layout:
                    background = pending_layout["background"]
                bg_image, layout = build_static_background(info.width, info.height, fonts, elements, background)
                # Rebuild the per-graph history dict to match: keep an
                # existing element's buffer (so its trend line doesn't
                # visibly reset) unless its history_seconds changed, in
                # which case its maxlen needs to change too; drop
                # buffers for graphs that no longer exist, add fresh
                # ones for graphs that are new.
                new_history = {}
                for el in elements:
                    if el.get("type") != "graph":
                        continue
                    maxlen = max(2, int(el.get("history_seconds", 20) / target_period))
                    old = history.get(el["id"])
                    new_history[el["id"]] = (
                        old if old is not None and old.maxlen == maxlen
                        else deque(old or (), maxlen=maxlen)
                    )
                history = new_history

            # "Keep the panel updating while Windows is locked" setting
            # (power_state.py, on by default -- matches this app's
            # original always-on behavior) -- when it's off and Windows
            # is actually locked, skip building and pushing a frame
            # entirely this tick rather than just the screen.show()
            # call, so a locked machine doesn't keep burning CPU on
            # stats/rendering nobody's watching either.
            if not power_state.should_pause():
                sysinfo_frame = read_systeminfos()
                media = get_media_info()
                gpu = get_gpu_stats(sysinfo_frame)
                stats = {
                    "cpu_load": get_cpu_stats()["util"],
                    "gpu_load": gpu["util"] if gpu else None,
                    "gpu_temp": gpu["temp"] if gpu else None,
                    "ram": get_ram_percent(),
                    "network": get_network_rate_mb_s(),
                    "cpu_freq": get_cpu_freq_ghz(),
                    "disk_usage": get_disk_usage_percent(),
                    "vram_usage": get_vram_percent(),
                    "swap": get_swap_percent(),
                    "disk_io": get_disk_io_mb_s(),
                    "gpu_power": get_gpu_power_w(),
                    "process_count": get_process_count(),
                    "cpu_load_peak": get_cpu_load_peak_core(),
                    "battery": get_battery_percent(),
                    "volume": get_volume_percent(),
                }

                for graph_id, buf in history.items():
                    stat_key = next((el.get("stat") for el in elements
                                      if el["id"] == graph_id), None)
                    buf.append(stats.get(stat_key) if stat_key else None)

                img = render_frame(bg_image, layout, info.width, info.height, fonts, stats, media, history)
                screen.show(img)

            elapsed = time.time() - frame_start
            sleep_for = max(0.0, target_period - elapsed)
            if stop_event is not None:
                stop_event.wait(sleep_for)  # wakes immediately if stopped mid-sleep
            else:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        if owns_screen:
            screen.close(log=log)
            log("Stopped, disconnected cleanly.")
        else:
            log("Stopped.")
        stop_systeminfos()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("port", nargs="?", default=None, help="COM port (auto-detected if omitted)")
    ap.add_argument("--web-port", type=int, default=8765,
                     help="port for the live web mirror (default 8765)")
    ap.add_argument("--no-web", action="store_true",
                     help="disable the live web mirror entirely")
    ap.add_argument("--default-art", default=None,
                     help="image to show in place of album art when nothing is playing "
                          "(default: a plain drawn placeholder, no file needed)")
    ap.add_argument("--not-playing-message", default=None,
                     help="text to show in place of the track title when nothing is "
                          f"playing (default: {DEFAULT_NOT_PLAYING_MESSAGE!r})")
    args = ap.parse_args()

    # Weather (like the now-playing display, text/image/graph elements,
    # and clock customization) is a design-canvas-only element -- see
    # default_weather_element() -- with no CLI flag of its own, same as
    # those other element types never had one either.
    run(port=args.port, web_port=args.web_port, enable_web=not args.no_web,
        default_art_path=args.default_art, not_playing_message=args.not_playing_message)


if __name__ == "__main__":
    main()
