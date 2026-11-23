#!/usr/bin/env python3
"""
Mascot gallery: the three candidate characters, animated, plus tako's moods.

    python3 mascots.py            # animates for 12 seconds (Ctrl+C to stop early)
    python3 mascots.py --once     # print one still frame

Stdlib only, Python 3.9+.  Best in a truecolor terminal (macOS Terminal on
macOS 26, iTerm2, Ghostty, Warp, VS Code); falls back to 256 colours.
"""
import argparse
import sys
import time

from pixels import MASCOTS, RESET, TAKO_PAL, fg, render

BOLD, DIM = "\x1b[1m", "\x1b[2m"
COL = 24          # width of one gallery column
GAP = "  "


def pad(s, visible, width):
    return s + " " * max(0, width - visible)


def candidates(tick):
    phase = (tick // 25) % 3                    # each mood ~2.5 s
    mood = ("work", "happy", "idle")[phase]
    blocks = []
    for name, (fn, pal, pitch) in MASCOTS.items():
        if name == "stash":
            art = fn(mood, tick, fill=(tick % 75) / 75 if mood == "work" else 0.0)
        else:
            art = fn(mood, tick)
        lines = [pad("  " + l, 18, COL) for l in render(art, pal)]
        accent = next(iter(pal.values()))
        lines.append("")
        lines.append(pad("  " + fg(accent) + BOLD + name + RESET, 2 + len(name), COL))
        words, cur, wrapped = pitch.split(), "", []
        for w in words:
            if len(cur) + len(w) + 1 > COL - 3:
                wrapped.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        wrapped.append(cur)
        for w in wrapped[:2]:
            lines.append(pad("  " + DIM + w + RESET, 2 + len(w), COL))
        while len(lines) < 10:
            lines.append(" " * COL)
        blocks.append(lines)
    return [GAP.join(parts) for parts in zip(*blocks)]


MOODS = [("work", "downloading"), ("whoa", "server said 429"), ("sleep", "paused"), ("happy", "done!")]


def moods(tick):
    fn, pal, _ = MASCOTS["tako"]
    blocks = []
    for mood, label in MOODS:
        lines = [l + " " for l in render(fn(mood, tick), pal)]
        lines.append(pad(DIM + label + RESET, len(label), 17))
        blocks.append(lines)
    return ["  " + "  ".join(parts) for parts in zip(*blocks)]


def frame(tick):
    out = ["", "  " + BOLD + "Pick a mascot" + RESET + DIM + "   (all drawn with ▀ half-blocks, 16×12 pixels)" + RESET, ""]
    out += candidates(tick)
    out += ["", "  " + fg(TAKO_PAL["B"]) + BOLD + "tako" + RESET + BOLD + "'s moods" + RESET +
            DIM + "   (the UI switches mood when something happens)" + RESET, ""]
    out += moods(tick)
    out.append("")
    return out


def main():
    ap = argparse.ArgumentParser(description="Mascot gallery")
    ap.add_argument("--once", action="store_true", help="print one still frame")
    ap.add_argument("--seconds", type=float, default=12)
    args = ap.parse_args()
    if args.once or not sys.stdout.isatty():
        print("\n".join(frame(7)))
        return
    out = sys.stdout
    out.write("\x1b[?25l")
    drawn = 0
    try:
        t0 = time.time()
        tick = 0
        while time.time() - t0 < args.seconds:
            lines = frame(tick)
            if drawn:
                out.write("\x1b[%dA" % drawn)
            out.write("".join("\r\x1b[K" + l + "\n" for l in lines))
            out.flush()
            drawn = len(lines)
            tick += 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h" + RESET)
        out.flush()


if __name__ == "__main__":
    main()
