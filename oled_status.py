#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2025-2026 Ryan Lush <ryan.lush@gmail.com>
#
# Licensed under the PolyForm Noncommercial License 1.0.0. You may use,
# study, modify, and share it for any noncommercial purpose. Commercial use
# requires a separate license from the author -- contact ryan.lush@gmail.com.
# Full license text: see the LICENSE file beside this one, or
# https://polyformproject.org/licenses/noncommercial/1.0.0/

"""
oled_status.py — one-file OLED status display for Linux SBCs.

The merge of two programs that had drifted apart: `oled_status.py`, which ran
on the Pi and could work out its own hardware and read a dozen metrics, and
`bbb_oled.py`, which ran on the BeagleBone and was the one that had been
debugged in anger. This file is the second one's skeleton carrying the first
one's features. What each side brought, and why:

FROM bbb_oled.py (operational)
  * A one-byte I2C probe instead of `write_quick`. This is not a preference.
    The AM335x OMAP adapter declares I2C_AQ_NO_ZERO_LEN, so the kernel rejects
    a zero-length write before it reaches the wire -- oled_status.py's bus scan
    therefore found nothing at all on a BeagleBone, panel present or not, and
    logged `adapter quirk: no zero length` once per attempt while doing it.
  * Connect that cannot leak descriptors. luma's i2c() opens /dev/i2c-N and
    then probes inside its own __init__, so a failed probe leaves the fd open
    with no object to close. Measured with the panel unplugged: ~2h to EMFILE,
    164MB RSS on a 512MB board, taking systemctl down with it.
  * Failures are logged once per distinct cause, not swallowed. The old
    `except Exception: pass` made a missing font, a wrong address and a dead
    bus all look identical: blank screen, empty journal.
  * A status file the service publishes about itself, because a display loop
    that connected once and has dropped every frame since still looks green to
    systemd.
  * Exit non-zero if the panel was never found, so `systemctl --failed` shows
    it instead of a healthy-looking unit drawing to nothing.
  * Clear the panel on SIGTERM, so a stopped service doesn't leave its last
    frame burned on screen looking live.

FROM oled_status.py (features)
  * Board, bus, address, font, thermal zone, default route and service
    discovery, so the same file runs on a Pi 5, a BeagleBone Blue, or whatever
    else has an SSD1306 hanging off it.
  * CPU / load / temp / throttle flags / memory / disk / uptime / wifi SSID and
    RSSI / per-service state / battery volts and percent / Pi 5 PMIC power.
  * Pages, --simulate, --probe, and --install.

AND THE LAYOUT IS NEW. The old one stacked four rows on a 12px pitch from y=0.
The two-colour 128x64 panels put the top 16 rows in a separate yellow segment
with a physical gap under it, so the second row landed square on the seam: the
top third of those glyphs came out yellow, the bottom two thirds blue, split by
the gap. It reads as a doubled, half-erased line. Nothing here is ever drawn
across the seam: the header bar owns rows 0..BANNER-1 and the body starts below
it. See Layout.

Use
---
    ./oled_status.py                    run it
    ./oled_status.py --probe            print what it detected, exit
    ./oled_status.py --simulate         draw to the terminal, no hardware
    ./oled_status.py --simulate --png /tmp/f.png    dump a frame per page
    ./oled_status.py --service foo      watch extra units (repeatable)
    sudo ./oled_status.py --install     write + enable a systemd unit
    sudo ./oled_status.py --uninstall
"""

import argparse
import contextlib
import glob
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time

LOG = logging.getLogger("oled_status")

# ── configuration ───────────────────────────────────────────────────

I2C_ADDRS = (0x3C, 0x3D)          # some modules are strapped to 0x3D

# Units worth showing if they happen to be installed. --service adds to this.
KNOWN_SERVICES = (
    "balance_bot",
    "batt_monitor",
    "hailo-tracker",
    "tunehud",
    "bbb_oled",
)

# Four characters is what fits beside a value on a 128px panel. Anything not
# listed falls back to the first four alphanumerics of the unit name.
SERVICE_LABELS = {
    "balance_bot": "BBOT",
    "batt_monitor": "BATM",
    "hailo-tracker": "HAIL",
    "tunehud": "TUNE",
}

BATT_PATHS = (
    "/run/batt_status.json",
    "/run/battery.json",
    "/tmp/batt_status.json",
)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

UNIT_NAME = "oled-status"
BATT_STALE_SEC = 30.0             # older than this and the reading is unknown
STATUS_WRITE_SEC = 5.0            # how often to republish our own status

# Highlight band on the two-colour panels: rows 0..15 are a separate yellow
# segment with a physical gap beneath them. Nothing may straddle row BANNER_H.
BANNER_H = 16
BANNER_GAP = 2                    # dead rows under the segment
MARGIN_X = 4


# ── logging that survives a 1Hz loop ────────────────────────────────

class Once:
    """Log a message only when it changes.

    A display loop repeats its errors every second. Without this the journal
    fills with thousands of identical lines and the real first failure scrolls
    away.
    """

    def __init__(self):
        self._last = {}

    def log(self, key, level, msg, *args):
        text = msg % args if args else msg
        if self._last.get(key) != text:
            LOG.log(level, text)
            self._last[key] = text

    def clear(self, key):
        self._last.pop(key, None)


ONCE = Once()


def default_status_path():
    """Where this process publishes what it is ACTUALLY doing.

    systemd sets $RUNTIME_DIRECTORY from the unit's RuntimeDirectory=, which is
    the only writable directory a ProtectSystem=strict unit has. Reading it
    rather than hard-coding a path is what lets the same file run as the
    `bbb_oled` unit on the bot (publishing /run/bbb_oled/status.json, which the
    dashboard already reads) and as `oled-status` on the Pi, with no flag.
    """
    runtime = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0]
    if runtime:
        return os.path.join(runtime, "status.json")
    return f"/run/{UNIT_NAME}/status.json"


def publish_status(path, **fields):
    """Write the status file atomically. Never raises, never blocks startup.

    Atomic because a reader that catches a half-written file cannot tell that
    from a malformed one, and would report a fault that does not exist.

    Failure here is deliberately near-silent: this file is diagnostics. If the
    directory is missing because someone ran the script by hand outside
    systemd, that is not a reason to take the display down -- the display is
    the actual job.
    """
    if not path:
        return
    fields["ts"] = time.time()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(fields, fh)
        os.replace(tmp, path)
    except OSError as e:
        ONCE.log("status", logging.WARNING,
                 "cannot write %s: %s (display continues)", path, e)


# ── small helpers ───────────────────────────────────────────────────

_cache = {}


