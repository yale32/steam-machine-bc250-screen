#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
BC-250 screen monitor — live host stats on a Turing Smart Screen 3.5" (RevA,
serial USB35INCHIPSV2, 320x480 portrait) mounted in landscape (480x320).

  ┌ BC-250 SteamMachine (logo) ─────────────── 12:34:56 ┐
  │ CPU  usage %, cores/threads   │ MEM / VRAM / GTT    │
  │ GPU  usage %, CU count, power │ DISK  one bar per fs │
  │ TEMPS  every hwmon sensor     │ WIFI  status, SSID, IP│
  └─────────────────────────────────────────────────────┘

Usage:
  ./monitor.py                     drive the screen (runs until stopped)
  ./monitor.py --preview out.png   render one frame to a PNG, no screen needed

Configuration is via environment variables (see the block below). Host
readings live in hoststats.py; this file only draws and talks to the screen.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor", "turing-smart-screen-python"))

from hoststats import HostStats, Snapshot  # noqa: E402

# ── Configuration from environment ────────────────────────────────────────────
COM_PORT       = os.environ.get("COM_PORT", "AUTO")           # AUTO finds 1a86:5722 / USB35INCHIPSV2
BRIGHTNESS     = int(os.environ.get("BRIGHTNESS", "60"))      # 0-100
UPDATE_SECS    = float(os.environ.get("UPDATE_SECS", "1"))    # refresh interval
ORIENTATION    = os.environ.get("ORIENTATION", "reverse_landscape")  # reverse_landscape = rotated 180° from landscape
# Resetting the screen makes its firmware play its own boot logo for ~5 s. "auto" only resets when the
# previous run did not shut down cleanly (crash / first run), since only then can the screen be out of sync.
RESET_ON_START = os.environ.get("RESET_ON_START", "auto")     # auto | 1 (always) | 0 (never)
SPLASH_SECS    = float(os.environ.get("SPLASH_SECS", "2.5"))  # how long the Steam splash stays up before the stats
# Per-channel gain (R,G,B) applied to every frame just before it is sent. The panel's white point is
# cool, so a neutral grey looks blue; pulling blue (and a little green) down compensates. "1,1,1" = off.
# Default 1,0.82,0.76 was picked by eye for this screen (cell D4 of the grid); re-pick with:  monitor.py --calibrate
WHITE_BALANCE  = tuple(float(v) for v in os.environ.get("WHITE_BALANCE", "1,0.82,0.76").split(","))
FULL_REFRESH_SECS = float(os.environ.get("FULL_REFRESH_SECS", "600"))  # full repaint (~2 s) to heal glitches
DISK_MOUNTS    = [m for m in os.environ.get("DISK_MOUNTS", "").split(",") if m]  # e.g. "/,/home"; empty = auto-detect
RUN_DIR        = os.path.join(HERE, "run")                    # the driver library writes log.log into the cwd

CLEAN_MARKER = os.path.join(RUN_DIR, "screen-clean")           # exists only if the last run ended with a clean stop
stop = threading.Event()                                       # set by SIGTERM / SIGINT; the loop ends after the current frame

log = logging.getLogger("bc250screen")

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H = 480, 320             # logical size in landscape
PAD = 8                     # outer margin and gutter
COL_W = (W - 3 * PAD) // 2  # 228
XL, XR = PAD, PAD + COL_W + PAD

HEADER_H = 24               # title bar including its 2 px accent line
LOGO_H = 16                 # Steam logo height, matched to the title text
TILE = 16                   # dirty-tracking block size; W and H must both be multiples of it
LOGO_PATH = os.path.join(HERE, "assets", "steam-logo.png")

# Panels: (x, y, w, h). Left column is CPU / GPU / TEMPS, right is MEM / DISK / WIFI.
P_CPU   = (XL, 26,  COL_W, 88)
P_GPU   = (XL, 120, COL_W, 88)
P_TEMPS = (XL, 214, COL_W, 98)
P_MEM   = (XR, 26,  COL_W, 104)
P_DISK  = (XR, 136, COL_W, 98)
P_WIFI  = (XR, 240, COL_W, 72)

