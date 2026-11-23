#!/usr/bin/env python3
"""
Phase 1: download one file over ONE connection, done properly.

    python3 download.py URL                  # saves into the current folder
    python3 download.py URL -o clip.mp4      # choose the file name
    python3 download.py URL -d ~/Downloads   # choose the folder

  * Bytes go into NAME.part, which is renamed to NAME only when every byte is there.
  * Ctrl+C pauses.  Run the same command again and it carries on from where it stopped.
  * A dropped connection is retried, carrying on from the last byte saved.
  * A busy server (429 / 503) is given as long as it asks for.
  * A file that changed on the server since last time is noticed, and downloaded afresh.

Only the Python standard library.  Python 3.9+.
"""
import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

CHUNK = 64 * 1024                                   # read and write 64 KB at a time
TIMEOUT = 30                                        # silent for 30 s = the connection is dead
TRIES = 8                                           # failures in a row before giving up
USER_AGENT = "Mozilla/5.0 (compatible; tako/0.1)"   # some servers refuse Python's default one
LIVE = sys.stdout.isatty()                          # draw a live progress line?


# ---- the whole job, step by step ----------------------------------------------------------
def download(url, folder, name=None):
    """Download `url` into `folder`.  Returns the exit code: 0 done, 1 failed, 130 paused."""
    started, remote, part = time.time(), None, None
    try:
        remote = with_retries(lambda: probe(url))               # 1. what is this file?
        part = PartFile(folder, name or remote.name)            # 2. where does it go?
        announce(part, remote)
        report_leftovers(part, remote)                          # 3. anything from last time?
        bar = Progress(remote.size)
        with_retries(lambda: fetch_rest(remote, part, bar),     # 4. get the missing bytes
                     progress=part.have)
        saved = part.finish()                                   # 5. NAME.part -> NAME
    except KeyboardInterrupt:
        say(paused_message(part, remote))
        return 130
    except Fatal as e:
        say(str(e))
        return 1
    say(f"✓ saved {saved}  ({mb(os.path.getsize(saved))} in {time.time() - started:.1f}s)")
    return 0


@dataclass
class Remote:
    """What the server told us about the file."""
    url: str
    name: str                     # its file name
    size: Optional[int]           # in bytes; None if the server won't say
    can_resume: bool              # it answered our Range request with 206 (just that part)
    fingerprint: Optional[str]    # ETag or Last-Modified: changes when the file changes


def probe(url):
    """Ask for the file from byte 0 and read only the answer's headers."""
    with request(url, 0) as resp:
        return Remote(url=url, name=pick_name(resp), size=total_size(resp),
                      can_resume=resp.status == 206, fingerprint=fingerprint(resp))


def fetch_rest(remote, part, bar):
    """One try at downloading whatever is still missing from the .part file."""
    offset = part.have() if remote.can_resume else 0
    if remote.size is not None and offset >= remote.size:
        return                                                  # nothing missing
    with request(remote.url, offset) as resp:
        if resp.status == 206 and total_size(resp) != remote.size:
            raise Fatal("the file changed on the server mid-download. Run again to start over.")
        start = offset if resp.status == 206 else 0             # 200 = the whole file, from byte 0
        part.write(resp, start, bar.show)
    if remote.size is not None and part.have() < remote.size:
        raise TryAgain(f"the server stopped sending at {mb(part.have())}")


