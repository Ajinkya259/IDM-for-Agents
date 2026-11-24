"""
IDM for Agent: a download manager made for AI agents (and people).

    idm get URL            download it, and wait until it is done
    idm start URL          start downloading in the background, and reply at once
    idm wait ID            wait for a download to finish
    idm status ID          how a download is going, right now
    idm watch [ID]         show it as halo, the face (no ID: every download, as it happens)
    idm list               every download
    idm pause ID | resume ID | cancel ID

People see halo, the face, while get, wait and watch run (in a terminal big enough for it).

When an agent runs it (output is not a terminal, or --json), every command prints exactly one
line of JSON: `ok`, the download's `state`, and on failure an `error` with a fixed `code`,
whether it is `retryable`, and a `hint`.  `next` is the exact command to run next, if any.
An agent's `get` and `wait` reply within 90 seconds, so its tool call is never cut off.

Exit codes: 0 fine, 1 failed, 2 bad command, 3 failed but worth retrying (run `next`).
"""
import argparse
import json
import os
import shlex
import sys
import time
import urllib.parse

from . import __version__, halo, jobs
from .errors import RETRYABLE, IDMError, from_disk

AGENT_WAIT = 90     # seconds an agent's get/wait may take (tool calls are cut off at ~120 s)
MAX_CONNECTIONS = 16
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
    with jobs.locked(jobs.HOME / "jobs.lock"):         # two resumes at once start one worker
        if job.status().get("state") not in (*jobs.ACTIVE, "done"):
            jobs.start(job)
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
        job = None if args.again else jobs.find(url, folder, args.output)
        job = job or jobs.new(url, folder, args.output)
        st = job.status()
        if st.get("state") == "done" and os.path.exists(st.get("file", "")):
            return job, True
        if st.get("state") not in jobs.ACTIVE:
            job.headers, job.cookies, job.allow_page, job.connections = choices   # latest wins
            job.save()
            jobs.start(job)
    return job, False


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
    """--header, --cookies, --allow-page and --connections, checked."""
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
    return headers, cookies, args.allow_page, args.connections


def follow(job, limit, out, pause_on_ctrl_c=False):
    """Watch a job until it ends (done, failed, paused, cancelled) or `limit` seconds pass,
    showing people a progress line.  Ctrl+C pauses the download (get) or stops watching (wait)."""
    deadline = None if limit is None else time.time() + limit
    try:
        while True:
            st = job.status()
            out.progress(st)
            if st.get("state") in jobs.FINAL or (deadline is not None and time.time() >= deadline):
                return st
            time.sleep(0.1)
    except KeyboardInterrupt:
        out.stopped = True
        if pause_on_ctrl_c:
            jobs.stop(job)
        return job.status()


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
    """Follow every download as it happens, one after another, until Ctrl+C."""
    print("  watching for downloads (Ctrl+C to stop)")
    try:
        while True:
            going = [j for j in jobs.every() if j.status().get("state") in jobs.ACTIVE]
            if not going:
                time.sleep(0.2)
                continue
            out.new_face()
            st = follow(going[-1], None, out)
            out.result(going[-1], st)
            if out.stopped:                              # Ctrl+C while following it
                return 0
    except KeyboardInterrupt:
        return 0


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
    active = st.get("state") in jobs.ACTIVE
    known = {"file": st.get("file"), "bytes": st.get("bytes"), "size": st.get("size"),
             "percent": percent(st), "speed": st.get("speed") if active else None,
             "eta": st.get("eta") if active else None, "note": st.get("note")}
    return {k: v for k, v in known.items() if v is not None}


def next_step(job, st):
    state, code = st.get("state"), (st.get("error") or {}).get("code")
    if state in jobs.ACTIVE:
        return f"idm wait {job.id}"
    if state in ("paused", "new") or (state == "failed" and code in RETRYABLE):
        return f"idm resume {job.id}"
    if code == "AUTH_REQUIRED":
        return get_command(job, "--cookies <cookies.txt>")
    links = (st.get("error") or {}).get("links", [])
    if code == "NOT_A_FILE" and len(links) == 1:       # the page links to one file: that one
        return f"idm get {shlex.quote(links[0])} -d {shlex.quote(job.folder)}"
    return None


def get_command(job, extra):
    """`idm get` for this job's link and destination, plus `extra`."""
    name = f" -o {shlex.quote(job.name)}" if job.name else ""
    return f"idm get {shlex.quote(job.url)} -d {shlex.quote(job.folder)}{name} {extra}"


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
        self.stopped = False                               # the person pressed Ctrl+C

    def wait_limit(self, asked):
        """How long get/wait may watch: as asked; else forever for a person, 90 s for an agent."""
        return asked if asked is not None else (AGENT_WAIT if self.json else None)

    def result(self, job, st, already=False):
        if self.face_rows:
            self.last_face(st)
        reply = summary(job, st, already)
        self.emit(reply, lambda: human_result(reply))
        return exit_code(reply)

    def error(self, e):
        reply = {"ok": False, "error": e.to_dict()}
        self.emit(reply, lambda: [f"✗ {e}", f"  {reply['error']['hint']}"])
        return exit_code(reply)

    def listing(self, all_jobs, shown=LIST_SHOWS):
        rows = [brief(j, j.status()) for j in all_jobs[-shown:]]       # the latest, oldest first
        older = {"older": len(all_jobs) - shown} if len(all_jobs) > shown else {}
        self.emit({"ok": True, "jobs": rows, **older},
                  lambda: [list_line(r) for r in rows] or ["no downloads yet"])
        return 0

    def progress(self, st):
        if self.face:
            self.draw_face(st)
        elif not self.json:
            sys.stdout.write("\r  " + progress_line(st) + "\x1b[K")
            sys.stdout.flush()
            self.line_drawn = True

    def draw_face(self, st, settle=False):
        """Draw halo over the one drawn last."""
        rows = self.face.draw(st, time.time(), settle).ansi()
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


def human_result(r):
    state, step = r["state"], r.get("next")
    if state == "done":
        took = "" if r.get("already") else f" in {r['seconds']}s"
        verb = "already downloaded" if r.get("already") else "saved"
        return [f"✓ {verb} {r['file']}  ({mb(r['bytes'])}{took})"]
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


def progress_line(st):
    if st.get("trouble"):
        t = st["trouble"]
        return f"{t['message']}; trying again in {t['retry_in']}s"
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
        c.add_argument("--connections", type=int, default=8, metavar="N",
                       help=f"download in up to N pieces at once (1-{MAX_CONNECTIONS}, default 8)")
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
        command(name, run, what).add_argument("id")
    c = command("watch", cmd_watch, "show a download as halo, the face (no ID: every download)")
    c.add_argument("job", nargs="?", metavar="ID|LINK")
    c.add_argument("--max", type=float, metavar="SECONDS", help="stop watching after this long")
    c.add_argument("--cells", action="store_true", help=argparse.SUPPRESS)   # for the mod
    command("list", cmd_list, "every download")
    return p