# ── Colours (RGB) — pure monochrome: every colour is a grey level (R=G=B) ─────
BG        = (10, 10, 10)      # page background, visible in the gaps between tiles
HEADER_BG = (0, 0, 0)         # title bar
HEADER_FG = (255, 255, 255)   # title bar text and logo
HEADER_DIM = (170, 170, 170)  # title bar clock
ACCENT    = (255, 255, 255)   # line under the title bar
PANEL_BG  = (38, 38, 38)      # tiles
BAR_BG    = (74, 74, 74)      # bar track, lighter than the tile so the fill/empty split is obvious
FG        = (255, 255, 255)   # primary text
DIM       = (150, 150, 150)   # secondary text

CPU_COL  = (255, 255, 255)
GPU_COL  = (255, 255, 255)
MEM_COL  = (255, 255, 255)
VRAM_COL = (215, 215, 215)
GTT_COL  = (170, 170, 170)
DISK_COLS = [(255, 255, 255), (215, 215, 215), (170, 170, 170)]

# With no colour to flag trouble, warnings are shown by inversion: black text on a white badge.
TEMP_OK, TEMP_WARN = (200, 200, 200), (255, 255, 255)   # light grey normally, white when warm
TEMP_WARN_AT, TEMP_HOT_AT = 70, 85                       # °C
BADGE_BG, BADGE_FG = (255, 255, 255), (0, 0, 0)

# Display order for temperature sensors; anything else follows alphabetically.
TEMP_ORDER = ["CPU", "GPU", "NVMe", "WiFi"]

FONT_DIRS = ["/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]


# ── Drawing helpers ───────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """DejaVu Sans Mono (ships with SteamOS); falls back to Pillow's built-in font."""
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def text(d, xy, s, size, fill, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)


def panel(d, rect):
    x, y, w, h = rect
    d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=6, fill=PANEL_BG)


def bar(d, x, y, w, h, pct, col):
    """Progress bar: dark track with a coloured fill for pct (0-100)."""
    d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=3, fill=BAR_BG)
    fill_w = int(w * max(0.0, min(100.0, pct)) / 100)
    if fill_w >= 2:
        d.rounded_rectangle([x, y, x + fill_w - 1, y + h - 1], radius=3, fill=col)


def badge(d, xy, s, size, anchor="la"):
    """Inverted text (black on a white pill): the monochrome way to flag a warning."""
    f = font(size, True)
    x0, y0, x1, y1 = d.textbbox(xy, s, font=f, anchor=anchor)
    d.rounded_rectangle([x0 - 3, y0 - 1, x1 + 3, y1 + 1], radius=3, fill=BADGE_BG)
    d.text(xy, s, font=f, fill=BADGE_FG, anchor=anchor)


def temp_text(d, xy, s, size, celsius, anchor="la"):
    """
    A temperature reading: light grey, white from 70 °C (the BC-250 idles in the 60s),
    and an inverted badge from 85 °C.
    """
    if celsius >= TEMP_HOT_AT:
        badge(d, xy, s, size, anchor)
    else:
        d.text(xy, s, font=font(size, True), fill=TEMP_WARN if celsius >= TEMP_WARN_AT else TEMP_OK, anchor=anchor)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pct_of(used, total):
    return 100.0 * used / total if used is not None and total else None