# ---- talking to the server ----------------------------------------------------------------
def request(url, offset):
    """Ask for the file from byte `offset` to the end ("Range: bytes=offset-").

    The status code of the answer says what we got:
      206 Partial Content   just the part we asked for: this server can resume
      200 OK                it ignored our Range and is sending the whole file
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Range": f"bytes={offset}-",
        "Accept-Encoding": "identity",   # no compression: byte 5,000 must be byte 5,000 of the file
    }
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)


def total_size(resp):
    """The size of the whole file, from 'Content-Range: bytes 0-99/4096' (on a 206) or
    'Content-Length: 4096' (on a 200).  None if the server doesn't say."""
    if resp.status == 206:
        m = re.match(r"bytes \d+-\d+/(\d+)", resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None
    length = resp.headers.get("Content-Length", "")
    return int(length) if length.isdigit() else None


def fingerprint(resp):
    return resp.headers.get("ETag") or resp.headers.get("Last-Modified")


def pick_name(resp):
    """The server's name for the file (Content-Disposition), or else the last part of the link."""
    cd = resp.headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd)   # filename*=UTF-8''a%20b.mp4
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', cd)                 # filename="a b.mp4"
    if encoded:
        name = urllib.parse.unquote(encoded.group(1))
    elif plain:
        name = plain.group(1)
    else:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(resp.geturl()).path))
    name = name.replace("/", "_").replace("\\", "_").strip().lstrip(".")   # no folders, not hidden
    return name or "download"


# ---- when things go wrong -----------------------------------------------------------------
class TryAgain(Exception):
    """A problem that may pass: a dropped connection, a timeout, a busy server."""

    def __init__(self, why, wait=None):
        super().__init__(why)
        self.wait = wait                                        # seconds the server asked for


class Fatal(Exception):
    """A problem that won't pass by waiting: 404, a full disk..."""


NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError,
                  ssl.SSLError, http.client.HTTPException)


def with_retries(attempt, progress=lambda: 0):
    """Run attempt() until it works.  If it fails in a way that may pass, wait (1, 2, 4... s,
    or as long as a busy server asks) and run it again.  Gives up after TRIES failures in a
    row; any progress in between starts the count again."""
    fails = 0
    while True:
        before = progress()
        try:
            with sorted_errors():
                return attempt()
        except TryAgain as e:
            fails = 1 if progress() > before else fails + 1
            if fails > TRIES:
                raise Fatal(f"giving up after {TRIES} tries ({e}). "
                            "Run the same command again to carry on.")
            wait = e.wait or min(30, 2 ** (fails - 1))
            say(f"{e}; trying again in {wait}s")
            time.sleep(wait)


@contextmanager
def sorted_errors():
    """Sort the many ways a download can fail into the two that matter: TryAgain or Fatal."""
    try:
        yield
    except urllib.error.HTTPError as e:                         # the server answered: with an error
        why = f"the server said {e.code} {e.reason}"
        if e.code in (429, 503):
            raise TryAgain(why + " (too busy)", retry_after(e.headers.get("Retry-After")))
        if e.code >= 500:
            raise TryAgain(why)
        raise Fatal(why)                                        # 404, 403...
    except NETWORK_ERRORS as e:                                 # no answer, or the answer stopped
        raise TryAgain(describe(e))
    except OSError as e:                                        # our side: disk full, no permission
        raise Fatal(f"couldn't save the file: {e.strerror or e}")
    except ValueError as e:                                     # not a link urllib understands
        raise Fatal(f"that doesn't look like a download link ({e})")


def retry_after(value):
    """Retry-After is usually a number of seconds.  Anything else: 5 s.  Never more than 60."""
    return min(60, int(value)) if value and value.strip().isdigit() else 5


def describe(e):
    """A short reason for a network error, like 'Connection refused' or 'timed out'."""
    e = getattr(e, "reason", e)                                 # URLError wraps the real error
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


