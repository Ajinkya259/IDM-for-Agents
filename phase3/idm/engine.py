"""
The download itself.

When the server can send parts of the file (it answers Range requests), several connections
download it at once, each on its own piece (crew.py).  When it can't, one connection streams
the whole file, as in phase 2.  The engine doesn't print: it tells a `report` object what is
happening (so the caller decides how to show it), and every failure is an IDMError with a code.
"""
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import __version__
from .crew import Crew
from .errors import AUTO_RETRY, TRIES, IDMError, backoff, classify

CHUNK = 64 * 1024                                     # read and write 64 KB at a time
TIMEOUT = 30                                          # silent for 30 s = the connection is dead
USER_AGENT = f"Mozilla/5.0 (compatible; idm-for-agent/{__version__})"
PAGE_TYPES = ("text/html", "application/xhtml+xml")


class Report:
    """What the engine says while it works.  These do nothing; a caller overrides what it shows."""

    def found(self, remote, part): """The server described the file; `part` is where it goes."""

    def leftovers(self, why_discarded, have): """Bytes from last time: kept, or discarded and why."""

    def progress(self, done, connections=1): """`done` bytes are on disk, over `connections`."""

    def retrying(self, error, wait): """`error` happened; trying again in `wait` seconds."""


# ---- the whole download, step by step -------------------------------------------------------
def download(job, report=Report()):
    """Download job.url into job.folder and return the saved file's path.  Raises IDMError when
    it fails, and KeyboardInterrupt when paused (the .part file stays, to resume from)."""
    opener = make_opener(job)
    remote = with_retries(lambda: probe(opener, job), report)            # 1. what is this file?
    part = PartFile(job.folder, job.name or remote.name)                 # 2. where does it go?
    report.found(remote, part)
    report.leftovers(part.keep_or_discard(remote), part.have())          # 3. anything from last time?
    if remote.splittable:                                                # 4. get the missing bytes:
        fetch_in_pieces(opener, job, remote, part, report)               #    many connections at once,
    else:                                                                #    or one, start to end
        with_retries(lambda: fetch_rest(opener, job, remote, part, report), report, progress=part.have)
    return part.finish()                                                 # 5. NAME.part -> NAME


@dataclass
class Remote:
    """What the server told us about the file."""
    url: str
    name: str                     # its file name
    size: Optional[int]           # in bytes; None if the server won't say
    can_resume: bool              # it answered our Range request with 206 (just that part)
    fingerprint: Optional[str]    # ETag or Last-Modified: changes when the file changes

    @property
    def splittable(self):
        """Several connections can share it: each can ask for its own piece."""
        return self.can_resume and bool(self.size)


def probe(opener, job):
    """Ask for the file from byte 0 and read only the answer's headers."""
    with request(opener, job, 0) as resp:
        if resp.headers.get_content_type() in PAGE_TYPES and not job.allow_page:
            raise IDMError("NOT_A_FILE", f"the link opens a web page "
                                          f"({resp.headers.get_content_type()}), not a file")
        return Remote(url=job.url, name=pick_name(resp), size=total_size(resp),
                      can_resume=resp.status == 206, fingerprint=fingerprint(resp))


def fetch_in_pieces(opener, job, remote, part, report):
    """Several connections at once, each on its own piece of the file (see crew.py)."""

    def fetch_piece(piece, save):
        """One try at one piece: ask for its bytes, and save them as they come."""
        with request(opener, job, piece.pos, piece.end - 1) as resp:
            check_piece(resp, piece.pos, remote.size)
            for chunk in iter(lambda: resp.read(CHUNK), b""):
                if not save(piece, chunk):
                    return                                              # complete (or stopping)
        raise IDMError("NETWORK", f"the server stopped sending at {piece.pos:,} bytes")

    Crew(fetch_piece, part, report, job.connections).run()


def fetch_rest(opener, job, remote, part, report):
    """One connection: one try at downloading whatever is still missing from the .part file."""
    offset = part.have() if remote.can_resume else 0
    if remote.size is not None and offset >= remote.size:
        return                                                          # nothing missing
    with request(opener, job, offset) as resp:
        if resp.status == 206 and total_size(resp) != remote.size:
            raise IDMError("FILE_CHANGED", "the file changed on the server mid-download")
        start = offset if resp.status == 206 else 0                     # 200 = the whole file again
        part.write(resp, start, report.progress)
    if remote.size is not None and part.have() < remote.size:
        raise IDMError("NETWORK", f"the server stopped sending at {part.have():,} bytes")


def with_retries(attempt, report, progress=lambda: 0):
    """Run attempt() until it works.  A failure that may pass (network, busy server) is retried
    after a wait: 1, 2, 4... s, or as long as the server asks.  After TRIES failures in a row
    with no progress in between, the last one is raised."""
    fails = 0
    while True:
        before = progress()
        try:
            return attempt()
        except Exception as e:
            error = classify(e)
            if error is None:
                raise                                                   # a bug in idm: let it show
            if error.code not in AUTO_RETRY:
                raise error from None
            fails = 1 if progress() > before else fails + 1
            if fails > TRIES:
                raise IDMError(error.code, f"{error} (gave up after {TRIES} tries)",
                                error.retry_after) from None
            wait = backoff(error, fails)
            report.retrying(error, wait)
            time.sleep(wait)


