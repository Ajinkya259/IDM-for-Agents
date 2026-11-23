#!/usr/bin/env python3
"""
Terminal mockup for the IDM CLI.  NOTHING IS DOWNLOADED.

It simulates a 61.9 MB video download (8 connections, a server that throttles
each connection and rejects more than 6 with HTTP 429) so you can see how each
display style would look in your own terminal.

    python3 ui_demo.py --style map     # IDM-style: segment map + per-connection table
    python3 ui_demo.py --style line    # one line, like yt-dlp / aria2
    python3 ui_demo.py --style agent   # what an AI agent (Claude Code) would read

Stdlib only, Python 3.9+.
"""
import argparse
import json
import os
import random
import shutil
import sys
import time

# ---- the simulated world -------------------------------------------------
NAME = "big_buck_bunny_720p.mp4"
SIZE = 61_878_609          # bytes
LINE_CAP = 4.8e6           # your internet line, bytes/s
CONN_CAP = 0.85e6          # server throttles each single connection to this
MAX_CONNS = 8
HOST_LIMIT = 6             # server answers 429 to connection #7 and #8
MIN_SPLIT = 1_000_000      # never split a piece smaller than 2 x this
TICK = 0.1                 # seconds per simulation step

BRAND = "idm"              # tako_ui.py renames this
COLORS = [33, 208, 36, 178, 170, 70, 99, 166]   # one colour per connection
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text, code=None, dim=False, bold=False):
    if not USE_COLOR:
        return text
    pre = ""
    if bold:
        pre += "\x1b[1m"
    if dim:
        pre += "\x1b[2m"
    if code is not None:
        pre += "\x1b[38;5;%dm" % code
    return pre + text + "\x1b[0m"


def fmt_bytes(n):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return "%.1f %s" % (n / size, unit)
    return "%d B" % n


