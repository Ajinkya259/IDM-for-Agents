#!/usr/bin/env python3
"""
Animation experiments: other ways to show "pulling a file in from the internet".
NOTHING IS DOWNLOADED.  The same simulated download as ui_demo.py drives every
one of them (61.9 MB, 8 connections, the server says 429 to connections 7 and 8).

    python3 experiments.py              # play all six on a loop
    python3 experiments.py vortex       # just one: ring | orbit | halo | vortex | assemble | pipeline
    python3 experiments.py colors       # every colour set for halo, side by side
    keys: n or → = next, p or ← = back, 1-6 = jump, r/f/c = halo ring/face/colours, q = quit (loops until you quit)

Circles:  ring, orbit, halo (ring + orbit), vortex.   Squares:  assemble, pipeline.
Stdlib only, Python 3.9+.  Best in a truecolor terminal at least 80 x 24.
"""
import math
import random
import shutil
import sys
import time

from pixels import ARM_COLORS as ARM
from pixels import RESET, bg, fg
from tako_ui import Keys
from ui_demo import MAX_CONNS, NAME, SIZE, Sim, fmt_bytes, fmt_eta

BOLD, DIM = "\x1b[1m", "\x1b[2m"
TRACK = "#E2DEEA"      # the empty part of anything
LAV, LAV_D, INK = "#A98BFF", "#7456DD", "#2B1D4E"
EMPTY = "#EEE9FF"
GOLD, RED, WHITE = "#FFC23D", "#E5484D", "#FFFFFF"
TAU = 2 * math.pi
HOST = "archive.org"


def _rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def mix(a, b, t):
    """Blend two hex colours (t = 0 gives a, t = 1 gives b)."""
    t = max(0.0, min(1.0, t))
    return "#%02X%02X%02X" % tuple(int(x * (1 - t) + y * t) for x, y in zip(_rgb(a), _rgb(b)))


def seg_at(sim, offset):
    for s in sim.segs:
        if s.start <= offset < s.end:
            return s
    return sim.segs[-1]


