"""
IDM for Agents: a download manager made for AI agents (and people).

    idm get URL            download it, and wait until it is done
    idm start URL          start downloading in the background, and reply at once
                             (both take --at TIME / --in WAIT, --after ID and --limit SPEED)
    idm wait ID            wait for a download to finish
    idm status ID          how a download is going, right now
    idm watch [ID]         show it as halo, the face (no ID: every download, as it happens)
    idm list               every download
    idm limit ID SPEED     change a download's speed limit (0: none), even while it runs
    idm pause ID | resume ID [--now] | cancel ID

People see halo, the face, while get, wait and watch run (in a terminal big enough for it).

When an agent runs it (output is not a terminal, or --json), every command prints exactly one
line of JSON: `ok`, the download's `state`, and on failure an `error` with a fixed `code`,
whether it is `retryable`, and a `hint`.  `next` is the exact command to run next, if any.
An agent's `get` and `wait` reply within 90 seconds, so its tool call is never cut off.

Exit codes: 0 fine, 1 failed, 2 bad command, 3 failed but worth retrying (run `next`).
"""
import argparse
import contextlib
import json
import os
import select
import shlex
import sys
import termios
import time
import tty
import urllib.parse

from . import __version__, halo, jobs, video, when
from .errors import RETRYABLE, IDMError, from_disk

AGENT_WAIT = 90     # seconds an agent's get/wait may take (tool calls are cut off at ~120 s)
MAX_CONNECTIONS = 16
DEFAULT_CONNECTIONS = 8
LIST_SHOWS = 20     # `idm list` shows the latest 20 downloads


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_work"]:                          # the background worker, started by jobs.start
        jobs.work(jobs.load(argv[1]))
        return 0
    out = Output(json_mode="--json" in argv or not sys.stdout.isatty())
    args = parser(out).parse_args(argv)
    try:
        return args.run(args, out)
    except IDMError as e:
        return out.error(e)
    except OSError as e:                               # IDM_HOME can't be written, say
        return out.error(from_disk(e))


# ---- the commands ------------------------------------------------------------------------------
def cmd_get(args, out):
    job, already = find_or_start(args)
    st = follow(job, out.wait_limit(args.max), out, pause_on_ctrl_c=True)
    return out.result(job, st, already)


def cmd_start(args, out):
    job, already = find_or_start(args)
    return out.result(job, job.status(), already)


def cmd_wait(args, out):
    job = jobs.load(args.id)
    return out.result(job, follow(job, out.wait_limit(args.max), out))


def cmd_watch(args, out):
    if args.cells:                                     # for the Claude Code mod
        return stream_cells(find_job(args.job))
    if args.job or out.json:
        job = find_job(args.job)
        return out.result(job, follow(job, out.wait_limit(args.max), out))
    return watch_all(out)


def cmd_status(args, out):
    job = jobs.load(args.id)
    return out.result(job, job.status())


def cmd_list(args, out):
    return out.listing(jobs.every())


def cmd_pause(args, out):
    job = jobs.load(args.id)
    jobs.stop(job)
    return out.result(job, job.status())


def cmd_resume(args, out):
    job = jobs.load(args.id)
    resume(job, now=args.now)
    return out.result(job, job.status())


def resume(job, now=False):
    """Carry on with a paused or failed job (now: without waiting for its time or the job it
    follows)."""
    with jobs.locked(jobs.HOME / "jobs.lock"):         # two resumes at once start one worker
        if now and (job.starts_at or job.after):
            job.starts_at = job.after = None           # a waiting worker sees it within a second
            job.save()
            if job.status().get("state") == "scheduled":
                job.set_status(state="queued", starts_at=None)
        if job.status().get("state") not in (*jobs.ACTIVE, "done"):
            jobs.start(job)


def cmd_limit(args, out):
    job, limit = jobs.load(args.id), checked(when.rate, args.speed)
    with jobs.locked(jobs.HOME / "jobs.lock"):
        job = jobs.load(args.id)
        job.limit = limit                              # a running worker reads it every second
        job.save()
        job.set_status(limit=limit)
    return out.result(job, job.status())


def cmd_cancel(args, out):
    job = jobs.load(args.id)
    jobs.cancel(job)
    return out.result(job, job.status())


