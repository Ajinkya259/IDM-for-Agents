"""
The download itself.

When the server can send parts of the file (it answers Range requests), several connections
download it at once, each on its own piece (crew.py).  When it can't, one connection streams
the whole file, as in phase 2.  The engine doesn't print: it tells a `report` object what is
happening (so the caller decides how to show it), and every failure is an IDMError with a code.
"""
import email.utils
import glob
import http.client
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

from . import __version__, shares
from .crew import Crew
from .errors import AUTO_RETRY, TRIES, IDMError, backoff, classify, gave_up, out_of_patience

CHUNK = 64 * 1024                                     # read and write 64 KB at a time
CONNECT_TIMEOUT = float(os.environ.get("IDM_CONNECT_TIMEOUT", "10"))  # no answer in 10 s: try again
READ_TIMEOUT = float(os.environ.get("IDM_READ_TIMEOUT", "20"))        # silent 20 s mid-file: dead
USER_AGENT = f"Mozilla/5.0 (compatible; idm-for-agents/{__version__})"
PAGE_TYPES = ("text/html", "application/xhtml+xml")
PAGE_EXTENSIONS = ("", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml", ".cgi")


class Report:
    """What the engine says while it works.  These do nothing; a caller overrides what it shows."""

    def found(self, remote, part): """The server described the file; `part` is where it goes."""

    def leftovers(self, why_discarded, have): """Bytes from last time: kept, or discarded and why."""

    def progress(self, done, connections=1): """`done` bytes are on disk, over `connections`."""

    def retrying(self, error, wait): """`error` happened; trying again in `wait` seconds."""

    def video(self, file, size): """yt-dlp described the video: it will be saved as `file`."""

    def parts(self, paths): """yt-dlp is downloading into these files (its streams)."""


# ---- the whole download, step by step -------------------------------------------------------
def download(job, report):
    """Download job.url into job.folder and return the saved file's path.  Raises IDMError when
    it fails, and KeyboardInterrupt when paused (the .part file stays, to resume from)."""
    opener, limit = make_opener(job), Limit(job)
    file = with_retries(lambda: shares.the_file(opener, job), report)    # 1. a share link? the file's
    remote = with_retries(lambda: probe(opener, file), report)           # 2. what is this file?
    part = PartFile(job.folder, safe_name(file.name or remote.name), job.url)  # 3. where to?
    report.found(remote, part)
    report.leftovers(part.keep_or_discard(remote), part.have())          # 4. anything from last time?
    if remote.splittable:                                                # 5. get the missing bytes:
        fetch_in_pieces(opener, file, remote, part, report, limit)       #    many connections at once,
    else:                                                                #    or one, start to end
        with_retries(lambda: fetch_rest(opener, file, remote, part, report, limit), report,
                     progress=part.have)
    return part.finish()                                                 # 6. NAME.part -> NAME


@dataclass
class Remote:
    """What the server told us about the file."""
    name: str                    # its file name
    size: Optional[int]           # in bytes; None if the server won't say
    can_resume: bool              # it answered our Range request with 206 (just that part)
    fingerprint: Optional[str]    # ETag or Last-Modified: changes when the file changes

    @property
    def splittable(self):
        """Several connections can share it: each can ask for its own piece."""
        return self.can_resume and bool(self.size)


def probe(opener, job):
    """Ask for the file's first byte and read the answer's headers: its size, name and so on.
    (Just one byte, so a server that allows few connections isn't left sending the rest to
    nobody, holding a place that a real connection needs.)"""
    try:
        resp = request(opener, job, 0, 0)
    except urllib.error.HTTPError as e:
        if e.code != 416 or e.headers.get("Content-Range") != "bytes */0":
            raise
        with e:                                        # 416 "there is no byte 0": an empty file
            return Remote(name=pick_name(e), size=0,   # (whatever page comes with it)
                          can_resume=True, fingerprint=fingerprint(e))
    with resp:
        page = resp.headers.get_content_type() in PAGE_TYPES and not job.allow_page
        remote = Remote(name=pick_name(resp), size=total_size(resp),
                        can_resume=resp.status == 206, fingerprint=fingerprint(resp))
    if page and shares.sign_in_page(resp.geturl()):
        raise IDMError("AUTH_REQUIRED", "the link leads to a sign-in page: it isn't shared with "
                                        "everyone")
    if page:
        raise IDMError("NOT_A_FILE", "the link opens a web page, not a file",
                       links=links_on_page(opener, job))
    return remote


def links_on_page(opener, job):
    """The links on a web page that look like files (at most 5), to offer in its place."""
    try:
        headers = {"User-Agent": USER_AGENT, **job.headers}
        with opener.open(urllib.request.Request(job.url, headers=headers), timeout=READ_TIMEOUT) as resp:
            html = resp.read(1_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
            base = resp.geturl()
    except (OSError, ValueError, LookupError, http.client.HTTPException):   # (LookupError: a
        return []                                                         # charset Python lacks)
    finder = LinkFinder()
    try:
        finder.feed(html)
    except (NotImplementedError, AssertionError):      # markup Python 3.9's parser gives up on
        return []
    links = (urllib.parse.urljoin(base, href) for href in finder.hrefs)
    files = [url for url in links if url.startswith(("http://", "https://"))
             and os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower() not in PAGE_EXTENSIONS]
    return list(dict.fromkeys(files))[:5]


class LinkFinder(HTMLParser):
    """Collects the href of every <a> on a page."""

    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href")
        if tag == "a" and href:
            self.hrefs.append(href)


def fetch_in_pieces(opener, job, remote, part, report, limit):
    """Several connections at once, each on its own piece of the file (see crew.py)."""

    def fetch_piece(piece, save):
        """One try at one piece: ask for its bytes, and save them as they come."""
        with request(opener, job, piece.pos, piece.end - 1) as resp:
            check_piece(resp, piece.pos, remote)
            for chunk in limit.chunks(resp):
                if not save(piece, chunk):
                    return                                              # complete (or stopping)
        raise IDMError("NETWORK", f"the server stopped sending at {piece.pos:,} bytes")

    Crew(fetch_piece, part, report, job.connections).run()


def fetch_rest(opener, job, remote, part, report, limit):
    """One connection: one try at downloading whatever is still missing from the .part file."""
    offset = part.have() if remote.can_resume else 0
    if remote.size is not None and offset >= remote.size:
        return                                                          # nothing missing
    with request(opener, job, offset) as resp:
        check_same_file(resp, remote)
        start = offset if resp.status == 206 else 0                     # 200 = the whole file again
        part.write(limit.chunks(resp), start, report.progress)
    if remote.size is not None and part.have() < remote.size:
        raise IDMError("NETWORK", f"the server stopped sending at {part.have():,} bytes")


def with_retries(attempt, report, progress=lambda: 0):
    """Run attempt() until it works.  A failure that may pass (network, busy server) is retried
    after a wait: 1, 2, 4... s, or as long as the server asks.  After TRIES failures in a row
    with no progress in between, or PATIENCE seconds of a silent server, the last one is raised."""
    fails, moved = 0, time.time()
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
            if progress() > before:
                fails, moved = 0, time.time()
            fails += 1
            if out_of_patience(error, time.time() - moved):
                raise gave_up(error, silent_for=time.time() - moved) from None
            if fails > TRIES:
                raise gave_up(error) from None
            wait = backoff(error, fails)
            report.retrying(error, wait)
            time.sleep(wait)


# ---- talking to the server ---------------------------------------------------------------------
def make_opener(job):
    """urllib's opener, carrying the browser cookies the job was given (if any)."""
    if not job.cookies:
        return urllib.request.build_opener(KeepLoginsHome, QuickHTTP, QuickHTTPS)
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(job.cookies, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as e:
        raise IDMError("BAD_INPUT", f"can't read the cookies file {job.cookies} ({e})") from None
    for cookie in jar:                     # browsers export login cookies with expiry 0, meaning
        if cookie.expires == 0:            # "until the browser closes", not "expired in 1970"
            cookie.expires, cookie.discard = None, True
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), KeepLoginsHome,
                                       QuickHTTP, QuickHTTPS)


class QuickConnect:
    """A connection that gives a server CONNECT_TIMEOUT seconds to answer (and agree on TLS),
    then READ_TIMEOUT seconds between bytes.  (urllib has one timeout for both, so a server
    that never answers would cost the long one.)"""

    def connect(self):
        self.timeout = CONNECT_TIMEOUT
        super().connect()
        self.sock.settimeout(READ_TIMEOUT)


class QuickHTTPConnection(QuickConnect, http.client.HTTPConnection):
    pass


class QuickHTTPSConnection(QuickConnect, http.client.HTTPSConnection):
    pass


class QuickHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(QuickHTTPConnection, req)


class QuickHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(QuickHTTPSConnection, req, context=self._context)


class KeepLoginsHome(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but don't hand the user's login (an Authorization or Cookie header
    from --header) to a different host, as curl doesn't.  (Cookies from --cookies go only
    where their own domain says.)"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            for name in ("Authorization", "Cookie"):
                new.headers.pop(name, None)
                new.unredirected_hdrs.pop(name, None)
        return new


def request(opener, job, offset, last=None):
    """Ask for the file from byte `offset` to byte `last`, or to the end ("Range: bytes=offset-").

    The answer's status says what we got: 206 Partial Content is just the part we asked for
    (this server can resume); 200 OK means it ignored the Range and sends the whole file.
    """
    headers = {"User-Agent": USER_AGENT, **job.headers,
               "Range": f"bytes={offset}-{'' if last is None else last}",
               "Accept-Encoding": "identity"}       # no compression: byte 5,000 must be byte 5,000
    return opener.open(urllib.request.Request(job.url, headers=headers), timeout=READ_TIMEOUT)


class Limit:
    """A speed limit for one download, shared by all its connections: at most `job.limit`
    bytes a second (None: no limit).  It reads the limit again every second, so `idm limit`
    can change it while the download runs."""

    def __init__(self, job):
        self.job_file, self.rate, self.read_at = job.dir / "job.json", job.limit, time.monotonic()
        self.lock, self.free_at = threading.Lock(), time.monotonic()   # when the line is free

    def chunks(self, resp):
        """The answer's bytes, a chunk at a time, no faster than the limit."""
        while True:
            chunk = resp.read(self.chunk_size())
            if not chunk:
                return
            self.take(len(chunk))
            yield chunk

    def chunk_size(self):
        rate = self.current()
        return min(CHUNK, max(1024, rate // 8)) if rate else CHUNK    # small steps when slow

    def take(self, n):
        """Wait until n more bytes fit under the limit."""
        rate = self.current()
        if not rate:
            return
        with self.lock:
            now = time.monotonic()
            turn = max(self.free_at, now)               # after the chunks before it
            self.free_at = turn + n / rate
        if turn > now:
            time.sleep(turn - now)

    def current(self):
        if time.monotonic() - self.read_at >= 1:
            self.rate, self.read_at = read_json(self.job_file).get("limit", self.rate), time.monotonic()
        return self.rate


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


def check_piece(resp, start, remote):
    """The answer must be the piece we asked for, of the same file."""
    if resp.status != 206:                             # the whole file: it changed, and the server
        raise IDMError("FILE_CHANGED", "the file changed on the server mid-download")   # knows
    check_same_file(resp, remote)
    if first_byte(resp) != start:
        raise IDMError("HTTP_ERROR", f"asked for the file from byte {start:,}, but the server "
                                     f"sent it from byte {first_byte(resp):,}")


def check_same_file(resp, remote):
    """A part of the file must come from the same version of it as the rest: the same size,
    and the same ETag (or Last-Modified).  Otherwise the bytes would be a mix of two files."""
    changed = fingerprint(resp) != remote.fingerprint
    if resp.status == 206 and (total_size(resp) != remote.size or changed):
        raise IDMError("FILE_CHANGED", "the file changed on the server mid-download")


def fingerprint(resp):
    return resp.headers.get("ETag") or resp.headers.get("Last-Modified")


def pick_name(resp):
    """The server's name for the file (Content-Disposition), or else the last part of the
    link.  Made safe to save under."""
    return safe_name(server_name(resp) or
                     urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(resp.geturl()).path)))


def safe_name(name):
    """A name to save a file under: no folders in it, not hidden, not too long."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name or "").strip().lstrip(".")
    stem, ext = os.path.splitext(name or "download")
    if len(ext.encode()) > 20:                         # a dot in a long name, not an extension
        stem, ext = stem + ext, ""
    while len((stem + ext).encode()) > 200:            # file names stop at 255 bytes; leave room
        stem = stem[:-1]                               # for " (1)" and ".part.json"
    return stem + ext


def server_name(resp):
    """Content-Disposition's file name.  Servers often give two: filename*= in full (any
    language, spaces kept) and a plain filename= for old browsers; the full one wins."""
    names = [value for key, value in resp.headers.get_params([], header="content-disposition")
             if key == "filename"]
    full = [email.utils.collapse_rfc2231_value(v) for v in names if isinstance(v, tuple)]
    return (full or [resp.headers.get_filename()])[0]


# ---- the file on disk --------------------------------------------------------------------------
class PartFile:
    """The download in progress: NAME.part holds the bytes so far, and NAME.part.json notes
    which link (and which version of the file) they belong to, and, for a file downloaded in
    pieces, which pieces are still missing.  It becomes NAME when complete."""

    def __init__(self, folder, name, url):
        self.url = url                      # the link as given: what the bytes belong to
        self.final = free_name(os.path.join(folder, name), url)
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
        notes = read_json(self.notes)
        why = mismatch(notes, remote, self.url) if self.on_disk() else None
        if why:
            os.remove(self.path)
        open(self.path, "ab").close()                  # there is a .part now (if empty: an empty file)
        self.about = {"url": self.url, "size": remote.size, "fingerprint": remote.fingerprint}
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
        """Update the notes."""
        self.missing = missing
        write_json(self.notes, {**self.about, "missing": missing})

    def write(self, chunks, start, on_progress):
        """Save what the server sends, from byte `start` (dropping anything after it)."""
        with open(self.path, "r+b" if start else "wb") as f:
            f.seek(start)
            f.truncate()
            done = start
            for chunk in chunks:
                f.write(chunk)
                done += len(chunk)
                on_progress(done)

    def finish(self):
        """Every byte is here: make sure it is on the disk itself, then give the file its name."""
        with open(self.path, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(self.path, self.final)
        os.remove(self.notes)
        return self.final


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    """Write the whole file or nothing: a reader (or a crash) never sees half of it."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def mismatch(notes, remote, url):
    """Why bytes from an earlier try (at `url`) can't be used for this file, or None if they can."""
    if not remote.can_resume:
        return "this server can't resume"
    if remote.size is None:
        return "the server won't say how big the file is"
    if notes.get("url") != url:
        return "the old .part file is from a different link"
    if notes.get("size") != remote.size or notes.get("fingerprint") != remote.fingerprint:
        return "the file on the server changed since last time"
    return None


def free_name(path, url):
    """clip.mp4 is taken, or another link's download is under way as clip.mp4.part?  Use
    'clip (1).mp4'.  This link's own unfinished download is the one to carry on, under the
    name it got then (clip.mp4 may have been taken that time, and moved away since)."""
    stem, ext = os.path.splitext(path)
    numbered = glob.escape(f"{stem} (") + "*" + glob.escape(f"){ext}.part.json")
    for notes in [path + ".part.json", *glob.glob(numbered)]:
        name = notes[:-len(".part.json")]
        if read_json(notes).get("url") == url and not os.path.exists(name):
            return name
    n = 1
    while os.path.exists(path) or read_json(path + ".part.json").get("url", url) != url:
        path, n = f"{stem} ({n}){ext}", n + 1
    return path