# ---- drawing ---------------------------------------------------------------
class Canvas:
    """Square pixels (two per cell, with ▀), plus text dropped into cells."""

    def __init__(self, w, h):
        self.w, self.h = w, h + (h % 2)
        self.px = [[None] * w for _ in range(self.h)]
        self.txt = {}

    def put(self, x, y, c):
        x, y = int(round(x)), int(round(y))
        if c and 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = c

    def get(self, x, y):
        return self.px[y][x] if 0 <= x < self.w and 0 <= y < self.h else None

    def text(self, col, row, s, color=None, bold=False, dim=False):
        for i, ch in enumerate(s):
            self.txt[(col + i, row)] = (ch, color, bold, dim)

    def center(self, row, s, color=None, bold=False, dim=False, cx=None):
        col = (self.w - len(s)) // 2 if cx is None else int(round(cx - len(s) / 2.0 + 0.5))
        self.text(col, row, s, color, bold, dim)

    def lines(self):
        out = []
        for row in range(self.h // 2):
            parts, last = [], None
            for x in range(self.w):
                top, bot = self.px[2 * row][x], self.px[2 * row + 1][x]
                t = self.txt.get((x, row))
                if t:
                    ch, col, bold, dim = t
                    back = top or bot
                    style = ((bg(back) if back else "") + (fg(col) if col else "") +
                             (BOLD if bold else "") + (DIM if dim else ""))
                elif top is None and bot is None:
                    ch, style = " ", ""
                elif top is None:
                    ch, style = "▄", fg(bot)
                elif top == bot:                         # solid cell: paint the background, so
                    ch, style = " ", bg(top)              # macOS Terminal shows no line gaps
                elif bot is None:
                    ch, style = "▀", fg(top)
                else:
                    ch, style = "▀", fg(top) + bg(bot)
                parts.append((RESET + style if style != last else "") + ch)
                last = style
            out.append("".join(parts) + RESET)
        return out


# ---- the experiments ---------------------------------------------------------------
class Ring:
    name, title = "ring", "the file is the circle; each colour is one connection filling its part"

    def reset(self, sim):
        pass

    def event(self, ev, kw):
        pass

    def frame(self, sim, phase, t, tick):
        c = Canvas(40, 32)
        cx, cy, r_in, r_out = 19.5, 15.5, 8.6, 12.6
        for y in range(32):
            for x in range(40):
                dx, dy = x - cx, y - cy
                r = math.hypot(dx, dy)
                if not (r_in <= r <= r_out):
                    continue
                ang = math.atan2(dx, -dy) % TAU
                if phase == "probe":
                    d = (ang - t * 5) % TAU
                    color = mix(LAV, TRACK, d / 1.2) if d < 1.2 else TRACK      # a comet chasing round
                else:
                    off = ang / TAU * SIZE
                    seg = seg_at(sim, off)
                    color = ARM[seg.conn - 1] if off < seg.pos else TRACK
                    if phase == "done":
                        color = mix(color, WHITE, 0.45 * max(0.0, math.cos(ang * 2 - t * 7)))
                c.put(x, y, color)
        if phase == "download":                                  # packets flying in to each write head
            for s in sim.segs:
                conn = sim.conns[s.conn]
                if conn.state != "active" or conn.seg is not s:
                    continue
                ang = s.pos / SIZE * TAU
                for k in range(3):
                    rr = r_out + 1.2 + ((k * 2.4 - t * 10) % 7.2)
                    c.put(cx + rr * math.sin(ang), cy - rr * math.cos(ang), ARM[s.conn - 1])
        if phase == "done":                                      # a burst of confetti
            for k in range(24):
                ang = k * TAU / 24
                rr = r_out + 1.5 + (t * 9) % 6
                c.put(cx + rr * math.sin(ang), cy - rr * math.cos(ang), ARM[k % 8])
        pct = 100.0 * sim.done_bytes() / SIZE
        if phase == "probe":
            c.center(7, "hello?", LAV_D, bold=True)
            c.center(8, HOST, dim=True)
        elif phase == "done":
            c.center(7, "✓ 100%", "#1F9D55", bold=True)
            c.center(8, fmt_bytes(SIZE), dim=True)
        else:
            c.center(7, "%d%%" % pct, bold=True)
            c.center(8, "%s/s" % fmt_bytes(sim.speed()), dim=True)
        return c.lines()


class Orbit:
    name, title = "orbit", "8 moons (connections) circle a little planet and beam data into it"

    def reset(self, sim):
        self.phi = [k * TAU / 8 for k in range(8)]

    def event(self, ev, kw):
        pass

    def frame(self, sim, phase, t, tick):
        c = Canvas(40, 32)
        cx, cy, core, orbit = 19.5, 15.5, 6.6, 12.5
        fill = sim.done_bytes() / SIZE if phase != "done" else 1.0
        for a in range(0, 360, 10):                               # dotted orbit track
            c.put(cx + orbit * math.sin(math.radians(a)), cy - orbit * math.cos(math.radians(a)), TRACK)
        for y in range(32):                                       # the planet, filling up
            for x in range(40):
                r = math.hypot(x - cx, y - cy)
                if r > core:
                    continue
                if r > core - 1.0:
                    c.put(x, y, LAV_D)
                    continue
                level = cy + core - fill * 2 * core + 0.5 * math.sin(x / 2.0 - t * 4)
                c.put(x, y, LAV if y >= level else EMPTY)
        blink = tick % 30 in (28, 29)                             # it has a face, of course
        for ex in (cx - 2.5, cx + 2.5):
            c.put(ex, cy - 2, INK if not blink else None)
            c.put(ex, cy - 1, INK)
        for k in range(8):
            conn = sim.conns[k + 1]
            st = conn.state if phase == "download" else ("active" if phase == "done" and conn.bytes else "idle")
            if st == "active":
                self.phi[k] += 0.05 + 0.06 * (conn.speed / 0.85e6 if phase == "download" else 1.0)
            r = orbit + (2.5 if st == "backoff" else 0.0)
            mx, my = cx + r * math.sin(self.phi[k]), cy - r * math.cos(self.phi[k])
            col = ARM[k]
            if st in ("idle", "parked", "connecting"):
                col = mix(col, WHITE, 0.65)
            if st == "backoff" and (tick // 3) % 2:
                col = mix(col, WHITE, 0.7)
            for ox, oy in ((0, -1), (-1, 0), (0, 0), (1, 0), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                if abs(ox) + abs(oy) < 2 or st == "active":       # active moons are round 3x3, others small
                    c.put(mx + ox, my + oy, col)
            if st == "active" and phase == "download":            # beams of packets into the planet
                for j in range(3):
                    f = (t * 1.6 + j / 3.0) % 1.0
                    px, py = mx + (cx - mx) * f, my + (cy - my) * f
                    if math.hypot(px - cx, py - cy) > core + 0.5:
                        c.put(px, py, ARM[k])
                        c.put(px + (mx - cx) * 0.06, py + (my - cy) * 0.06, mix(ARM[k], WHITE, 0.5))
        if phase == "done":
            for k in range(12):
                ang = k * TAU / 12 + t
                rr = orbit + 3 + (t * 6) % 3
                c.put(cx + rr * math.sin(ang), cy - rr * math.cos(ang), GOLD)
        pct = 100.0 * sim.done_bytes() / SIZE
        c.center(8, ("%d%%" % pct) if phase == "download" else ("✓" if phase == "done" else "…"), INK, bold=True)
        return c.lines()


# Colour sets for halo: face, ring (filled dots), track (empty dots), ink, blush, tongue, tear.
PALETTES = [
    ("lavender",  "#B49CFF", "#7456DD", "#DCD6EA", "#2B1D4E", "#FF8FB8", "#FF6F91", "#4DA3FF"),
    ("peach",     "#FFB48F", "#F0622D", "#F0DDD2", "#3B1F14", "#FF7A8A", "#E8475F", "#4DA3FF"),
    ("mint",      "#8FE0C0", "#12A37E", "#D5EBE2", "#10362B", "#FF9EB0", "#FF6F91", "#3D8BFF"),
    ("sky",       "#A3CCFF", "#2F78E8", "#D7E2F2", "#10264A", "#FF9EC4", "#FF6F91", "#FFFFFF"),
    ("butter",    "#FFD666", "#D99400", "#EFE6C8", "#3D2E00", "#FF9F80", "#FF6F61", "#4DA3FF"),
    ("bubblegum", "#FFADD6", "#E0408A", "#F1D8E4", "#4A1230", "#FF6FA8", "#E0405F", "#4DA3FF"),
    ("graphite",  "#D6D6DC", "#3A3A46", "#E6E6EA", "#1E1E26", "#F4A3B5", "#FF6F91", "#4DA3FF"),
    ("berry",     "#C7B0FF", "#FF4F7B", "#EEDCE4", "#2B1D4E", "#FF8FB8", "#FF4F7B", "#4DA3FF"),
]

# Face features as (dx, dy, part) from the face's centre.  k = ink, p = blush, t = tongue.
_EYES = [(-2, -3, "k"), (-2, -2, "k"), (2, -3, "k"), (2, -2, "k")]
_BLUSH = [(-4, -1, "p"), (4, -1, "p")]
FACES = {
    "happy": _EYES + _BLUSH + [(-1, 0, "k"), (1, 0, "k"), (0, 1, "k")],                 # v smile
    "blink": [(-2, -2, "k"), (2, -2, "k")] + _BLUSH + [(-1, 0, "k"), (1, 0, "k"), (0, 1, "k")],
    "look":  _EYES + _BLUSH + [(0, 0, "k")],                                            # o mouth
    "sad":   _EYES + [(0, 0, "k"), (-1, 1, "k"), (1, 1, "k")],                          # frown
    "joy":   [(-3, -2, "k"), (-2, -3, "k"), (-1, -2, "k"), (1, -2, "k"), (2, -3, "k"), (3, -2, "k")]
             + _BLUSH + [(-2, 0, "k"), (-1, 0, "k"), (0, 0, "k"), (1, 0, "k"), (2, 0, "k"),
                         (-1, 1, "k"), (0, 1, "t"), (1, 1, "k")],                       # ^ ^ and a grin
}


# Face styles.  Pixel faces list features as (dx, dy, part) from the centre (k ink, p blush,
# t tongue); "glyph" draws its eyes and mouth with text characters instead, so they stay thin.
# rx/ry = the face's size, pct = the row (from the centre) for the %, tear = where a sad tear starts.
FACE_STYLES = [
    ("chunky", dict(rx=5.4, ry=5.4, pct=3, tear=(-2, -1), moods=FACES)),
    ("small", dict(rx=4.3, ry=4.3, pct=5, tear=(-2, -1), moods={
        "happy": [(-2, -2, "k"), (2, -2, "k"), (-3, 0, "p"), (3, 0, "p"), (-1, 0, "k"), (1, 0, "k"), (0, 1, "k")],
        "blink": [(-3, -2, "k"), (-2, -2, "k"), (2, -2, "k"), (3, -2, "k"), (-3, 0, "p"), (3, 0, "p"),
                  (-1, 0, "k"), (1, 0, "k"), (0, 1, "k")],
        "look":  [(-2, -2, "k"), (2, -2, "k"), (-3, 0, "p"), (3, 0, "p"), (0, 0, "k")],
        "sad":   [(-2, -2, "k"), (2, -2, "k"), (0, 0, "k"), (-1, 1, "k"), (1, 1, "k")],
        "joy":   [(-3, -1, "k"), (-2, -2, "k"), (-1, -1, "k"), (1, -1, "k"), (2, -2, "k"), (3, -1, "k"),
                  (-4, 1, "p"), (4, 1, "p"), (-1, 1, "k"), (0, 1, "k"), (1, 1, "k"), (0, 2, "t")]})),
    ("mochi", dict(rx=6.4, ry=4.4, pct=3, tear=(-3, -1), moods={
        "happy": [(-3, -2, "k"), (3, -2, "k"), (-5, -1, "p"), (5, -1, "p"),
                  (-2, 0, "k"), (0, 0, "k"), (2, 0, "k"), (-1, 1, "k"), (1, 1, "k")],            # ω mouth
        "blink": [(-4, -2, "k"), (-3, -2, "k"), (3, -2, "k"), (4, -2, "k"), (-5, -1, "p"), (5, -1, "p"),
                  (-2, 0, "k"), (0, 0, "k"), (2, 0, "k"), (-1, 1, "k"), (1, 1, "k")],
        "look":  [(-3, -2, "k"), (3, -2, "k"), (-5, -1, "p"), (5, -1, "p"), (0, 0, "k")],
        "sad":   [(-3, -2, "k"), (3, -2, "k"), (0, 0, "k"), (-1, 1, "k"), (1, 1, "k")],
        "joy":   [(-4, -1, "k"), (-3, -2, "k"), (-2, -1, "k"), (2, -1, "k"), (3, -2, "k"), (4, -1, "k"),
                  (-5, 0, "p"), (5, 0, "p"), (-1, 1, "k"), (0, 1, "k"), (1, 1, "k"), (0, 2, "t")]})),
    ("glyph", dict(rx=4.6, ry=4.6, pct=2, tear=None, blush=[(-3, 0), (3, 0)], moods={
        "happy": ("●    ●", "◡"), "blink": ("-    -", "◡"), "look": ("●    ●", "o"),
        "sad": ("╥    ╥", "⌒"), "joy": ("^    ^", "▽")})),
]
RING_STYLES = ["float", "beads", "crew", "stream"]


class Blocks:
    """Chunky square pixels: each one is two spaces with a background colour (no half-block
    characters, so macOS Terminal draws them solid).  Plus small dots, half a row tall and one
    column wide, for things that stand alone (no neighbour above or below to show a gap)."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = [[None] * w for _ in range(h)]
        self.dots = {}                                              # (column, half-row) -> colour
        self.glyphs = {}                                            # (column, row) -> (character, colour)
        self.txt = {}

    def put(self, x, y, c):
        if c and 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = c

    def dot(self, col, half, c):
        self.dots[(col, half)] = c

    def glyph(self, col, row, ch, c):
        self.glyphs[(col, row)] = (ch, c)

    def text(self, col, row, s, color=None, bold=False):
        for i, ch in enumerate(s):
            if ch != " ":
                self.txt[(col + i, row)] = (ch, color, bold)

    def center(self, row, s, color=None, bold=False):
        self.text(self.w - len(s) // 2, row, s, color, bold)       # self.w chunky = the middle column

    def lines(self):
        out = []
        for y in range(self.h):
            parts, last = [], None
            for col in range(self.w * 2):
                back = self.px[y][col // 2]
                t = self.txt.get((col, y))
                if t:
                    ch = t[0]
                    style = (bg(back) if back else "") + (fg(t[1]) if t[1] else "") + (BOLD if t[2] else "")
                elif back:
                    ch, style = " ", bg(back)
                elif (col, y) in self.glyphs:
                    ch, gc = self.glyphs[(col, y)]
                    style = fg(gc)
                else:
                    top, bot = self.dots.get((col, 2 * y)), self.dots.get((col, 2 * y + 1))
                    if top and bot:
                        ch, style = "▀", fg(top) + bg(bot)
                    elif top:
                        ch, style = "▀", fg(top)
                    elif bot:
                        ch, style = "▄", fg(bot)
                    else:
                        ch, style = " ", ""
                parts.append((RESET + style if style != last else "") + ch)
                last = style
            out.append("".join(parts) + RESET)
        return out


class Halo:
    """A face in the middle that shows the % and how it feels (curious while checking, happy while
    downloading, sad on a 429, overjoyed when done), and a ring of 32 dots that comes together as
    the file fills.  The pick is beads + glyph + lavender; r = ring, f = face, c = colours to compare.
      rings:  float  - dots drift around freely and fly in to their place
              beads  - the same, with round dots
              crew   - each dot is a slice of the file and lands once most of it is in, in the
                       colour of the connection that fetched it (so parts fill at the same time)
              stream - a faint ring waits; dots shoot in from the edge (the internet) to fill it
    """
    name = "halo"
    W, H, CX, CY = 28, 17, 14, 8               # chunky pixels (2 columns x 1 row each)
    DOTS, DOT_R = 32, 15.0                     # the ring, in small dots (1 column = 1 half-row)
    FLY = 0.8                                  # seconds for a dot to fly home
    pi, ri, fi, tour = 0, 1, 3, False          # starts on the pick: beads ring, glyph face, lavender

    @property
    def title(self):
        return "ring: %s (r) · face: %s (f) · colours: %s (c)" % (
            RING_STYLES[self.ri], FACE_STYLES[self.fi][0], PALETTES[self.pi][0])

    def reset(self, sim):
        if self.tour:                          # a new ring + face each time, until a key is pressed
            self.ri = (self.ri + 1) % len(RING_STYLES)
            self.fi = (self.fi + 1) % len(FACE_STYLES)
        self.t0 = [None] * self.DOTS           # when each dot set off for home
        cx, cy = 2 * self.CX + 1.0, 2 * self.CY + 1.0
        rng, self.free = random.Random(11), []
        tries = 0
        while len(self.free) < self.DOTS:      # scatter them, not too close to the face or each other
            tries += 1
            x, y = rng.uniform(1, 2 * self.W - 2), rng.uniform(1, 2 * self.H - 2)
            if math.hypot(x - cx, y - cy) < 13.5:
                continue
            if tries < 5000 and any(math.hypot(x - fx, y - fy) < 4.5 for fx, fy, _, _ in self.free):
                continue
            self.free.append((x, y, rng.uniform(0, TAU), rng.uniform(0, TAU)))

    def event(self, ev, kw):
        pass

    def on_key(self, key):
        if key in ("r", "f", "c"):
            self.tour = False
            if key == "r":
                self.ri = (self.ri + 1) % len(RING_STYLES)
            elif key == "f":
                self.fi = (self.fi + 1) % len(FACE_STYLES)
            else:
                self.pi = (self.pi + 1) % len(PALETTES)
            return True
        return False

    def mood(self, sim, phase, tick):
        if phase == "probe":
            return "look"
        if phase == "done":
            return "joy"
        if any(sim.conns[k].state == "backoff" for k in sim.conns):
            return "sad"                                            # the server said 429
        return "blink" if tick % 32 in (30, 31) else "happy"

    # ---- the ring ----
    def _floating(self, i, t):
        x, y, p1, p2 = self.free[i]
        x += 2.2 * math.sin(t * 0.6 + p1)
        y += 1.6 * math.sin(t * 0.8 + p2)
        return min(max(x, 0.0), 2 * self.W - 1.0), min(max(y, 0.0), 2 * self.H - 1.0)

    def _edge(self, hx, hy):
        """Where a 'stream' dot comes from: straight out past its home, at the edge of the picture."""
        cx, cy = 2 * self.CX + 1.0, 2 * self.CY + 1.0
        d = math.hypot(hx - cx, hy - cy) or 1.0
        x, y = cx + (hx - cx) / d * 40, cy + (hy - cy) / d * 40
        return min(max(x, 0.0), 2 * self.W - 1.0), min(max(y, 0.0), 2 * self.H - 1.0)

    def _have(self, sim, i):
        """Has most of slice i of the file (1/32 of it) arrived?  (Most, not all: the slices a
        connection is in the middle of would otherwise hold the ring back behind the %.)"""
        lo, hi = i * SIZE / self.DOTS, (i + 1) * SIZE / self.DOTS
        got = sum(max(0.0, min(hi, s.pos) - max(lo, s.start)) for s in sim.segs)
        return got >= 0.5 * (hi - lo)

    def _mark(self, c, x, y, color, home):
        if RING_STYLES[self.ri] == "beads":                         # round: ● at home, • on the move
            c.glyph(int(math.floor(x)), int(math.floor(y)) // 2, "●" if home else "•", color)
        else:
            c.dot(int(math.floor(x)), int(math.floor(y)), color)

    def _ring(self, c, sim, phase, t, tick, ring, track):
        style = RING_STYLES[self.ri]
        soft = mix(ring, WHITE, 0.6)                               # a dot still floating
        cx, cy = 2 * self.CX + 1.0, 2 * self.CY + 1.0
        if phase == "done":
            lit, nxt = set(range(self.DOTS)), None
        elif style == "crew":
            lit, nxt = {i for i in range(self.DOTS) if self._have(sim, i)}, None
        else:
            n = int(sim.done_bytes() / SIZE * self.DOTS + 1e-9)
            lit, nxt = set(range(n)), n
        for i in range(self.DOTS):
            a = i * TAU / self.DOTS                                 # home: from the top, clockwise
            hx, hy = cx + self.DOT_R * math.sin(a), cy - self.DOT_R * math.cos(a)
            home_col = ARM[seg_at(sim, (i + 0.5) * SIZE / self.DOTS).conn - 1] if style == "crew" else ring
            if style == "stream" and phase == "probe":              # the waiting ring, with a light running round
                d = (t * 12 - i) % self.DOTS
                self._mark(c, hx, hy, mix(ring, track, d / 5.0) if d < 5 else track, True)
                continue
            if i in lit:                                            # its share has arrived: fly home
                if self.t0[i] is None:
                    self.t0[i] = t
                f = min(1.0, (t - self.t0[i]) / self.FLY)
                e = 1 - (1 - f) ** 3                                # ease out: quick, then settles
                if style == "stream":
                    sx, sy = self._edge(hx, hy)
                    for lag, fade in ((0.12, 0.45), (0.24, 0.7)):   # a little tail behind it
                        if 0 < f - lag < 1:
                            eb = 1 - (1 - (f - lag)) ** 3
                            self._mark(c, sx + (hx - sx) * eb, sy + (hy - sy) * eb, mix(home_col, WHITE, fade), False)
                    color = home_col
                else:
                    sx, sy = self._floating(i, self.t0[i])
                    color = mix(soft, home_col, e)
                self._mark(c, sx + (hx - sx) * e, sy + (hy - sy) * e, color, f >= 1)
            elif style == "stream":                                 # not here yet: a faint placeholder
                self._mark(c, hx, hy, track, True)
            else:
                x, y = self._floating(i, t)
                tw = i == nxt and phase == "download" and (tick // 4) % 2
                self._mark(c, x, y, ring if tw else soft, False)    # the next one to come in twinkles

    # ---- the face ----
    def _face(self, c, mood, tick, face, ink, blush, tongue, tear, pct):
        st = FACE_STYLES[self.fi][1]
        for y in range(self.H):
            for x in range(self.W):
                if ((x - self.CX) / st["rx"]) ** 2 + ((y - self.CY) / st["ry"]) ** 2 <= 1:
                    c.put(x, y, face)
        look = (-1, 0, 1, 0)[(tick // 6) % 4] if mood == "look" else 0
        mid = 2 * self.CX + 1                                       # the face's centre, in columns
        if "blush" in st:                                           # the text-drawn face
            eyes, mouth = st["moods"][mood]
            for dx, dy in st["blush"]:
                c.put(self.CX + dx, self.CY + dy, blush)
            c.text(mid - len(eyes) // 2 + look, self.CY - 1, eyes, ink, True)
            c.text(mid - 1, self.CY, mouth, ink, True)
        else:
            part = {"k": ink, "p": blush, "t": tongue}
            for dx, dy, k in st["moods"][mood]:
                c.put(self.CX + dx + (look if dy < 0 else 0), self.CY + dy, part[k])
            if mood == "sad" and st["tear"]:                        # one tear, rolling down
                tx, ty = st["tear"]
                c.put(self.CX + tx, self.CY + ty + (tick // 4) % 3, tear)
        c.center(self.CY + st["pct"], "%d%%" % pct, ink, bold=True)

    def frame(self, sim, phase, t, tick):
        _, face, ring, track, ink, blush, tongue, tear = PALETTES[self.pi]
        c = Blocks(self.W, self.H)
        self._ring(c, sim, phase, t, tick, ring, track)
        pct = 100 if phase == "done" else int(100.0 * sim.done_bytes() / SIZE)
        self._face(c, self.mood(sim, phase, tick), tick, face, ink, blush, tongue, tear, pct)
        return c.lines()


def colour_sheet():
    """Every colour set side by side (happy, at about 70%), two per row."""
    sim = Sim(random.Random(7), lambda ev, **kw: None)
    while sim.done_bytes() < 0.7 * SIZE:
        sim.step()
    out = []
    for row in range(0, len(PALETTES), 2):
        blocks = []
        for pi in range(row, min(row + 2, len(PALETTES))):
            h = Halo()
            h.W, h.CX = 19, 9                                       # narrower, so two fit in 80 columns
            h.reset(sim)
            h.pi, h.t0 = pi, [-10.0] * h.DOTS                       # dots already home stay home
            art = h.frame(sim, "download", 0.0, 0)
            name = PALETTES[pi][0]
            blocks.append(art + [" " * ((38 - len(name)) // 2) + BOLD + name + RESET + " " * (38 - (38 - len(name)) // 2 - len(name))])
        out += ["  ".join(parts) for parts in zip(*blocks)] + [""]
    return out


class Vortex:
    name, title = "vortex", "data spirals in from all sides; the core grows as the file fills"

    def reset(self, sim):
        self.parts, self.acc, self.rng = [], [0.0] * 8, random.Random(3)

    def event(self, ev, kw):
        if ev == "throttle":                                      # rejected: a red puff flies back out
            for _ in range(10):
                self.parts.append([self.rng.uniform(0, TAU), 6.0, kw["conn"], True, 0, 0])

    def frame(self, sim, phase, t, tick):
        c = Canvas(48, 32)
        cx, cy = 23.5, 15.5
        fill = sim.done_bytes() / SIZE if phase != "done" else 1.0
        core_r = 2.2 + fill * 8.5
        if phase == "probe":                                      # radar sweep
            for ring in (6.5, 12.5):
                for a in range(0, 360, 12):
                    c.put(cx + ring * math.sin(math.radians(a)), cy - ring * math.cos(math.radians(a)), TRACK)
            for j, fade in ((0, 0.0), (1, 0.45), (2, 0.75)):
                a = t * 3.0 - j * 0.12
                for i in range(1, 15):
                    c.put(cx + i * math.sin(a), cy - i * math.cos(a), mix(LAV_D, WHITE, fade))
        if phase == "download":
            for k in range(8):
                conn = sim.conns[k + 1]
                if conn.state == "active":
                    self.acc[k] += conn.speed / 0.85e6 * 0.9
                    while self.acc[k] >= 1:
                        self.acc[k] -= 1
                        self.parts.append([self.rng.uniform(0, TAU), self.rng.uniform(24, 29), k + 1, False, 0, 0])
        if phase == "done" and tick % 40 == 0:
            for p in self.parts:
                p[3] = True
        alive = []
        for p in self.parts:
            a, r, conn, out, _, _ = p
            px, py = cx + r * math.sin(a), cy - r * math.cos(a)
            if out:
                r += 0.9
                a += 0.02
            else:
                r -= 0.35 + 1.0 * (1 - r / 29.0)
                a += 0.75 / max(r, 2.0)
            p[0], p[1], p[4], p[5] = a, r, px, py
            if (out and r > 30) or (not out and r < core_r):
                continue
            alive.append(p)
            col = RED if out and phase == "download" else ARM[conn - 1]
            c.put(px, py, mix(col, WHITE, 0.6))                   # a short comet tail
            c.put(cx + r * math.sin(a), cy - r * math.cos(a), col)
        self.parts = alive
        for y in range(int(cy - core_r) - 1, int(cy + core_r) + 2):     # the core
            for x in range(int(cx - core_r) - 1, int(cx + core_r) + 2):
                d = math.hypot(x - cx, y - cy)
                if d <= core_r:
                    c.put(x, y, LAV_D if d > core_r - 1.0 else LAV)
        pct = 100.0 * sim.done_bytes() / SIZE
        if phase == "probe":
            c.center(15, "listening for %s…" % HOST, LAV_D)
        elif core_r > 4.5:
            c.center(7, "✓" if phase == "done" else "%d%%" % pct, INK, bold=True)
        else:
            c.center(15, "%d%%" % pct, bold=True)
        return c.lines()


class Assemble:
    name, title = "assemble", "every piece flies in from the internet and snaps into place"
    N = 20                                                       # 20 x 20 = 400 pixels, 0.25 % each

    def reset(self, sim):
        self.arrived, self.rng, self.done_at = {}, random.Random(5), None

    def event(self, ev, kw):
        pass

    def _picture(self, x, y):
        """What the finished file turns into: a play button."""
        n = self.N
        u, v = (x + 0.5) / n, (y + 0.5) / n
        inside = 0.36 <= u <= 0.72 and abs(v - 0.5) <= (0.72 - u) * 0.62
        return WHITE if inside else LAV_D

    def frame(self, sim, phase, t, tick):
        c = Canvas(40, 32)
        n, x0, y0 = self.N, 10, 6
        per = SIZE / float(n * n)
        if phase != "probe":
            for i in range(n * n):
                if i in self.arrived:
                    continue
                mid = (i + 0.5) * per
                seg = seg_at(sim, mid)
                if mid < seg.pos or phase == "done":
                    side = self.rng.random() * 4
                    if side < 2:                                 # most come from the top: the internet is up there
                        sx, sy = self.rng.uniform(-4, 44), -3
                    elif side < 3:
                        sx, sy = -3, self.rng.uniform(0, 28)
                    else:
                        sx, sy = 43, self.rng.uniform(0, 28)
                    self.arrived[i] = (t, sx, sy, ARM[seg.conn - 1])
        for i in range(n * n):                                   # the empty outline of the square
            x, y = x0 + i % n, y0 + i // n
            c.put(x, y, TRACK if (i % n + i // n) % 2 else mix(TRACK, WHITE, 0.4))
        if phase == "probe":
            k = int(t * 30) % (4 * n)
            for j in range(6):                                   # a scanner runs round the edge
                p = (k - j) % (4 * n)
                if p < n:
                    px, py = x0 + p, y0 - 1
                elif p < 2 * n:
                    px, py = x0 + n, y0 + p - n
                elif p < 3 * n:
                    px, py = x0 + 3 * n - p, y0 + n
                else:
                    px, py = x0 - 1, y0 + 4 * n - p
                c.put(px, py, mix(LAV, TRACK, j / 6.0))
        if phase == "done" and self.done_at is None:
            self.done_at = t
        morph = 0.0 if self.done_at is None else min(1.0, (t - self.done_at - 0.6) / 1.0)
        for i, (t0, sx, sy, col) in self.arrived.items():
            x, y = x0 + i % n, y0 + i // n
            f = min(1.0, (t - t0) / 0.55)
            e = 1 - (1 - f) ** 3                                 # ease out
            if f < 1.0:
                c.put(sx + (x - sx) * e, sy + (y - sy) * e, col)
            else:
                c.put(x, y, mix(col, self._picture(i % n, i // n), morph) if morph > 0 else col)
        if phase == "download":
            c.center(15, "%d%% · %d pieces in flight" % (100.0 * sim.done_bytes() / SIZE,
                     sum(1 for v in self.arrived.values() if t - v[0] < 0.55)), dim=True)
        elif phase == "done":
            c.center(15, "▶ ready to play" if morph >= 1 else "", LAV_D, bold=True)
        else:
            c.center(15, "scanning…", dim=True)
        return c.lines()


class Pipeline:
    name, title = "pipeline", "the internet sends packets down 8 lanes into the box"

    def reset(self, sim):
        self.blocked = {}

    def event(self, ev, kw):
        if ev == "throttle":
            self.blocked[kw["conn"]] = True

    def frame(self, sim, phase, t, tick):
        c = Canvas(64, 24)
        gx, gy, gr = 9.0, 11.5, 8.2
        for y in range(24):                                      # a spinning pixel globe
            for x in range(20):
                dx, dy = x - gx, y - gy
                d = math.hypot(dx, dy)
                if d > gr:
                    continue
                z = math.sqrt(max(0.0, gr * gr - d * d))
                lon = math.atan2(dx, z) + t * 0.8
                lat = math.asin(max(-1.0, min(1.0, dy / gr)))
                land = math.sin(lon * 2 + 1.3) * math.cos(lat * 2.4) + 0.4 * math.sin(lon * 5 + lat * 3) > 0.3
                col = "#4CC38A" if land else "#5AA9F0"
                if d > gr - 1.2:
                    col = mix(col, "#1E3A5F", 0.45)
                c.put(x, y, col)
        bx0, bx1, by0, by1 = 46, 61, 3, 20                       # the box
        for x in range(bx0, bx1 + 1):
            c.put(x, by0, LAV_D)
            c.put(x, by1, LAV_D)
        for y in range(by0, by1 + 1):
            c.put(bx0, y, LAV_D)
            c.put(bx1, y, LAV_D)
        inner = by1 - by0 - 1
        level = by1 - 1
        stack = []
        for k in range(1, MAX_CONNS + 1):                        # layers: what each connection brought
            got = sim.conns[k].bytes if phase != "done" else sim.conns[k].bytes
            stack.append((k, got / SIZE * inner))
        acc = 0.0
        for k, h in stack:
            if h <= 0:
                continue
            top = acc + h
            for yy in range(int(round(acc)), int(round(top))):
                for x in range(bx0 + 1, bx1):
                    c.put(x, level - yy, ARM[k - 1])
            acc = top
        for k in range(8):                                       # the lanes
            ly = 4 + 2 * k
            conn = sim.conns[k + 1]
            st = conn.state if phase == "download" else "idle"
            for x in range(19, bx0, 2):
                c.put(x, ly, TRACK)
            if st == "active":
                v = 6 + 14 * conn.speed / 0.85e6
                for j in range(4):
                    x = 19 + ((t * v + j * 7) % (bx0 - 20))
                    c.put(x, ly, ARM[k])
                    c.put(x - 1, ly, mix(ARM[k], WHITE, 0.5))
            elif st == "backoff":
                mxp = 32
                for d in (-1, 0, 1):                             # a red x: the server said no
                    c.put(mxp + d, ly + d, RED)
                    c.put(mxp + d, ly - d, RED)
            elif st == "parked":
                c.put(32, ly, mix(ARM[k], WHITE, 0.5))
        if phase == "probe":                                     # one ping goes there and back
            f = (t * 1.4) % 2.0
            x = 19 + (bx0 - 20) * (f if f < 1 else 2 - f)
            c.put(x, 11, LAV_D)
            c.put(x + 1, 11, LAV_D)
        if phase == "done":
            for k in range(6):
                c.put(bx0 + 2 + k * 2.5, by0 - 2 - ((t * 6 + k) % 2), GOLD)
        c.center(11, HOST, dim=True, cx=gx + 0.5)
        label = {"probe": "ping…", "done": "✓ full"}.get(phase, "%d%%" % (100.0 * sim.done_bytes() / SIZE))
        c.center(11, label, bold=True, cx=(bx0 + bx1) / 2.0 + 0.5)
        return c.lines()


EXPERIMENTS = [Ring(), Orbit(), Halo(), Vortex(), Assemble(), Pipeline()]


# ---- driver --------------------------------------------------------------------------
def status_line(sim, phase, took):
    if phase == "probe":
        return DIM + "  checking %s…" % HOST + RESET
    if phase == "done":
        return "  " + fg("#1F9D55") + BOLD + "✓ got it" + RESET + "  %s in %.1fs" % (fmt_bytes(SIZE), took)
    done = sim.done_bytes()
    active = sum(1 for k in range(1, MAX_CONNS + 1) if sim.conns[k].state == "active")
    return ("  " + BOLD + "%3.0f%%" % (100.0 * done / SIZE) + RESET + "  %s / %s · %s/s · ETA %s · %d connections" % (
        fmt_bytes(done), fmt_bytes(SIZE), fmt_bytes(sim.speed()), fmt_eta(sim.eta()), active))


def play(exps, out):
    drawn = 0

    def draw(lines):
        nonlocal drawn
        if drawn:
            out.write("\x1b[%dA" % drawn)
        out.write("".join("\r\x1b[K" + l + "\n" for l in lines))
        if len(lines) < drawn:
            out.write("\x1b[J")
        out.flush()
        drawn = len(lines)

    def tabs(idx):
        """'1 ring  2 orbit …' with the one playing now highlighted."""
        parts = []
        for i, e in enumerate(exps):
            label = "%d %s" % (i + 1, e.name)
            parts.append(BOLD + fg(LAV_D) + label + RESET if i == idx else DIM + label + RESET)
        return "  " + "  ".join(parts)

    keys_help = "n next · p back · 1-%d pick · q quit" % len(exps) if len(exps) > 1 else "q quit"
    if any(hasattr(e, "on_key") for e in exps):
        keys_help += " · r ring · f face · c colours"
    else:
        keys_help += "   (plays on a loop)"
    footer = "  " + DIM + keys_help + RESET
    idx = 0
    with Keys() as keys:
        while True:                                              # loops until q, so nothing is missed
            ex = exps[idx]
            events = []
            sim = Sim(random.Random(7), lambda ev, **kw: events.append((ev, kw)))
            ex.reset(sim)
            phase, t, tick, done_t = "probe", 0.0, 0, 0.0
            nxt = (idx + 1) % len(exps)
            while True:
                cols = shutil.get_terminal_size((80, 24)).columns
                W = min(cols, 80)
                art = ex.frame(sim, phase, t, tick)
                art_w = {"pipeline": 64, "vortex": 48, "halo": 56}.get(ex.name, 40)
                pad = " " * max(0, (W - art_w) // 2)
                sub = "  " + DIM + ex.title[:W - 3] + RESET            # never wider than the terminal
                lines = [tabs(idx), sub] + [pad + a for a in art] + ["", status_line(sim, phase, sim.t), footer]
                draw(lines)
                key = keys.wait(0.1)
                if key == "q":
                    return
                if key in ("n", "\x1b[c"):                       # n or right arrow
                    break
                if key in ("p", "\x1b[d"):                       # p or left arrow
                    nxt = (idx - 1) % len(exps)
                    break
                if key and hasattr(ex, "on_key") and ex.on_key(key):
                    continue
                if key.isdigit() and 1 <= int(key) <= len(exps):
                    nxt = int(key) - 1
                    break
                t += 0.1
                tick += 1
                if phase == "probe" and t >= 1.8:
                    phase = "download"
                elif phase == "download":
                    for _ in range(2):                           # 2x speed so each one takes ~10 s
                        sim.step()
                        for ev, kw in events:
                            ex.event(ev, kw)
                        events.clear()
                    if sim.finished():
                        phase, done_t = "done", t
                elif phase == "done" and t - done_t > 3.2:
                    break
            idx = nxt


def main():
    names = [e.name for e in EXPERIMENTS]
    if sys.argv[1:] in (["colors"], ["colours"]):
        print("\n".join(colour_sheet()))
        return
    pick = sys.argv[1:] or names
    bad = [p for p in pick if p not in names]
    if bad:
        print("unknown: %s (choose from %s)" % (", ".join(bad), ", ".join(names)))
        return
    if not sys.stdout.isatty():
        print("experiments.py draws animations; run it in a terminal.")
        return
    out = sys.stdout
    out.write("\x1b[?25l")
    try:
        play([e for e in EXPERIMENTS if e.name in pick], out)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h" + RESET + "\n")
        out.flush()


if __name__ == "__main__":
    main()