def cached(ttl):
    """Memoize a cheap-to-call, expensive-to-run reader for `ttl` seconds.

    Every metric below is read at the draw rate but changes far more slowly,
    and several of them are a fork+exec. On an AM335x that is the difference
    between a display that costs nothing and one that shows up in `top`.

    A reader that raises is logged (once, by name) and cached as None, so a
    permanently broken source costs one log line and then nothing.
    """
    def deco(fn):
        def wrapper(*args):
            key = (fn.__name__, args)
            now = time.monotonic()
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
            try:
                val = fn(*args)
                ONCE.clear("metric:" + fn.__name__)
            except Exception as e:
                ONCE.log("metric:" + fn.__name__, logging.WARNING,
                         "%s unavailable: %s", fn.__name__, e)
                val = None
            _cache[key] = (now, val)
            return val
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def slurp(path, limit=4096):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace").strip("\x00\n ")
    except OSError:
        return None


def run(cmd, timeout=2.0):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        ONCE.log("run:" + cmd[0], logging.DEBUG, "%s failed: %s", cmd[0], e)
        return ""


def clock():
    return time.strftime("%H:%M:%S")


# ── hardware / platform detection ───────────────────────────────────

def detect_board():
    """(model string, family) where family is pi | bbb | jetson | generic."""
    model = (slurp("/proc/device-tree/model")
             or slurp("/sys/firmware/devicetree/base/model")
             or "")
    if not model:
        cpuinfo = slurp("/proc/cpuinfo", 65536) or ""
        m = re.search(r"^(?:Model|Hardware)\s*:\s*(.+)$", cpuinfo, re.M)
        model = m.group(1).strip() if m else ""
    low = model.lower()
    if "raspberry" in low:
        family = "pi"
    elif "beagle" in low or "am335x" in low or "am62" in low:
        family = "bbb"
    elif "jetson" in low or "tegra" in low:
        family = "jetson"
    else:
        family = "generic"
    return model or "unknown board", family


def i2c_buses():
    nums = []
    for path in glob.glob("/dev/i2c-*"):
        try:
            nums.append(int(path.rsplit("-", 1)[1]))
        except ValueError:
            pass
    return sorted(nums)


def probe_i2c(bus, addr):
    """True if something ACKs at `addr` on `bus`. Owns and closes its own fd.

    NOT write_quick(). A zero-data write is the probe i2cdetect uses and it is
    the obvious choice, but it cannot work on this SoC: the AM335x OMAP adapter
    declares I2C_AQ_NO_ZERO_LEN, so the i2c core rejects the transfer before it
    reaches the wire. That probe failed 100% of the time, panel present or not,
    and the kernel logged

        i2c i2c-1: adapter quirk: no zero length (addr 0x003c, size 0, write)

    once per attempt -- 189 times in one boot of the log captured during the
    2026-08-23 read-only event, at the retry loop's 30s back-off cadence.

    A one-byte write is not zero-length, so it passes the quirk check. 0x00 is
    an SSD1306 command-mode control byte (Co=0, D/C#=0) with no command
    following it: a real panel accepts it and does nothing, an absent one still
    raises OSError, which is all this needs.
    """
    try:
        from smbus2 import SMBus
    except ImportError:
        # i2c-tools fallback. Parse the grid by slot, not by (addr & 0x0F):
        # row 00: is printed with its first three slots blanked.
        grid = run(["i2cdetect", "-y", str(bus)], timeout=4)
        return f"{addr:02x}" in grid.lower().split()
    handle = None
    try:
        handle = SMBus(bus)
        handle.write_byte(addr, 0x00)
        return True
    except OSError:
        return False
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def find_display(preferred_bus=None, preferred_addr=None):
    """Scan for a panel. Returns (bus, addr) or (None, None)."""
    buses = i2c_buses()
    if preferred_bus is not None:
        buses = [preferred_bus] + [b for b in buses if b != preferred_bus]
    addrs = [preferred_addr] if preferred_addr else list(I2C_ADDRS)
    for bus in buses:
        for addr in addrs:
            if probe_i2c(bus, addr):
                return bus, addr
    return None, None


def font_paths():
    paths = list(FONT_CANDIDATES)
    paths += sorted(glob.glob("/usr/share/fonts/**/*Mono*.ttf", recursive=True))
    return paths


def load_fonts(explicit, size):
    """(body_font, bar_font, path) — two sizes off one file.

    A missing TrueType font is not fatal: PIL's built-in bitmap font is stuck
    at one size, so both come back identical. Everything in Layout measures the
    font it is handed instead of assuming a cell size, so that still lines up.
    """
    from PIL import ImageFont
    candidates = [explicit] if explicit else font_paths()
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            return (ImageFont.truetype(path, size),
                    ImageFont.truetype(path, max(7, size - 2)),
                    path)
        except OSError as e:
            LOG.warning("font %s unusable: %s", path, e)
    LOG.warning("no TrueType font found (tried %d paths) — falling back to "
                "PIL's built-in bitmap font, which ignores --font-size",
                len([c for c in candidates if c]))
    fallback = ImageFont.load_default()
    return fallback, fallback, "<default>"


def auto_font_size(height):
    """Body point size that fills the rows the layout will actually make."""
    return 10 if height >= 64 else 8


# ── layout ──────────────────────────────────────────────────────────

