#!/usr/bin/env python3
"""
Google Timeline (location-history.json) -> stylish animated movie.

Parses the mobile-export semantic timeline (activity / visit / timelinePath),
builds a time-warped playback (idle periods run faster), renders frames with a
smooth follow camera over CartoDB basemap tiles, and pipes them to ffmpeg.

Example:
  python timeline_movie.py --start 2025-07-01 --end 2025-07-31 \
      --speedup 1800 --orientation both -o july
"""

import argparse
import io
import json
import math
import multiprocessing as mp
import os
import re
import subprocess
import sys
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

JST = timezone(timedelta(hours=9))
GEO_RE = re.compile(r"geo:(-?[\d.]+),(-?[\d.]+)")
TILE_SIZE = 256

STYLES = {
    "dark": {
        "tile_url": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "bg": (18, 18, 22),
        "trail": (0, 229, 255),      # cyan
        "trail_tail": (120, 80, 255),  # violet tail
        "head_core": (255, 255, 255),
        "text": (245, 245, 245),
        "text_dim": (150, 150, 158),
        "tile_dim": 0.92,            # darken tiles a bit for contrast
    },
    "light": {
        "tile_url": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "bg": (240, 240, 238),
        "trail": (0, 122, 255),
        "trail_tail": (255, 60, 120),
        "head_core": (10, 10, 10),
        "text": (30, 30, 34),
        "text_dim": (120, 120, 126),
        "tile_dim": 1.0,
    },
}

FONT_CANDIDATES = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ---------------------------------------------------------------- parsing

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_geo(s):
    m = GEO_RE.match(s or "")
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def load_points(path):
    """Return sorted list of (epoch_seconds, lat, lon)."""
    with open(path) as f:
        data = json.load(f)
    pts = []
    for e in data:
        try:
            t0 = parse_time(e["startTime"])
            t1 = parse_time(e["endTime"])
        except (KeyError, ValueError):
            continue
        if "timelinePath" in e:
            for p in e["timelinePath"]:
                g = parse_geo(p.get("point"))
                if g:
                    t = t0 + float(p.get("durationMinutesOffsetFromStartTime", 0)) * 60
                    pts.append((t, g[0], g[1]))
        elif "activity" in e:
            a = e["activity"]
            g0, g1 = parse_geo(a.get("start")), parse_geo(a.get("end"))
            if g0:
                pts.append((t0, g0[0], g0[1]))
            if g1:
                pts.append((t1, g1[0], g1[1]))
        elif "visit" in e:
            g = parse_geo(e["visit"].get("topCandidate", {}).get("placeLocation"))
            if g:
                pts.append((t0, g[0], g[1]))
                pts.append((t1, g[0], g[1]))
    pts.sort(key=lambda p: p[0])

    # dedupe identical timestamps, drop teleport glitches (> 1200 km/h)
    out = []
    for t, la, lo in pts:
        if out:
            pt, pla, plo = out[-1]
            if t - pt < 1e-3:
                continue
            d = haversine_km(pla, plo, la, lo)
            if d / max((t - pt) / 3600.0, 1e-9) > 1200 and d > 5:
                continue
        out.append((t, la, lo))
    return out


