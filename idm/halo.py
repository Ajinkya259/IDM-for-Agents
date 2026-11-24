"""
halo: the face that shows a download.

A ring of 32 dots floats around a face.  As the file arrives the dots fly in, one by one, to
form the ring (each dot is 1/32 of the file), and the face shows the % and how it feels:
curious while it starts, happy while it downloads, sad when the server pushes back, asleep
when paused, overjoyed when done.

It is drawn from a job's status.json alone, so anything can show it:
    idm get / wait / watch    in a terminal, as text with colours   (Picture.ansi)
    the Claude Code mod       under the agent's command, as cells   (Picture.packed)
"""
import base64
import math
import os
import random
import shutil
import struct
import time

from . import when

# The colours (the "lavender" set picked from the mockups).
FACE, RING, INK, BLUSH = "#B49CFF", "#7456DD", "#2B1D4E", "#FF8FB8"
FLOATING = "#CDBDF5"                 # a dot that hasn't come in yet: the ring colour, faded

# The size, in chunky pixels: two terminal columns wide and one row tall, so they look square.
W, H = 28, 17                        # the face and its ring
CX, CY = 14, 8                       # the middle
COLUMNS, ROWS = 2 * W, H + 3         # and three lines of words under it
DOTS, RADIUS = 32, 15.0              # the ring: 32 dots, 15 columns (or half-rows) from the middle
FLY = 0.8                            # seconds for a dot to fly home

# How it feels: (eyes, mouth), drawn with characters so they stay thin.
MOODS = {
    "happy": ("●    ●", "◡"),
    "blink": ("-    -", "◡"),        # also: asleep (paused)
    "look":  ("●    ●", "o"),        # curious: starting, eyes looking around
    "sad":   ("╥    ╥", "⌒"),        # the server pushed back, or it failed
    "joy":   ("^    ^", "▽"),        # done
}


class Halo:
    """The face for one download.  It remembers when each dot came in, so dots fly home
    once and then stay; make a new Halo for another download."""

    def __init__(self):
        self.came_in = [None] * DOTS                       # when each dot set off for home
        self.loose = scatter(random.Random(11))           # where each dot floats until then

    def draw(self, st, t, settle=False):
        """The picture for status `st` at time `t` (seconds).  `settle`: every dot that
        is coming in is already home (for a last, still picture)."""
        pic = Picture(COLUMNS, ROWS)
        self.ring(pic, st, t, settle)
        face(pic, mood(st, t), t, middle_text(st))
        for row, (text, color) in enumerate(caption(st)):
            centred(pic, H + row, text, color)
        return pic

    def ring(self, pic, st, t, settle):
        lit = DOTS if st.get("state") == "done" else int(DOTS * fraction(st) + 1e-9)
        for i in range(lit, DOTS):                        # still floating (drawn first, so they
            twinkle = i == lit and st.get("state") == "downloading" and int(t * 2.5) % 2
            pic.dot(*drift(self.loose[i], t), "•", RING if twinkle else FLOATING)  # pass under)
        for i in range(lit):                              # its share of the file is here: fly home
            if self.came_in[i] is None:
                self.came_in[i] = t
            f = 1.0 if settle else min(1.0, (t - self.came_in[i]) / FLY)
            eased = 1 - (1 - f) ** 3                      # quick, then settles
            angle = i * 2 * math.pi / DOTS                # home: from the top, clockwise
            home = (2 * CX + 1 + RADIUS * math.sin(angle), 2 * CY + 1 - RADIUS * math.cos(angle))
            start = drift(self.loose[i], self.came_in[i])
            x, y = (a + (b - a) * eased for a, b in zip(start, home))
            pic.dot(x, y, "●" if f >= 1 else "•", mix(FLOATING, RING, eased))