def fmt_eta(sec):
    if sec is None:
        return "--"
    sec = int(sec + 0.5)
    return "%dm%02ds" % (sec // 60, sec % 60) if sec >= 60 else "%ds" % sec


class Seg:
    def __init__(self, start, end, conn):
        self.start, self.end, self.pos, self.conn = start, end, start, conn


class Conn:
    def __init__(self, cid):
        self.id, self.seg, self.state = cid, None, "connecting"
        self.speed, self.bytes, self.until = 0.0, 0, 0.0


class Sim:
    """Same rules as IDM / NDM / axel: a free connection takes the back half
    of the largest piece still left."""

    def __init__(self, rng, emit):
        self.rng, self.emit = rng, emit
        self.t = 0.0
        first = Seg(0, SIZE, 1)
        self.segs = [first]
        self.conns = {k: Conn(k) for k in range(1, MAX_CONNS + 1)}
        self.conns[1].seg, self.conns[1].state = first, "active"
        self.join_at = {k: 0.6 + 0.35 * (k - 2) for k in range(2, MAX_CONNS + 1)}
        self.limit = None
        self.history = [(0.0, 0)]

    def done_bytes(self):
        return sum(s.pos - s.start for s in self.segs)

    def finished(self):
        return all(s.pos >= s.end for s in self.segs)

    def split_for(self, conn):
        cands = [s for s in self.segs if s.end - s.pos >= 2 * MIN_SPLIT]
        if not cands:
            return False
        big = max(cands, key=lambda s: s.end - s.pos)
        mid = big.pos + (big.end - big.pos) // 2
        new = Seg(mid, big.end, conn.id)
        big.end = mid
        self.segs.append(new)
        self.segs.sort(key=lambda s: s.start)
        conn.seg, conn.state = new, "active"
        self.emit("split", conn=conn.id, at=mid, from_conn=big.conn, segments=len(self.segs))
        return True

    def step(self):
        self.t += TICK
        for k, at in sorted(self.join_at.items()):
            if self.t >= at:
                del self.join_at[k]
                conn = self.conns[k]
                if k > HOST_LIMIT:
                    conn.state, conn.until = "backoff", self.t + 4.5
                    self.emit("throttle", conn=k, status=429, retry_in=4.5)
                else:
                    self.split_for(conn)
        for conn in self.conns.values():
            if conn.state == "backoff" and self.t >= conn.until:
                conn.state = "parked"
                if self.limit is None:
                    self.limit = HOST_LIMIT
                    self.emit("host_limit", connections=HOST_LIMIT)
        active = [x for x in self.conns.values() if x.state == "active"]
        if active:
            share = min(CONN_CAP, LINE_CAP / len(active))
            for conn in active:
                conn.speed = share * self.rng.uniform(0.85, 1.1)
                seg = conn.seg
                got = min(seg.end - seg.pos, int(conn.speed * TICK))
                seg.pos += got
                conn.bytes += got
                if seg.pos >= seg.end:
                    conn.speed = 0.0
                    if not self.split_for(conn):
                        conn.state = "done"
        for conn in self.conns.values():
            if conn.state != "active":
                conn.speed = 0.0
        self.history.append((self.t, self.done_bytes()))

    def speed(self):
        # average over the last 2 seconds
        t_now, b_now = self.history[-1]
        for t_old, b_old in self.history:
            if t_now - t_old <= 2.0:
                dt = t_now - t_old
                return (b_now - b_old) / dt if dt > 0 else 0.0
        return 0.0

    def eta(self):
        sp = self.speed()
        return (SIZE - self.done_bytes()) / sp if sp > 0 else None

    def active_count(self):
        return sum(1 for x in self.conns.values() if x.state == "active")


# ---- style 1: IDM-style dashboard -----------------------------------------
def render_map(sim, width):
    w = width - 2
    lines = []
    head = " " + c("idm", 39, bold=True) + "  " + c(NAME, bold=True)
    right = "%s · resumable (206)" % fmt_bytes(SIZE)
    pad = max(1, width - 6 - len(NAME) - len(right))
    lines.append(head + " " * pad + c(right, dim=True))

    cells = []
    for i in range(w):
        a, b = SIZE * i // w, SIZE * (i + 1) // w
        mid = (a + b) // 2
        seg = next((s for s in sim.segs if s.start <= mid < s.end), sim.segs[-1])
        col = COLORS[(seg.conn - 1) % len(COLORS)]
        if mid < seg.pos:
            cells.append(c("█", col))
        elif a <= seg.pos < b and seg.pos < seg.end:
            cells.append(c("▓", col))
        else:
            cells.append(c("░", col, dim=True))
    lines.append(" " + "".join(cells))
    lines.append("")
    lines.append(c("  #   piece                 got        speed      state", dim=True))
    for k in range(1, MAX_CONNS + 1):
        conn = sim.conns[k]
        dot = c("●", COLORS[(k - 1) % len(COLORS)])
        if conn.seg is not None:
            piece = "%5.1f – %5.1f MB" % (conn.seg.start / 1e6, conn.seg.end / 1e6)
        else:
            piece = "      –        "
        got = fmt_bytes(conn.bytes) if conn.bytes else "–"
        spd = (fmt_bytes(conn.speed) + "/s") if conn.speed else "–"
        if conn.state == "active":
            state = c("receiving", 36)
        elif conn.state == "backoff":
            state = c("429 Too Many Requests · retry %.1fs" % max(0.0, conn.until - sim.t), 203)
        elif conn.state == "parked":
            state = c("idle · server allows %d" % HOST_LIMIT, dim=True)
        elif conn.state == "done":
            state = c("✓ done", 70)
        else:
            state = c("connecting…", dim=True)
        lines.append("  %s %d  %-20s  %-9s  %-9s  %s" % (dot, k, piece, got, spd, state))
    lines.append(c(" " + "─" * w, dim=True))
    done = sim.done_bytes()
    pct = 100.0 * done / SIZE
    foot = " %5.1f%%   %s / %s   %s/s   ETA %s   %d/%d connections" % (
        pct, fmt_bytes(done), fmt_bytes(SIZE), fmt_bytes(sim.speed()),
        fmt_eta(sim.eta()), sim.active_count(), MAX_CONNS)
    if sim.limit:
        foot += c("  (host limit %d)" % sim.limit, dim=True)
    lines.append(c(foot, bold=True))
    return lines


# ---- style 2: one line ------------------------------------------------------
def render_line(sim, width):
    done = sim.done_bytes()
    frac = done / SIZE
    barw = 24
    full = int(frac * barw)
    bar = "█" * full + ("▌" if frac * barw - full >= 0.5 else "")
    bar = bar.ljust(barw, "░")
    tail = " %3.0f%%  %s/%s  %s/s  ETA %s  ⇉%d" % (
        100 * frac, fmt_bytes(done), fmt_bytes(SIZE), fmt_bytes(sim.speed()),
        fmt_eta(sim.eta()), sim.active_count())
    name = NAME if len(NAME) <= 28 else NAME[:27] + "…"
    return c(BRAND, 39, bold=True) + " ▸ " + name + "  " + c(bar, 39) + tail


def run(style, seed):
    rng = random.Random(seed)
    width = min(shutil.get_terminal_size((100, 30)).columns, 100)
    out = sys.stdout
    log = []

    if style == "agent":
        def emit(event, **kw):
            if event in ("split",):
                return              # agents don't need every split; keep output tiny
            print(json.dumps(dict(event=event, t=round(sim.t, 1), **kw)), flush=True)
    else:
        def emit(event, **kw):
            if event == "throttle":
                log.append("  ! connection %d: server said 429 Too Many Requests, backing off %.1fs"
                           % (kw["conn"], kw["retry_in"]))
            elif event == "host_limit":
                log.append("  ! server allows %d connections at once, continuing with %d"
                           % (kw["connections"], kw["connections"]))

    sim = Sim(rng, emit)
    t0 = time.time()

    if style == "agent":
        print(json.dumps({"event": "start", "file": NAME, "size": SIZE, "resumable": True,
                          "connections": MAX_CONNS, "path": "~/Downloads/" + NAME, "demo": True}), flush=True)
        next_report = 3.0
        while not sim.finished():
            sim.step()
            if sim.t >= next_report:
                next_report += 3.0
                print(json.dumps({"event": "progress", "t": round(sim.t, 1),
                                  "pct": round(100.0 * sim.done_bytes() / SIZE, 1),
                                  "speed": int(sim.speed()), "eta": int(sim.eta() or 0),
                                  "connections": sim.active_count()}), flush=True)
            time.sleep(TICK)
        print(json.dumps({"event": "done", "path": "~/Downloads/" + NAME, "size": SIZE,
                          "seconds": round(sim.t, 1), "avg_speed": int(SIZE / sim.t),
                          "segments": len(sim.segs), "sha256": "demo-not-computed"}), flush=True)
        return

    if USE_COLOR:
        out.write("\x1b[?25l")                     # hide cursor while redrawing
    try:
        if style == "map":
            drawn = 0
            printed_log = 0
            while True:
                frame = render_map(sim, width)
                if drawn:
                    out.write("\x1b[%dA" % drawn)  # move back up over the last frame
                new_log = log[printed_log:]
                printed_log = len(log)
                for line in new_log + frame:
                    out.write("\r\x1b[K" + line + "\n")
                out.flush()
                drawn = len(frame)
                if sim.finished():
                    break
                time.sleep(TICK)
                sim.step()
        else:
            printed_log = 0
            while True:
                for line in log[printed_log:]:
                    out.write("\r\x1b[K" + c(line, 203) + "\n")
                printed_log = len(log)
                out.write("\r\x1b[K" + render_line(sim, width))
                out.flush()
                if sim.finished():
                    break
                time.sleep(TICK)
                sim.step()
            out.write("\n")
        took = time.time() - t0
        out.write(c("✓", 70, bold=True) + " Saved ~/Downloads/%s  %s in %.1fs (%s/s avg) · %d segments · %s\n" % (
            NAME, fmt_bytes(SIZE), took, fmt_bytes(SIZE / took), len(sim.segs),
            c("demo: nothing was downloaded", dim=True)))
    except KeyboardInterrupt:
        out.write("\n" + c("⏸ Paused. Run the same command again to resume.", 178) + "\n")
    finally:
        if USE_COLOR:
            out.write("\x1b[?25h")
        out.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="IDM CLI terminal mockup (simulated, downloads nothing)")
    ap.add_argument("--style", choices=["map", "line", "agent"], default="map")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if args.style != "agent" and not sys.stdout.isatty():
        print("(not a terminal, so showing the agent style)", file=sys.stderr)
        args.style = "agent"
    run(args.style, args.seed)
