"""
Pixel-art mascots for the terminal.

How it works: every terminal cell is split into two square "pixels" with the
half-block character ▀.  The top pixel is the text colour, the bottom pixel is
the background colour.  So a 16 x 12 pixel sprite becomes 16 columns x 6 rows.
(Claude Code draws Clawd the same way, but with quarter-blocks like ▛ ▜ and one
colour; half-blocks give us two colours per cell, so we can have eyes, blush...)

Each sprite is a list of strings; one letter = one pixel, "." = see-through.
Stdlib only, Python 3.9+.
"""
import os

TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
RESET = "\x1b[0m"


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _xterm256(rgb):
    """Nearest colour in the 256-colour palette (for terminals without truecolor)."""
    r, g, b = rgb
    steps = [0, 95, 135, 175, 215, 255]

    def q(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40

    ri, gi, bi = q(r), q(g), q(b)
    cube = 16 + 36 * ri + 6 * gi + bi
    dc = (steps[ri] - r) ** 2 + (steps[gi] - g) ** 2 + (steps[bi] - b) ** 2
    gray_i = max(0, min(23, ((r + g + b) // 3 - 3) // 10))
    gv = 8 + 10 * gray_i
    dg = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 232 + gray_i if dg < dc else cube


def fg(h):
    return "\x1b[38;2;%d;%d;%dm" % _rgb(h) if TRUECOLOR else "\x1b[38;5;%dm" % _xterm256(_rgb(h))


def bg(h):
    return "\x1b[48;2;%d;%d;%dm" % _rgb(h) if TRUECOLOR else "\x1b[48;5;%dm" % _xterm256(_rgb(h))


def render(rows, pal):
    """Pixel rows -> terminal lines (two pixel rows per line)."""
    lines = []
    for y in range(0, len(rows), 2):
        top = rows[y]
        bot = rows[y + 1] if y + 1 < len(rows) else "." * len(top)
        out, last = [], None
        for a, b in zip(top, bot):
            ca, cb = pal.get(a), pal.get(b)
            if a in "zZ" or b in "zZ":                  # a letter, not a pixel (sleepy z's)
                key, s = ("txt", a, b), RESET + fg(pal["Z"]) + (a if a in "zZ" else b)
            elif ca is None and cb is None:
                key, s = "_", RESET + " "
            elif ca is None:
                key, s = ("lo", cb), RESET + fg(cb) + "▄"
            elif ca == cb:                              # solid: paint the background (no line gaps
                key, s = ("solid", ca), RESET + bg(ca) + " "    # in macOS Terminal, unlike █)
            elif cb is None:
                key, s = ("hi", ca), RESET + fg(ca) + "▀"
            else:
                key, s = ("both", ca, cb), RESET + fg(ca) + bg(cb) + "▀"
            out.append(s if key != last else s[len(s) - 1:])   # skip repeated colour codes
            last = key
        lines.append("".join(out) + RESET)
    return lines


def _put(rows, pts, ch):
    for x, y in pts:
        if 0 <= y < len(rows) and 0 <= x < len(rows[y]):
            r = rows[y]
            rows[y] = r[:x] + ch + r[x + 1:]


# ---------------------------------------------------------------------------
# 1. TAKO - the octopus.  Eight arms, eight connections.
# ---------------------------------------------------------------------------
TAKO_PAL = {
    "B": "#A98BFF",  # body
    "D": "#8467E6",  # shade
    "L": "#DCD2FF",  # shine
    "K": "#2B1D4E",  # eyes / mouth
    "W": "#FFFFFF",  # eye sparkle
    "P": "#FF8FB8",  # blush / tongue
    "S": "#4CB8F5",  # sweat drop
    "Y": "#FFC23D",  # sparkles
    "Z": "#8F86B3",  # zzz
}
_TAKO_HEAD = [
    "......BBBB......",
    "....BBBBBBBB....",
    "...BLLBBBBBBD...",
    "..BLLBBBBBBBBD..",
    ".BLBBBBBBBBBBBD.",
    ".BBBBBBBBBBBBBD.",
    ".BBBBBBBBBBBBBD.",
    ".BBBBBBBBBBBBBD.",
    ".BPPBBBBBBBBPPD.",
    ".BBBBBBBBBBBBBD.",
]
_TAKO_LEGS = {
    "out": [".BBB.BB..BB.BBB.", "BB...DB..BD...BB"],    # tips curl outwards
    "in": [".BBB.BB..BB.BBB.", "..BD.B....B.DB.."],     # tips curl inwards
    "tuck": [".BBBBBBBBBBBBBB.", ".BB.BB.BB.BB.BB."],   # sitting down
}
_SPARKLE_A = [(1, 0), (0, 1), (1, 1), (2, 1), (1, 2)]
_SPARKLE_B = [(14, 0), (13, 1), (14, 1), (15, 1), (14, 2)]


def tako(mood="idle", tick=0):
    """mood: idle | look | work | whoa | sleep | happy.  tick = animation frame counter."""
    rows = list(_TAKO_HEAD)
    legs = "out"
    blink = mood in ("idle", "work", "look") and tick % 32 in (30, 31)
    if mood == "look":
        dx = (-1, 0, 1, 0)[(tick // 6) % 4]
        if not blink:
            _put(rows, [(4 + dx, 6), (10 + dx, 6)], "W")
            _put(rows, [(5 + dx, 6), (11 + dx, 6)], "K")
            _put(rows, [(4 + dx, 7), (5 + dx, 7), (10 + dx, 7), (11 + dx, 7)], "K")
    elif mood == "whoa":
        _put(rows, [(4, 5), (10, 5)], "W")
        _put(rows, [(5, 5), (11, 5), (4, 6), (5, 6), (10, 6), (11, 6), (4, 7), (5, 7), (10, 7), (11, 7)], "K")
        _put(rows, [(7, 8), (8, 8), (7, 9), (8, 9)], "K")
        _put(rows, [(14, 0), (13, 1), (14, 1), (13, 2), (14, 2)], "S")
        legs = "in" if tick % 2 else "out"              # shivering
    elif mood == "sleep":
        _put(rows, [(4, 7), (5, 7), (10, 7), (11, 7)], "K")
        _put(rows, [(7, 8), (8, 8)], "K")
        _put(rows, [(14, 2)], "z")                      # z / Z are drawn as letters
        if (tick // 8) % 2:
            _put(rows, [(15, 0)], "Z")
        legs = "tuck"
    elif mood == "happy":
        _put(rows, [(4, 6), (3, 7), (5, 7), (11, 6), (10, 7), (12, 7)], "K")
        _put(rows, [(7, 8), (8, 8)], "K")
        _put(rows, [(7, 9), (8, 9)], "P")
        _put(rows, _SPARKLE_A if (tick // 3) % 2 else _SPARKLE_B, "Y")
        legs = "in" if (tick // 2) % 2 else "out"       # little dance
    if mood in ("idle", "work") or (mood == "look" and blink):
        if blink:
            _put(rows, [(4, 7), (5, 7), (10, 7), (11, 7)], "K")
        else:
            _put(rows, [(4, 6), (10, 6)], "W")
            _put(rows, [(5, 6), (11, 6), (4, 7), (5, 7), (10, 7), (11, 7)], "K")
    if mood in ("idle", "work", "look"):
        _put(rows, [(7, 8), (8, 8)], "K")
    if mood == "work":
        legs = "in" if (tick // 3) % 2 else "out"
    return rows + _TAKO_LEGS[legs]


# ---------------------------------------------------------------------------
# 2. STASH - the hamster.  Cheeks fill up as the file fills up.
# ---------------------------------------------------------------------------
STASH_PAL = {
    "F": "#F2A65E",  # fur
    "D": "#D6823A",  # shade
    "H": "#FFE7CB",  # cream
    "K": "#2E1F14",  # eyes
    "W": "#FFFFFF",
    "P": "#FF9FB2",  # ears / feet / blush
    "N": "#EE6A8E",  # nose
    "Y": "#FFC23D",
    "S": "#4CB8F5",
    "Z": "#A39A8E",
}
_STASH = [
    "...FF......FF...",
    "..FPPFFFFFFPPF..",
    ".FFFFFFFFFFFFFD.",
    ".FFFFFFFFFFFFFD.",
    ".FFFFFFFFFFFFFD.",
    ".FFFFFFFFFFFFFD.",
    ".FHHHHHNNHHHHHD.",
    ".HHPHHHHHHHHPHH.",
    ".HHHHHHHHHHHHHH.",
    ".FHHHHHHHHHHHHD.",
    "..FFHHHHHHHHFD..",
    "...PP......PP...",
]


def stash(mood="idle", tick=0, fill=0.0):
    rows = list(_STASH)
    if fill > 0.33 or mood == "happy":
        _put(rows, [(0, 7), (15, 7)], "H")
    if fill > 0.66 or mood == "happy":
        _put(rows, [(0, 6), (0, 8), (15, 6), (15, 8)], "H")
    blink = mood in ("idle", "work") and tick % 32 in (30, 31)
    if mood == "happy":
        _put(rows, [(4, 4), (3, 5), (5, 5), (11, 4), (10, 5), (12, 5)], "K")
        _put(rows, _SPARKLE_A if (tick // 3) % 2 else _SPARKLE_B, "Y")
    elif mood == "sleep" or blink:
        _put(rows, [(3, 5), (4, 5), (11, 5), (12, 5)], "K")
    else:
        _put(rows, [(3, 4), (11, 4)], "W")
        _put(rows, [(4, 4), (12, 4), (3, 5), (4, 5), (11, 5), (12, 5)], "K")
    _put(rows, [(7, 7), (8, 7)], "K")
    if mood == "work" and (tick // 3) % 2:       # chewing
        _put(rows, [(7, 7), (8, 7)], "H")
        _put(rows, [(7, 8), (8, 8)], "K")
    return rows


# ---------------------------------------------------------------------------
# 3. PARCEL - a cardboard box.  Flaps open while bytes go in.
# ---------------------------------------------------------------------------
PARCEL_PAL = {
    "C": "#DDA46A",  # cardboard
    "D": "#B9814A",  # shade
    "E": "#8C5E30",  # dark edge / legs
    "T": "#F3D9A8",  # top of the box
    "K": "#2B1D14",
    "W": "#FFFFFF",
    "P": "#FF9FB2",
    "Y": "#FFC23D",
}
_PARCEL_SHUT = ["..TTTTTTTTTTTT..", ".TTTTTTEETTTTTT."]
_PARCEL_OPEN = ["CC............CC", ".CD..........DC."]
_PARCEL_BODY = [
    ".CCCCCCCCCCCCCD.",
    ".CCCCCCCCCCCCCD.",
    ".CCCCCCCCCCCCCD.",
    ".CCCCCCCCCCCCCD.",
    ".CPPCCCCCCCCPPD.",
    ".CCCCCCCCCCCCCD.",
    ".CCCCCCCCCCCCCD.",
    ".DDDDDDDDDDDDDD.",
]
_PARCEL_LEGS = {"stand": ["...EE......EE...", "..EEE......EEE.."],
                "step": ["...EE......EE...", "..EEE.......EEE."]}


def parcel(mood="idle", tick=0):
    top = _PARCEL_OPEN if mood == "work" and (tick // 4) % 2 else _PARCEL_SHUT
    rows = list(top) + list(_PARCEL_BODY) + list(
        _PARCEL_LEGS["step" if mood in ("work", "happy") and (tick // 3) % 2 else "stand"])
    blink = mood in ("idle", "work") and tick % 32 in (30, 31)
    if mood == "happy":
        _put(rows, [(4, 4), (3, 5), (5, 5), (11, 4), (10, 5), (12, 5)], "K")
        _put(rows, [(7, 6), (8, 6)], "K")
        _put(rows, _SPARKLE_A if (tick // 3) % 2 else _SPARKLE_B, "Y")
    elif mood == "sleep" or blink:
        _put(rows, [(3, 5), (4, 5), (11, 5), (12, 5), (7, 6), (8, 6)], "K")
    else:
        _put(rows, [(3, 4), (11, 4)], "W")
        _put(rows, [(4, 4), (12, 4), (3, 5), (4, 5), (11, 5), (12, 5), (7, 6), (8, 6)], "K")
    return rows


MASCOTS = {
    "tako": (tako, TAKO_PAL, "an octopus: 8 arms, 8 connections"),
    "stash": (stash, STASH_PAL, "a hamster: cheeks fill as the file does"),
    "parcel": (parcel, PARCEL_PAL, "a box: flaps open while bytes go in"),
}

for _name, (_fn, _pal, _) in MASCOTS.items():           # every frame must be 16 x 12
    for _m in ("idle", "look", "work", "whoa", "sleep", "happy"):
        for _t in range(40):
            _r = _fn(_m, _t)
            assert len(_r) == 12 and all(len(x) == 16 for x in _r), (_name, _m, _t)


# ---------------------------------------------------------------------------
# TAKO HERO - the big tako that IS the progress display.
#   * its round head fills up like a glass as the file comes in (rippling surface)
#   * its 8 arms are the 8 connections; each arm tip wears that connection's colour,
#     the same colour as its pieces in the bar.  The SHAPE of an arm is its state:
#       swimming coloured tip = receiving      fully coloured + still = finished its work
#       stub + blinking tip   = server said 429  plain lavender = resting / not started
#       lavender with a slowly pulsing tip = connecting
#   * its face shows the mood
# 25 x 16 pixels = 25 columns x 8 rows.
# ---------------------------------------------------------------------------
import math

ARM_COLORS = ["#3B82F6", "#F97316", "#10B981", "#E11D48", "#8B5CF6", "#CA8A04", "#0891B2", "#DB2777"]
HERO_W, HERO_H = 25, 16
HERO_PAL = dict(TAKO_PAL)
HERO_PAL.update({
    "O": "#7456DD",  # outline
    "F": "#A98BFF",  # liquid
    "E": "#EEE9FF",  # empty part of the head
    "V": "#D6CAFF",  # bubbles / shine on glass
    "A": "#9677F5",  # skirt and arms
})
HERO_PAL.update({str(i + 1): c for i, c in enumerate(ARM_COLORS)})

# head rows: (first x, last x); a round bulb centred on x = 12.  Row 9 is the bottom edge.
_HEAD = {0: (9, 15), 1: (7, 17), 2: (5, 19), 3: (4, 20), 4: (3, 21), 5: (3, 21),
         6: (3, 21), 7: (3, 21), 8: (4, 20), 9: (5, 19)}
_LIQUID = 8                                           # liquid rows: y = 8 (bottom) .. 1
_SKIRT = {10: (3, 21)}                                # where the arms join the head
# The 4 left arms (the right 4 are mirrored).  Each arm lives in its own 3-column lane
# (x 0-2, 3-5, 6-8, 9-11; column 12 stays empty), so arms can never touch.
# body = lavender pixels; ext / mid / down = tip poses for the swimming wave.
_ARMS_LEFT = [
    {"body": [(1, 11), (2, 11), (0, 12), (1, 12), (0, 13), (0, 14)],           # outer arm: swept out
     "ext": [(0, 15), (1, 15)], "mid": [(1, 15), (2, 14)], "down": [(0, 15)]},
    {"body": [(4, 11), (5, 11), (4, 12), (5, 12), (4, 13), (4, 14)],
     "ext": [(4, 15), (5, 15)], "mid": [(5, 15), (5, 14)], "down": [(4, 15)]},
    {"body": [(7, 11), (8, 11), (7, 12), (8, 12), (7, 13), (8, 13), (7, 14)],
     "ext": [(7, 15), (8, 15)], "mid": [(8, 15), (8, 14)], "down": [(7, 15)]},
    {"body": [(10, 11), (11, 11), (10, 12), (11, 12), (10, 13), (11, 13), (10, 14)],   # inner arm: long
     "ext": [(10, 15), (11, 15)], "mid": [(11, 15), (11, 14)], "down": [(10, 15)]},
]
_WAVE = ("ext", "mid", "ext", "down")                 # 4-step swim cycle, neighbours one step apart


def _in_head(x, y):
    span = _HEAD.get(y)
    return span is not None and span[0] <= x <= span[1]


def _arm(k):
    """Pixel sets for arm k (0..7)."""
    if k < 4:
        return _ARMS_LEFT[k]
    m = _ARMS_LEFT[7 - k]
    return {key: [(24 - x, y) for x, y in pts] for key, pts in m.items()}


def _arm_pixels(k, state, mood, tick):
    """[(x, y, colour letter)] for arm k (0..7).  Each state has its own still pose:
      active      coloured tip, swimming wave
      done        lower arm fully in its colour, straight and still
      backoff     short stub with a blinking coloured tip (server said 429)
      connecting  lavender arm whose tip pulses slowly in its colour
      idle/parked plain lavender arm hanging down, no colour"""
    arm, tip = _arm(k), str(k + 1)
    body = [(x, y, "A") for x, y in arm["body"]]
    if state == "backoff":
        px = [(x, y, "A") for x, y in arm["body"] if y <= 12]
        if mood != "sleep" and (tick // 4) % 2 == 0:
            x13 = [p for p in arm["body"] if p[1] == 13][0]
            px.append((x13[0], 13, tip))
        return px
    if state == "done":
        return [(x, y, tip if y >= 13 else "A") for x, y in arm["body"]] + [(x, y, tip) for x, y in arm["ext"]]
    if state == "active":
        if mood == "sleep":
            pose = "ext"
        elif mood == "happy":
            pose = _WAVE[(tick // 3) % 4]                   # everyone dances together
        else:
            pose = _WAVE[(tick // 3 + k) % 4]               # a wave travelling across the arms
        return body + [(x, y, tip) for x, y in arm[pose]]
    ext = [(x, y, "A") for x, y in arm["ext"]]
    if state == "connecting" and mood != "sleep" and (tick // 5) % 2 == 0:
        ext[0] = (ext[0][0], ext[0][1], tip)                # reaching out: tip pulses in its colour
    return body + ext                                       # idle / parked: plain lavender


def tako_hero(fill=0.0, arms=None, mood="work", tick=0):
    """fill: 0..1 of the file done.
    arms: 8 entries, each a state string or (state, used bool);
          states: connecting / active / backoff / parked / done.
    mood: look | work | whoa | sleep | happy."""
    arms = arms or ["active"] * 8
    g = [["."] * HERO_W for _ in range(HERO_H)]
    still = mood == "sleep"

    # 1) the head: outline, then liquid up to `fill` with a gentle sine ripple
    level = max(0.0, min(1.0, fill)) * _LIQUID
    amp = 0.6 * min(1.0, level, _LIQUID - level)
    phase = 0.0 if still else tick / 4.0
    for y, (a, b) in _HEAD.items():
        for x in range(a, b + 1):
            inside = _in_head(x - 1, y) and _in_head(x + 1, y) and _in_head(x, y - 1) and _in_head(x, y + 1)
            if not inside:
                g[y][x] = "O"
                continue
            h = level + amp * math.sin(2 * math.pi * (x - phase) / 10.0)
            g[y][x] = "F" if (8 - y) + 0.5 < h else "E"
    for y, (a, b) in _SKIRT.items():
        for x in range(a, b + 1):
            g[y][x] = "A"
    if not still and level >= 2:                            # a bubble rising up the side
        bx = (4, 20)[(tick // 12) % 2]
        by = 7 - (tick % 12) // 3
        if by >= 4 and g[by][bx] == "F":
            g[by][bx] = "V"
    for x, y in [(8, 2), (7, 2), (6, 3), (5, 4)]:          # shine on the glass
        g[y][x] = "W" if g[y][x] == "F" else "V"

    # 2) the face
    def put(pts, ch):
        for x, y in pts:
            if 0 <= x < HERO_W and 0 <= y < HERO_H:
                g[y][x] = ch

    blink = mood in ("work", "look") and tick % 40 in (38, 39)
    put([(5, 6), (6, 6), (18, 6), (19, 6)], "P")
    if mood == "happy":
        put([(7, 5), (8, 4), (9, 4), (10, 5), (14, 5), (15, 4), (16, 4), (17, 5)], "K")
        put([(11, 6), (12, 6), (13, 6)], "K")
        put([(12, 7)], "P")
        sp = [(2, 0), (1, 1), (2, 1), (3, 1), (2, 2)] if (tick // 6) % 2 else [(22, 0), (21, 1), (22, 1), (23, 1), (22, 2)]
        put(sp, "Y")
    elif mood == "whoa":
        put([(8, 3), (9, 3), (15, 3), (16, 3), (8, 4), (9, 4), (15, 4), (16, 4), (8, 5), (9, 5), (15, 5), (16, 5)], "K")
        put([(11, 6), (12, 6), (13, 6), (11, 7), (12, 7), (13, 7)], "K")
        put([(23, 0), (22, 1), (23, 1), (22, 2), (23, 2)], "S")
    elif mood == "sleep" or blink:
        put([(8, 5), (9, 5), (15, 5), (16, 5)], "K")
        put([(12, 7)] if mood == "sleep" else [(11, 6), (13, 6), (12, 7)], "K")
        if mood == "sleep":
            put([(22, 2)], "z")
            if (tick // 8) % 2:
                put([(23, 0)], "Z")
    else:
        dx = (-1, 0, 1, 0)[(tick // 6) % 4] if mood == "look" else 0
        sparkle = fill >= 0.8                               # the white glint only shows once liquid covers the eyes
        put([(8 + dx, 4), (9 + dx, 4), (15 + dx, 4), (16 + dx, 4),
             (8 + dx, 5), (9 + dx, 5), (15 + dx, 5), (16 + dx, 5)], "K")
        if sparkle:
            put([(8 + dx, 4), (15 + dx, 4)], "W")
        put([(11, 6), (13, 6), (12, 7)], "K")

    # 3) the arms, one per connection
    for k in range(8):
        entry = arms[k] if k < len(arms) else "idle"
        state, used = (entry, True) if isinstance(entry, str) else (entry[0], entry[-1])
        if mood == "happy":
            state = "active" if used else "idle"
        for x, y, col in _arm_pixels(k, state, mood, tick):
            if 0 <= x < HERO_W:
                g[y][x] = col
    return ["".join(r) for r in g]


def _selftest():
    states = ["active", "connecting", "backoff", "parked", "done", "idle"]
    for f in (0.0, 0.03, 0.5, 0.97, 1.0):                  # every frame is 25 x 16
        for m in ("look", "work", "whoa", "sleep", "happy"):
            for t in range(24):
                r = tako_hero(f, ["active", "connecting", "backoff", "parked", "done", "active", "idle", "active"], m, t)
                assert len(r) == HERO_H and all(len(x) == HERO_W for x in r)
    for t in range(24):                                     # arms never overlap, in any mix of states
        for st in states:
            seen = {}
            for k in range(8):
                for x, y, _ in _arm_pixels(k, states[(k + t) % len(states)] if st == "active" else st, "work", t):
                    assert (x, y) not in seen, ("overlap", k, seen[(x, y)], t)
                    assert 0 <= x < HERO_W and 11 <= y < HERO_H
                    seen[(x, y)] = k
            for (x, y), k in seen.items():                  # and never side by side
                for n in ((x + 1, y), (x, y + 1)):
                    assert seen.get(n, k) == k, ("touching", k, seen[n], t, (x, y))


_selftest()