class Layout:
    """Where the lines go on a panel of this size. Computed once, at startup.

    THE RULE: nothing is drawn across row `banner_h`.

    On the two-colour 128x64 panels, rows 0..15 are a physically separate
    yellow segment and there is a dead gap beneath them. The previous layout
    stacked four rows on a 12px pitch from y=0, so row two spanned y=12..23 and
    landed square on that seam -- its top four rows printed yellow, its bottom
    eight blue, with the gap through the middle of the glyphs. On the bench it
    reads as a doubled, half-erased line, and no amount of squinting at the
    data explains it, because the data was always fine.

    So the band gets the header bar and nothing else, and the body starts below
    the gap. On a single-colour panel the same arithmetic is just a title bar
    over evenly spaced rows, which is a reasonable way to lay it out anyway.
    --no-header gives back plain rows over the whole panel.
    """

    def __init__(self, width, height, banner_h=BANNER_H, header=True):
        self.width, self.height = width, height
        # A bar is only worth it if what is left under it is still legible --
        # two body rows' worth. Note the test is on what REMAINS, not on some
        # multiple of the band: --banner-h is the caller telling us what the
        # hardware does, and quietly ignoring it because 3x didn't fit would be
        # the same class of bug this layout exists to fix.
        usable = height - banner_h - BANNER_GAP
        self.banner_h = banner_h if (header and banner_h > 0 and usable >= 20) else 0
        top = self.banner_h + BANNER_GAP if self.banner_h else 0
        self.n_rows = max(1, (height - top) // 15) if height >= 64 else \
            max(1, (height - top) // 11)
        self.pitch = (height - top) // self.n_rows
        self.rows_y = [top + i * self.pitch for i in range(self.n_rows)]
        self.dot = max(5, min(self.banner_h - 6 if self.banner_h else 6, 8))

    def describe(self):
        return (f"{self.width}x{self.height} banner={self.banner_h} "
                f"rows={self.n_rows} pitch={self.pitch}")


def _size(draw, text, font):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _fit(draw, text, font, avail):
    """Trim an over-wide value from the LEFT.

    What identifies these values lives at their end -- the host octet of an
    address, the seconds on a clock, the percent after a size -- so an
    overflowing string should lose its head, not its tail. The old code let
    them run off the right edge instead, which silently turned 192.168.1.142
    into 192.168.1.14 the moment the font substituted wider.
    """
    if _size(draw, text, font)[0] <= avail:
        return text
    while text and _size(draw, "…" + text, font)[0] > avail:
        text = text[1:]
    return "…" + text if text else ""


@contextlib.contextmanager
def frame(device):
    """luma's canvas(), minus the luma. Works with anything exposing
    size/mode/display -- which includes both a luma device and the terminal
    stand-in, so --simulate exercises the real drawing code, not a copy."""
    from PIL import Image, ImageDraw
    img = Image.new(getattr(device, "mode", "1"), device.size)
    draw = ImageDraw.Draw(img)
    yield draw
    device.display(img)


def draw_page(draw, layout, fonts, page, beat, alive):
    """One frame: header bar, then label/value rows."""
    body_font, bar_font = fonts
    width = layout.width
    rows = page.rows

    if layout.banner_h:
        # Inverse bar. On a two-colour panel it lights the yellow segment
        # solid, which is the point: the split now looks deliberate instead of
        # like a rendering fault. --no-header is the way out if the standing
        # highlight ever worries you for burn-in.
        draw.rectangle((0, 0, width - 1, layout.banner_h - 1), fill="white")
        _, th = _size(draw, "Ag", bar_font)
        ty = max(0, (layout.banner_h - th) // 2) - 1
        draw.text((MARGIN_X, ty), page.title, font=bar_font, fill="black")
        tag_w = _size(draw, page.tag, bar_font)[0]
        tag_x = width - MARGIN_X - layout.dot - 4 - tag_w
        draw.text((tag_x, ty), page.tag, font=bar_font, fill="black")
        _beat(draw, layout, beat, alive,
              x1=width - MARGIN_X, y0=(layout.banner_h - layout.dot) // 2,
              ink="black", paper="white")
        top_reserved = 0
    else:
        top_reserved = layout.dot + 2
        _beat(draw, layout, beat, alive, x1=width - 2, y0=1,
              ink="white", paper="black")

    if not rows:
        return

    # One value column, measured from the widest label on THIS page. The old
    # code padded labels with spaces to line the values up, which holds only
    # while the font is monospaced -- and the font is whichever of a dozen
    # paths the image happens to have, one of which is proportional.
    label_w = max(_size(draw, f"{lab}:", body_font)[0] for lab, _ in rows)
    col_x = MARGIN_X + label_w + _size(draw, "n", body_font)[0]
    _, glyph_h = _size(draw, "Ag", body_font)

    for i, (label, value) in enumerate(rows[:layout.n_rows]):
        y = layout.rows_y[i] + max(0, (layout.pitch - glyph_h) // 2) - 1
        reserved = top_reserved if (i == 0 and not layout.banner_h) else 0
        draw.text((MARGIN_X, y), f"{label}:", font=body_font, fill="white")
        avail = width - col_x - MARGIN_X - reserved
        draw.text((col_x, y), _fit(draw, str(value), body_font, avail),
                  font=body_font, fill="white")


def _beat(draw, layout, beat, alive, x1, y0, ink, paper):
    """Heartbeat: blinks while the watched unit is up, crossed out when it
    isn't. Two states in one glyph, because a dot that simply stops blinking
    is indistinguishable from a display that has frozen."""
    d = layout.dot
    box = (x1 - d, y0, x1, y0 + d)
    if alive:
        draw.ellipse(box, outline=ink, fill=ink if beat else paper)
    else:
        draw.ellipse(box, outline=ink, fill=paper)
        draw.line((box[0], box[1], box[2], box[3]), fill=ink)
        draw.line((box[0], box[3], box[2], box[1]), fill=ink)


# ── metrics ─────────────────────────────────────────────────────────

@cached(5)
def hostname():
    return socket.gethostname().split(".")[0]


@cached(5)
def default_iface():
    routes = slurp("/proc/net/route", 65536) or ""
    for line in routes.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 2 and cols[1] == "00000000":
            return cols[0]
    return None


@cached(5)
def ip_address():
    iface = default_iface()
    if iface:
        addr = run(["ip", "-4", "-o", "addr", "show", "dev", iface])
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", addr)
        if m:
            return m.group(1)
    # No default route, or no `ip` binary: ask the kernel which source address
    # it would use. No packets are sent -- connect() on a UDP socket only sets
    # the route.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError as e:
        ONCE.log("ip", logging.INFO, "no IP (%s)", e)
        return None


@cached(10)
def wifi():
    """(ssid, dBm) for the default interface, or None if it isn't wireless."""
    iface = default_iface()
    if not iface or not os.path.isdir(f"/sys/class/net/{iface}/wireless"):
        return None
    ssid = run(["iwgetid", "-r"]) or "?"
    dbm = None
    for line in (slurp("/proc/net/wireless", 8192) or "").splitlines():
        if line.strip().startswith(iface + ":"):
            cols = line.split()
            if len(cols) > 3:
                try:
                    dbm = int(float(cols[3].rstrip(".")))
                except ValueError:
                    pass
    return ssid, dbm


class CpuMeter:
    """Percent busy between successive calls, from /proc/stat."""

    def __init__(self):
        self.prev = None

    def read(self):
        try:
            with open("/proc/stat") as fh:
                cols = [float(x) for x in fh.readline().split()[1:]]
        except (OSError, ValueError) as e:
            ONCE.log("cpu", logging.WARNING, "cannot read /proc/stat: %s", e)
            return None
        total = sum(cols)
        idle = cols[3] + (cols[4] if len(cols) > 4 else 0.0)
        prev, self.prev = self.prev, (total, idle)
        if prev is None:
            return None
        d_total, d_idle = total - prev[0], idle - prev[1]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))


@cached(30)
def temp_source():
    """Path of the most CPU-ish thermal zone, if there is one."""
    best = None
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        kind = (slurp(f"{zone}/type") or "").lower()
        if any(k in kind for k in ("cpu", "soc", "package", "x86_pkg")):
            return f"{zone}/temp"
        best = best or f"{zone}/temp"
    return best


@cached(2)
def cpu_temp():
    path = temp_source()
    if path:
        raw = slurp(path)
        if raw and raw.lstrip("-").isdigit():
            val = float(raw)
            # Zones report millidegrees; a few report degrees. 200 is the
            # only threshold that separates them without guessing at the SoC.
            return val / 1000.0 if abs(val) > 200 else val
    out = run(["vcgencmd", "measure_temp"])
    m = re.search(r"([\d.]+)", out)
    return float(m.group(1)) if m else None


@cached(10)
def throttled():
    """Pi undervoltage/throttle flags as a short string. '' when healthy,
    None where vcgencmd does not exist (i.e. everywhere that isn't a Pi)."""
    out = run(["vcgencmd", "get_throttled"])
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return None
    bits = int(m.group(1), 16)
    if bits == 0:
        return ""
    flags = []
    for mask, name in ((0x1, "UV"), (0x2, "FREQ"), (0x4, "THROT"), (0x8, "TEMP")):
        if bits & mask:
            flags.append(name)
    if not flags and bits & 0xF0000:
        flags.append("past")
    return ",".join(flags) or "past"


@cached(3)
def memory():
    info = {}
    for line in (slurp("/proc/meminfo", 4096) or "").splitlines():
        key, _, rest = line.partition(":")
        try:
            info[key] = int(rest.split()[0])
        except (IndexError, ValueError):
            pass
    total = info.get("MemTotal")
    avail = info.get("MemAvailable", info.get("MemFree"))
    if not total or avail is None:
        return None
    used = total - avail
    return used // 1024, total // 1024, 100.0 * used / total


@cached(30)
def disk(path="/"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return (total - free) / 1e9, total / 1e9, (100.0 * (total - free) / total
                                               if total else 0.0)


@cached(2)
def uptime():
    raw = slurp("/proc/uptime")
    secs = int(float(raw.split()[0])) if raw else 0
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    return f"{days}d{hours:02d}:{mins:02d}" if days else f"{hours:02d}:{mins:02d}"


@cached(30)
def installed_services(extra=()):
    """Whichever of the known/requested units actually exist on this box."""
    wanted = list(dict.fromkeys(list(extra) + list(KNOWN_SERVICES)))
    listing = run(["systemctl", "list-unit-files", "--type=service",
                   "--no-legend", "--no-pager"], timeout=6)
    present = {line.split()[0] for line in listing.splitlines() if line.split()}
    found = []
    for name in wanted:
        unit = name if name.endswith(".service") else name + ".service"
        if unit in present:
            found.append(name)
        elif not listing and run(["systemctl", "is-active", unit]):
            found.append(name)      # no listing available; the unit answered
    return found


@cached(2)
def service_state(name):
    """systemctl is-active, cached. It is a fork+exec, and on an AM335x doing
    that at the draw rate is a measurable share of the board."""
    return run(["systemctl", "is-active", name], timeout=2) or "unknown"


def service_label(name, width=4):
    known = SERVICE_LABELS.get(name)
    if known:
        return known
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    return base[:width] or "SVC"


@cached(10)
def pi_power():
    """(5V rail volts, total watts) from the Pi 5 PMIC. None anywhere else.

    Stands in for the battery readout on boards that run off a wall supply.
    `vcgencmd pmic_read_adc` reports each rail as a NAME_A current and a
    NAME_V voltage, so pairing them by prefix gives board power.
    """
    out = run(["vcgencmd", "pmic_read_adc"], timeout=3)
    if not out or "volt(" not in out:
        return None
    vals = dict(re.findall(r"(\S+?)\s+(?:volt|current)\(\d+\)=([\d.]+)", out))
    watts = 0.0
    for name, amps in vals.items():
        if name.endswith("_A"):
            volts = vals.get(name[:-2] + "_V")
            if volts:
                watts += float(amps) * float(volts)
    rail = vals.get("EXT5V_V")
    if rail is None and not watts:
        return None
    return (float(rail) if rail else None), watts


@cached(2)
def battery(path=None):
    """(volts, percent) from a JSON status file, either key optional.

    A stale file counts as no file. A voltage that stopped updating an hour ago
    is worse than no voltage: it is a number you will believe.
    """
    paths = [path] if path else list(BATT_PATHS)
    for candidate in paths:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            age = time.time() - os.stat(candidate).st_mtime
            if age > BATT_STALE_SEC:
                ONCE.log("batt", logging.INFO, "%s is stale (%.0fs old)",
                         candidate, age)
                continue
            with open(candidate) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            ONCE.log("batt", logging.WARNING, "cannot read %s: %s", candidate, e)
            continue
        volts = pct = None
        for key in ("voltage", "voltage_v", "volts", "v"):
            if key in data:
                volts = float(data[key])
                break
        for key in ("percent", "pct", "soc", "capacity"):
            if key in data:
                pct = float(data[key])
                break
        if volts is not None or pct is not None:
            ONCE.clear("batt")
            return volts, pct
    return None


def battery_text(path):
    batt = battery(path)
    if batt:
        volts, pct = batt
        parts = [f"{volts:.1f}V" if volts is not None else None,
                 f"{pct:.0f}%" if pct is not None else None]
        return " ".join(p for p in parts if p)
    power = pi_power()
    if power:
        rail, watts = power
        parts = [f"{rail:.2f}V" if rail else None,
                 f"{watts:.1f}W" if watts else None]
        return " ".join(p for p in parts if p)
    return "------"


# ── pages ───────────────────────────────────────────────────────────

class Page:
    __slots__ = ("title", "tag", "rows")

    def __init__(self, title, tag, rows):
        self.title, self.tag, self.rows = title, tag, rows


def page_bot(args, cpu_pct, budget):
    """The pinned page: what you walk over to the bot to find out.

    The clock lives in the bar rather than taking a body row, because it earns
    its place by being next to the heartbeat -- together they say "this display
    is live", which is the one thing a status screen must prove about itself.
    """
    unit = args.primary
    ip = ip_address() or "no link"
    batt = battery_text(args.battery)
    if budget >= 3:
        rows = [("IP", ip),
                (service_label(unit), service_state(unit)),
                ("BATT", batt)]
        if budget >= 4:
            # --no-header: there is no bar for the clock to live in, so it
            # comes back down as a row. This is the original four-line screen.
            rows.append(("TIME", clock()))
    else:
        # Two rows (a 128x32 panel): the service state is the row that goes,
        # because the heartbeat already carries it -- blinking when the unit is
        # up, crossed out when it is not.
        rows = [("IP", ip), ("BATT", batt)]
    return Page("BOT", clock(), rows)


def page_net(args, cpu_pct, budget):
    rows = [("HOST", hostname() or "?"),
            ("IP", ip_address() or "no link")]
    link = wifi()
    if link:
        ssid, dbm = link
        rows.append(("SSID", ssid))
        rows.append(("RSSI", f"{dbm} dBm" if dbm is not None else "?"))
        if budget == 3:
            # Fold the signal in beside the SSID rather than spilling a
            # one-row second screen for it.
            rows[2] = ("WIFI", f"{ssid} {dbm}" if dbm is not None else ssid)
            rows.pop()
    else:
        iface = default_iface()
        if iface:
            rows.append(("IF", iface))
    return Page("NET", clock(), rows[:max(2, budget)])


def page_sys(args, cpu_pct, budget):
    """Uptime rides in the bar so the three body rows can be the three numbers
    that change: busy, hot, full."""
    load = os.getloadavg()[0]
    rows = [("CPU", f"{cpu_pct:.0f}% ld{load:.2f}" if cpu_pct is not None
             else f"ld {load:.2f}")]

    temp, flags = cpu_temp(), throttled()
    temp_txt = f"{temp:.1f}C" if temp is not None else "--"
    if flags:
        temp_txt += " !" + flags
    rows.append(("TEMP", temp_txt))

    mem, dsk = memory(), disk()
    mem_txt = f"{mem[0]}/{mem[1]}M {mem[2]:.0f}%" if mem else None
    dsk_txt = f"{dsk[2]:.0f}% of {dsk[1]:.0f}G" if dsk else None

    if budget >= 4:
        if mem_txt:
            rows.append(("MEM", mem_txt))
        if dsk_txt:
            rows.append(("DISK", dsk_txt))
    elif mem and dsk:
        # One row for both, because a second screen holding a single line is
        # worse than a slightly terse one.
        rows.append(("MEM", f"{mem[2]:.0f}%  dsk {dsk[2]:.0f}%"))
    elif mem_txt or dsk_txt:
        rows.append(("MEM", mem_txt) if mem_txt else ("DISK", dsk_txt))

    return Page("SYS", uptime(), rows[:max(2, budget)])


def page_svc(args, cpu_pct, budget):
    """Every watched unit, however many screens that takes. This is the one
    page that is allowed to paginate: with six services there is no terser
    honest answer, and each screen it makes is a full one."""
    rows = []
    for name in installed_services(tuple(args.service)) or []:
        state = service_state(name)
        rows.append((service_label(name),
                     {"active": "up", "inactive": "down", "failed": "FAIL",
                      "activating": "start", "deactivating": "stop"
                      }.get(state, state)))
    rows.append(("BATT", battery_text(args.battery)))
    return Page("SVC", clock(), rows)


PAGE_BUILDERS = {"bot": page_bot, "net": page_net,
                 "sys": page_sys, "svc": page_svc}


def chunk_rows(rows, budget):
    """Split into screens of at most `budget` rows, as evenly as possible.

    Evenly, not greedily: seven rows in threes is 3/2/2, never 3/3/1. A screen
    holding one line under three empty ones looks like the display broke
    halfway through drawing it.
    """
    if len(rows) <= budget:
        return [rows]
    screens = -(-len(rows) // budget)
    base, extra = divmod(len(rows), screens)
    out, i = [], 0
    for n in range(screens):
        size = base + (1 if n < extra else 0)
        out.append(rows[i:i + size])
        i += size
    return out


def build_screens(args, layout, cpu_pct):
    """Every page, fitted into panel-sized screens.

    Each page is built against the row budget the panel actually has, so a
    128x32 gets a terser version of the same page rather than the 128x64
    version sliced in half. What still overflows (a box running six services)
    is chunked evenly.

    The pinned page is dealt back in between the others, so whatever the cycle
    is doing you are never more than one dwell away from the bot's state.
    """
    screens = []
    for name in args.pages:
        page = PAGE_BUILDERS[name](args, cpu_pct, layout.n_rows)
        if not page.rows:
            continue
        for rows in chunk_rows(page.rows, layout.n_rows):
            screens.append(Page(page.title, page.tag, rows))

    if args.pin and args.pin in args.pages and len(screens) > 1:
        pinned = [s for s in screens if s.title == PAGE_TITLES[args.pin]]
        others = [s for s in screens if s.title != PAGE_TITLES[args.pin]]
        if pinned and others:
            woven = []
            for other in others:
                woven.extend(pinned)
                woven.append(other)
            screens = woven
    return screens or [Page("SYS", clock(), [("HOST", hostname() or "?")])]


PAGE_TITLES = {"bot": "BOT", "net": "NET", "sys": "SYS", "svc": "SVC"}


# ── the panel ───────────────────────────────────────────────────────

class TerminalDevice:
    """--simulate target. Keeps the last frame so it can be printed or saved.

    Worth having for its own sake: every layout decision in this file was made
    against this, not against a panel on a bench, and a change can be seen
    before it is deployed to something you have to walk over to.
    """

    def __init__(self, width=128, height=64):
        self.size = (width, height)
        self.mode = "1"
        self.image = None
        self.persist = False

    def display(self, image):
        self.image = image.copy()

    def show(self):
        if self.image is None:
            return
        px = self.image.convert("1").load()
        width, height = self.size
        out = ["+" + "-" * width + "+"]
        for y in range(0, height, 2):
            line = []
            for x in range(width):
                top = px[x, y] != 0
                bot = px[x, y + 1] != 0 if y + 1 < height else False
                line.append("█" if top and bot else
                            "▀" if top else "▄" if bot else " ")
            out.append("|" + "".join(line) + "|")
        out.append("+" + "-" * width + "+")
        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(out) + "\n")
        sys.stdout.flush()

    def clear(self):
        self.image = None

    def hide(self):
        pass

    def cleanup(self):
        pass


class Display:
    """Owns the panel. Reconnects on I2C errors rather than dying."""

    def __init__(self, args):
        self.args = args
        self.width, self.height = args.width, args.height
        self.bus = args.i2c_bus
        self.addr = args.i2c_addr
        self.device = None
        self._fail_streak = 0

    @property
    def fail_streak(self):
        """Consecutive failed connects. Read by the status file."""
        return self._fail_streak

    @staticmethod
    def _close_serial(serial):
        """Release luma's bus handle. Best effort, never raises."""
        if serial is None:
            return
        fn = getattr(serial, "cleanup", None) or getattr(serial, "close", None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass
        bus = getattr(serial, "_bus", None)
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _driver(self):
        if self.args.driver == "sh1106":
            from luma.oled.device import sh1106 as driver
        elif self.args.driver == "ssd1309":
            from luma.oled.device import ssd1309 as driver
        else:
            from luma.oled.device import ssd1306 as driver
        return driver

    def connect(self):
        if self.args.simulate:
            self.device = TerminalDevice(self.width, self.height)
            self._fail_streak = 0
            return

        from luma.core.interface.serial import i2c

        bus, addr = find_display(self.args.i2c_bus, self.args.i2c_addr)
        if bus is None:
            raise RuntimeError(
                "no panel ACKs on " +
                (", ".join(f"/dev/i2c-{b}" for b in i2c_buses()) or "any I2C bus")
                + " — is I2C enabled and the panel wired?")
        self.bus, self.addr = bus, addr

        serial = None
        try:
            serial = i2c(port=bus, address=addr)
            self.device = self._driver()(serial, width=self.width,
                                         height=self.height,
                                         rotate=self.args.rotate)
        except Exception:
            # luma opens /dev/i2c-N inside i2c() BEFORE it probes the address,
            # so a failed probe leaves the descriptor open. Refcounting does
            # NOT clean this up: the exception being raised keeps a traceback
            # that references this frame, and therefore `serial`, so the fd
            # survives until the cycle collector happens to get to it.
            #
            # Retrying every few seconds against an absent panel therefore
            # climbs to EMFILE. Measured on the BeagleBone with the panel
            # unplugged: after ~2h it hit "[Errno 24] Too many open files" on
            # both /dev/i2c-1 and the systemctl subprocess, at 164MB RSS and
            # 28MB of swap on a 512MB board. A status display that cannot find
            # its panel must idle cheaply, not gradually take the board down.
            #
            # (find_display() above now rejects the common case -- absent
            # panel -- before luma is involved at all, using a probe whose
            # descriptor we own and close. This stays because a panel that
            # ACKs and then fails to initialise takes the same path.)
            self._close_serial(serial)
            raise
        if self.args.contrast is not None:
            self.device.contrast(self.args.contrast)
        self.device.persist = True      # do not blank on GC; we clear on exit
        ONCE.clear("connect")
        self._fail_streak = 0
        LOG.info("connected to %s at i2c-%d 0x%02X (%dx%d, rotate=%d)",
                 self.args.driver, bus, addr, self.width, self.height,
                 self.args.rotate)

    def ensure(self):
        if self.device is not None:
            return True
        try:
            self.connect()
            return True
        except Exception as e:      # luma raises a wide variety here
            self._fail_streak += 1
            # str(e) rather than e: keeps no reference to the exception, whose
            # traceback would otherwise pin the very objects we just closed.
            ONCE.log("connect", logging.ERROR,
                     "cannot open the panel: %s (check the bus exists and the "
                     "address is right: i2cdetect -y -r %s)", str(e),
                     self.args.i2c_bus if self.args.i2c_bus is not None else 1)
            self._check_fd_leak()
            return False

    def backoff(self, base):
        """Seconds before the next connect attempt.

        A missing panel is not a transient condition, so stop probing it at the
        draw rate. Capped so a panel plugged in later is still picked up within
        a minute, without a restart.
        """
        if self._fail_streak <= 1:
            return base
        return min(30.0, base * min(self._fail_streak, 16))

    def _check_fd_leak(self):
        """Shout if our own descriptor count is climbing.

        This is the regression detector for the bug described in connect().
        The failure was silent for two hours and then broke unrelated things
        with a confusing errno, so it is worth five lines to have it announce
        itself instead.
        """
        try:
            n = len(os.listdir("/proc/self/fd"))
        except OSError:
            return
        if n > 64:
            ONCE.log("fds", logging.ERROR,
                     "%d open file descriptors after %d failed connects — "
                     "this is a descriptor leak, not a display fault",
                     n, self._fail_streak)

    def drop(self, why):
        ONCE.log("draw", logging.WARNING, "display error, reconnecting: %s",
                 str(why))
        self._release()

    def _release(self):
        dev, self.device = self.device, None
        if dev is None:
            return
        self._close_serial(getattr(dev, "_serial_interface", None))

    def shutdown(self):
        """Blank the panel so a stopped service doesn't look like a live one."""
        if self.device is None:
            return
        try:
            self.device.clear()
            self.device.hide()
        except Exception as e:
            LOG.warning("could not clear display on exit: %s", e)
        finally:
            self._release()


# ── systemd ─────────────────────────────────────────────────────────

def exec_args(args):
    """Reconstruct the command line, minus the one-shot flags."""
    out = []
    for name in args.service:
        out += ["--service", name]
    if args.i2c_bus is not None:
        out += ["--i2c-bus", str(args.i2c_bus)]
    if args.i2c_addr is not None:
        out += ["--i2c-addr", hex(args.i2c_addr)]
    if args.driver != "ssd1306":
        out += ["--driver", args.driver]
    if (args.width, args.height) != (128, 64):
        out += ["--width", str(args.width), "--height", str(args.height)]
    if args.rotate:
        out += ["--rotate", str(args.rotate)]
    if args.contrast is not None:
        out += ["--contrast", str(args.contrast)]
    if args.battery:
        out += ["--battery", args.battery]
    if args.refresh != 1.0:
        out += ["--refresh", str(args.refresh)]
    if args.page_sec != 5.0:
        out += ["--page-sec", str(args.page_sec)]
    if args.no_header:
        out += ["--no-header"]
    if args.banner_h != BANNER_H:
        out += ["--banner-h", str(args.banner_h)]
    return out


def unit_text(args):
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    script = os.path.abspath(__file__)
    cmd = " ".join([sys.executable, "-u", script] + exec_args(args))
    # The hardening below is not decoration; each line is here for a reason
    # recorded in the bot's HANDOFF notes:
    #   * network.target, not network-online.target -- the display shows
    #     "no link" quite happily and there is no reason to hold up boot by
    #     tens of seconds waiting for DHCP just to draw a status screen.
    #   * SupplementaryGroups lists i2c AND gpio. Most images ship /dev/i2c-*
    #     as root:i2c, but the BeagleBone image ships it root:gpio -- a unit
    #     granting only i2c started fine and then exited 1 on first bus access,
    #     which systemd reported as a restart loop with no hint of a permission
    #     problem. Naming a group that does not exist is harmless. usermod -aG
    #     is NOT sufficient: systemd starts the service with exactly these.
    #   * StartLimit* with --give-up-after: five starts must fit inside the
    #     interval for systemd to ever give up. 5 x (15 + 5) = 100 < 120.
    #   * RuntimeDirectory is both where the status file goes and how this
    #     script finds it ($RUNTIME_DIRECTORY), and systemd removes it on stop,
    #     so a stopped service leaves no stale "ok" behind to be believed.
    return f"""[Unit]
Description=OLED Status Display
Documentation=https://github.com/lusher00/oled-utils
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User={user}
SupplementaryGroups=i2c gpio
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={os.path.dirname(script)}
ExecStart={cmd}
Restart=always
RestartSec=5
RuntimeDirectory={UNIT_NAME}

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
DeviceAllow=char-i2c rw

[Install]
WantedBy=multi-user.target
"""


def other_units():
    """Units that also drive an OLED. Two of them fighting over one panel is a
    real failure mode and it looks exactly like a broken display."""
    found = []
    for path in glob.glob("/etc/systemd/system/*oled*.service"):
        name = os.path.basename(path)
        if name != f"{UNIT_NAME}.service":
            found.append(name)
    return found


def install_service(args):
    if os.geteuid() != 0:
        sys.exit(f"--install needs root: sudo {sys.argv[0]} --install")
    clash = other_units()
    if clash and not args.force:
        sys.exit(f"{', '.join(clash)} already drives a panel. Two units on one "
                 f"bus is a dead screen with no error. Stop and disable it "
                 f"first, or re-run with --force.")
    path = f"/etc/systemd/system/{UNIT_NAME}.service"
    with open(path, "w") as fh:
        fh.write(unit_text(args))
    run(["systemctl", "daemon-reload"], timeout=15)
    run(["systemctl", "enable", "--now", UNIT_NAME], timeout=20)
    print(f"wrote {path} and started {UNIT_NAME}")
    print(run(["systemctl", "--no-pager", "status", UNIT_NAME], timeout=10))


def uninstall_service():
    if os.geteuid() != 0:
        sys.exit("--uninstall needs root")
    run(["systemctl", "disable", "--now", UNIT_NAME], timeout=20)
    path = f"/etc/systemd/system/{UNIT_NAME}.service"
    if os.path.exists(path):
        os.remove(path)
    run(["systemctl", "daemon-reload"], timeout=15)
    print(f"removed {path}")


def probe_report(args):
    model, family = detect_board()
    print(f"board        {model}  (family: {family})")
    buses = i2c_buses()
    print(f"i2c buses    {', '.join(str(b) for b in buses) or 'none — I2C not enabled?'}")
    for bus in buses:
        hits = [hex(a) for a in I2C_ADDRS if probe_i2c(bus, a)]
        print(f"  bus {bus}      {', '.join(hits) if hits else 'no display'}")
    bus, addr = find_display(args.i2c_bus, args.i2c_addr)
    print("display      " + (f"bus {bus} @ {hex(addr)}" if bus is not None
                             else "not found"))
    _, _, font_path = load_fonts(args.font, args.font_size or
                                 auto_font_size(args.height))
    print(f"font         {font_path} @ {args.font_size or auto_font_size(args.height)}pt")
    print(f"layout       {Layout(args.width, args.height, args.banner_h, not args.no_header).describe()}")
    print(f"temp source  {temp_source() or 'vcgencmd / none'}")
    print(f"iface        {default_iface()}  ip {ip_address()}")
    print(f"services     {', '.join(installed_services(tuple(args.service))) or 'none found'}")
    print(f"primary      {args.primary} ({service_state(args.primary)})")
    print(f"battery      {battery(args.battery) or 'no status file'}")
    print(f"pmic         {pi_power() or 'not a Pi 5 / vcgencmd unavailable'}")
    print(f"status file  {args.status_file or '(disabled)'}")
    clash = other_units()
    if clash:
        print(f"WARNING      {', '.join(clash)} also drives a panel")


# ── main ────────────────────────────────────────────────────────────

def hex_or_int(text):
    return int(text, 0)


def page_list(text):
    names = [p.strip().lower() for p in text.split(",") if p.strip()]
    bad = [p for p in names if p not in PAGE_BUILDERS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown page(s): {', '.join(bad)} "
            f"(choose from {', '.join(PAGE_BUILDERS)})")
    return names


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Auto-detecting OLED status display for Linux SBCs.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--service", action="append", default=[],
                   help="systemd unit to watch (repeatable). The first one "
                        "given is the primary: it drives the BOT page and the "
                        "heartbeat.")
    # --batt-path is bbb_oled.py's spelling, kept so an existing
    # /etc/default/bbb_oled keeps working after this file replaces it.
    p.add_argument("--battery", "--batt-path", dest="battery",
                   help="JSON file holding battery voltage/percent")
    p.add_argument("--i2c-bus", "--i2c-port", dest="i2c_bus", type=int,
                   help="force an I2C bus instead of scanning every one")
    p.add_argument("--i2c-addr", type=hex_or_int,
                   help="force the panel address, e.g. 0x3C or 0x3D")
    p.add_argument("--driver", default="ssd1306",
                   choices=("ssd1306", "sh1106", "ssd1309"))
    p.add_argument("--width", type=int, default=128)
    p.add_argument("--height", type=int, default=64, help="64 or 32")
    p.add_argument("--rotate", type=int, default=0, choices=(0, 1, 2, 3),
                   help="90-degree steps")
    p.add_argument("--contrast", type=int, help="0-255")
    p.add_argument("--font", help="TrueType font path")
    p.add_argument("--font-size", type=int,
                   help="body point size (default: 10 on 64px panels, 8 on "
                        "32px; the header bar is drawn 2pt smaller)")
    p.add_argument("--banner-h", type=int, default=BANNER_H,
                   help=f"height of the panel's highlight band, which the "
                        f"header bar is sized to fill exactly (default: "
                        f"{BANNER_H} — the yellow segment on the usual "
                        f"two-colour modules). Nothing is ever drawn across "
                        f"this row.")
    p.add_argument("--no-header", action="store_true",
                   help="single-colour panel: drop the bar and use plain rows")
    p.add_argument("--pages", type=page_list, default=None,
                   help=f"comma-separated subset of "
                        f"{','.join(PAGE_BUILDERS)} (default: all)")
    p.add_argument("--pin", default="bot",
                   help="page to return to between the others, or 'none' "
                        "(default: bot)")
    p.add_argument("--refresh", type=float, default=1.0,
                   help="redraw interval, seconds")
    p.add_argument("--page-sec", type=float, default=5.0,
                   help="seconds per page")
    p.add_argument("--status-file", default=None,
                   help="where to publish this process's own status "
                        "(default: $RUNTIME_DIRECTORY/status.json, else "
                        f"/run/{UNIT_NAME}/status.json; '' to disable)")
    p.add_argument("--retry", type=float, default=2.0,
                   help="reconnect delay after an I2C error")
    p.add_argument("--give-up-after", type=float, default=15.0,
                   help="exit non-zero if the panel has never been reached "
                        "within this many seconds (0 = retry forever)")
    p.add_argument("--simulate", action="store_true",
                   help="render to the terminal, no hardware needed")
    p.add_argument("--png", help="with --simulate: write each page to "
                                 "PNG-<n> beside this path instead of drawing")
    p.add_argument("--once", action="store_true",
                   help="draw a single frame and exit (for testing)")
    p.add_argument("--probe", action="store_true",
                   help="report what was detected, then exit")
    p.add_argument("--list-fonts", action="store_true")
    p.add_argument("--install", action="store_true",
                   help="write + enable a systemd unit")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--print-unit", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="--install: proceed even if another OLED unit exists")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    args = p.parse_args(argv)
    args.primary = args.service[0] if args.service else "balance_bot"
    if args.pages is None:
        args.pages = list(PAGE_BUILDERS)
    if args.pin in ("none", "", None):
        args.pin = None
    elif args.pin not in PAGE_BUILDERS:
        p.error(f"--pin must be one of {', '.join(PAGE_BUILDERS)} or none")
    if args.status_file is None:
        args.status_file = default_status_path()
    if not args.font_size:
        args.font_size = auto_font_size(args.height)
    return args


def dump_png(args, layout, fonts):
    """--simulate --png: one file per screen, upscaled, colourised like the
    two-colour panel. This is how the layout gets reviewed without a bench."""
    from PIL import Image
    device = TerminalDevice(args.width, args.height)
    screens = build_screens(args, layout, 17.0)
    seen, paths = set(), []
    for i, page in enumerate(screens):
        key = (page.title, tuple(page.rows))
        if key in seen:
            continue
        seen.add(key)
        with frame(device) as draw:
            draw_page(draw, layout, fonts, page, i % 2 == 0, True)
        src = device.image.convert("1")
        out = Image.new("RGB", src.size, (0, 0, 0))
        spx, opx = src.load(), out.load()
        for y in range(src.size[1]):
            for x in range(src.size[0]):
                if not spx[x, y]:
                    continue
                if layout.banner_h:
                    if y < layout.banner_h:
                        opx[x, y] = (255, 205, 0)
                    elif y < layout.banner_h + BANNER_GAP:
                        opx[x, y] = (30, 30, 30)
                    else:
                        opx[x, y] = (80, 190, 255)
                else:
                    opx[x, y] = (80, 190, 255)
        scale = 5
        path = f"{args.png.rsplit('.', 1)[0]}-{i}-{page.title.lower()}.png"
        out.resize((src.size[0] * scale, src.size[1] * scale),
                   Image.NEAREST).save(path)
        paths.append(path)
    for path in paths:
        print(path)
    return 0


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s", stream=sys.stderr)

    if args.list_fonts:
        for path in font_paths():
            print(f"{'OK     ' if os.path.exists(path) else 'missing'}  {path}")
        return 0
    if args.probe:
        probe_report(args)
        return 0
    if args.print_unit:
        sys.stdout.write(unit_text(args))
        return 0
    if args.install:
        install_service(args)
        return 0
    if args.uninstall:
        uninstall_service()
        return 0

    try:
        from PIL import ImageFont  # noqa: F401
        if not args.simulate:
            import luma.oled  # noqa: F401
    except ImportError as e:
        LOG.error("missing dependency: %s", e)
        LOG.error("install with: sudo pip3 install --break-system-packages "
                  "luma.oled pillow smbus2")
        return 1

    body_font, bar_font, font_path = load_fonts(args.font, args.font_size)
    LOG.debug("font %s at %dpt", font_path, args.font_size)
    layout = Layout(args.width, args.height, args.banner_h, not args.no_header)
    LOG.debug("layout %s", layout.describe())

    if args.png:
        if not args.simulate:
            LOG.error("--png only makes sense with --simulate")
            return 2
        return dump_png(args, layout, (body_font, bar_font))

    display = Display(args)
    cpu = CpuMeter()
    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        stopping = True
        LOG.info("caught %s, clearing display", signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    beat = False
    frames = 0
    screen_idx = 0
    next_publish = 0.0
    last_flip = time.monotonic()
    deadline = time.monotonic()
    started_at = time.monotonic()
    ever_connected = False

    try:
        while not stopping:
            now = time.monotonic()

            if not display.ensure():
                publish_status(args.status_file, ok=False, panel="absent",
                               ever_connected=ever_connected,
                               fail_streak=display.fail_streak, frames=frames,
                               uptime_s=round(now - started_at, 1))
                if args.once:
                    return 1

                # Do not sit here forever pretending to be a status display.
                # With no panel this process used to run happily until the end
                # of time: systemctl showed a green "active (running)", hours
                # of uptime and real CPU consumed, while it had never once
                # talked to a display. A component that cannot do its job
                # should say so where someone will see it -- systemctl
                # --failed. See unit_text() for why 15s and not 60.
                if (not ever_connected and args.give_up_after > 0
                        and now - started_at >= args.give_up_after):
                    LOG.error(
                        "no panel found after %.0fs — exiting non-zero rather "
                        "than reporting healthy with nothing attached. Check "
                        "the wiring, then run: %s --probe  (errno 121 = bus "
                        "fine, nobody home; 110 = a line is held down). To "
                        "retry after plugging it in: systemctl reset-failed "
                        "%s && systemctl restart %s",
                        now - started_at, sys.argv[0], UNIT_NAME, UNIT_NAME)
                    return 1

                time.sleep(display.backoff(args.retry))
                deadline = time.monotonic()
                continue

            ever_connected = True

            screens = build_screens(args, layout, cpu.read())
            if now - last_flip >= args.page_sec:
                screen_idx += 1
                last_flip = now
            page = screens[screen_idx % len(screens)]
            alive = service_state(args.primary) == "active"

            try:
                with frame(display.device) as draw:
                    draw_page(draw, layout, (body_font, bar_font), page,
                              beat, alive)
                if args.simulate:
                    display.device.show()
            except Exception as e:
                publish_status(args.status_file, ok=False, panel="dropped",
                               ever_connected=True, error=str(e),
                               fail_streak=display.fail_streak, frames=frames,
                               uptime_s=round(time.monotonic() - started_at, 1))
                display.drop(e)
                if args.once:
                    return 1
                time.sleep(args.retry)
                deadline = time.monotonic()
                continue

            ONCE.clear("draw")
            beat = not beat
            frames += 1

            # Rate-limited: this is a 1Hz display read by a 1Hz consumer, so
            # writing the status file every frame buys nothing and costs a card
            # write on a board whose card is already the suspect.
            if now >= next_publish:
                publish_status(args.status_file, ok=True, panel="ok",
                               ever_connected=True, fail_streak=0,
                               frames=frames, page=page.title,
                               screens=len(screens),
                               bus=display.bus, addr=display.addr,
                               uptime_s=round(now - started_at, 1))
                next_publish = now + STATUS_WRITE_SEC
            if args.once:
                return 0

            # Schedule against a fixed deadline so a slow I2C write doesn't
            # make the clock drift.
            deadline += args.refresh
            sleep_for = deadline - time.monotonic()
            if sleep_for < 0:
                deadline = time.monotonic()
                sleep_for = 0
            time.sleep(sleep_for)
    finally:
        # A clean stop must not leave the last "ok" sitting there looking
        # current. systemd removes RuntimeDirectory on stop, but this script
        # also runs by hand, and SIGTERM is the common path.
        publish_status(args.status_file, ok=False, panel="stopped",
                       ever_connected=ever_connected, frames=frames,
                       uptime_s=round(time.monotonic() - started_at, 1))
        display.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
