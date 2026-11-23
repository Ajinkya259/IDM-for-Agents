#!/usr/bin/env python3
"""
tako - full terminal UI mockup.  NOTHING IS DOWNLOADED.

Same simulated download as ui_demo.py (61.9 MB video, 8 connections, a server
that throttles each connection and answers 429 to more than 6).

tako IS the progress display:
  * its head fills up like a glass as the file comes in
  * its 8 tentacles are the 8 connections; each tip wears that connection's
    colour (the same colour as its pieces in the bar below)
  * an arm's shape is its state: swimming coloured tip = receiving, fully
    coloured and still = finished, short stub with a blinking tip = the server
    said 429, plain lavender = resting, pulsing tip = connecting
  * its face shows what is going on (looking, working, whoa, napping, happy)

    python3 tako_ui.py              # watch it (about 20 s)
    keys:  p = pause / resume,  q or Ctrl+C = quit (progress would be kept)

Needs a terminal at least 70 columns wide; narrower falls back to one line.
When output is piped (an AI agent running it) you get JSON lines instead.
Stdlib only, Python 3.9+.
"""
import argparse
import os
import random
import re
import select
import shutil
import sys
import termios
import time
import tty

import ui_demo
from pixels import ARM_COLORS as ARM
from pixels import HERO_PAL, HERO_W, RESET, fg, render, tako_hero
from ui_demo import MAX_CONNS, NAME, SIZE, TICK, Sim, fmt_bytes, fmt_eta

BOLD, DIM, ITAL = "\x1b[1m", "\x1b[2m", "\x1b[3m"
LAV, LAV_D = HERO_PAL["F"], HERO_PAL["O"]
GREEN, RED, AMBER = "#1F9D55", "#E5484D", "#D99A06"
HOST = "archive.org"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MIN_COLS = 70
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---- text helpers ------------------------------------------------------------------
def vis(s):
    return len(_ANSI.sub("", s))


def fit(s, width):
    """Pad or cut a coloured string to exactly `width` visible columns."""
    n = vis(s)
    if n <= width:
        return s + " " * (width - n)
    out, count, i = [], 0, 0
    while i < len(s) and count < width - 1:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        count += 1
        i += 1
    return "".join(out) + "…" + RESET