# ---- what the commands share -------------------------------------------------------------------
def find_or_start(args):
    """The job for this link: the earlier one if there is one (so running the same command
    twice, even at the same moment, never downloads twice), else a new one.  Starts it unless
    it is running or done.  Returns (job, True if the file was already downloaded)."""
    url, choices = link(args.url), options(args)
    folder = os.path.abspath(os.path.expanduser(args.dir))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise from_disk(e) from None
    with jobs.locked(jobs.HOME / "jobs.lock"):
        job = jobs.find(url, folder, args.output)
        if args.again and job and job.status().get("state") not in jobs.ACTIVE:
            job = None                                   # (--again while it runs: that run)
        job = job or jobs.new(url, folder, args.output)
        st = job.status()
        if st.get("state") == "done" and os.path.exists(st.get("file", "")):
            return job, True
        if st.get("state") not in jobs.ACTIVE:
            if waits_for(choices["after"], job.id):
                raise IDMError("BAD_INPUT", f"--after {choices['after']} would make download "
                                            f"{job.id} wait for itself")
            for name, value in choices.items():          # the latest command's options win
                setattr(job, name, value)
            job.save()
            jobs.start(job)
    return job, False


def waits_for(after, job_id):
    """Does following `after` (and what it follows, and so on) lead back to job_id?"""
    seen = set()
    while after and after not in seen:
        if after == job_id:
            return True
        seen.add(after)
        try:
            after = jobs.load(after).after
        except IDMError:                                 # (that job's folder is gone)
            return False
    return False


def link(url):
    """The link, checked, with any space or non-ASCII letter in it encoded the way a browser
    does (so `café menu.pdf` works)."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IDMError("BAD_URL", f"not a web link: {url!r}")
    try:
        host = parts.netloc.encode("idna").decode()
    except UnicodeError:
        raise IDMError("BAD_URL", f"not a valid server name: {parts.netloc!r}") from None
    keep = "/%:@!$&'()*+,;=~-._"                           # what may stay as it is
    return urllib.parse.urlunsplit((parts.scheme, host, urllib.parse.quote(parts.path, keep),
                                    urllib.parse.quote(parts.query, keep + "?"), parts.fragment))


def options(args):
    """The options of get/start, checked: what the job's fields become."""
    headers = {}
    for h in args.header:
        name, sep, value = h.partition(":")
        if not sep or not name.strip():
            raise IDMError("BAD_INPUT", f"--header must look like 'Name: value', not {h!r}")
        headers[name.strip()] = value.strip()
    cookies = os.path.abspath(os.path.expanduser(args.cookies)) if args.cookies else None
    if cookies and not os.path.isfile(cookies):
        raise IDMError("BAD_INPUT", f"no cookies file at {cookies}")
    if not 1 <= args.connections <= MAX_CONNECTIONS:
        raise IDMError("BAD_INPUT", f"--connections must be 1 to {MAX_CONNECTIONS}, "
                                    f"not {args.connections}")
    after = jobs.load(args.after).id if args.after else None   # (NO_SUCH_JOB if there's none)
    return {"headers": headers, "cookies": cookies, "allow_page": args.allow_page,
            "connections": args.connections, "after": after, "audio": args.audio,
            "video": args.video or args.audio or video.is_video(args.url),
            "starts_at": checked(when.start_time, args.at, args.in_),
            "limit": checked(when.rate, args.limit) if args.limit else None}


def checked(read, *values):
    """An option read by `when`, its complaint made a BAD_INPUT error."""
    try:
        return read(*values)
    except ValueError as e:
        raise IDMError("BAD_INPUT", str(e)) from None


def follow(job, limit, out, pause_on_ctrl_c=False):
    """Watch a job until it ends (done, failed, paused, cancelled) or `limit` seconds pass,
    showing people a progress line.  A scheduled job is not waited for: that could be hours.

    A person watching has keys (Keys): space pauses and resumes, q stops watching.  A job
    they paused stays on screen, to resume.  Ctrl+C pauses the download (get) or stops
    watching (wait)."""
    deadline = None if limit is None else time.time() + limit
    with out.listening() as keys:
        try:
            while True:
                st = job.status()
                out.progress(st)
                key = keys.pressed()
                if key == "q":
                    out.stopped = True
                    return st
                if key:
                    press(job, st, key)
                    continue
                over = deadline is not None and time.time() >= deadline
                ends = ("done", "failed", "cancelled", "scheduled") if keys.on else \
                       (*jobs.FINAL, "scheduled")
                if st.get("state") in ends or over:
                    return st
                time.sleep(0.1)
        except KeyboardInterrupt:
            out.stopped = True
            if pause_on_ctrl_c:
                jobs.stop(job)
            return job.status()