def haversine_km(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------- projection

def merc(lat, lon):
    """WGS84 -> normalized web-mercator [0,1)x[0,1)."""
    x = (lon + 180.0) / 360.0
    lat = max(-85.05112878, min(85.05112878, lat))
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


# ---------------------------------------------------------------- time warp

def detect_homes(points, min_days=21.0, merge_km=40.0):
    """Return [(lat, lon), ...] of every cell lived in for min_days total.

    People move; each long-term residence (and e.g. a parents' home visited
    every other weekend) should count as "home" so that only real trips play
    slowly. Cells within merge_km of an accepted home are merged into it.
    """
    from collections import defaultdict
    acc = defaultdict(float)
    for i in range(1, len(points)):
        t0, la, lo = points[i - 1]
        dt = points[i][0] - t0
        if 0 < dt < 7 * 86400:
            acc[(round(la, 2), round(lo, 2))] += dt
    homes = []
    for cell, dwell in sorted(acc.items(), key=lambda kv: -kv[1]):
        if dwell < min_days * 86400:
            break
        if all(haversine_km(cell[0], cell[1], h[0], h[1]) > merge_km
               for h in homes):
            homes.append(cell)
    return homes or [max(acc, key=acc.get)]


def build_timewarp(points, t_start, t_end, speedup, idle_speedup,
                   home=None, home_radius_km=50.0, home_speedup=1.0,
                   pan_kms=2500.0, min_leg_s=1.5,
                   trip_min_s=0.0, trip_far_km=100.0,
                   idle_kmh=0.7, idle_min_s=900, leg_km=200.0):
    """Piecewise-linear map real time -> video time.

    Moving intervals play at `speedup` (real s per video s); stationary
    stretches (< idle_kmh for at least idle_min_s) run `idle_speedup` times
    faster.  If `home` is set, intervals within `home_radius_km` of it run
    `home_speedup` times faster still — daily life fast-forwards, trips play
    slow enough to see.  Fast vehicles never teleport: playback is slowed so
    the head crosses at most `pan_kms` km per video second, and any single
    hop longer than `leg_km` (flights) takes at least `min_leg_s` seconds.
    Returns (breakpoints [(real_t, video_t)], total_video_seconds).
    """
    # classify each inter-point interval: [t0, t1, moving, near_home, v, d]
    ivals = []
    prev = None
    for t, la, lo in points:
        if t < t_start or t > t_end:
            prev = (max(min(t, t_end), t_start), la, lo) if t < t_start else prev
            if t > t_end:
                break
            continue
        if prev is not None:
            dt = t - prev[0]
            if dt > 0:
                d = haversine_km(prev[1], prev[2], la, lo)
                v = d / (dt / 3600.0)
                moving = v >= idle_kmh and d >= 0.03
                dmin = (min(haversine_km(la, lo, h[0], h[1]) for h in home)
                        if home else 0.0)
                near = home is not None and dmin < home_radius_km
                ivals.append([prev[0], t, moving, near, v, d, dmin])
        prev = (t, la, lo)
    if not ivals:
        ivals = [[t_start, t_end, False, True, 0.0, 0.0, 0.0]]
    # pad to full range
    if ivals[0][0] > t_start:
        ivals.insert(0, [t_start, ivals[0][0], False, ivals[0][3], 0.0, 0.0,
                         ivals[0][6]])
    if ivals[-1][1] < t_end:
        ivals.append([ivals[-1][1], t_end, False, ivals[-1][3], 0.0, 0.0,
                      ivals[-1][6]])

    # a stationary stretch only counts as idle if long enough in total
    run_start = None
    for i, iv in enumerate(ivals + [None]):
        if iv is not None and not iv[2]:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            span = ivals[i - 1][1] - ivals[run_start][0]
            if span < idle_min_s:
                for j in range(run_start, i):
                    ivals[j][2] = True
            run_start = None

    vdts = []
    for t0, t1, moving, near, v, d, dmin in ivals:
        rate = speedup
        if not moving:
            rate *= idle_speedup
        if near:
            rate *= home_speedup
        if pan_kms and v > 0:
            rate = min(rate, pan_kms * 3600.0 / v)
        vdt = (t1 - t0) / rate
        if min_leg_s and d >= leg_km:
            vdt = max(vdt, min_leg_s)
        vdts.append(vdt)

    # guarantee each far trip enough screen time to see local movement:
    # inflate the non-transit part of any contiguous far-away span below
    # trip_min_s of video. Brief passes through the home radius (< 6 h,
    # e.g. driving past the boundary) do not split a trip.
    if trip_min_s:
        n = len(ivals)
        runs = []  # (near, i0, i1_exclusive)
        for k in range(n):
            if runs and runs[-1][0] == ivals[k][3]:
                runs[-1][2] = k + 1
            else:
                runs.append([ivals[k][3], k, k + 1])
        spans = []  # away spans, bridged
        cur = None
        for idx, (near, i0, i1) in enumerate(runs):
            if not near:
                cur = [i0, i1] if cur is None else [cur[0], i1]
            else:
                dur = ivals[i1 - 1][1] - ivals[i0][0]
                bridge = (cur is not None and dur < 6 * 3600 and
                          idx + 1 < len(runs))
                if not bridge and cur is not None:
                    spans.append(cur)
                    cur = None
        if cur is not None:
            spans.append(cur)
        for i, j in spans:
            if max(ivals[k][6] for k in range(i, j)) <= trip_far_km:
                continue
            local = [k for k in range(i, j)
                     if not ivals[k][3] and ivals[k][4] <= 170.0]
            local_v = sum(vdts[k] for k in local)
            # longer trips earn a proportionally larger floor
            days = (ivals[j - 1][1] - ivals[i][0]) / 86400.0
            floor_v = max(trip_min_s, trip_min_s * 0.4 * days)
            if 0 < local_v < floor_v:
                f = floor_v / local_v
                for k in local:
                    vdts[k] *= f

    bps = [(ivals[0][0], 0.0)]
    vtot = 0.0
    for (t0, t1, *_), vdt in zip(ivals, vdts):
        vtot += vdt
        bps.append((t1, vtot))
    return bps, vtot


def solve_speedup(points, t_start, t_end, duration, idle_speedup,
                  home=None, home_radius_km=50.0, home_speedup=1.0,
                  pan_kms=2500.0, min_leg_s=1.5,
                  trip_min_s=0.0, trip_far_km=100.0):
    """Find speedup so that total video length == duration (bisection:
    the pan/leg/trip floors make the mapping nonlinear)."""
    lo, hi = 1.0, 1e8
    for _ in range(48):
        mid = math.sqrt(lo * hi)
        _, tot = build_timewarp(points, t_start, t_end, mid, idle_speedup,
                                home, home_radius_km, home_speedup,
                                pan_kms, min_leg_s, trip_min_s, trip_far_km)
        if tot > duration:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def warp_real_time(bps, video_t):
    """Invert breakpoints: video time -> real time."""
    lo, hi = 0, len(bps) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if bps[mid][1] < video_t:
            lo = mid + 1
        else:
            hi = mid
    i = max(1, lo)
    (r0, v0), (r1, v1) = bps[i - 1], bps[i]
    if v1 <= v0:
        return r1
    f = (video_t - v0) / (v1 - v0)
    return r0 + f * (r1 - r0)


# ---------------------------------------------------------------- tiles

class TileProvider:
    def __init__(self, url_tpl, cache_dir, bg, fetchers=8):
        self.url = url_tpl
        self.dir = cache_dir
        self.bg = bg
        os.makedirs(cache_dir, exist_ok=True)
        self.mem = {}
        self.order = []
        self.lock = threading.Lock()
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = "timeline-movie/1.0 (personal use)"
        self.pool = ThreadPoolExecutor(max_workers=fetchers)
        self.fail = Image.new("RGB", (TILE_SIZE, TILE_SIZE), bg)

    def _fetch(self, z, x, y):
        n = 2 ** z
        x %= n
        if y < 0 or y >= n:
            return self.fail
        key = (z, x, y)
        with self.lock:
            if key in self.mem:
                return self.mem[key]
        fp = os.path.join(self.dir, f"{z}_{x}_{y}.png")
        img = None
        if os.path.exists(fp):
            try:
                img = Image.open(fp).convert("RGB")
            except OSError:
                img = None
        if img is None:
            for attempt in range(3):
                try:
                    r = self.sess.get(self.url.format(z=z, x=x, y=y), timeout=15)
                    if r.status_code == 200:
                        img = Image.open(io.BytesIO(r.content)).convert("RGB")
                        # atomic write: safe under concurrent processes
                        tmp = f"{fp}.{os.getpid()}.tmp"
                        with open(tmp, "wb") as f:
                            f.write(r.content)
                        os.replace(tmp, fp)
                        break
                    if r.status_code == 404:
                        break
                except (requests.RequestException, OSError):
                    _time.sleep(0.5 * (attempt + 1))
            if img is None:
                img = self.fail
        with self.lock:
            self.mem[key] = img
            self.order.append(key)
            while len(self.order) > 1024:
                old = self.order.pop(0)
                self.mem.pop(old, None)
        return img

    def mosaic(self, z, tx0, ty0, tx1, ty1):
        """Return PIL image of tiles [tx0..tx1] x [ty0..ty1] at zoom z."""
        coords = [(z, tx, ty) for ty in range(ty0, ty1 + 1)
                  for tx in range(tx0, tx1 + 1)]
        tiles = list(self.pool.map(lambda c: self._fetch(*c), coords))
        w = (tx1 - tx0 + 1) * TILE_SIZE
        h = (ty1 - ty0 + 1) * TILE_SIZE
        img = Image.new("RGB", (w, h), self.bg)
        i = 0
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                img.paste(tiles[i], ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))
                i += 1
        return img