# ── Panels ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def steam_logo(height: int):
    """
    The Steam logo as a light RGBA icon. The source is a black disc on white, which
    would vanish on a dark UI, so its darkness becomes the alpha mask instead.
    Returns None if the asset is missing.
    """
    try:
        src = Image.open(LOGO_PATH).convert("L")
    except OSError:
        return None
    icon = Image.new("RGBA", (height, height), HEADER_FG + (255,))
    icon.putalpha(ImageOps.invert(src).resize((height, height), Image.LANCZOS))
    return icon


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def draw_header(img, d):
    d.rectangle([0, 0, W - 1, HEADER_H - 1], fill=HEADER_BG)
    d.rectangle([0, HEADER_H - 2, W - 1, HEADER_H - 1], fill=ACCENT)
    title = "BC-250 SteamMachine"
    text(d, (PAD, 3), title, 15, HEADER_FG, bold=True)
    logo = steam_logo(LOGO_H)
    if logo:
        x = PAD + int(font(15, True).getlength(title)) + 6
        img.paste(logo, (x, (HEADER_H - 2 - LOGO_H) // 2), logo)
    text(d, (W - PAD, 3), time.strftime("%H:%M:%S"), 15, HEADER_DIM, anchor="ra")


def draw_load(d, rect, label, col, pct, left, right):
    """CPU / GPU block: label, big percentage, bar, and one line of detail either side."""
    x, y, w, _ = rect
    panel(d, rect)
    text(d, (x + 8, y + 8), label, 16, col, bold=True)
    text(d, (x + w - 8, y + 2), "--" if pct is None else f"{pct:.0f}%", 28, FG, bold=True, anchor="ra")
    bar(d, x + 8, y + 38, w - 16, 10, pct or 0, col)
    text(d, (x + 8, y + 58), left, 12, FG, bold=True)
    text(d, (x + w - 8, y + 58), right, 12, FG, bold=True, anchor="ra")


def draw_temps(d, rect, temps):
    """Tile grid of every temperature sensor, highlighted as it gets hot."""
    x, y, w, h = rect
    panel(d, rect)
    text(d, (x + 8, y + 6), "TEMPS", 12, DIM, bold=True)
    if not temps:
        text(d, (x + 8, y + 30), "no sensors", 12, DIM)
        return

    order = {n: i for i, n in enumerate(TEMP_ORDER)}
    temps = sorted(temps, key=lambda t: (order.get(t.name, len(order)), t.name))[:8]
    rows = (len(temps) + 1) // 2
    cell_w, cell_h = (w - 16) // 2, min(36, (h - 26) // rows)
    for i, t in enumerate(temps):
        cx, cy = x + 8 + (i % 2) * cell_w, y + 24 + (i // 2) * cell_h
        val = f"{t.celsius:.0f}°C"
        if cell_h >= 30:        # roomy: label over a large value
            text(d, (cx, cy), t.name, 11, DIM)
            temp_text(d, (cx, cy + 12), val, 20, t.celsius)
        else:                   # crowded: label and value on one line
            text(d, (cx, cy + 2), t.name, 11, DIM)
            temp_text(d, (cx + cell_w - 8, cy), val, 15, t.celsius, anchor="ra")


def draw_row(d, x, y, w, label, col, pct, detail, bar_h=8, alert=False):
    """One labelled usage bar: label left (as a badge if alert), '<pct>%  <detail>' right, bar beneath."""
    if alert:
        badge(d, (x + 3, y), label, 12)
    else:
        text(d, (x, y), label, 12, col, bold=True)
    value = "--" if pct is None else f"{pct:.0f}%"
    text(d, (x + w, y), f"{value}  {detail}" if detail else value, 12, FG, bold=True, anchor="ra")
    bar(d, x, y + 17, w, bar_h, pct or 0, col)


def draw_memory(d, rect, s: Snapshot):
    x, y, w, _ = rect
    panel(d, rect)
    rows = [
        ("MEM",  MEM_COL,  s.mem_pct,
         f"{fmt_bytes(s.mem_used)} / {fmt_bytes(s.mem_total)}"),
        ("VRAM", VRAM_COL, pct_of(s.vram_used, s.vram_total),
         f"{fmt_bytes(s.vram_used)} / {fmt_bytes(s.vram_total)}" if s.vram_total else ""),
        ("GTT",  GTT_COL,  pct_of(s.gtt_used, s.gtt_total),
         f"{fmt_bytes(s.gtt_used)} / {fmt_bytes(s.gtt_total)}" if s.gtt_total else ""),
    ]
    for i, (label, col, pct, detail) in enumerate(rows):
        draw_row(d, x + 8, y + 8 + i * 31, w - 16, label, col, pct, detail)


def draw_disks(d, rect, disks):
    x, y, w, h = rect
    panel(d, rect)
    text(d, (x + 8, y + 6), "DISK", 12, DIM, bold=True)
    disks = disks[:3]           # the panel has room for three rows
    if not disks:
        text(d, (x + 8, y + 30), "no disks", 12, DIM)
        return
    pitch = min(44, (h - 26) // len(disks))
    for i, dk in enumerate(disks):
        draw_row(d, x + 8, y + 24 + i * pitch, w - 16, dk.label, DISK_COLS[i % len(DISK_COLS)], dk.percent,
                 f"{fmt_bytes(dk.used)} / {fmt_bytes(dk.total)}", bar_h=10 if pitch >= 30 else 6,
                 alert=dk.percent >= 90)     # nearly full


def draw_wifi(d, rect, wifi):
    """WiFi status, SSID and signal, with the host's IP address underneath."""
    x, y, w, _ = rect
    panel(d, rect)
    text(d, (x + 8, y + 6), "WIFI", 12, DIM, bold=True)
    if wifi.iface is None:
        text(d, (x + w - 8, y + 6), "NO ADAPTER", 12, DIM, bold=True, anchor="ra")
    elif wifi.connected:
        text(d, (x + w - 8, y + 6), "CONNECTED", 12, FG, bold=True, anchor="ra")
    else:
        badge(d, (x + w - 8, y + 6), "DISCONNECTED", 12, anchor="ra")

    ssid = (wifi.ssid or "(hidden)") if wifi.connected else "--"
    text(d, (x + 8, y + 22), clip(ssid, 16), 16, FG, bold=True)
    if wifi.connected and wifi.signal_dbm is not None:
        text(d, (x + w - 8, y + 26), f"{wifi.signal_dbm} dBm", 12, DIM, anchor="ra")
    text(d, (x + 8, y + 46), f"IP  {wifi.ip or '--'}", 14, FG, bold=True)


def render(s: Snapshot) -> Image.Image:
    """Draw one full 480x320 frame from a stats snapshot."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    draw_header(img, d)
    draw_load(d, P_CPU, "CPU", CPU_COL, s.cpu_pct,
              f"{s.cores} cores active", f"{s.threads} threads")
    draw_load(d, P_GPU, "GPU", GPU_COL, s.gpu_pct,
              f"{s.cu} CU active" if s.cu else "CU --",
              f"{s.gpu_power_w:.0f} W" if s.gpu_power_w else "")
    draw_temps(d, P_TEMPS, s.temps)
    draw_memory(d, P_MEM, s)
    draw_disks(d, P_DISK, s.disks)
    draw_wifi(d, P_WIFI, s.wifi)
    return img


# ── Splash ────────────────────────────────────────────────────────────────────

def splash_image() -> Image.Image:
    """Start-up screen: the Steam logo, the machine name and a 'starting' line on black."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 120
    top = 58
    logo = steam_logo(size)
    if logo:
        img.paste(logo, ((W - size) // 2, top), logo)
    text(d, (W // 2, top + size + 18), "BC-250 SteamMachine", 22, HEADER_FG, bold=True, anchor="ma")
    text(d, (W // 2, top + size + 50), "starting…", 13, HEADER_DIM, anchor="ma")
    return img


# ── White balance ─────────────────────────────────────────────────────────────

def balance(img: Image.Image, gains=None) -> Image.Image:
    """Scale each colour channel by its gain (see WHITE_BALANCE). Applied to the frame that is *sent*."""
    gains = WHITE_BALANCE if gains is None else gains
    if tuple(gains) == (1.0, 1.0, 1.0):
        return img
    arr = np.asarray(img, dtype=np.float32) * np.array(gains, dtype=np.float32)
    return Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8))


# --calibrate shows a grid of candidate corrections: blue gain across the columns, green gain down the rows.
CAL_B = [0.94, 0.88, 0.82, 0.76, 0.70]
CAL_G = [1.00, 0.94, 0.88, 0.82]


def calibration_image() -> Image.Image:
    """
    Grid of neutral-grey cells (a light and a mid grey each), every cell with its own white
    balance applied. Columns are labelled 1-5 (blue gain), rows A-D (green gain), red is 1.00.
    """
    left, top = 64, 34
    cw, ch = (W - left) // len(CAL_B), (H - top) // len(CAL_G)
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for c, b in enumerate(CAL_B):
        text(d, (left + c * cw + cw // 2, 2), str(c + 1), 16, (255, 255, 255), bold=True, anchor="ma")
        text(d, (left + c * cw + cw // 2, 20), f"B{b:.2f}", 10, (200, 200, 200), anchor="ma")
    for r, g in enumerate(CAL_G):
        text(d, (30, top + r * ch + ch // 2 - 14), "ABCD"[r], 16, (255, 255, 255), bold=True, anchor="ma")
        text(d, (30, top + r * ch + ch // 2 + 4), f"G{g:.2f}", 10, (200, 200, 200), anchor="ma")
    out = None
    for r, _g in enumerate(CAL_G):
        for c, _b in enumerate(CAL_B):
            x0, y0 = left + c * cw, top + r * ch
            d.rectangle([x0 + 2, y0 + 2, x0 + cw - 3, y0 + ch // 2 - 1], fill=(235, 235, 235))
            d.rectangle([x0 + 2, y0 + ch // 2, x0 + cw - 3, y0 + ch - 3], fill=(140, 140, 140))
    out = np.asarray(img, dtype=np.float32).copy()
    for r, g in enumerate(CAL_G):
        for c, b in enumerate(CAL_B):
            x0, y0 = left + c * cw, top + r * ch
            out[y0:y0 + ch, x0:x0 + cw] *= np.array((1.0, g, b), dtype=np.float32)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


# ── Screen ────────────────────────────────────────────────────────────────────

def dirty_rects(prev: Image.Image, cur: Image.Image):
    """
    Rectangles covering everything that differs between two frames.

    The screen link moves only ~80k pixels/s (a full repaint takes ~1.9 s), so
    frames are diffed in TILE x TILE blocks and adjacent changed tiles in a row
    are merged into one rectangle. Each send has little fixed cost, so this is
    close to proportional to how much of the picture actually changed.
    """
    changed = np.any(np.asarray(prev) != np.asarray(cur), axis=2)               # (H, W) per-pixel
    tiles = changed.reshape(H // TILE, TILE, W // TILE, TILE).any(axis=(1, 3))  # (H/T, W/T) per-tile
    rects = []
    for row, flags in enumerate(tiles):
        col = 0
        while col < len(flags):
            if not flags[col]:
                col += 1
                continue
            start = col
            while col < len(flags) and flags[col]:
                col += 1
            rects.append((start * TILE, row * TILE, (col - start) * TILE, TILE))
    return rects


class Screen:
    """Wraps the Turing RevA driver: open, push frames (only changed regions), close."""

    def __init__(self):
        self.lcd = None
        self.prev = None
        self.last_full = 0.0

    def open(self):
        from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation
        orient = {"landscape": Orientation.LANDSCAPE,
                  "reverse_landscape": Orientation.REVERSE_LANDSCAPE}[ORIENTATION]
        # The panel is natively 320x480 portrait; orientation rotates the logical canvas to 480x320.
        self.lcd = LcdCommRevA(com_port=COM_PORT, display_width=320, display_height=480)

        was_clean = os.path.exists(CLEAN_MARKER)
        if was_clean:
            os.unlink(CLEAN_MARKER)     # from here on the screen is in use, so a crash must be treated as unclean
        if RESET_ON_START == "1" or (RESET_ON_START == "auto" and not was_clean):
            log.info("resetting screen (%s)", "forced" if RESET_ON_START == "1" else "previous run did not stop cleanly")
            self.lcd.Reset()        # closes and reopens the port; the screen re-enumerates for ~5 s
        else:
            log.info("skipping reset (%s)", "disabled" if RESET_ON_START == "0" else "previous run stopped cleanly")
        self.lcd.InitializeComm()
        self.lcd.ScreenOn()
        self.lcd.SetBrightness(level=BRIGHTNESS)
        self.lcd.SetOrientation(orientation=orient)
        self.prev = None            # force a full repaint after (re)opening
        log.info("screen ready (%s, brightness %d)", ORIENTATION, BRIGHTNESS)

    def show(self, img: Image.Image) -> int:
        """Push a frame, sending only the tiles that changed. Returns pixels sent."""
        now = time.monotonic()
        if self.prev is None or now - self.last_full > FULL_REFRESH_SECS:
            rects = [(0, 0, W, H)]              # first frame, or periodic repaint to heal any glitch
            self.last_full = now
        else:
            rects = dirty_rects(self.prev, img)
        for (x, y, w, h) in rects:
            self.lcd.DisplayPILImage(img.crop((x, y, x + w, y + h)), x=x, y=y, image_width=w, image_height=h)
        self.prev = img
        return sum(w * h for (_, _, w, h) in rects)

    def close(self, screen_off=True):
        lcd, self.lcd, self.prev = self.lcd, None, None
        if lcd is None:
            return
        try:
            if screen_off:
                lcd.ScreenOff()
            lcd.closeSerial()
            if screen_off:
                open(CLEAN_MARKER, "w").close()     # screen was left idle and in sync: the next start can skip the reset
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--preview", metavar="PNG", help="render one frame to a PNG and exit (no screen needed)")
    ap.add_argument("--preview-splash", metavar="PNG", help="render the start-up splash to a PNG and exit")
    ap.add_argument("--calibrate", action="store_true",
                    help="show a grid of white-balance options (green gain by blue gain) and wait")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    preview = os.path.abspath(args.preview) if args.preview else None
    preview_splash = os.path.abspath(args.preview_splash) if args.preview_splash else None
    os.makedirs(RUN_DIR, exist_ok=True)
    os.chdir(RUN_DIR)

    if preview_splash:
        splash_image().save(preview_splash)
        log.info("wrote %s", preview_splash)
        return

    # Stopping must never interrupt a frame mid-transfer, or the screen is left waiting for pixel data and
    # swallows the next commands. So the handlers only set a flag; the loops below check it between frames.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if args.calibrate:
        screen = Screen()
        try:
            screen.open()
            screen.show(calibration_image())
            log.info("calibration grid shown: pick the cell (row letter + column number) whose greys look neutral")
            stop.wait()
        finally:
            screen.close()
        return

    stats = HostStats(DISK_MOUNTS or None)

    if preview:
        stats.collect()             # CPU % and GPU % are deltas, so the first sample is empty
        time.sleep(1.0)
        render(stats.collect()).save(preview)
        log.info("wrote %s", preview)
        return

    screen = Screen()
    frames = 0
    try:
        while not stop.is_set():
            try:
                if screen.lcd is None:
                    screen.open()
                    screen.show(balance(splash_image()))
                    log.info("splash shown")
                    if stop.wait(SPLASH_SECS):
                        break
                t0 = time.monotonic()   # after open() and the splash, which are not frame costs
                frame = balance(render(stats.collect()))
                t1 = time.monotonic()
                pixels = screen.show(frame)
                frames += 1
                if frames in (1, 2, 3, 10, 20) or frames % 300 == 0:
                    log.info("frame %d: render %.0f ms, send %.0f ms (%d px)",
                             frames, (t1 - t0) * 1000, (time.monotonic() - t1) * 1000, pixels)
            except Exception as e:  # screen unplugged / serial error: reconnect instead of dying
                log.error("display error (%s: %s), reconnecting in 3 s", type(e).__name__, e)
                screen.close(screen_off=False)
                stop.wait(3)
                continue
            stop.wait(max(0.05, UPDATE_SECS - (time.monotonic() - t0)))
    finally:
        screen.close()


if __name__ == "__main__":
    main()