# ---- the file on disk ---------------------------------------------------------------------
class PartFile:
    """The download in progress: NAME.part holds the bytes so far, and NAME.part.json notes
    which link (and which version of the file) they belong to.  Becomes NAME when complete."""

    def __init__(self, folder, name):
        self.final = free_name(os.path.join(folder, name))
        self.path = self.final + ".part"
        self.notes = self.path + ".json"
        self.name = os.path.basename(self.final)

    def have(self):
        """Bytes already on disk."""
        return os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def keep_or_discard(self, remote):
        """Keep bytes from an earlier try only if they belong to this very file; otherwise
        delete them.  Returns why they were deleted, or None."""
        why = self._mismatch(remote) if self.have() else None
        if why:
            os.remove(self.path)
        with open(self.notes, "w") as f:
            json.dump({"url": remote.url, "size": remote.size,
                       "fingerprint": remote.fingerprint}, f)
        return why

    def _mismatch(self, remote):
        try:
            with open(self.notes) as f:
                notes = json.load(f)
        except (OSError, ValueError):
            notes = {}
        if not remote.can_resume:
            return "this server can't resume"
        if remote.size is None:
            return "the server won't say how big the file is"
        if notes.get("url") != remote.url:
            return "the old .part file is from a different link"
        if notes.get("size") != remote.size or notes.get("fingerprint") != remote.fingerprint:
            return "the file on the server changed since last time"
        return None

    def write(self, resp, start, on_progress):
        """Save what the server sends, from byte `start` (dropping anything after it)."""
        with open(self.path, "r+b" if start else "wb") as f:
            f.seek(start)
            f.truncate()
            done = start
            for chunk in iter(lambda: resp.read(CHUNK), b""):
                f.write(chunk)
                done += len(chunk)
                on_progress(done)

    def finish(self):
        """Every byte is here: give the file its real name."""
        os.replace(self.path, self.final)
        os.remove(self.notes)
        return self.final


def free_name(path):
    """clip.mp4 is taken?  Use 'clip (1).mp4'.  (An unfinished clip.mp4.part doesn't count.)"""
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path, n = f"{stem} ({n}){ext}", n + 1
    return path


# ---- what you see ---------------------------------------------------------------------------
class Progress:
    """The line that redraws itself:   42%  26.0 MB of 61.9 MB  ·  3.4 MB/s  ·  8s left"""

    def __init__(self, size):
        self.size, self.window, self.drawn_at = size, deque(), 0.0

    def show(self, done):
        now = time.time()
        if self.window and done < self.window[-1][1]:           # started again from byte 0
            self.window.clear()
        self.window.append((now, done))
        while now - self.window[0][0] > 3:                      # speed = the last 3 seconds
            self.window.popleft()
        if LIVE and now - self.drawn_at >= 0.2:
            self.drawn_at = now
            sys.stdout.write("\r  " + self.line(done) + "\x1b[K")
            sys.stdout.flush()

    def speed(self):
        (t0, b0), (t1, b1) = self.window[0], self.window[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def line(self, done):
        speed = self.speed()
        if not self.size:
            return f"{mb(done)}  ·  {mb(speed)}/s  (size unknown)"
        left = f"{(self.size - done) / speed:.0f}s left" if speed else "…"
        got = f"{percent(done, self.size)}  {mb(done)} of {mb(self.size)}"
        return f"{got}  ·  {mb(speed)}/s  ·  {left}"


def say(text):
    """Print a message on its own line (clearing the progress line first, if there is one)."""
    sys.stdout.write(("\r\x1b[K" if LIVE else "") + "  " + text + "\n")
    sys.stdout.flush()


def announce(part, remote):
    size = mb(remote.size) if remote.size else "size unknown"
    note = "" if remote.can_resume else ", this server can't resume"
    say(f"{part.name}  ({size}{note})")


def report_leftovers(part, remote):
    why = part.keep_or_discard(remote)
    if why:
        say(f"starting over: {why}")
    elif part.have():
        say(f"resuming at {percent(part.have(), remote.size)} ({mb(part.have())} already here)")


def paused_message(part, remote):
    if not part or not part.have():
        return "stopped."
    at = f" {percent(part.have(), remote.size)}" if remote.size else ""
    return f"paused at{at} ({mb(part.have())} saved). Run the same command again to carry on."


def mb(n):
    return f"{n / 1e6:.1f} MB"


def percent(done, size):
    return f"{100 * done // size}%"


def main():
    ap = argparse.ArgumentParser(description="Download one file over one connection (phase 1).")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", help="file name (default: the server's name for it)")
    ap.add_argument("-d", "--dir", default=".", help="folder to save into (default: here)")
    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    sys.exit(download(args.url, args.dir, args.output))


if __name__ == "__main__":
    main()