# ---------------------------------------------------------------- helpers

def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_tracked_text(draw, xy, text, font, fill, tracking=0.0, anchor_right=False):
    """Draw text with letterspacing. tracking = extra px between chars."""
    widths = [draw.textlength(c, font=font) for c in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x, y = xy
    if anchor_right:
        x -= total
    for c, w in zip(text, widths):
        draw.text((x, y), c, font=font, fill=fill)
        x += w + tracking
    return total


def lerp(a, b, f):
    return a + (b - a) * f


def make_vignette(w, h):
    """Subtle radial vignette + top/bottom gradients for HUD legibility."""
    small = Image.new("L", (w // 8, h // 8), 0)
    d = ImageDraw.Draw(small)
    sw, sh = small.size
    for i in range(sh):
        # vertical gradient: darker at very top and bottom
        dy = min(i, sh - 1 - i) / sh
        v = int(90 * max(0.0, 1 - dy * 7))
        d.line([(0, i), (sw, i)], fill=v)
    small = small.filter(ImageFilter.GaussianBlur(4))
    alpha = small.resize((w, h), Image.BILINEAR)
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black.putalpha(alpha)
    return black


# ---------------------------------------------------------------- renderer

class Renderer:
    def __init__(self, args, points, style, size, fetchers=8):
        self.a = args
        self.pts = points
        self.style = style
        self.w, self.h = size
        # per-style cache dir: dark and light tiles must never mix
        cache_dir = os.path.join(args.tile_cache, args.style)
        self.tiles = TileProvider(style["tile_url"], cache_dir,
                                  style["bg"], fetchers=fetchers)
        # precompute mercator coords
        self.mx = [merc(la, lo) for _, la, lo in points]
        self.lls = [(la, lo) for _, la, lo in points]
        self.times = [p[0] for p in points]
        self.vignette = make_vignette(self.w, self.h)
        base = min(self.w, self.h)
        self.f_date = load_font(int(base * 0.052))
        self.f_time = load_font(int(base * 0.030))
        self.f_small = load_font(max(11, int(base * 0.013)))

    # ---- data access
    def _index_at(self, t):
        lo, hi = 0, len(self.times)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.times[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        return lo  # first index with time >= t

    def pos_at(self, t):
        i = self._index_at(t)
        if i == 0:
            return self.mx[0]
        if i >= len(self.times):
            return self.mx[-1]
        t0, t1 = self.times[i - 1], self.times[i]
        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        (x0, y0), (x1, y1) = self.mx[i - 1], self.mx[i]
        return lerp(x0, x1, f), lerp(y0, y1, f)

    def pos_ll_at(self, t):
        i = self._index_at(t)
        if i == 0:
            return self.lls[0]
        if i >= len(self.times):
            return self.lls[-1]
        t0, t1 = self.times[i - 1], self.times[i]
        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        (a0, o0), (a1, o1) = self.lls[i - 1], self.lls[i]
        return lerp(a0, a1, f), lerp(o0, o1, f)

    def trail_at(self, t, window):
        i1 = self._index_at(t)
        i0 = self._index_at(t - window)
        seq = []
        if i0 > 0:
            # interpolated tail entry point
            seq.append((t - window,) + self.pos_at(t - window))
        for i in range(i0, min(i1, len(self.times))):
            seq.append((self.times[i],) + self.mx[i])
        seq.append((t,) + self.pos_at(t))
        return seq

    # ---- camera
    def target_zoom(self, trail, head):
        xs = [p[1] for p in trail] + [head[0]]
        ys = [p[2] for p in trail] + [head[1]]
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        margin = 2.2
        zx = math.log2(self.w / (TILE_SIZE * max(span_x * margin, 1e-12)))
        zy = math.log2(self.h / (TILE_SIZE * max(span_y * margin, 1e-12)))
        z = min(zx, zy)
        return max(self.a.zoom_min, min(self.a.zoom_max, z))

    # ---- frame
    def render_frame(self, real_t, video_t, cam, period, fade_alpha):
        w, h, st = self.w, self.h, self.style
        cx, cy, zoom = cam
        scale = TILE_SIZE * (2 ** zoom)  # world px at fractional zoom

        # --- basemap via affine transform of an integer-zoom mosaic
        zint = max(2, min(18, round(zoom)))
        s = 2 ** (zoom - zint)             # screen px per zint px
        n = 2 ** zint
        # viewport in zint pixel coords
        cxz, cyz = cx * TILE_SIZE * n, cy * TILE_SIZE * n
        half_w, half_h = (w / 2) / s, (h / 2) / s
        tx0 = math.floor((cxz - half_w) / TILE_SIZE)
        tx1 = math.floor((cxz + half_w) / TILE_SIZE)
        ty0 = max(0, math.floor((cyz - half_h) / TILE_SIZE))
        ty1 = min(n - 1, math.floor((cyz + half_h) / TILE_SIZE))
        mosaic = self.tiles.mosaic(zint, tx0, ty0, tx1, ty1)
        # output (x,y) -> mosaic coords: u = (x - w/2)/s + cxz - tx0*256
        inv = 1.0 / s
        u0 = cxz - tx0 * TILE_SIZE - (w / 2) * inv
        v0 = cyz - ty0 * TILE_SIZE - (h / 2) * inv
        frame = mosaic.transform((w, h), Image.AFFINE,
                                 (inv, 0, u0, 0, inv, v0),
                                 resample=Image.BILINEAR,
                                 fillcolor=st["bg"])
        if st["tile_dim"] < 1.0:
            frame = Image.eval(frame, lambda px: int(px * st["tile_dim"]))
        frame = frame.convert("RGBA")

        # --- trail (2x supersampled overlay)
        ss = 2
        ov = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        trail = self.trail_at(real_t, self.a.trail_hours * 3600)

        def to_screen(x, y):
            return ((x - cx) * scale + w / 2) * ss, ((y - cy) * scale + h / 2) * ss

        pts = []
        last = None
        for tt, x, y in trail:
            sx, sy = to_screen(x, y)
            if last is None or abs(sx - last[0]) + abs(sy - last[1]) > 2.5:
                age = (real_t - tt) / max(self.a.trail_hours * 3600, 1)
                pts.append((sx, sy, max(0.0, min(1.0, 1 - age))))
                last = (sx, sy)
        if last is None or len(pts) == 0:
            hx, hy = to_screen(*self.pos_at(real_t))
            pts = [(hx, hy, 1.0)]

        lw_glow = max(6, int(min(w, h) * 0.010)) * ss
        lw_core = max(2, int(min(w, h) * 0.0032)) * ss
        tr, tg, tb = st["trail"]
        qr, qg, qb = st["trail_tail"]
        for i in range(1, len(pts)):
            x0, y0, k0 = pts[i - 1]
            x1, y1, k1 = pts[i]
            if max(x0, x1) < -50 or min(x0, x1) > w * ss + 50:
                continue
            if max(y0, y1) < -50 or min(y0, y1) > h * ss + 50:
                continue
            k = (k0 + k1) / 2
            cr = int(lerp(qr, tr, k)); cg = int(lerp(qg, tg, k)); cb = int(lerp(qb, tb, k))
            od.line([(x0, y0), (x1, y1)], fill=(cr, cg, cb, int(75 * k ** 0.7 + 14)),
                    width=lw_glow)
        for i in range(1, len(pts)):
            x0, y0, k0 = pts[i - 1]
            x1, y1, k1 = pts[i]
            k = (k0 + k1) / 2
            cr = int(lerp(qr, tr, k)); cg = int(lerp(qg, tg, k)); cb = int(lerp(qb, tb, k))
            od.line([(x0, y0), (x1, y1)], fill=(cr, cg, cb, int(205 * k ** 0.6 + 50)),
                    width=lw_core)

        # head: pulsing glow + white core
        hx, hy = to_screen(*self.pos_at(real_t))
        pulse = 0.5 + 0.5 * math.sin(video_t * 2 * math.pi * 0.9)
        r_glow = (min(w, h) * (0.016 + 0.006 * pulse)) * ss
        r_core = (min(w, h) * 0.0042) * ss
        for rr, aa in ((r_glow, 60), (r_glow * 0.62, 90), (r_glow * 0.36, 130)):
            od.ellipse([hx - rr, hy - rr, hx + rr, hy + rr], fill=(tr, tg, tb, aa))
        od.ellipse([hx - r_core, hy - r_core, hx + r_core, hy + r_core],
                   fill=st["head_core"] + (255,))
        ov = ov.resize((w, h), Image.LANCZOS)
        frame.alpha_composite(ov)

        # --- vignette + HUD
        frame.alpha_composite(self.vignette)
        d = ImageDraw.Draw(frame)
        dt = datetime.fromtimestamp(real_t, JST)
        mgn = int(min(w, h) * 0.045)
        date_s = dt.strftime("%Y.%m.%d")
        wd = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][dt.weekday()]
        time_s = dt.strftime("%H:%M") + "  " + wd
        tw = draw_tracked_text(d, (mgn, mgn), date_s, self.f_date,
                               st["text"], tracking=int(min(w, h) * 0.008))
        draw_tracked_text(d, (mgn, mgn + self.f_date.size * 1.25), time_s,
                          self.f_time, st["text_dim"],
                          tracking=int(min(w, h) * 0.006))

        # progress bar along the bottom
        t0, t1 = period
        frac = 0.0 if t1 <= t0 else (real_t - t0) / (t1 - t0)
        frac = max(0.0, min(1.0, frac))
        by = h - mgn
        d.line([(mgn, by), (w - mgn, by)], fill=st["text_dim"] + (70,), width=2)
        d.line([(mgn, by), (mgn + (w - 2 * mgn) * frac, by)],
               fill=st["trail"] + (220,), width=2)
        d.ellipse([mgn + (w - 2 * mgn) * frac - 3, by - 3,
                   mgn + (w - 2 * mgn) * frac + 3, by + 3],
                  fill=st["trail"] + (255,))

        d.text((mgn, by - int(self.f_small.size * 1.8)),
               "© OpenStreetMap © CARTO", font=self.f_small,
               fill=st["text_dim"] + (140,))

        out = frame.convert("RGB")
        if fade_alpha < 1.0:
            out = Image.eval(out, lambda px: int(px * fade_alpha))
        return out


# ---------------------------------------------------------------- encoding

ENCODER_ARGS = {
    "x264": ["-c:v", "libx264", "-preset", "medium", "-crf", "18"],
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
              "-cq", "19", "-b:v", "0"],
    "videotoolbox": ["-c:v", "h264_videotoolbox", "-b:v", "12M"],
}


def _encoder_works(ffmpeg, name):
    """Try a tiny test encode with the given codec args."""
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1:r=10"]
            + ENCODER_ARGS[name] + ["-f", "null", "-"],
            capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_encoder(ffmpeg, choice):
    """Resolve --encoder: 'auto' probes GPU encoders and falls back to x264."""
    if choice != "auto":
        return choice
    for name in ("nvenc", "videotoolbox"):
        if _encoder_works(ffmpeg, name):
            return name
    return "x264"


# ---------------------------------------------------------------- main

def compute_camera_path(args, r, bps, total_v, n_frames):
    """Sequential camera smoothing pass (cheap, no rendering).

    Returns list of (frame_i, real_t, video_t, (cx, cy, zoom), fade_alpha).
    """
    fade = args.fade
    cam = None
    tau_c, tau_z = 0.55, 1.6  # seconds (video time)
    dt_v = 1.0 / args.fps
    kc = 1 - math.exp(-dt_v / tau_c)
    kz = 1 - math.exp(-dt_v / tau_z)
    plan = []
    recent = []  # head history in video time, for --view-seconds framing
    for i in range(n_frames):
        video_t = i * dt_v
        real_t = warp_real_time(bps, video_t)
        head = r.pos_at(real_t)
        if args.view_seconds:
            # Frame the current "chapter" of movement. Head positions are
            # kept per frame for the last N video seconds, each tagged with
            # its real-world speed. While in fast transit (flights,
            # expresses) the whole window is framed so the journey stays
            # visible; once at local speeds the framing cluster walks back
            # only until the previous transit leg (or a spatial jump), so
            # the camera zooms into the destination even while the trail
            # still shows the journey leg.
            la, lo = r.pos_ll_at(real_t)
            transit = False
            if recent:
                pvt, pla_, plo_ = recent[-1][0], recent[-1][1], recent[-1][2]
                real_dt_h = (real_t - warp_real_time(bps, pvt)) / 3600.0
                if real_dt_h > 1e-9:
                    speed = haversine_km(pla_, plo_, la, lo) / real_dt_h
                    transit = speed > args.transit_kmh
            recent.append((video_t, la, lo, head[0], head[1], transit))
            cutoff = video_t - args.view_seconds
            while recent and recent[0][0] < cutoff:
                recent.pop(0)
            if transit:
                pts = [(vt, x, y) for vt, _, _, x, y, _ in recent]
            else:
                pts = []
                pla, plo = la, lo
                for vt, cla, clo, x, y, tr in reversed(recent):
                    if tr or haversine_km(pla, plo, cla, clo) > args.view_jump_km:
                        break
                    pts.append((vt, x, y))
                    pla, plo = cla, clo
                if not pts:
                    pts = [(0, head[0], head[1])]
            tz = r.target_zoom(pts, head)
        else:
            view = r.trail_at(real_t, args.view_hours * 3600)
            tz = r.target_zoom(view, head)
        if cam is None:
            cam = [head[0], head[1], tz]
        else:
            # zoom out faster than zoom in (keeps head on screen)
            k = kz * (3.0 if tz < cam[2] - 0.05 else 1.0)
            cam[2] += (tz - cam[2]) * min(1.0, k)
            cam[0] += (head[0] - cam[0]) * kc
            cam[1] += (head[1] - cam[1]) * kc
            # hard clamp: keep head within 32% of half-viewport
            scale = TILE_SIZE * (2 ** cam[2])
            lim_x, lim_y = 0.32 * r.w / scale, 0.32 * r.h / scale
            dx, dy = head[0] - cam[0], head[1] - cam[1]
            if abs(dx) > lim_x:
                cam[0] = head[0] - math.copysign(lim_x, dx)
            if abs(dy) > lim_y:
                cam[1] = head[1] - math.copysign(lim_y, dy)

        fa = 1.0
        if fade > 0:
            fa = min(1.0, video_t / fade, max(0.0, (total_v - video_t) / fade))
            fa = max(0.0, fa) ** 1.5
        plan.append((i, real_t, video_t, tuple(cam), fa))
    return plan


_W = {}  # per-worker-process state


def _worker_init(args, points, style, size, period, fetchers):
    _W["r"] = Renderer(args, points, style, size, fetchers=fetchers)
    _W["period"] = period


def _worker_render(task):
    i, real_t, video_t, cam, fa = task
    frame = _W["r"].render_frame(real_t, video_t, cam, _W["period"], fa)
    return i, frame.tobytes()


def render_video(args, points, size, out_path):
    style = STYLES[args.style]
    t_start, t_end = args._t_start, args._t_end
    speedup = args._speedup
    bps, total_v = build_timewarp(points, t_start, t_end, speedup,
                                  args.idle_speedup, args._home,
                                  args.home_radius_km, args.home_speedup,
                                  args.pan_kms, args.min_leg_seconds,
                                  args.trip_min_seconds, args.trip_far_km)
    n_frames = max(1, int(total_v * args.fps))
    if args.max_frames:
        n_frames = min(n_frames, args.max_frames)
    workers = args.workers or max(1, (os.cpu_count() or 2) - 2)
    encoder = pick_encoder(args.ffmpeg, args.encoder)

    print(f"[{os.path.basename(out_path)}] {size[0]}x{size[1]}  "
          f"video={total_v:.1f}s  frames={n_frames}  speedup={speedup:.0f}x "
          f"(idle x{args.idle_speedup:g})  workers={workers}  "
          f"encoder={encoder}")

    r = Renderer(args, points, style, size)
    plan = compute_camera_path(args, r, bps, total_v, n_frames)

    ff = subprocess.Popen(
        [args.ffmpeg, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{size[0]}x{size[1]}", "-r", str(args.fps), "-i", "-"]
        + ENCODER_ARGS[encoder]
        + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE)

    t_wall = _time.time()

    def progress(i, real_t):
        if i % max(1, n_frames // 20) == 0:
            el = _time.time() - t_wall
            print(f"  frame {i}/{n_frames} ({100 * i // n_frames}%)  "
                  f"{datetime.fromtimestamp(real_t, JST):%Y-%m-%d %H:%M}  "
                  f"[{el:.0f}s elapsed]", flush=True)

    if workers <= 1:
        for task in plan:
            i, real_t = task[0], task[1]
            _W["r"], _W["period"] = r, (t_start, t_end)
            ff.stdin.write(_worker_render(task)[1])
            progress(i, real_t)
    else:
        # keep total tile-fetch concurrency polite across processes
        fetchers = max(2, 16 // workers)
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_worker_init,
                      initargs=(args, points, style, size,
                                (t_start, t_end), fetchers)) as pool:
            for i, buf in pool.imap(_worker_render, plan, chunksize=2):
                ff.stdin.write(buf)
                progress(i, plan[i][1])
    ff.stdin.close()
    ff.wait()
    if ff.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {out_path}")
    el = _time.time() - t_wall
    print(f"  -> wrote {out_path}  ({el:.0f}s, {n_frames / max(el, 1e-9):.1f} fps)")


def parse_user_date(s, end=False):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d" and end:
                dt += timedelta(days=1)
            return dt.replace(tzinfo=JST).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"invalid date: {s} (use YYYY-MM-DD or YYYY-MM-DDTHH:MM)")


def main():
    ap = argparse.ArgumentParser(
        description="Render Google Timeline location history as a movie.")
    ap.add_argument("-i", "--input", default="location-history.json")
    ap.add_argument("-o", "--output", default="timeline",
                    help="output basename (suffix _landscape/_portrait added)")
    ap.add_argument("--start", help="period start, JST (YYYY-MM-DD[THH:MM])")
    ap.add_argument("--end", help="period end, JST (YYYY-MM-DD[THH:MM], "
                                  "date-only is inclusive)")
    ap.add_argument("--speedup", type=float,
                    help="real seconds per 1 video second while moving "
                         "(e.g. 1800 = 30min/s)")
    ap.add_argument("--duration", type=float,
                    help="target video length in seconds (computes speedup); "
                         "ignored if --speedup given")
    ap.add_argument("--idle-speedup", type=float, default=10.0,
                    help="extra fast-forward factor while stationary "
                         "(default 10; 1 = uniform speed)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--orientation", choices=["landscape", "portrait", "both"],
                    default="both")
    ap.add_argument("--width", type=int, help="override width (single video)")
    ap.add_argument("--height", type=int, help="override height (single video)")
    ap.add_argument("--trail-hours", type=float,
                    help="trail persistence in real hours "
                         "(default: auto from period length)")
    ap.add_argument("--view-hours", type=float,
                    help="camera framing window in real hours: zoom fits the "
                         "last N hours of movement. Smaller than trail-hours "
                         "zooms in on local wandering at a destination but "
                         "makes the camera busier (default: = trail-hours, "
                         "the calm whole-journey framing)")
    ap.add_argument("--view-seconds", type=float,
                    help="camera framing window in VIDEO seconds: zoom fits "
                         "the head's recent on-screen path, cut at the first "
                         "big spatial jump, so the camera zooms into a "
                         "destination even while the trail still shows the "
                         "journey leg. Overrides --view-hours")
    ap.add_argument("--view-jump-km", type=float, default=80.0,
                    help="with --view-seconds: spatial gap that ends the "
                         "framing cluster (default 80)")
    ap.add_argument("--pan-kms", type=float, default=2500.0,
                    help="max on-screen travel in km per video second; fast "
                         "vehicles are slowed so flights glide instead of "
                         "teleporting (0 = off, default 2500)")
    ap.add_argument("--min-leg-seconds", type=float, default=1.5,
                    help="minimum video seconds for any single hop longer "
                         "than 200 km, e.g. a flight (0 = off, default 1.5)")
    ap.add_argument("--trip-min-seconds", type=float, default=0.0,
                    help="guarantee each far trip (contiguous stay beyond "
                         "--trip-far-km from home) at least this many video "
                         "seconds, inflating its local (non-transit) time "
                         "(0 = off)")
    ap.add_argument("--trip-far-km", type=float, default=100.0)
    ap.add_argument("--transit-kmh", type=float, default=170.0,
                    help="with --view-seconds: real speed above which the "
                         "head counts as in transit (flight/express) and "
                         "the whole window is framed (default 170)")
    ap.add_argument("--home-speedup", type=float, default=1.0,
                    help="extra fast-forward factor while within "
                         "--home-radius-km of home (auto-detected); >1 makes "
                         "the movie focus on trips (default 1 = off)")
    ap.add_argument("--home-radius-km", type=float, default=50.0)
    ap.add_argument("--home", help="home areas as 'lat,lon[;lat,lon...]' "
                                   "(default: auto-detect every area lived "
                                   "in 21+ days when --home-speedup > 1)")
    ap.add_argument("--zoom-min", type=float, default=3.0)
    ap.add_argument("--zoom-max", type=float, default=16.0)
    ap.add_argument("--style", choices=list(STYLES), default="dark")
    ap.add_argument("--fade", type=float, default=0.8,
                    help="fade in/out seconds (0 = off)")
    ap.add_argument("--tile-cache", default="tile_cache")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel render processes (0 = auto: n_cpu - 2, "
                         "1 = serial)")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary path")
    ap.add_argument("--encoder", choices=["auto", "x264", "nvenc",
                                          "videotoolbox"], default="auto",
                    help="video encoder; auto probes GPU encoders "
                         "(NVENC, VideoToolbox) and falls back to libx264")
    ap.add_argument("--max-frames", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    points = load_points(args.input)
    if not points:
        raise SystemExit("no location points found")
    data_t0, data_t1 = points[0][0], points[-1][0]
    t_start = parse_user_date(args.start) if args.start else data_t0
    t_end = parse_user_date(args.end, end=True) if args.end else data_t1
    t_start = max(t_start, data_t0)
    t_end = min(t_end, data_t1)
    if t_end <= t_start:
        raise SystemExit("empty period")
    args._t_start, args._t_end = t_start, t_end

    span_h = (t_end - t_start) / 3600.0
    if args.trail_hours is None:
        args.trail_hours = max(12.0, min(240.0, span_h * 0.03))
    if args.view_hours is None:
        args.view_hours = args.trail_hours

    args._home = None
    if args.home:
        args._home = [tuple(float(x) for x in part.split(","))
                      for part in args.home.split(";")]
    elif args.home_speedup > 1.0:
        args._home = detect_homes(points)
        homes_s = "; ".join(f"{h[0]:.2f},{h[1]:.2f}" for h in args._home)
        print(f"home areas auto-detected: {homes_s} "
              f"(radius {args.home_radius_km:.0f} km, x{args.home_speedup:g})")

    if args.speedup:
        args._speedup = args.speedup
    else:
        dur = args.duration or 75.0
        args._speedup = solve_speedup(points, t_start, t_end, dur,
                                      args.idle_speedup, args._home,
                                      args.home_radius_km, args.home_speedup,
                                      args.pan_kms, args.min_leg_seconds,
                                      args.trip_min_seconds, args.trip_far_km)

    print(f"period: {datetime.fromtimestamp(t_start, JST):%Y-%m-%d %H:%M} .. "
          f"{datetime.fromtimestamp(t_end, JST):%Y-%m-%d %H:%M}  "
          f"({span_h / 24:.1f} days)   trail={args.trail_hours:.0f}h  "
          f"view={args.view_hours:.1f}h")

    jobs = []
    if args.width and args.height:
        jobs.append(((args.width, args.height), f"{args.output}.mp4"))
    else:
        if args.orientation in ("landscape", "both"):
            jobs.append(((1920, 1080), f"{args.output}_landscape.mp4"))
        if args.orientation in ("portrait", "both"):
            jobs.append(((1080, 1920), f"{args.output}_portrait.mp4"))
    for size, path in jobs:
        render_video(args, points, size, path)


if __name__ == "__main__":
    main()