def face(pic, feeling, t, text):
    """The round face, its eyes and mouth, and `text` (the %) under them."""
    for y in range(H):
        for x in range(W):
            if (x - CX) ** 2 + (y - CY) ** 2 <= 4.6 ** 2:
                pic.block(x, y, FACE)
    for dx in (-3, 3):
        pic.block(CX + dx, CY, BLUSH)
    eyes, mouth = MOODS[feeling]
    look = (-1, 0, 1, 0)[int(t * 1.6) % 4] if feeling == "look" else 0
    pic.write(2 * CX + 1 - len(eyes) // 2 + look, CY - 1, eyes, INK, bold=True)
    pic.write(2 * CX, CY, mouth, INK, bold=True)
    centred(pic, CY + 2, text, INK, bold=True)


def mood(st, t):
    state = st.get("state")
    if state == "done":
        return "joy"
    if state in ("failed", "cancelled") or st.get("trouble"):
        return "sad"
    if state == "paused":
        return "blink"
    if state != "downloading" or not st.get("bytes"):    # nothing has arrived yet
        return "look"
    return "blink" if t % 3.2 < 0.2 else "happy"        # a blink every few seconds


def middle_text(st):
    if st.get("state") == "done":
        return "100%"
    if st.get("size"):
        return f"{int(100 * fraction(st))}%"
    return f"{st['bytes'] / 1e6:.0f}MB" if st.get("bytes") else "…"


def caption(st):
    """Three lines under the face: (text, colour), None for the terminal's own colour."""
    name = os.path.basename(st.get("file") or "") or st.get("url", "")
    state, done, size = st.get("state"), st.get("bytes") or 0, st.get("size")
    error, trouble = st.get("error") or {}, st.get("trouble")
    if state == "done":
        took = st.get("finished", 0) - st.get("started", 0)
        lines = [f"saved · {mb(done)} in {took:.1f}s", f"in {short(os.path.dirname(st['file']))}"]
    elif state == "failed":
        lines = [error.get("message", "failed"), error.get("hint", "").split(". ")[0]]
    elif state == "paused":
        lines = [f"paused at {int(100 * fraction(st))}%" if size else f"paused at {mb(done)}",
                 f"carry on: idm resume {st.get('job')}"]
    elif state == "cancelled":
        lines = ["cancelled", ""]
    elif state == "scheduled" and st.get("starts_at"):
        starts = st["starts_at"]
        lines = [f"starts at {when.clock(starts)}", f"in {when.how_long(starts - time.time())}"]
    elif state == "queued" and st.get("waiting_for"):
        lines = ["waiting for", st["waiting_for"]]
    elif (state != "downloading" or not done) and not trouble:
        lines = ["starting…", ""]
    else:
        amount = f"{mb(done)[:-3]} of {mb(size)}" if size else mb(done)
        many = f" · {st['connections']} connections" if (st.get("connections") or 1) > 1 else ""
        left = f"{st['eta']}s left" if st.get("eta") is not None else ""
        quiet = f"nothing for {when.how_long(st['stalled'])} · " if st.get("stalled") else ""
        lines = [f"{amount} · {mb(st.get('speed'))}/s{many}",
                 f"{quiet}{trouble['message']} · retrying in {trouble['retry_in']}s" if trouble else left]
    return [(name, RING)] + [(line, None) for line in lines]


# ---- the picture ---------------------------------------------------------------------------------
class Picture:
    """A grid of terminal cells, each a character, its colour, a background colour ("#RRGGBB",
    or None for the terminal's own) and whether it is bold."""

    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows
        self.cells = [[[" ", None, None, False] for _ in range(columns)] for _ in range(rows)]

    def block(self, x, y, color):
        """A chunky pixel: two cells of solid colour."""
        for col in (2 * x, 2 * x + 1):
            self.cells[y][col] = [" ", None, color, False]

    def dot(self, x, y, char, color):
        """A dot at (x columns, y half-rows), so it can move in half-row steps."""
        col, row = int(x), int(y) // 2
        if 0 <= col < self.columns and 0 <= row < H:
            self.cells[row][col][:2] = [char, color]

    def write(self, col, row, text, color=None, bold=False):
        """Text over whatever background is there, cut at the edge."""
        for i, char in enumerate(text):
            if char != " " and 0 <= col + i < self.columns:
                self.cells[row][col + i] = [char, color, self.cells[row][col + i][2], bold]

    def ansi(self):
        """The rows as text with colour codes (true colour), for a terminal."""
        lines = []
        for row in self.cells:
            out, last = [], None
            for char, fg, bg, bold in row:
                style = (fg, bg, bold)
                if style != last:
                    out.append("\x1b[0m" + (code(38, fg) if fg else "") + (code(48, bg) if bg else "")
                               + ("\x1b[1m" if bold else ""))
                    last = style
                out.append(char)
            lines.append("".join(out) + "\x1b[0m")
        return lines

    def packed(self):
        """Every cell as three little-endian 32-bit numbers (character, text colour, background),
        in base64: what a Claude Code `Raster` takes."""
        def color(c):
            return int(c[1:], 16) if c else 0x01000000      # bit 24: the terminal's own colour
        return base64.b64encode(b"".join(struct.pack("<3I", ord(char), color(fg), color(bg))
                                         for row in self.cells for char, fg, bg, _ in row)).decode()


# ---- small helpers -------------------------------------------------------------------------------
def centred(pic, row, text, color=None, bold=False):
    """Text centred under the face (cut with … if it is wider than the picture)."""
    if len(text) > pic.columns:
        text = text[:pic.columns - 1] + "…"
    start = min(max(0, 2 * CX + 1 - (len(text) + 1) // 2), pic.columns - len(text))
    pic.write(start, row, text, color, bold)


def fits():
    """Can this terminal show the face: big enough, and in true colour?"""
    size = shutil.get_terminal_size()
    return (size.columns >= COLUMNS and size.lines >= ROWS + 3
            and os.environ.get("COLORTERM") in ("truecolor", "24bit"))


def fraction(st):
    size = st.get("size")
    return min(1.0, (st.get("bytes") or 0) / size) if size else 0.0


def scatter(rng):
    """Where the dots float: spread out, away from the face and from each other."""
    spots, tries = [], 0
    while len(spots) < DOTS:
        x, y = rng.uniform(1, 2 * W - 2), rng.uniform(1, 2 * H - 2)
        tries += 1
        far_from_face = math.hypot(x - 2 * CX - 1, y - 2 * CY - 1) >= 13.5
        apart = tries > 5000 or all(math.hypot(x - a, y - b) >= 4.5 for a, b, _, _ in spots)
        if far_from_face and apart:
            spots.append((x, y, rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)))
    return spots


def drift(spot, t):
    """Where a floating dot is at time t: it bobs gently around its spot."""
    x, y, phase_x, phase_y = spot
    return (min(max(x + 2.2 * math.sin(t * 0.6 + phase_x), 0), 2 * W - 1),
            min(max(y + 1.6 * math.sin(t * 0.8 + phase_y), 0), 2 * H - 1))


def mix(a, b, t):
    """A colour between a (t = 0) and b (t = 1)."""
    rgb = [(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(round(c) for c in rgb)


def code(kind, color):
    return f"\x1b[{kind};2;{int(color[1:3], 16)};{int(color[3:5], 16)};{int(color[5:7], 16)}m"


def mb(n):
    return f"{(n or 0) / 1e6:.1f} MB"


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path