def press(job, st, key):
    """A person's key while they watch: space pauses or resumes, p pauses, r resumes."""
    state = st.get("state")
    if key in " p" and state in jobs.ACTIVE:
        jobs.stop(job)
    elif key in " r" and state in ("paused", "failed", "new"):
        resume(job)


class Keys:
    """Single key presses from a person's terminal, read without waiting (nothing at all when
    an agent runs idm, or when the input isn't a terminal)."""

    def __init__(self, on):
        self.on, self.saved = on and sys.stdin.isatty(), None

    def __enter__(self):
        if self.on:
            self.saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin)                   # keys as they are pressed; Ctrl+C still works
        return self

    def __exit__(self, *exc):
        if self.saved:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)

    def pressed(self):
        """The key pressed since last time (lowercase), or None."""
        if not self.on or not select.select([sys.stdin], [], [], 0)[0]:
            return None
        return os.read(sys.stdin.fileno(), 1).decode(errors="ignore").lower() or None


def find_job(target):
    """The download to watch: by its id; for a link, its download that is running or ends
    from now on (waiting a moment for it: `idm get` and the face's watcher start together);
    or else the latest of all."""
    if target and "://" not in target:
        return jobs.load(target)
    url, since, deadline = target and link(target), time.time(), time.time() + (5 if target else 0)
    while True:
        found = [j for j in jobs.every() if url in (None, j.url)]
        going = [j for j in found if j.status().get("state") in jobs.ACTIVE
                 or j.status().get("finished", 0) > since]
        if going or time.time() >= deadline:
            break
        time.sleep(0.1)
    if not (going if url else found):                  # (a link's earlier, finished download
        raise IDMError("NO_SUCH_JOB", f"no download of {target} is under way" if target  # isn't
                       else "no downloads yet")                                          # news)
    return (going or found)[-1]


def watch_all(out):
    """Follow every download as it happens, one after another, until q or Ctrl+C."""
    print("  watching for downloads (q or Ctrl+C to stop)")
    try:
        while True:
            job = next_download(out)
            if not job:                                  # q, while nothing was downloading
                return 0
            out.new_face()
            out.result(job, follow(job, None, out))
            if out.stopped:                              # q or Ctrl+C while following it
                return 0
    except KeyboardInterrupt:
        return 0


def next_download(out):
    """The download to show: the latest one downloading, else the latest waiting its turn.
    Waits for one; None if the person presses q first."""
    with out.listening() as keys:
        while keys.pressed() != "q":
            states = [(j, j.status().get("state")) for j in jobs.every()]
            going = [j for j, state in states if state == "downloading"] or \
                    [j for j, state in states if state == "queued"]
            if going:
                return going[-1]
            time.sleep(0.2)
    return None


def stream_cells(job):
    """halo as terminal cells, one JSON line a frame (10 a second) until the download ends:
    what the Claude Code mod shows under the agent's command."""
    face = halo.Halo()
    try:
        while True:
            st = job.status()
            final = st.get("state") in jobs.FINAL
            pic = face.draw(st, time.time(), settle=final)
            label = " · ".join(text for text, _ in halo.caption(st) if text)
            print(json.dumps({"state": st.get("state"), "percent": percent(st), "label": label,
                              "columns": pic.columns, "rows": pic.rows, "cells": pic.packed()}),
                  flush=True)
            if final:
                return 0
            time.sleep(0.1)
    except BrokenPipeError:                              # the mod stopped reading
        return 0


def summary(job, st, already=False):
    """The reply about a job: only what an agent needs, plus `next`, the command to run next."""
    state = st.get("state")
    reply = {"ok": state != "failed", "job": job.id, "state": state}
    if already:
        reply["already"] = True
    reply.update(facts(st))
    if state in jobs.ACTIVE and st.get("trouble"):
        reply["trouble"] = st["trouble"]
    if state == "failed":
        reply["error"] = st["error"]
    step = next_step(job, st)
    if step:
        reply["next"] = step
    return reply


def brief(job, st):
    """A short row for `idm list`: enough to pick a job, without every detail."""
    row = {"job": job.id, "state": st.get("state"), "url": job.url}
    row.update({k: v for k, v in facts(st).items() if k in ("file", "percent")})
    if st.get("error"):
        row["error"] = st["error"]["code"]
    return row