# ---- talking to the server ---------------------------------------------------------------------
def make_opener(job):
    """urllib's opener, carrying the browser cookies the job was given (if any)."""
    if not job.cookies:
        return urllib.request.build_opener()
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(job.cookies, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as e:
        raise IDMError("BAD_INPUT", f"can't read the cookies file {job.cookies} ({e})") from None
    for cookie in jar:                     # browsers export login cookies with expiry 0, meaning
        if cookie.expires == 0:            # "until the browser closes", not "expired in 1970"
            cookie.expires, cookie.discard = None, True
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(opener, job, offset, last=None):
    """Ask for the file from byte `offset` to byte `last`, or to the end ("Range: bytes=offset-").

    The answer's status says what we got: 206 Partial Content is just the part we asked for
    (this server can resume); 200 OK means it ignored the Range and sends the whole file.
    """
    headers = {"User-Agent": USER_AGENT, **job.headers,
               "Range": f"bytes={offset}-{'' if last is None else last}",
               "Accept-Encoding": "identity"}       # no compression: byte 5,000 must be byte 5,000
    return opener.open(urllib.request.Request(job.url, headers=headers), timeout=TIMEOUT)


def total_size(resp):
    """The size of the whole file, from 'Content-Range: bytes 0-99/4096' (on a 206) or
    'Content-Length: 4096' (on a 200).  None if the server doesn't say."""
    if resp.status == 206:
        m = re.match(r"bytes \d+-\d+/(\d+)", resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None
    length = resp.headers.get("Content-Length", "")
    return int(length) if length.isdigit() else None


def first_byte(resp):
    """Where the answer starts in the file: N in 'Content-Range: bytes N-...' on a 206, else 0."""
    m = re.match(r"bytes (\d+)-", resp.headers.get("Content-Range", ""))
    return int(m.group(1)) if resp.status == 206 and m else 0


def check_piece(resp, start, size):
    """The answer must be the piece we asked for, of the same file."""
    if total_size(resp) != size:
        raise IDMError("FILE_CHANGED", "the file changed on the server mid-download")
    if first_byte(resp) != start:
        raise IDMError("HTTP_ERROR", f"asked for the file from byte {start:,}, but the server "
                                     f"sent it from byte {first_byte(resp):,}")


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


# ---- the file on disk --------------------------------------------------------------------------
class PartFile:
    """The download in progress: NAME.part holds the bytes so far, and NAME.part.json notes
    which link (and which version of the file) they belong to, and, for a file downloaded in
    pieces, which pieces are still missing.  It becomes NAME when complete."""

    def __init__(self, folder, name):
        self.final = free_name(os.path.join(folder, name))
        self.path = self.final + ".part"
        self.notes = self.path + ".json"
        self.about, self.size = {}, None
        self.missing = None                 # for a file in pieces: [[from, to], ...] still missing

    def have(self):
        """Bytes already on disk."""
        if self.missing is not None:
            return self.size - sum(to - frm for frm, to in self.missing)
        return self.on_disk()

    def on_disk(self):
        return os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def keep_or_discard(self, remote):
        """Keep bytes from an earlier try only if they belong to this very file; otherwise
        delete them.  Returns why they were deleted, or None."""
        notes = read_notes(self.notes)
        why = mismatch(notes, remote) if self.on_disk() else None
        if why:
            os.remove(self.path)
        self.about = {"url": remote.url, "size": remote.size, "fingerprint": remote.fingerprint}
        if remote.splittable:
            kept = notes.get("missing") if self.on_disk() else None   # phase 2's notes: none
            self.size, self.missing = remote.size, kept or [[self.on_disk(), remote.size]]
        self.save_missing(self.missing)
        return why

    def open_full_size(self):
        """Open the .part file for writing anywhere in it, made as big as the whole file."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(fd, self.size)
        return fd

    def save_missing(self, missing):
        """Update the notes (all or nothing: a crash never leaves half of them)."""
        self.missing = missing
        with open(self.notes + ".tmp", "w") as f:
            json.dump({**self.about, "missing": missing}, f)
        os.replace(self.notes + ".tmp", self.notes)

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


def read_notes(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def mismatch(notes, remote):
    """Why bytes from an earlier try can't be used for this file, or None if they can."""
    if not remote.can_resume:
        return "this server can't resume"
    if remote.size is None:
        return "the server won't say how big the file is"
    if notes.get("url") != remote.url:
        return "the old .part file is from a different link"
    if notes.get("size") != remote.size or notes.get("fingerprint") != remote.fingerprint:
        return "the file on the server changed since last time"
    return None


def free_name(path):
    """clip.mp4 is taken?  Use 'clip (1).mp4'.  (An unfinished clip.mp4.part doesn't count.)"""
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path, n = f"{stem} ({n}){ext}", n + 1
    return path
