"""Prints what every source on THIS machine reports for the two stats
that can't be verified from anywhere else -- CPU clock and volume.

Run it on the Windows box the panel is plugged into:

    python scripts\\check_sensors.py

Why it exists: CPU clock was reported as stuck at one value ("always
showing as 3.4G which isn't accurate") while the vendor app read
5.5GHz at the same moment. That's psutil's Windows behavior -- its
`current` comes from CallNtPowerInformation, which on modern machines
just reports the nominal clock -- so dashboard_theme.get_cpu_freq_ghz()
now prefers the `% Processor Performance` perf counter instead. This
shows all the readings side by side so the fix can be confirmed
against Task Manager rather than assumed.

Nothing here writes anything or touches the panel; it only reads.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from hongtai_screen_app.themes import dashboard_theme as dt  # noqa: E402


def main():
    print("platform:", sys.platform)
    print()

    print("--- CPU clock ---------------------------------------------")
    try:
        import psutil
        freq = psutil.cpu_freq()
        if freq is None:
            print("psutil.cpu_freq():           not available")
        else:
            print(f"psutil current:              {freq.current:.0f} MHz   <- what this used to show")
            print(f"psutil min/max (base):       {freq.min:.0f} / {freq.max:.0f} MHz")
    except Exception as e:  # noqa: BLE001
        print("psutil.cpu_freq() failed:", e)

    # The perf counter needs two samples a moment apart to mean
    # anything -- the first call only primes the query.
    dt._pdh_cpu_performance_percent()
    time.sleep(1.0)
    percent = dt._pdh_cpu_performance_percent()
    if percent is None:
        print("% Processor Performance:     not available on this machine")
    else:
        print(f"% Processor Performance:     {percent:.1f}%  (>100 means boosting)")

    print()
    print("Reading it 5 times, a second apart -- these should MOVE if")
    print("the machine is doing anything, and match Task Manager's")
    print("Performance tab > CPU > Speed:")
    for _ in range(5):
        dt._cpu_freq_state["sampled_at"] = 0.0   # bypass the 1s cache for this check
        ghz = dt.get_cpu_freq_ghz()
        print(f"    get_cpu_freq_ghz() -> {ghz:.2f} GHz" if ghz is not None
              else "    get_cpu_freq_ghz() -> unavailable")
        time.sleep(1.0)

    print()
    print("--- Volume ------------------------------------------------")
    try:
        import pycaw  # noqa: F401
        print("pycaw:                       installed")
    except ImportError:
        print("pycaw:                       NOT installed -- the Volume stat will read '--'.")
        print("                             Install it with:  py -m pip install pycaw")

    vol = dt.get_volume_percent()
    if vol is None:
        print("get_volume_percent():        unavailable")
    else:
        print(f"get_volume_percent():        {vol:.0f}%")
        print()
        print("Change the volume (or mute) now -- these should follow it;")
        print("muted reads 0:")
        for _ in range(5):
            dt._volume_state["sampled_at"] = 0.0
            v = dt.get_volume_percent()
            print(f"    volume -> {v:.0f}%" if v is not None else "    volume -> unavailable")
            time.sleep(1.5)


if __name__ == "__main__":
    main()