def facts(st):
    """Where the file goes and how far along it is (only the numbers that are known)."""
    if st.get("state") == "done":
        return {"file": st.get("file"), "bytes": st.get("bytes"),
                "seconds": round(st.get("finished", 0) - st.get("started", 0), 1)}
    active, starts = st.get("state") in jobs.ACTIVE, st.get("starts_at")
    known = {"file": st.get("file"), "bytes": st.get("bytes"), "size": st.get("size"),
             "percent": percent(st), "speed": st.get("speed") if active else None,
             "eta": st.get("eta") if active else None, "stalled": st.get("stalled") if active else None,
             "starts_at": when.iso(starts) if starts else None,
             "starts_in": when.how_long(starts - time.time()) if starts else None,
             "waiting_for": st.get("waiting_for"), "limit": st.get("limit"), "note": st.get("note")}
    return {k: v for k, v in known.items() if v is not None}


def next_step(job, st):
    state, code = st.get("state"), (st.get("error") or {}).get("code")
    if state == "scheduled":
        return None                                    # nothing to do till then: tell the person
    if state in jobs.ACTIVE:
        return f"idm wait {job.id}"
    if state in ("paused", "new") or (state == "failed" and code in RETRYABLE):
        return f"idm resume {job.id}"
    if code == "NEEDS_TOOL":
        return f"idm resume {job.id}"                  # once yt-dlp is installed
    if code == "AUTH_REQUIRED":                         # again, with the user's login
        again = [*(["-o", job.name] if job.name else []), *kept(job, cookies=False), *kind(job)]
        return get_command(job.url, job.folder, again) + " --cookies <cookies.txt>"
    links = (st.get("error") or {}).get("links", [])
    if code == "NOT_A_FILE" and len(links) == 1:       # the page links to one file: that one
        return get_command(links[0], job.folder, kept(job, headers=False))
    return None


def get_command(url, folder, options):
    """`idm get` for this link, into this folder, with these options."""
    return " ".join(shlex.quote(word) for word in ["idm", "get", url, "-d", folder, *options])


def kept(job, headers=True, cookies=True):
    """A job's options that its next command must keep (running it sets them all again): its
    login, speed limit and connections.  (Not its headers for another link: an Authorization
    header is for the job's own server.)"""
    words = []
    if headers:
        words += [w for name, value in job.headers.items() for w in ("--header", f"{name}: {value}")]
    words += ["--cookies", job.cookies] if job.cookies and cookies else []
    words += ["--limit", str(job.limit)] if job.limit else []
    words += ["--connections", str(job.connections)] if job.connections != DEFAULT_CONNECTIONS else []
    return words


def kind(job):
    """What the job asked for beyond a plain file: the sound alone, a video, the page itself."""
    if job.audio:
        words = ["--audio"]
    else:
        words = ["--video"] if job.video and not video.is_video(job.url) else []
    return words + (["--allow-page"] if job.allow_page else [])


def percent(st):
    done, size = st.get("bytes"), st.get("size")
    return int(100 * done / size) if done is not None and size else None


def exit_code(reply):
    if reply["ok"]:
        return 0
    if reply["error"]["code"] in ("BAD_URL", "BAD_INPUT"):
        return 2                                       # the command itself was wrong
    return 3 if reply["error"]["retryable"] else 1