def say(text, width):
    """tako's speech: '› text', wrapped onto a second line if needed. Returns 2 lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width - 2:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    if len(lines) > 2:
        lines = [lines[0], lines[1][:width - 3] + "…"]
    first = fg(LAV_D) + "› " + RESET + ITAL + lines[0] + RESET
    return [first, ("  " + ITAL + lines[1] + RESET) if len(lines) > 1 else ""]


TITLE = (" tako ", " v0.3 · demo, nothing is really downloaded ")


# ---- pieces of the screen ------------------------------------------------------------
def header(W, art_lines, texts):
    tw = W - HERO_W - 7
    edge = fg(LAV)
    name, ver = TITLE
    top = (edge + "╭─" + RESET + BOLD + fg(LAV_D) + name + RESET + DIM + ver + RESET +
           edge + "─" * (W - 3 - len(name) - len(ver)) + "╮" + RESET)
    lines = [top]
    for i, art in enumerate(art_lines):
        t = texts[i] if i < len(texts) else ""
        lines.append(edge + "│" + RESET + " " + art + "   " + fit(t, tw) + " " + edge + "│" + RESET)
    lines.append(edge + "╰" + "─" * (W - 2) + "╯" + RESET)
    return lines


def hero(sim, mood, tick, fill=None):
    arms = []
    for k in range(1, MAX_CONNS + 1):
        conn = sim.conns[k]
        arms.append((conn.state, conn.bytes > 0))
    f = sim.done_bytes() / SIZE if fill is None else fill
    return render(tako_hero(f, arms, mood, tick), HERO_PAL)


def seg_map(sim, w):
    out, last = [], None
    for i in range(w):
        a, b = SIZE * i // w, SIZE * (i + 1) // w
        mid = (a + b) // 2
        seg = next((s for s in sim.segs if s.start <= mid < s.end), sim.segs[-1])
        col = ARM[(seg.conn - 1) % len(ARM)]
        if mid < seg.pos:
            ch = "█"
        elif a <= seg.pos < b and seg.pos < seg.end:
            ch = "▓"
        else:
            ch = "░"
        out.append((fg(col) if col != last else "") + ch)
        last = col
    return "".join(out) + RESET


def map_lines(sim, W):
    w = W - 4
    pieces = "%d pieces" % len(sim.segs)
    left, right = "0", fmt_bytes(SIZE)
    gap = w - len(left) - len(right) - len(pieces)
    return ["  " + seg_map(sim, w),
            "  " + DIM + left + " " * (gap // 2) + pieces + " " * (gap - gap // 2) + right + RESET]


def arm_cell(sim, k, width, paused=False):
    conn = sim.conns[k]
    head = fg(ARM[k - 1]) + "●" + RESET + " arm %d  " % k
    bw = width - vis(head)
    if conn.state == "active":
        seg = conn.seg
        frac = (seg.pos - seg.start) / max(1, seg.end - seg.start)
        barw = bw - 11
        full = int(frac * barw)
        bar = fg(ARM[k - 1]) + "━" * full + RESET + DIM + "─" * (barw - full) + RESET
        speed = DIM + "paused".rjust(10) + RESET if paused else ("%s/s" % fmt_bytes(conn.speed)).rjust(10)
        body = bar + " " + speed
    elif conn.state == "backoff" and paused:
        body = DIM + "429 too many · paused" + RESET
    elif conn.state == "backoff":
        body = fg(RED) + "429 too many · retry %.1fs" % max(0.0, conn.until - sim.t) + RESET
    elif conn.state == "parked":
        body = DIM + "resting (server allows %d)" % (sim.limit or 0) + RESET
    elif conn.state == "done":
        body = fg(GREEN) + "✓ done" + RESET + DIM + " · %s fetched" % fmt_bytes(conn.bytes) + RESET
    else:
        body = DIM + "reaching out…" + RESET
    return fit(head + body, width)


# ---- whole frames ------------------------------------------------------------------
def frame_probe(W, sim, tick, t):
    ok = fg(GREEN) + "✓" + RESET
    spin = fg(LAV_D) + SPIN[tick % len(SPIN)] + RESET
    steps = [spin + " knocking on " + HOST + "…"] if t < 0.6 else [ok + " server answers " + BOLD + "206 Partial Content" + RESET]
    if t >= 1.1:
        steps.append(ok + " %s · video/mp4 · can be split" % fmt_bytes(SIZE))
    elif t >= 0.6:
        steps.append(spin + " reading size and type…")
    texts = [BOLD + NAME + RESET, ""] + steps
    texts += [""] * (6 - len(texts)) + say("let me look at this server first…", W - HERO_W - 7)
    arms = ["connecting"] + ["idle"] * 7
    art = render(tako_hero(0.0, arms, "look", tick), HERO_PAL)
    return header(W, art, texts) + [""]


def frame_download(W, sim, tick, mood, speech, paused, compact):
    done = sim.done_bytes()
    if paused:
        rate = fg(AMBER) + BOLD + "paused" + RESET
    else:
        rate = BOLD + "%s/s" % fmt_bytes(sim.speed()) + RESET + DIM + "   ETA " + RESET + fmt_eta(sim.eta())
    texts = [BOLD + NAME + RESET,
             DIM + "%s · video/mp4 · %s → ~/Downloads" % (fmt_bytes(SIZE), HOST) + RESET, "",
             BOLD + "%.0f%%" % (100.0 * done / SIZE) + RESET + DIM + "   %s of %s" % (fmt_bytes(done), fmt_bytes(SIZE)) + RESET,
             rate, ""] + say(speech, W - HERO_W - 7)
    lines = header(W, hero(sim, mood, tick), texts) + [""] + map_lines(sim, W) + [""]
    if not compact:
        cw = (W - 7) // 2
        for k in range(1, 5):
            lines.append("  " + arm_cell(sim, k, cw, paused) + "   " + arm_cell(sim, k + 4, cw, paused))
        lines.append("")
    keys = "p resume   q quit" if paused else "p pause   q quit (keeps what's downloaded)"
    lines.append("  " + DIM + keys + RESET)
    return lines


def frame_done(W, sim, tick, took):
    texts = [fg(GREEN) + BOLD + "✓ got it!" + RESET + "  " + BOLD + NAME + RESET,
             DIM + "saved to ~/Downloads (demo: not really)" + RESET, "",
             BOLD + "100%" + RESET + "   %s in %.1fs · %s/s average" % (fmt_bytes(SIZE), took, fmt_bytes(SIZE / took)),
             DIM + "%d pieces · %d arms · sha256 ✓" % (len(sim.segs), sim.limit or MAX_CONNS) + RESET,
             ""] + say("all pieces home!", W - HERO_W - 7)
    return header(W, hero(sim, "happy", tick, fill=1.0), texts) + map_lines(sim, W)[:1] + [
        "  " + DIM + "↑ how the file was split: each colour is one arm" + RESET, ""]


def frame_quit(W, sim):
    pct = 100.0 * sim.done_bytes() / SIZE
    texts = [fg(AMBER) + BOLD + "⏸ paused at %.0f%%" % pct + RESET + "  " + BOLD + NAME + RESET, "",
             "%s of %s kept · %d pieces on disk" % (fmt_bytes(sim.done_bytes()), fmt_bytes(SIZE), len(sim.segs)),
             DIM + "run the same command again to carry on" + RESET, "", ""] + say("nap time. i remember every piece.", W - HERO_W - 7)
    return header(W, hero(sim, "sleep", 8), texts) + ["  " + seg_map(sim, W - 4), ""]


# ---- keyboard ----------------------------------------------------------------------
class Keys:
    """Read single key presses without Enter (cbreak mode); restores the terminal after."""

    def __enter__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        if self.fd is not None:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def wait(self, timeout):
        if self.fd is None:
            time.sleep(timeout)
            return ""
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 32).decode(errors="ignore").lower() if r else ""

    def __exit__(self, *exc):
        if self.fd is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


# ---- main loop ----------------------------------------------------------------------
def run(seed):
    out = sys.stdout
    drawn, last_cols = 0, None

    def draw(lines):
        nonlocal drawn, last_cols
        cols = shutil.get_terminal_size((80, 24)).columns
        if last_cols is not None and cols != last_cols:      # resized: start clean
            out.write("\x1b[2J\x1b[H")
            drawn = 0
        last_cols = cols
        if drawn:
            out.write("\x1b[%dA" % drawn)
        out.write("".join("\r\x1b[K" + l + "\n" for l in lines))
        if len(lines) < drawn:
            out.write("\x1b[J")
        out.flush()
        drawn = len(lines)

    state = {"speech": "8 arms out, grabbing pieces at the same time", "mood": None, "mood_until": 0.0,
             "endgame": False}

    def on_event(event, **kw):
        if event == "split":
            if sim.t > 3.0:
                state["speech"] = "arm %d done early, so it's helping arm %d" % (kw["conn"], kw["from_conn"])
            elif kw["conn"] <= 3:
                state["speech"] = "arm %d took the back half of arm %d's piece" % (kw["conn"], kw["from_conn"])
        elif event == "throttle":
            state["speech"] = "whoa! the server said 429 (too many) to arm %d, so it curled up to wait" % kw["conn"]
            state["mood"], state["mood_until"] = "whoa", sim.t + 2.5
        elif event == "host_limit":
            n = kw["connections"]
            state["speech"] = "ok, this server allows %d arms. the other %d rest" % (n, MAX_CONNS - n)

    sim = Sim(random.Random(seed), on_event)
    tick, paused = 0, False
    width = lambda: min(shutil.get_terminal_size((80, 24)).columns, 92)
    out.write("\x1b[?25l")
    try:
        with Keys() as keys:
            t = 0.0
            while t < 1.6:                                   # 1) look at the server
                draw(frame_probe(width(), sim, tick, t))
                if keys.wait(TICK) == "q":
                    raise KeyboardInterrupt
                t += TICK
                tick += 1
            while not sim.finished():                        # 2) download
                rows = shutil.get_terminal_size((80, 24)).lines
                if not paused:
                    if state["mood"] and sim.t >= state["mood_until"]:
                        state["mood"] = None
                    if not state["endgame"] and sim.done_bytes() > 0.88 * SIZE:
                        state["endgame"] = True
                        state["speech"] = "almost there… fast arms help the slow ones"
                mood = "sleep" if paused else (state["mood"] or "work")
                speech = "zzz… press p to wake me" if paused else state["speech"]
                draw(frame_download(width(), sim, tick, mood, speech, paused, compact=rows < 22))
                key = keys.wait(TICK)
                if key == "q":
                    raise KeyboardInterrupt
                if key == "p":
                    paused = not paused
                if not paused:
                    sim.step()
                tick += 1
            took = sim.t
            for i in range(24):                              # 3) celebrate
                draw(frame_done(width(), sim, i, took))
                time.sleep(TICK)
            draw(frame_done(width(), sim, 0, took))
    except KeyboardInterrupt:
        draw(frame_quit(width(), sim))
    finally:
        out.write("\x1b[?25h" + RESET)
        out.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="tako terminal UI mockup (simulated, downloads nothing)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if not sys.stdout.isatty():
        print("(not a terminal, so showing what an AI agent would read)", file=sys.stderr)
        ui_demo.run("agent", args.seed)
    elif shutil.get_terminal_size((80, 24)).columns < MIN_COLS:
        ui_demo.USE_COLOR, ui_demo.BRAND = True, "tako"
        ui_demo.run("line", args.seed)
    else:
        run(args.seed)