# ---- what you see ------------------------------------------------------------------------------
class Output:
    """How replies look: one JSON line for an agent, a few friendly lines for a person."""

    def __init__(self, json_mode):
        self.json, self.line_drawn = json_mode, False
        self.face = None if json_mode or not halo.fits() else halo.Halo()
        self.face_rows = 0                                 # how many rows of it are on screen
        self.stopped = False                               # the person pressed Ctrl+C (or q)
        self.keys = False                                  # a person's keys work (show them)

    def wait_limit(self, asked):
        """How long get/wait may watch: as asked; else forever for a person, 90 s for an agent."""
        return asked if asked is not None else (AGENT_WAIT if self.json else None)

    def result(self, job, st, already=False):
        if self.face_rows:
            self.last_face(st)
        reply = summary(job, st, already)
        self.emit(reply, lambda: human_result(reply, st))
        return exit_code(reply)

    def error(self, e):
        reply = {"ok": False, "error": e.to_dict()}
        self.emit(reply, lambda: [f"✗ {e}", f"  {reply['error']['hint']}"])
        return exit_code(reply)

    def listing(self, all_jobs):
        rows = [brief(j, j.status()) for j in all_jobs[-LIST_SHOWS:]]  # the latest, oldest first
        older = {"older": len(all_jobs) - LIST_SHOWS} if len(all_jobs) > LIST_SHOWS else {}
        self.emit({"ok": True, "jobs": rows, **older},
                  lambda: [list_line(r) for r in rows] or ["no downloads yet"])
        return 0

    @contextlib.contextmanager
    def listening(self):
        """A person's keys, while a download shows."""
        with Keys(not self.json) as keys:
            self.keys = keys.on
            try:
                yield keys
            finally:
                self.keys = False

    def progress(self, st):
        if self.face:
            self.draw_face(st)
        elif not self.json:
            hint = f"   ({key_hint(st)})" if self.keys and key_hint(st) else ""
            sys.stdout.write("\r  " + progress_line(st) + hint + "\x1b[K")
            sys.stdout.flush()
            self.line_drawn = True

    def draw_face(self, st, settle=False):
        """Draw halo over the one drawn last (with the keys under it, while they work)."""
        rows = self.face.draw(st, time.time(), settle).ansi()
        if self.keys or self.face_rows > len(rows):        # (a blank row clears the keys after)
            hint = "" if settle or not self.keys else key_hint(st)
            rows.append(f"\x1b[2m{hint.center(halo.COLUMNS)}\x1b[0m")
        up = f"\x1b[{self.face_rows}F" if self.face_rows else ""
        sys.stdout.write(up + "".join(row + "\x1b[K\n" for row in rows))
        sys.stdout.flush()
        self.face_rows = len(rows)

    def last_face(self, st):
        """How it ended: the last dots fly home, then the face stays as it is.  Ctrl+C skips
        to the end."""
        try:
            end = time.time() + halo.FLY
            while time.time() < end:
                self.draw_face(st)
                time.sleep(0.05)
        except KeyboardInterrupt:
            self.stopped = True
        self.draw_face(st, settle=True)

    def new_face(self):
        """The next download gets a face of its own, under the last one."""
        if self.face:
            self.face, self.face_rows = halo.Halo(), 0

    def emit(self, reply, human_lines):
        if self.json:
            print(json.dumps(reply))
            return
        if self.line_drawn:
            sys.stdout.write("\r\x1b[K")
        for line in human_lines():
            print("  " + line)


def human_result(r, st):
    state, step = r["state"], r.get("next")
    if state == "done":
        took = "" if r.get("already") else f" in {r['seconds']}s"
        verb = "already downloaded" if r.get("already") else "saved"
        return [f"✓ {verb} {r['file']}  ({mb(r['bytes'])}{took})"]
    if state == "scheduled":
        return [f"scheduled: starts at {when.clock(st['starts_at'])} (in {r['starts_in']}). "
                f"Check on it: idm status {r['job']}"]
    if state == "queued" and r.get("waiting_for"):
        return [f"waiting for {r['waiting_for']}. Check on it: {step}"]
    if state in jobs.ACTIVE:
        return [f"downloading in the background ({r.get('percent', 0)}%). Check on it: {step}"]
    if state == "paused":
        return [f"paused at {r.get('percent', 0)}%. Carry on with: {step}"]
    if state == "cancelled":
        return ["cancelled"]
    if "error" not in r:
        return [f"{state}" + (f". Next: {step}" if step else "")]
    error = r["error"]
    return [f"✗ {error['message']}", f"  {error['hint']}"] + ([f"  next: {step}"] if step else [])


def key_hint(st):
    """The keys that work now, in words."""
    state = st.get("state")
    if state in jobs.ACTIVE:
        return "space pause · q stop watching (it carries on)"
    if state == "paused":
        return "space resume · q quit"
    return ""


def progress_line(st):
    if st.get("state") == "scheduled" and st.get("starts_at"):
        starts = st["starts_at"]
        return f"scheduled: starts at {when.clock(starts)} (in {when.how_long(starts - time.time())})"
    if st.get("state") == "queued" and st.get("waiting_for"):
        return f"waiting for {st['waiting_for']}"
    if st.get("trouble"):
        t, silent = st["trouble"], st.get("stalled")
        quiet = f"nothing for {when.how_long(silent)} · " if silent else ""
        return f"{quiet}{t['message']}; trying again in {t['retry_in']}s"
    done, size, speed = st.get("bytes") or 0, st.get("size"), st.get("speed") or 0
    if not size:
        return f"{mb(done)}  ·  {mb(speed)}/s" if done else "starting…"
    left = f"{st['eta']}s left" if st.get("eta") is not None else "…"
    n = st.get("connections") or 1
    over = f"  ·  {n} connections" if n > 1 else ""
    return f"{percent(st)}%  {mb(done)} of {mb(size)}  ·  {mb(speed)}/s{over}  ·  {left}"


def list_line(r):
    where = os.path.basename(r["file"]) if r.get("file") else r["url"]
    progress = f"{r['percent']}%" if "percent" in r else ""
    return f"{r['job']}  {r['state']:<10} {progress:>4}  {where}"


def mb(n):
    return f"{(n or 0) / 1e6:.1f} MB"


# ---- the command line ----------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    """argparse, except that a mistake in the command is reported as JSON to an agent too."""
    json_mode = False                                  # set by parser(), before parsing

    def error(self, message):
        if Parser.json_mode:
            print(json.dumps({"ok": False, "error": IDMError("BAD_INPUT", message).to_dict()}))
            sys.exit(2)
        super().error(message)


def parser(out):
    Parser.json_mode = out.json
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", help="reply with one line of JSON")
    p = Parser(prog="idm", description="A download manager made for AI agents (and people).",
               epilog="Exit codes: 0 fine, 1 failed, 2 bad command, 3 worth retrying (see `next`).")
    p.add_argument("--version", action="version", version=f"idm {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, run, what):
        c = sub.add_parser(name, parents=[common], help=what, description=what)
        c.set_defaults(run=run)
        return c

    for name, run, what in (("get", cmd_get, "download a file and wait until it is done"),
                            ("start", cmd_start, "start downloading in the background")):
        c = command(name, run, what)
        c.add_argument("url")
        c.add_argument("-o", "--output", metavar="NAME", help="file name (default: the server's)")
        c.add_argument("-d", "--dir", default=".", metavar="FOLDER", help="where to save it")
        c.add_argument("--header", action="append", default=[], metavar="'NAME: VALUE'",
                       help="send this request header too (repeatable)")
        c.add_argument("--cookies", metavar="FILE", help="browser cookies (a cookies.txt file)")
        c.add_argument("--allow-page", action="store_true", help="save a web page, not refuse it")
        c.add_argument("--again", action="store_true", help="download again even if already done")
        c.add_argument("--connections", type=int, default=DEFAULT_CONNECTIONS, metavar="N",
                       help=f"download in up to N pieces at once (1-{MAX_CONNECTIONS}, "
                            f"default {DEFAULT_CONNECTIONS})")
        c.add_argument("--at", metavar="TIME", help="start at this time: 02:00, 2am, '2026-11-25 02:00'")
        c.add_argument("--in", dest="in_", metavar="WAIT", help="start after this long: 30m, 2h, 1h30m")
        c.add_argument("--after", metavar="ID", help="start when this download has ended")
        c.add_argument("--limit", metavar="SPEED", help="at most this fast: 500K, 2M (bytes a second)")
        c.add_argument("--audio", action="store_true", help="a video's sound alone (YouTube...)")
        c.add_argument("--video", action="store_true",
                       help="a video page idm doesn't know as one: let yt-dlp find the video")
        if name == "get":
            c.add_argument("--max", type=float, metavar="SECONDS",
                           help="reply after this long even if not done (agents: 90)")

    c = command("wait", cmd_wait, "wait for a download to finish")
    c.add_argument("id")
    c.add_argument("--max", type=float, metavar="SECONDS", help="reply after this long (agents: 90)")
    for name, run, what in (("status", cmd_status, "how a download is going, right now"),
                            ("pause", cmd_pause, "pause a download (keeps what it has)"),
                            ("resume", cmd_resume, "carry on with a paused or failed download"),
                            ("cancel", cmd_cancel, "stop a download and delete what it had")):
        c = command(name, run, what)
        c.add_argument("id")
        if name == "resume":
            c.add_argument("--now", action="store_true",
                           help="start now, without waiting for its time or the download it follows")
    c = command("limit", cmd_limit, "change a download's speed limit, even while it runs")
    c.add_argument("id")
    c.add_argument("speed", help="500K, 2M... or 0 for no limit")
    c = command("watch", cmd_watch, "show a download as halo, the face (no ID: every download)")
    c.add_argument("job", nargs="?", metavar="ID|LINK")
    c.add_argument("--max", type=float, metavar="SECONDS", help="stop watching after this long")
    c.add_argument("--cells", action="store_true", help=argparse.SUPPRESS)   # for the mod
    command("list", cmd_list, "every download")
    return p
