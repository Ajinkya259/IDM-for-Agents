#!/usr/bin/env python3
"""
Tests for idm: every command, every error code, the background jobs, downloading in pieces
over many connections, halo (in a terminal, and as cells for Claude Code), what a critic
found broken, phase 6 (scheduling, the waiting line, speed limits, silent servers),
phase 7 (videos), phase 8 (share links: Drive, Dropbox, OneDrive, GitHub, Hugging Face), and
what a review of the whole code found.

    python3 tests/run_tests.py          (about 90 seconds; uses free local ports)
    python3 tests/run_tests.py pause    (only the tests with "pause" in their name)

Each test runs the real `bin/idm` against tests/testserver.py, with its own IDM_HOME and
download folder, and checks the JSON replies, the exit codes and the saved bytes.
"""
import base64
import datetime
import fcntl
import hashlib
import io
import json
import os
import pty
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDM = [sys.executable, str(HERE.parent / "bin" / "idm")]
sys.path.insert(0, str(HERE.parent))
from idm import cli, engine, halo, jobs, shares, video, when  # noqa: E402  (needs the path above)
from idm.errors import IDMError  # noqa: E402
from idm.crew import MIN_PIECE, Missing  # noqa: E402
TESTS, RESULTS = [], []
LEFTOVERS = []                     # servers and sandboxes to clean up after each test, pass or fail


def test(fn):
    TESTS.append(fn)
    return fn


def check(what, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + what + ("" if ok else f"\n        {detail}"))


# ---- tools for the tests -------------------------------------------------------------------------
class Server:
    """tests/testserver.py on a free port, with the given flags."""

    def __init__(self, *flags, size=2, rate=20, port=None):
        self.port = port or free_port()
        self.size, self.version = size, int(flags[flags.index("--version") + 1]) if "--version" in flags else 1
        self.proc = subprocess.Popen([sys.executable, str(HERE / "testserver.py"), "--port", str(self.port),
                                      "--size", str(size), "--rate", str(rate), *flags],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.proc.stdout.readline()                                     # "serving ..." = it is up
        LEFTOVERS.append(self.stop)

    def url(self, path="/test.bin"):
        return f"http://127.0.0.1:{self.port}{path}"

    def stats(self):
        with urllib.request.urlopen(self.url("/stats")) as resp:
            return json.loads(resp.read())

    def sha(self):
        pattern = bytes((i + self.version) % 251 for i in range(251))
        size = int(self.size * 1e6)
        return hashlib.sha256((pattern * (size // 251 + 1))[:size]).hexdigest()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()


class Sandbox:
    """A IDM_HOME and a download folder of its own, and a way to run idm in them."""

    def __init__(self, **env):
        self.root = Path(tempfile.mkdtemp(prefix="idm-test-"))
        self.dl = self.root / "dl"
        self.env = {**os.environ, "IDM_HOME": str(self.root / "home"), "COLORTERM": "", **env}
        LEFTOVERS.append(self.clean)

    def idm(self, *args, timeout=60):
        """Run idm as an agent would (output is a pipe).  Returns (exit code, the JSON reply)."""
        p = subprocess.run([*IDM, *args], env=self.env, capture_output=True, text=True,
                           timeout=timeout)
        lines = p.stdout.strip().splitlines()
        try:
            reply = json.loads(lines[-1]) if len(lines) == 1 else {"_not_one_line": p.stdout}
        except ValueError:
            reply = {"_not_json": p.stdout + p.stderr}
        return p.returncode, reply

    def get(self, url, *args, **kw):
        return self.idm("get", url, "-d", str(self.dl), *args, **kw)

    def start(self, url, *args):
        return self.idm("start", url, "-d", str(self.dl), *args)

    def status_file(self, job):
        return json.loads((self.root / "home" / "jobs" / job / "status.json").read_text())

    def files(self):
        return sorted(os.listdir(self.dl)) if self.dl.exists() else []

    def notes(self, name="halo test.bin"):
        return json.loads((self.dl / f"{name}.part.json").read_text())

    def clean(self):
        for status in (self.root / "home" / "jobs").glob("*/status.json"):   # stop any worker left
            pid = json.loads(status.read_text()).get("pid")
            if pid and alive(pid):
                os.kill(pid, signal.SIGKILL)
        for path in self.root.rglob("*"):                                   # unlock read-only folders
            if path.is_dir():
                path.chmod(0o700)
        shutil.rmtree(self.root, ignore_errors=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(condition, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def in_terminal(args, env, interrupt_after=None, timeout=60, size=(40, 100), keys=()):
    """Run idm in a pseudo-terminal (rows, columns), as a person would, pressing `keys`
    ((seconds, key), ...) on the way.  Returns (exit code, what was shown)."""
    keys = list(keys)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
    p = subprocess.Popen([*IDM, *args], env=env, stdin=slave, stdout=slave, stderr=slave,
                         preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    os.close(slave)                                  # (Ctrl+C works even if these tests run with &)
    shown, start, sent = b"", time.time(), False
    while True:
        if interrupt_after and not sent and time.time() - start > interrupt_after:
            p.send_signal(signal.SIGINT)
            sent = True
        while keys and time.time() - start > keys[0][0]:
            os.write(master, keys.pop(0)[1].encode())
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if not chunk and p.poll() is not None:
                break
            shown += chunk
        elif p.poll() is not None:
            break
        if time.time() - start > timeout:
            p.kill()
            break
    os.close(master)
    return p.wait(), shown.decode(errors="replace")


# ---- downloading ---------------------------------------------------------------------------------
@test
def get_downloads_the_file():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url())
    f = box.dl / "halo test.bin"
    check("get: exit 0, ok, state done", code == 0 and r.get("ok") and r.get("state") == "done", r)
    check("get: the reply says where the file is, its size and how long it took",
          r.get("file") == str(f) and r.get("bytes") == 2_000_000 and "seconds" in r, r)
    check("get: the bytes are exactly right", f.exists() and sha(f) == srv.sha())
    check("get: no .part or notes left behind", box.files() == ["halo test.bin"], box.files())


@test
def same_get_twice_does_not_download_twice():
    srv, box = Server(), Sandbox()
    _, first = box.get(srv.url())
    code, again = box.get(srv.url())
    check("get twice: second reply is the same job, already: true",
          code == 0 and again.get("job") == first.get("job") and again.get("already") is True, again)
    check("get twice: still one file", box.files() == ["halo test.bin"], box.files())
    _, fresh = box.get(srv.url(), "--again")
    check("get --again: a new job and a second copy",
          fresh.get("job") != first.get("job") and "halo test (1).bin" in box.files(), (fresh, box.files()))


@test
def output_name():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url(), "-o", "mine.bin")
    check("get -o: saved under the given name", code == 0 and box.files() == ["mine.bin"], box.files())


# ---- errors an agent must understand -----------------------------------------------------------------
@test
def not_found():
    srv, box = Server(), Sandbox()
    for status in (404, 410):
        code, r = box.get(srv.url(f"/status/{status}"))
        e = r.get("error", {})
        check(f"{status}: NOT_FOUND, not retryable, exit 1, a hint, no `next`",
              code == 1 and e.get("code") == "NOT_FOUND" and e.get("retryable") is False
              and e.get("hint") and "next" not in r, r)
    check("404: no files left behind", box.files() == [], box.files())


@test
def login_needed():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url("/secret.bin"))
    check("403: AUTH_REQUIRED, exit 1, `next` adds --cookies",
          code == 1 and r.get("error", {}).get("code") == "AUTH_REQUIRED"
          and "--cookies" in r.get("next", ""), r)
    code, r2 = box.get(srv.url("/secret.bin"), "--header", "Authorization: Bearer ok")
    check("403 then --header: the same job carries on and succeeds",
          code == 0 and r2.get("state") == "done" and r2.get("job") == r.get("job"), r2)
    check("403 then --header: right bytes", sha(box.dl / "halo test.bin") == srv.sha())


@test
def cookies_file():
    srv, box = Server(), Sandbox()
    jar = box.root / "cookies.txt"
    # expiry 0 = a session cookie, the way browser extensions export a login
    jar.write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsession\tok\n")
    code, r = box.get(srv.url("/secret.bin"), "--cookies", str(jar))
    check("--cookies FILE: logged in, downloaded", code == 0 and r.get("state") == "done", r)
    code, r = box.get(srv.url("/secret.bin"), "--cookies", str(box.root / "missing.txt"), "--again")
    check("--cookies with no such file: BAD_INPUT, exit 2 (a bad command)",
          code == 2 and r.get("error", {}).get("code") == "BAD_INPUT", r)


@test
def web_page_instead_of_file():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url("/page.html"))
    error = r.get("error", {})
    check("a web page: NOT_A_FILE, exit 1, with the file it links to, and `next` to get that",
          code == 1 and error.get("code") == "NOT_A_FILE" and error.get("links") == [srv.url()]
          and r.get("next", "").startswith(f"idm get {srv.url()} -d "), r)
    code, done = box.idm(*shlex.split(r.get("next", "x"))[1:])
    check("running that `next` gets the real file", done.get("state") == "done"
          and sha(done["file"]) == srv.sha(), done)
    code, r = box.get(srv.url("/page.html"), "--allow-page")
    check("--allow-page: the page is saved", code == 0 and "page.html" in box.files(), (r, box.files()))


@test
def bad_input():
    box = Sandbox()
    code, r = box.get("not a link")
    check("bad link: BAD_URL, exit 2, and no job created",
          code == 2 and r.get("error", {}).get("code") == "BAD_URL"
          and box.idm("list")[1].get("jobs") == [], r)
    code, r = box.get("http://127.0.0.1:9/x", "--header", "no colon")
    check("bad --header: BAD_INPUT", code == 2 and r.get("error", {}).get("code") == "BAD_INPUT", r)
    code, r = box.get("http://127.0.0.1:9/x", "--connections", "99")
    check("--connections 99: BAD_INPUT, says the range",
          code == 2 and r.get("error", {}).get("code") == "BAD_INPUT" and "1 to 16" in r["error"]["message"], r)
    check("a bad option leaves no job behind", box.idm("list")[1].get("jobs") == [], box.idm("list"))
    code, r = box.idm("get")
    check("missing argument: still one line of JSON, BAD_INPUT, exit 2",
          code == 2 and r.get("error", {}).get("code") == "BAD_INPUT", r)
    for command in ("status", "wait", "pause", "resume", "cancel"):
        code, r = box.idm(command, "nope42")
        check(f"{command} with an unknown id: NO_SUCH_JOB, exit 1",
              code == 1 and r.get("error", {}).get("code") == "NO_SUCH_JOB", r)


@test
def no_server_gives_up_with_next():
    box = Sandbox(IDM_TRIES="2")
    code, r = box.get(f"http://127.0.0.1:{free_port()}/x")
    e = r.get("error", {})
    check("nothing listening: NETWORK, retryable, exit 3, after retrying",
          code == 3 and e.get("code") == "NETWORK" and e.get("retryable") is True
          and "gave up after 3 tries" in e.get("message", ""), r)       # the first, and 2 retries
    check("nothing listening: `next` resumes the same job",
          r.get("next") == f"idm resume {r.get('job')}", r)


@test
def server_errors():
    srv, box = Server(), Sandbox(IDM_TRIES="1")
    code, r = box.get(srv.url("/status/500"))
    check("500: SERVER_ERROR, retryable, exit 3",
          code == 3 and r.get("error", {}).get("code") == "SERVER_ERROR", r)
    code, r = box.get(srv.url("/status/429"))
    e = r.get("error", {})
    check("429 that never ends: RATE_LIMITED, retryable, with the server's retry_after",
          code == 3 and e.get("code") == "RATE_LIMITED" and e.get("retry_after") == 1, r)
    code, r = box.get(srv.url("/status/400"))
    check("400: HTTP_ERROR, not retryable", code == 1 and r.get("error", {}).get("code") == "HTTP_ERROR", r)


@test
def busy_server_is_waited_for():
    srv, box = Server("--busy", "2"), Sandbox()
    _, r = box.start(srv.url())
    seen = set()
    wait_until(lambda: seen.add((box.idm("status", r["job"])[1].get("trouble") or {}).get("code"))
               or "RATE_LIMITED" in seen, timeout=6)
    check("429 twice: while waiting, status shows trouble RATE_LIMITED", "RATE_LIMITED" in seen, seen)
    code, done = box.idm("wait", r["job"])
    check("429 twice: then downloads, right bytes",
          code == 0 and done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def dropped_connection_recovers():
    srv, box = Server("--drop", "1", size=4), Sandbox()
    code, r = box.get(srv.url())
    check("connection cut at 1 MB: carries on, right bytes",
          code == 0 and sha(box.dl / "halo test.bin") == srv.sha(), r)


@test
def no_permission():
    box = Sandbox()
    srv = Server()
    locked = box.root / "locked"
    locked.mkdir(mode=0o500)
    code, r = box.idm("get", srv.url(), "-d", str(locked))
    check("read-only folder: NO_PERMISSION, exit 1",
          code == 1 and r.get("error", {}).get("code") == "NO_PERMISSION", r)


# ---- background jobs ---------------------------------------------------------------------------------
@test
def start_replies_at_once_then_wait():
    srv, box = Server(size=6, rate=0.5), Sandbox()
    t = time.time()
    code, r = box.start(srv.url())
    took = time.time() - t
    check("start: replies at once (under 1.5 s) with a job id and `next`: idm wait",
          code == 0 and took < 1.5 and r.get("state") in ("queued", "downloading")
          and r.get("next") == f"idm wait {r.get('job')}", (took, r))
    code, mid = box.idm("wait", r["job"], "--max", "1")
    check("wait --max 1: replies while still downloading, with percent, speed and eta",
          code == 0 and mid.get("state") == "downloading" and 0 < mid.get("percent", 0) < 100
          and (mid.get("speed") or 0) > 0 and "eta" in mid, mid)
    code, done = box.idm("wait", r["job"])
    check("wait: done, right bytes", done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def get_with_max_hands_over_to_wait():
    srv, box = Server(size=6, rate=0.5), Sandbox()
    t = time.time()
    code, r = box.get(srv.url(), "--max", "0.5")
    check("get --max 0.5: replies in time, still downloading, `next`: idm wait",
          time.time() - t < 2 and r.get("state") == "downloading" and r.get("next", "").startswith("idm wait"), r)
    code, done = box.idm("wait", r["job"])
    check("then wait: done", code == 0 and done.get("state") == "done", done)


@test
def pause_and_resume():
    srv, box = Server(size=8, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    job = r["job"]
    wait_until(lambda: box.status_file(job).get("bytes", 0) > 500_000)
    pid = box.status_file(job)["pid"]
    code, p = box.idm("pause", job)
    check("pause: state paused, some percent, `next`: idm resume",
          code == 0 and p.get("state") == "paused" and 0 < p.get("percent", 0) < 100
          and p.get("next") == f"idm resume {job}", p)
    check("pause: the worker has stopped, the .part file stays",
          not alive(pid) and "halo test.bin.part" in box.files(), box.files())
    missing = box.notes().get("missing", [])
    check("pause: the notes list the pieces still missing (several: it was downloading in pieces)",
          len(missing) >= 2 and sum(b - a for a, b in missing) == 8_000_000 - p["bytes"], missing)
    code, again = box.idm("resume", job)
    check("resume: downloading again", code == 0 and again.get("state") in ("queued", "downloading"), again)
    code, done = box.idm("wait", job)
    check("resume: finishes with the right bytes",
          done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def cancel_deletes_partial():
    srv, box = Server(size=8, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 200_000)
    code, c = box.idm("cancel", r["job"])
    check("cancel: state cancelled, nothing left on disk",
          code == 0 and c.get("state") == "cancelled" and box.files() == [], (c, box.files()))


@test
def worker_killed_is_reported_and_resumable():
    srv, box = Server(size=8, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    job = r["job"]
    wait_until(lambda: box.status_file(job).get("bytes", 0) > 500_000)
    os.kill(box.status_file(job)["pid"], signal.SIGKILL)
    time.sleep(0.2)
    code, s = box.idm("status", job)
    check("worker killed: INTERRUPTED, retryable, exit 3, `next` resumes",
          code == 3 and s.get("error", {}).get("code") == "INTERRUPTED"
          and s.get("next") == f"idm resume {job}", s)
    box.idm("resume", job)
    code, done = box.idm("wait", job)
    check("worker killed: resume finishes with the right bytes",
          done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def agent_cut_off_then_same_get_reattaches():
    srv, box = Server(size=6, rate=2), Sandbox()
    p = subprocess.Popen([*IDM, "get", srv.url(), "-d", str(box.dl)], env=box.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    p.kill()                                                   # the agent's tool call was cut off
    p.wait()
    code, r = box.get(srv.url())
    jobs = box.idm("list")[1].get("jobs", [])
    check("cut off, then the same get: one job, carried on to done",
          code == 0 and r.get("state") == "done" and len(jobs) == 1, (r, jobs))
    check("cut off, then the same get: one file, right bytes",
          box.files() == ["halo test.bin"] and sha(box.dl / "halo test.bin") == srv.sha(), box.files())


@test
def pause_resume_on_server_that_cannot_resume():
    srv, box = Server("--no-ranges", size=6, rate=2), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 300_000)
    box.idm("pause", r["job"])
    box.idm("resume", r["job"])
    code, done = box.idm("wait", r["job"])
    note = box.status_file(r["job"]).get("note", "")
    check("no-ranges server: resume starts over (and says so), right bytes",
          done.get("state") == "done" and "can't resume" in (note or "")
          and sha(box.dl / "halo test.bin") == srv.sha(), (done, note))


@test
def file_changed_while_paused():
    srv, box = Server(size=6, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 300_000)
    box.idm("pause", r["job"])
    port = srv.port
    srv.stop()
    srv = Server("--version", "2", size=6, rate=20, port=port)       # same link, new file
    box.idm("resume", r["job"])
    code, done = box.idm("wait", r["job"])
    note = box.status_file(r["job"]).get("note", "")
    check("file changed while paused: noticed, starts over, gets the NEW bytes",
          done.get("state") == "done" and "changed" in (note or "")
          and sha(box.dl / "halo test.bin") == srv.sha(), (done, note))


@test
def status_file_has_what_the_face_needs():
    srv, box = Server(size=6, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: (box.status_file(r["job"]).get("speed") or 0) > 0)
    st = box.status_file(r["job"])
    needed = {"job", "url", "file", "state", "bytes", "size", "speed", "eta", "connections", "pid",
              "updated"}
    check("status.json: has state, bytes, size, speed, eta, connections, file... and is fresh",
          needed <= st.keys() and st["state"] == "downloading" and time.time() - st["updated"] < 2,
          sorted(needed - st.keys()))
    box.idm("cancel", r["job"])


@test
def list_is_short():
    srv, box = Server(), Sandbox()
    box.get(srv.url())
    box.get(srv.url("/status/404"))
    code, r = box.idm("list")
    rows = r.get("jobs", [])
    check("list: one row per job, with job, state, url (and the error code, no hint)",
          code == 0 and len(rows) == 2 and all({"job", "state", "url"} <= row.keys() for row in rows)
          and rows[1].get("error") == "NOT_FOUND" and "hint" not in json.dumps(rows), rows)


# ---- many connections (phase 3) --------------------------------------------------------------------
@test
def splitting_takes_half_of_the_biggest_piece():
    m = Missing([[0, 8 * MIN_PIECE]])
    first, second = m.take(), m.take()
    check("the first connection takes the whole file; the next takes its second half",
          (first.pos, first.end, second.pos, second.end) == (0, 4 * MIN_PIECE, 4 * MIN_PIECE, 8 * MIN_PIECE),
          [(p.pos, p.end) for p in m.pieces])
    third = m.take()
    check("the third splits the biggest piece left (both are equal: the first one)",
          (third.pos, third.end, first.end) == (2 * MIN_PIECE, 4 * MIN_PIECE, 2 * MIN_PIECE),
          [(p.pos, p.end) for p in m.pieces])
    m.wrote(first, 2 * MIN_PIECE - MIN_PIECE // 2)                     # nearly done...
    small = Missing([[0, 2 * MIN_PIECE - 1]])
    small.take()
    check("a piece is split only if both halves get at least MIN_PIECE",
          small.take() is None and len(m.pieces) == 3, [(p.pos, p.end) for p in small.pieces])
    m.step_back(third)
    check("a piece handed back is the next one taken", m.take() is third)


@test
def many_connections_are_faster():
    srv, box = Server(size=6, rate=2), Sandbox()
    t = time.time()
    code1, one = box.get(srv.url(), "-o", "one.bin", "--connections", "1")
    alone = time.time() - t
    check("--connections 1: one connection at a time", code1 == 0 and srv.stats()["peak"] <= 2, srv.stats())
    t = time.time()
    code8, many = box.get(srv.url(), "-o", "many.bin")
    together = time.time() - t
    check(f"8 connections (the default): faster ({together:.1f}s vs {alone:.1f}s alone)",
          code8 == 0 and together < 0.6 * alone and srv.stats()["peak"] >= 3, (srv.stats(), many))
    check("8 connections: the bytes are exactly right (pieces in their places)",
          sha(box.dl / "one.bin") == sha(box.dl / "many.bin") == srv.sha())


@test
def status_shows_connections_working():
    srv, box = Server(size=8, rate=0.5), Sandbox()
    _, r = box.start(srv.url())
    seen = []
    wait_until(lambda: seen.append(box.status_file(r["job"]).get("connections") or 0) or max(seen) >= 3)
    check("status.json: `connections` says how many are working (3 or more here)", max(seen) >= 3, seen)
    box.idm("cancel", r["job"])


@test
def server_that_limits_connections():
    srv, box = Server("--max-connections", "2", size=8, rate=2), Sandbox()
    code, r = box.get(srv.url())
    check("server allows 2 at once: the extra connections step back, the download finishes",
          code == 0 and r.get("state") == "done" and srv.stats()["peak"] <= 2, (r, srv.stats()))
    check("server allows 2 at once: right bytes", sha(box.dl / "halo test.bin") == srv.sha())


@test
def limited_server_still_gets_every_connection_it_allows():
    srv, box = Server("--max-connections", "2", size=8, rate=1), Sandbox()
    started = time.time()
    code, r = box.get(srv.url())
    took = time.time() - started
    check("server allows 2 at once: idm uses both (8 MB at 1 MB/s each: well under 8 s), "
          "without hammering it", code == 0 and took < 6.8 and srv.stats()["requests"] < 30,
          (round(took, 1), srv.stats()))


@test
def resumes_what_phase_2_left():
    srv, box = Server(size=4), Sandbox()
    box.dl.mkdir(parents=True)
    pattern = bytes((i + 1) % 251 for i in range(251))
    (box.dl / "halo test.bin.part").write_bytes((pattern * 4000)[:1_000_000])   # 1 MB, from the start
    (box.dl / "halo test.bin.part.json").write_text(json.dumps(
        {"url": srv.url(), "size": 4_000_000, "fingerprint": '"test-v1"'}))        # no "missing"
    code, r = box.get(srv.url())
    note = box.status_file(r["job"]).get("note", "")
    check("a phase 2 .part file: resumed from its 1 MB, right bytes",
          code == 0 and "1,000,000" in (note or "") and sha(box.dl / "halo test.bin") == srv.sha(),
          (r, note))


# ---- what a person sees ------------------------------------------------------------------------------
@test
def person_sees_progress_and_result():
    srv, box = Server(size=4, rate=4), Sandbox()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env)
    check("in a terminal: a progress line, then '✓ saved', no JSON",
          code == 0 and "MB/s" in shown and "✓ saved" in shown and '"ok"' not in shown, shown[-300:])


@test
def person_ctrl_c_pauses():
    srv, box = Server(size=8, rate=0.5), Sandbox()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env, interrupt_after=1.5)
    jobs = box.idm("list")[1].get("jobs", [])
    check("Ctrl+C in a terminal: says paused, the job is paused, `idm resume` shown",
          "paused at" in shown and "idm resume" in shown and jobs and jobs[0]["state"] == "paused",
          (shown[-300:], jobs))


# ---- what a critic found (phase 5) --------------------------------------------------------------------
@test
def file_changing_mid_download_never_mixes_bytes():
    port, box = free_port(), Sandbox()
    old = Server(size=8, rate=0.5, port=port)
    _, r = box.start(old.url())
    wait_until(lambda: (box.status_file(r["job"]).get("bytes") or 0) > 1_000_000)
    old.stop()
    new = Server("--version", "2", size=8, rate=4, port=port)       # same link, same size, new bytes
    code, reply = box.idm("wait", r["job"])
    if reply.get("state") == "failed":                               # FILE_CHANGED: `next` starts over
        code, reply = box.idm("resume", r["job"])
        code, reply = box.idm("wait", r["job"])
    check("the file changes on the server mid-download: never a mix; it ends with the new file",
          reply.get("state") == "done" and sha(reply["file"]) == new.sha(), reply)


@test
def two_links_with_one_name_keep_apart():
    a, b, box = Server(size=4, rate=1), Server("--version", "3", size=4, rate=1), Sandbox()
    _, ra = box.start(a.url())
    time.sleep(0.5)
    _, rb = box.start(b.url())                                      # both are "halo test.bin"
    da, db = box.idm("wait", ra["job"])[1], box.idm("wait", rb["job"])[1]
    check("two links whose files have the same name: two files, each with its own bytes",
          da.get("state") == db.get("state") == "done" and da["file"] != db["file"]
          and sha(da["file"]) == a.sha() and sha(db["file"]) == b.sha(), (da, db))


@test
def same_get_at_the_same_moment_downloads_once():
    srv, box = Server(size=4, rate=1), Sandbox()
    runs = [subprocess.Popen([*IDM, "get", srv.url(), "-d", str(box.dl)], env=box.env,
                             stdout=subprocess.PIPE, text=True) for _ in range(3)]
    replies = [json.loads(p.communicate(timeout=60)[0]) for p in runs]
    check("three `idm get`s of one link at once: one job, one file, all three done",
          {r["job"] for r in replies} == {replies[0]["job"]} and box.files() == ["halo test.bin"]
          and all(r["state"] == "done" for r in replies), (replies, box.files()))


@test
def an_idm_folder_here_is_never_run():
    srv, box = Server(size=1), Sandbox()
    planted = box.root / "work" / "idm"
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("")
    (planted / "__main__.py").write_text(f"open({str(box.root / 'PLANTED')!r}, 'w')")
    p = subprocess.run([*IDM, "get", srv.url(), "-d", str(box.dl)], env=box.env, cwd=planted.parent,
                       capture_output=True, text=True, timeout=60)
    check("an idm/ folder in the current folder is not what runs the download",
          not (box.root / "PLANTED").exists() and '"done"' in p.stdout, p.stdout)


@test
def a_login_stays_with_its_own_server():
    srv, box = Server(), Sandbox()
    login = ("--header", "Authorization: Bearer ok")
    away = srv.url("/redirect?to=" + srv.url("/secret.bin").replace("127.0.0.1", "localhost"))
    code, r = box.get(away, *login)
    check("a redirect to another host: the Authorization header is not sent there",
          r.get("error", {}).get("code") == "AUTH_REQUIRED", r)
    code, r = box.get(srv.url("/redirect?to=" + srv.url("/secret.bin")), *login, "-o", "x.bin")
    check("a redirect on the same host: it still is", r.get("state") == "done", r)


@test
def empty_file():
    for flags in ((), ("--no-ranges",)):
        srv, box = Server(*flags, size=0), Sandbox()
        code, r = box.get(srv.url())
        check(f"an empty file{' (no ranges)' if flags else ''}: done, one file of 0 bytes",
              code == 0 and r.get("bytes") == 0 and len(box.files()) == 1
              and os.path.getsize(r["file"]) == 0, (r, box.files()))


@test
def link_with_a_space():
    srv, box = Server(), Sandbox()
    started = time.time()
    code, r = box.get(srv.url("/files/my report.pdf"))
    check("a link with a space: encoded and asked for (here a 404), not retried as a network error",
          r.get("error", {}).get("code") == "NOT_FOUND" and time.time() - started < 10, r)


@test
def cancel_keeps_a_finished_file():
    srv, box = Server(size=1), Sandbox()
    _, r = box.get(srv.url())
    code, c = box.idm("cancel", r["job"])
    check("cancel on a finished download: nothing deleted, still done",
          c.get("state") == "done" and box.files() == ["halo test.bin"], (c, box.files()))


# ---- halo, the face (phase 4) ------------------------------------------------------------------------
TRUE_COLOUR = {"COLORTERM": "truecolor"}
FACE_COLOUR = "\x1b[48;2;180;156;255m"                 # halo.FACE as a background


def ring_dots(pic):
    """How many of the ring's dots are home (drawn as ● in the ring colour)."""
    return sum(1 for row in pic.cells[:halo.H] for char, fg, _, _ in row
               if char == "●" and fg == halo.RING)


def words(pic):
    return ["".join(cell[0] for cell in row) for row in pic.cells]


@test
def halo_draws_every_state():
    base = {"state": "downloading", "bytes": 50, "size": 100, "speed": 10, "eta": 5,
            "connections": 8, "file": "/tmp/a.zip", "job": "ab12cd"}
    states = {
        "downloading": (base, "●    ●", 16),
        "server pushing back": ({**base, "trouble": {"code": "RATE_LIMITED", "retry_in": 4,
                                                     "message": "the server said 429"}}, "╥    ╥", 16),
        "paused": ({**base, "state": "paused"}, "-    -", 16),
        "done": ({**base, "state": "done", "bytes": 100, "started": 0, "finished": 2}, "^    ^", 32),
        "failed": ({**base, "state": "failed", "error": {"message": "the server said 404",
                                                         "hint": "Wrong link. Find another."}},
                   "╥    ╥", 16),
        "starting": ({"state": "queued", "url": "https://x.org/a.zip"}, "●    ●", 0),
        "connecting": ({**base, "bytes": 0}, "●    ●", 0),
        "size unknown": ({**base, "size": None, "bytes": 3_000_000}, "●    ●", 0),
    }
    for name, (st, eyes, dots) in states.items():
        pic = halo.Halo().draw(st, 100.0, settle=True)
        text = "\n".join(words(pic))
        cells = base64.b64decode(pic.packed())
        chars = {cell[0] for row in pic.cells for cell in row}
        one_wide = all(unicodedata.east_asian_width(c) not in "WF" and unicodedata.category(c)[0]
                       not in "CM" and ord(c) < 0x10000 for c in chars)
        check(f"halo, {name}: {eyes.split()[0]} eyes, {dots} dots home, {halo.COLUMNS}x{halo.ROWS}"
              " cells that Claude Code can draw",
              eyes in text and ring_dots(pic) == dots and (pic.columns, pic.rows) == (56, 20)
              and len(cells) == 56 * 20 * 12 and one_wide, (text, ring_dots(pic)))
    paused = "\n".join(words(halo.Halo().draw(states["paused"][0], 0)))
    check("halo, paused: says how to carry on", "idm resume ab12cd" in paused, paused)
    connecting = "\n".join(words(halo.Halo().draw(states["connecting"][0], 0)))
    check("halo, connecting: a curious look (o) and 'starting…' until the first bytes",
          "o" in connecting.splitlines()[halo.CY] and "starting…" in connecting, connecting)


@test
def halo_dots_fly_home_one_by_one():
    face, st = halo.Halo(), {"state": "downloading", "bytes": 0, "size": 100}
    face.draw(st, 0.0)
    flying = face.draw({**st, "bytes": 25}, 1.0)                # 8 dots' worth just came in
    landed = face.draw({**st, "bytes": 25}, 1.0 + halo.FLY)
    check("halo: a dot flies in when its 1/32 of the file arrives, and lands in 0.8 s",
          ring_dots(flying) == 0 and ring_dots(landed) == 8, (ring_dots(flying), ring_dots(landed)))


@test
def watch_streams_cells_until_done():
    srv, box = Server(size=6, rate=0.4), Sandbox()
    _, r = box.start(srv.url())
    started = time.time()
    p = subprocess.run([*IDM, "watch", r["job"], "--cells"], env=box.env, capture_output=True,
                       text=True, timeout=60)
    frames = [json.loads(line) for line in p.stdout.splitlines()]
    took = time.time() - started
    sizes = {len(base64.b64decode(f["cells"])) for f in frames}
    check("watch --cells: one JSON frame per line, about 10 a second, the right size each",
          p.returncode == 0 and sizes == {56 * 20 * 12} and len(frames) >= 7 * took,
          (p.returncode, sizes, len(frames), took, p.stderr[-300:]))
    states = [f["state"] for f in frames]
    check("watch --cells: follows it to the end, and stops there with 'done' at 100%",
          "downloading" in states and states[-1] == "done" and frames[-1]["percent"] == 100
          and "halo test.bin" in frames[-1]["label"], (states[::10], frames[-1]["label"]))


@test
def watch_finds_a_link_that_is_about_to_start():
    srv, box = Server(size=4, rate=0.5), Sandbox()
    watcher = subprocess.Popen([*IDM, "watch", srv.url(), "--cells"], env=box.env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1)                                    # the mod starts it as the agent's command starts
    box.start(srv.url())
    out, _ = watcher.communicate(timeout=60)
    last = json.loads(out.splitlines()[-1])
    check("watch LINK --cells: waits for that link's download to start, then follows it",
          watcher.returncode == 0 and last["state"] == "done", out[-300:])


@test
def watch_for_an_agent_replies_like_wait():
    srv, box = Server(size=1), Sandbox()
    _, r = box.start(srv.url())
    code, reply = box.idm("watch", r["job"])
    code2, missing = box.idm("watch", "ffffff")
    check("watch, run by an agent: one JSON line like `wait`; an unknown id is NO_SUCH_JOB",
          code == 0 and reply.get("state") == "done" and code2 == 1
          and missing.get("error", {}).get("code") == "NO_SUCH_JOB", (reply, missing))


@test
def person_sees_halo():
    srv, box = Server(size=4, rate=1), Sandbox(**TRUE_COLOUR)
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env)
    redraws = shown.count(f"\x1b[{halo.ROWS + 1}F")             # the face, and the keys under it
    check("in a big true-colour terminal: halo is drawn and redrawn in place, ends happy, then ✓",
          code == 0 and FACE_COLOUR in shown and redraws > 5 and "▽" in shown     # ▽: joy
          and "✓ saved" in shown, (code, redraws, shown[-300:]))


@test
def person_ctrl_c_with_halo_pauses():
    srv, box = Server(size=8, rate=0.5), Sandbox(**TRUE_COLOUR)
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env, interrupt_after=1.5)
    jobs = box.idm("list")[1].get("jobs", [])
    check("Ctrl+C with halo showing: the download pauses, the face says how to carry on",
          "carry on: idm resume" in shown and jobs and jobs[0]["state"] == "paused",
          (shown[-300:], jobs))


@test
def small_terminal_gets_the_line():
    srv, box = Server(size=2, rate=2), Sandbox(**TRUE_COLOUR)
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env, size=(24, 50))
    check("in a terminal too small for halo: the one progress line instead",
          code == 0 and FACE_COLOUR not in shown and "MB/s" in shown and "✓ saved" in shown,
          shown[-300:])


@test
def watch_follows_every_download():
    srv, box = Server(size=2, rate=1), Sandbox(**TRUE_COLOUR)

    def two_downloads():
        time.sleep(1)
        box.get(srv.url(), "-o", "first.bin")
        box.get(srv.url(), "-o", "second.bin")
    agent = threading.Thread(target=two_downloads)
    agent.start()
    code, shown = in_terminal(["watch"], box.env, interrupt_after=9)   # (each takes about 2.5 s)
    agent.join()
    check("watch (no id): waits, shows each download as it comes, until Ctrl+C",
          code == 0 and "watching for downloads" in shown and shown.count("✓ saved") == 2
          and "first.bin" in shown and "second.bin" in shown, shown[-400:])


# ---- phase 6: when, in what order, how fast ---------------------------------------------------
@test
def scheduled_job_starts_at_its_time():
    srv, box = Server(), Sandbox()
    t0 = time.time()
    code, r = box.start(srv.url(), "--in", "3s")
    job = r.get("job", "?")
    check("--in 3s: scheduled, says when it starts, and no `next` (nothing to do till then)",
          code == 0 and r.get("state") == "scheduled" and r.get("starts_at") and r.get("starts_in")
          and "next" not in r, r)
    code, w = box.idm("wait", job, "--max", "20")
    check("wait on a scheduled job replies at once, still scheduled",
          w.get("state") == "scheduled" and time.time() - t0 < 2.5, w)
    done = wait_until(lambda: box.status_file(job).get("state") == "done", timeout=20)
    started = box.status_file(job).get("started", 0) - t0
    check("it starts at its time, not before, and saves the right file",
          done and 2.8 <= started <= 6 and sha(box.dl / "halo test.bin") == srv.sha(),
          f"started after {started:.1f}s")


@test
def get_at_a_clock_time_replies_at_once_and_resume_now_starts_it():
    srv, box = Server(), Sandbox()
    later = (datetime.datetime.now() + datetime.timedelta(hours=1)).strftime("%H:%M")
    t0 = time.time()
    code, r = box.get(srv.url(), "--at", later)
    job = r.get("job", "?")
    check("get --at: replies at once (it doesn't wait an hour), scheduled for that time",
          code == 0 and r.get("state") == "scheduled" and time.time() - t0 < 5
          and (r.get("starts_at") or "")[11:16] == later, r)
    code, now = box.idm("resume", job, "--now")
    check("resume --now: it starts now", now.get("state") in ("queued", "downloading"), now)
    code, w = box.idm("wait", job)
    check("...and downloads", w.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), w)


@test
def pause_keeps_the_schedule():
    srv, box = Server(), Sandbox()
    code, r = box.start(srv.url(), "--in", "1h")
    job = r.get("job", "?")
    code, p = box.idm("pause", job)
    check("a scheduled download can be paused", p.get("state") == "paused", p)
    code, q = box.idm("resume", job)
    check("resume (without --now) keeps its time",
          q.get("state") == "scheduled" and q.get("starts_at") == r.get("starts_at"), (r, q))


@test
def bad_times_and_speeds():
    srv, box = Server(), Sandbox()
    for args, what in ((["--in", "soon"], "--in soon"), (["--at", "25:00"], "--at 25:00"),
                       (["--at", "02:00", "--in", "1h"], "--at and --in together"),
                       (["--at", "2020-01-01 10:00"], "a time that has passed"),
                       (["--limit", "fast"], "--limit fast"), (["--limit", "10"], "--limit under 1K")):
        code, r = box.start(srv.url(), *args)
        check(f"{what}: BAD_INPUT, exit 2", code == 2 and r.get("error", {}).get("code") == "BAD_INPUT", r)
    code, r = box.start(srv.url(), "--after", "abc123")
    check("--after a download that doesn't exist: NO_SUCH_JOB",
          r.get("error", {}).get("code") == "NO_SUCH_JOB", r)
    check("none of them left a job behind", box.idm("list")[1].get("jobs") == [])
    check("times and speeds read as people write them",
          when.seconds_in("1h30m") == 5400 and when.rate("1.5M") == 1_500_000
          and when.rate("off") is None and when.how_long(5400) == "1h 30m"
          and 0 < when.clock_time("02:00") - time.time() <= 86400)


@test
def after_waits_for_the_other_download():
    srv, box = Server(size=2, rate=1), Sandbox()
    code, a = box.start(srv.url(), "--connections", "1")
    code, b = box.start(srv.url("/test.bin?copy=2"), "--after", a.get("job", "?"))
    ja, jb = a.get("job", "?"), b.get("job", "?")
    waiting = wait_until(lambda: f"download {ja}" in (box.status_file(jb).get("waiting_for") or ""), 5)
    check("--after: queued, waiting for that download to end", waiting, box.status_file(jb))
    code, w = box.idm("wait", jb, "--max", "30")
    sa, sb = box.status_file(ja), box.status_file(jb)
    check("it started only once the first had ended; both files are right",
          w.get("state") == "done" and sb.get("started", 0) >= sa.get("finished", 1e12)
          and all(sha(box.dl / f) == srv.sha() for f in box.files()) and len(box.files()) == 2,
          (sa.get("finished"), sb.get("started"), box.files()))


@test
def at_most_n_downloads_at_once():
    srv, box = Server(size=1.5, rate=1), Sandbox(IDM_MAX_DOWNLOADS="2")
    ids = [box.start(srv.url(f"/test.bin?n={n}"), "--connections", "1")[1].get("job", "?")
           for n in range(3)]
    peak, waited, deadline = 0, False, time.time() + 30
    while time.time() < deadline:
        states = [box.status_file(j) for j in ids]
        peak = max(peak, sum(st.get("state") == "downloading" for st in states))
        waited = waited or any("a free place" in (st.get("waiting_for") or "") for st in states)
        if all(st.get("state") == "done" for st in states):
            break
        time.sleep(0.05)
    check("IDM_MAX_DOWNLOADS=2: never more than 2 at once, and the third waited for a place",
          peak == 2 and waited, f"peak {peak}, waited {waited}")
    check("all three finished", all(box.status_file(j).get("state") == "done" for j in ids))


@test
def first_come_first_served():
    srv, box = Server(size=1, rate=1), Sandbox(IDM_MAX_DOWNLOADS="1")
    ids = []
    for n in range(3):
        ids.append(box.start(srv.url(f"/test.bin?n={n}"), "--connections", "1")[1].get("job", "?"))
        time.sleep(0.4)
    wait_until(lambda: all(box.status_file(j).get("state") == "done" for j in ids), 30)
    sts = [box.status_file(j) for j in ids]
    starts = [st.get("started", 0) for st in sts]
    one_at_a_time = all(sts[i + 1].get("started", 0) >= sts[i].get("finished", 1e12) for i in range(2))
    check("one place: one at a time, in the order they were started",
          all(starts) and starts == sorted(starts) and one_at_a_time, starts)


@test
def speed_limit():
    srv, box = Server(size=2, rate=20), Sandbox()
    t0 = time.time()
    code, r = box.get(srv.url(), "--limit", "500K")
    took = time.time() - t0
    check("--limit 500K: 2 MB takes about 4 s (not a fraction of one), and the bytes are right",
          r.get("state") == "done" and 3.3 <= took <= 9 and sha(box.dl / "halo test.bin") == srv.sha(),
          f"{took:.1f}s {r}")


@test
def limit_changes_while_it_runs():
    srv, box = Server(size=3, rate=20), Sandbox()
    code, r = box.start(srv.url(), "--limit", "200K")
    job = r.get("job", "?")
    check("start --limit: the reply says the limit", r.get("limit") == 200_000, r)
    time.sleep(2.5)
    st = box.status_file(job)
    check("it keeps to the limit (about 200 KB/s)", 100_000 <= (st.get("speed") or 0) <= 300_000, st)
    code, bad = box.idm("limit", job, "fast")
    check("idm limit ID fast: BAD_INPUT, exit 2", code == 2, bad)
    code, off = box.idm("limit", job, "0")
    check("idm limit ID 0: no limit any more", code == 0 and "limit" not in off, off)
    code, w = box.idm("wait", job, "--max", "15")
    check("...and it finishes fast", w.get("state") == "done" and w.get("seconds", 99) < 7, w)


# ---- phase 6: servers that go silent --------------------------------------------------------------
@test
def a_silent_server_is_shown_as_stalled_then_given_up_on():
    srv = Server("--stall", "0.5", "--silent", "3600", size=2, rate=1)
    box = Sandbox(IDM_READ_TIMEOUT="1", IDM_PATIENCE="8")
    code, r = box.start(srv.url(), "--connections", "2")
    job = r.get("job", "?")
    stalled = wait_until(lambda: (box.status_file(job).get("stalled") or 0) >= 5, 15)
    st = box.status_file(job)
    check("while nothing arrives: speed 0, no time left, `stalled` counts the seconds",
          stalled and st.get("speed") == 0 and st.get("eta") is None and st.get("trouble"), st)
    code, s = box.idm("status", job)
    check("...and the agent's reply says so", (s.get("stalled") or 0) >= 5 and s.get("speed") == 0, s)
    code, w = box.idm("wait", job, "--max", "30")
    e = w.get("error", {})
    check("after IDM_PATIENCE of silence: NETWORK, 'nothing arrived for', exit 3, next resumes",
          code == 3 and e.get("code") == "NETWORK" and "nothing arrived for" in e.get("message", "")
          and w.get("next") == f"idm resume {job}", w)


@test
def a_server_silent_for_a_while_is_waited_out():
    srv = Server("--stall", "0.5", "--silent", "3", size=2, rate=1)
    box = Sandbox(IDM_READ_TIMEOUT="1", IDM_PATIENCE="20")
    code, r = box.get(srv.url(), "--max", "40")
    check("a server that goes quiet for 3 s, then comes back: the download carries on by itself",
          r.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), r)


@test
def a_server_that_never_answers_is_given_up_on_in_seconds():
    box = Sandbox(IDM_CONNECT_TIMEOUT="1", IDM_PATIENCE="3")
    t0 = time.time()
    code, r = box.get("http://192.0.2.1/file.bin", "--max", "60")     # (an address nobody has)
    took = time.time() - t0
    check("a server that never answers: NETWORK in seconds, not minutes",
          code == 3 and r.get("error", {}).get("code") == "NETWORK" and took < 15, f"{took:.1f}s {r}")


@test
def halo_says_when_and_what_it_waits_for():
    def lines(**st):
        return [text for text, _ in halo.caption({"url": "http://x/a.zip", **st})]
    later = lines(state="scheduled", starts_at=time.time() + 5400)
    check("scheduled: when it starts, and how long till then",
          later[1].startswith("starts at") and later[2] in ("in 1h 29m", "in 1h 30m"), later)
    queued = lines(state="queued", waiting_for="download ab12cd to end")
    check("queued: what it waits for", queued[1:] == ["waiting for", "download ab12cd to end"], queued)
    stuck = lines(state="downloading", bytes=5, size=10, speed=0, stalled=12,
                  trouble={"code": "NETWORK", "message": "timed out", "retry_in": 2})
    check("stalled: how long nothing has come", stuck[2] == "nothing for 12s · timed out · retrying in 2s", stuck)


# ---- phase 7: videos (YouTube, reels...), with a stand-in for yt-dlp ----------------------------
FAKE_YTDLP = str(HERE / "fake_ytdlp.py")
CLIP = "Test clip [ok].mp4"


def clip_sha(audio_only=False):
    """What the stand-in's video is, byte for byte: its picture stream, then its sound."""
    streams = ((1_000_000, 241),) if audio_only else ((2_000_000, 251), (1_000_000, 241))
    whole = b"".join((bytes(i % p for i in range(p)) * (n // p + 1))[:n] for n, p in streams)
    return hashlib.sha256(whole).hexdigest()


@test
def youtube_link_downloads_through_ytdlp():
    box = Sandbox(IDM_YT_DLP=FAKE_YTDLP)
    code, r = box.get("https://www.youtube.com/watch?v=ok")
    f = box.dl / CLIP
    check("a YouTube link: done, saved under the video's title", code == 0 and r.get("state") == "done"
          and r.get("file") == str(f), r)
    check("its picture and sound, joined, byte for byte, and nothing else left",
          f.exists() and sha(f) == clip_sha() and box.files() == [CLIP], box.files())
    st = box.status_file(r.get("job", "?"))
    check("the status has the video's size (its streams together), for halo's percent",
          st.get("size") == 3_000_000, st)


@test
def reels_and_other_video_sites():
    box = Sandbox(IDM_YT_DLP=FAKE_YTDLP)
    code, r = box.get("https://www.instagram.com/reel/ok/")
    check("an Instagram reel goes through yt-dlp too", r.get("state") == "done", r)
    code, r = box.get("https://youtu.be/ok", "--audio", "--again")
    check("--audio: the sound alone", r.get("state") == "done" and r.get("file", "").endswith(".m4a")
          and sha(Path(r["file"])) == clip_sha(audio_only=True), r)
    srv = Server()
    code, r = box.get(srv.url(), "-o", "plain.bin")
    check("an ordinary file still uses idm's own connections", r.get("state") == "done"
          and sha(box.dl / "plain.bin") == srv.sha(), r)


@test
def video_errors_have_codes():
    box = Sandbox(IDM_YT_DLP=FAKE_YTDLP)
    for vid, want, exit_code in (("unavailable", "NOT_FOUND", 1), ("private", "AUTH_REQUIRED", 1),
                                 ("busy", "RATE_LIMITED", 3)):
        code, r = box.get(f"https://www.youtube.com/watch?v={vid}")
        check(f"yt-dlp says {vid}: {want}, exit {exit_code}",
              code == exit_code and r.get("error", {}).get("code") == want, r)
        if want == "AUTH_REQUIRED":
            check("...and `next` asks for the browser's cookies", "--cookies" in r.get("next", ""), r)
    code, r = box.get("https://www.youtube.com/playlist?list=playlist")
    e = r.get("error", {})
    check("a playlist: NOT_A_FILE, with its videos' links to pick from",
          e.get("code") == "NOT_A_FILE" and len(e.get("links", [])) == 3, r)


@test
def no_ytdlp_says_how_to_get_it():
    box = Sandbox(IDM_YT_DLP="/nowhere/yt-dlp")
    code, r = box.get("https://youtu.be/ok")
    e = r.get("error", {})
    check("no yt-dlp: NEEDS_TOOL, the hint says how to install it, `next` carries on after",
          e.get("code") == "NEEDS_TOOL" and "install yt-dlp" in e.get("hint", "")
          and r.get("next") == f"idm resume {r.get('job')}", r)


@test
def video_pause_resume_and_cancel():
    log = Path(tempfile.mkdtemp()) / "fake.log"
    box = Sandbox(IDM_YT_DLP=FAKE_YTDLP, IDM_FAKE_RATE="0.5", IDM_FAKE_LOG=str(log))
    code, r = box.start("https://youtu.be/ok")
    job = r.get("job", "?")
    wait_until(lambda: (box.status_file(job).get("bytes") or 0) > 600_000, 15)
    code, p = box.idm("pause", job)
    check("pause a video: paused, part-way, its .part kept",
          p.get("state") == "paused" and 0 < p.get("percent", 0) < 100
          and any(f.endswith(".part") for f in box.files()), (p, box.files()))
    code, q = box.idm("resume", job)
    code, w = box.idm("wait", job, "--max", "60")
    starts = log.read_text().splitlines()
    check("resume: it carries on from where it was (not from 0), and the file is right",
          w.get("state") == "done" and sha(box.dl / CLIP) == clip_sha()
          and starts[0] == "f1.mp4 from 0" and int(starts[1].split()[-1]) > 0, starts)
    code, r = box.start("https://youtu.be/two")
    job = r.get("job", "?")
    wait_until(lambda: (box.status_file(job).get("bytes") or 0) > 300_000, 15)
    code, c = box.idm("cancel", job)
    check("cancel a video: cancelled, and its streams' .part files are gone",
          c.get("state") == "cancelled" and box.files() == [CLIP], box.files())


@test
def video_limit_changes_while_it_runs():
    box = Sandbox(IDM_YT_DLP=FAKE_YTDLP, IDM_FAKE_RATE="20")
    code, r = box.start("https://youtu.be/ok", "--limit", "300K")
    job = r.get("job", "?")
    time.sleep(2)
    slow = box.status_file(job)
    code, off = box.idm("limit", job, "0")
    code, w = box.idm("wait", job, "--max", "30")
    check("--limit holds a video back, and idm limit ID 0 lets it go (yt-dlp restarted, carrying on)",
          (slow.get("bytes") or 0) < 1_200_000 and w.get("state") == "done"
          and sha(box.dl / CLIP) == clip_sha(), (slow.get("bytes"), w))


# ---- phase 7: keys for a person watching -------------------------------------------------------
@test
def person_space_pauses_and_resumes():
    srv, box = Server(size=3, rate=0.5), Sandbox()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl), "--connections", "1"], box.env,
                              keys=((1.5, " "), (4, " ")))
    check("space pauses (it says how to resume), space again resumes, and it finishes",
          code == 0 and "space resume" in shown and "✓ saved" in shown
          and sha(box.dl / "halo test.bin") == srv.sha(), shown[-300:])


@test
def person_q_leaves_it_running():
    srv, box = Server(size=3, rate=0.5), Sandbox()
    t0 = time.time()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl), "--connections", "1"], box.env,
                              keys=((1, "q"),))
    check("q: stops watching at once, and says the download carries on",
          code == 0 and time.time() - t0 < 5 and "in the background" in shown, shown[-300:])
    job = next((box.root / "home" / "jobs").iterdir()).name
    check("...and it does carry on, to the end",
          wait_until(lambda: box.status_file(job).get("state") == "done", 20))


@test
def person_sees_the_keys_under_halo():
    srv, box = Server(size=3, rate=0.5), Sandbox(**TRUE_COLOUR)
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env, keys=((1, "q"),))
    check("the keys are written under the face", "space pause" in shown, shown[-300:])

# ---- phase 8: share links (Drive, Dropbox, OneDrive, GitHub, Hugging Face) ------------------------
def drive_file(file_id, extra=""):
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t{extra}"


class FakeSite:
    """An opener that answers like the sites do, without the internet: the first `replies`
    key a request's link starts with gives the reply (its body, and where it ended up)."""

    def __init__(self, replies):
        self.replies, self.asked = replies, []

    def open(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.asked.append(req)
        body, landed = next(reply for start, reply in self.replies.items() if url.startswith(start))
        return FakeReply(body, landed or url)


class FakeReply(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body.encode())
        self.url = url

    def geturl(self):
        return self.url


def shared(url, site=None):
    """What idm downloads for this link: (the link, the name), or the IDMError it raises."""
    try:
        job = shares.the_file(site, jobs.Job(id="t", url=url, folder="/tmp"))
        return job.url, job.name
    except IDMError as e:
        return e


def failed_with(e, code):
    return isinstance(e, IDMError) and e.code == code


@test
def share_links_point_at_the_file():
    cases = {
        "https://drive.google.com/file/d/1AbC_d-9/view?usp=sharing": (drive_file("1AbC_d-9"), None),
        "https://drive.google.com/open?id=1AbC": (drive_file("1AbC"), None),
        "https://drive.google.com/uc?id=1AbC&export=download": (drive_file("1AbC"), None),
        "https://drive.google.com/file/d/1AbC/view?resourcekey=0-k": (drive_file("1AbC", "&resourcekey=0-k"), None),
        "https://docs.google.com/document/d/1Doc/edit": ("https://docs.google.com/document/d/1Doc/export?format=docx", None),
        "https://docs.google.com/spreadsheets/d/1Sh/edit#gid=0": ("https://docs.google.com/spreadsheets/d/1Sh/export?format=xlsx", None),
        "https://docs.google.com/presentation/d/1Sl/edit": ("https://docs.google.com/presentation/d/1Sl/export?format=pptx", None),
        "https://www.dropbox.com/scl/fi/abc/data%20set.zip?rlkey=k1&dl=0":
            ("https://www.dropbox.com/scl/fi/abc/data%20set.zip?rlkey=k1&dl=1", "data set.zip"),
        "https://www.dropbox.com/s/abc/figure.zip?dl=0": ("https://www.dropbox.com/s/abc/figure.zip?dl=1", "figure.zip"),
        "https://github.com/o/r/blob/main/data/a.zip": ("https://github.com/o/r/raw/main/data/a.zip", None),
        "https://huggingface.co/o/m/blob/main/model.safetensors": ("https://huggingface.co/o/m/resolve/main/model.safetensors", None),
        "https://huggingface.co/datasets/o/d/blob/main/train.parquet": ("https://huggingface.co/datasets/o/d/resolve/main/train.parquet", None),
        "https://example.com/a.zip": ("https://example.com/a.zip", None),
        "https://docs.google.com/document/u/0/d/1Doc/edit": ("https://docs.google.com/document/d/1Doc/export?format=docx", None),
        "https://docs.google.com/uc?export=download&id=1AbC": (drive_file("1AbC"), None),
        "https://huggingface.co/gpt2/blob/main/config.json": ("https://huggingface.co/gpt2/resolve/main/config.json", None),
        # links that already give a file stay as they are
        "https://docs.google.com/spreadsheets/d/e/2PACX-1v/pub?output=csv": ("https://docs.google.com/spreadsheets/d/e/2PACX-1v/pub?output=csv", None),
        "https://docs.google.com/spreadsheets/d/1Sh/export?format=csv&id=1Sh&gid=0": ("https://docs.google.com/spreadsheets/d/1Sh/export?format=csv&id=1Sh&gid=0", None),
        "https://docs.google.com/spreadsheets/d/1Sh/gviz/tq?tqx=out:csv": ("https://docs.google.com/spreadsheets/d/1Sh/gviz/tq?tqx=out:csv", None),
    }
    wrong = {link: shared(link) for link, want in cases.items() if shared(link) != want}
    check("Drive, Docs, Dropbox, GitHub and Hugging Face links point at the file itself "
          "(other links stay as they are)", not wrong, wrong)
    e = shared("https://www.dropbox.com/scl/fi/abc/data.zip?dl=0")
    check("a Dropbox link that lost its rlkey: BAD_URL, saying so",
          failed_with(e, "BAD_URL") and "rlkey" in str(e), e)


@test
def a_drive_folder_lists_its_files():
    page = ('<div class="flip-entry"><a href="https://drive.google.com/file/d/1One/view?usp=drive_web">a.zip</a></div>'
            '<div class="flip-entry"><a href="https://drive.google.com/file/d/1Two/view?usp=drive_web">b.zip</a></div>'
            '<div class="flip-entry"><a href="https://drive.google.com/drive/folders/1Sub">more</a></div>')
    e = shared("https://drive.google.com/drive/folders/1Folder?usp=sharing",
               FakeSite({"https://drive.google.com/embeddedfolderview?id=1Folder": (page, None)}))
    check("a Drive folder: NOT_A_FILE, with the links of what's in it",
          failed_with(e, "NOT_A_FILE") and e.more.get("links") == [
              "https://drive.google.com/file/d/1One", "https://drive.google.com/file/d/1Two",
              "https://drive.google.com/drive/folders/1Sub"], e)
    e = shared("https://drive.google.com/drive/folders/1Private",
               FakeSite({"https://drive.google.com/": ("<form>Sign in</form>", "https://accounts.google.com/v3/signin")}))
    check("a folder that isn't shared with everyone: AUTH_REQUIRED", failed_with(e, "AUTH_REQUIRED"), e)


@test
def a_onedrive_link_gets_a_guest_pass():
    link = "https://1drv.ms/u/s!AkSx?e=cUG"
    item = {"name": "weights.pth", "@content.downloadUrl": "https://my.microsoftpersonalcontent.com/download.aspx?tempauth=x"}
    site = FakeSite({shares.ONEDRIVE_PASS: ('{"token": "guest-1"}', None),
                     "https://my.microsoftpersonalcontent.com/": (json.dumps(item), None)})
    got = shared(link, site)
    share_id = base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
    asked = site.asked[-1]
    check("OneDrive: a guest pass, then the file's own link and name", got == (
        item["@content.downloadUrl"], "weights.pth") and f"/shares/u!{share_id}/" in asked.full_url
        and asked.get_header("Authorization") == "Badger guest-1", (got, asked.full_url))
    site.replies["https://my.microsoftpersonalcontent.com/"] = ('{"name": "Photos", "folder": {}}', None)
    check("a OneDrive folder: NOT_A_FILE", failed_with(shared(link, site), "NOT_A_FILE"))


@test
def a_share_link_resumes_though_its_file_link_changes():
    srv, box = Server(size=4), Sandbox()
    box.dl.mkdir(parents=True)
    given, passes = "https://share.example/s/abc", []

    def share_site(opener, url):                      # like OneDrive: a new pass, a new link, each time
        passes.append(len(passes) + 1)
        return srv.url(f"/test.bin?pass={passes[-1]}"), "shared.bin"

    pattern = bytes((i + 1) % 251 for i in range(251))
    (box.dl / "shared.bin.part").write_bytes((pattern * 4000)[:1_000_000])     # from last time
    (box.dl / "shared.bin.part.json").write_text(json.dumps(
        {"url": given, "size": 4_000_000, "fingerprint": '"test-v1"'}))

    class Seen(engine.Report):
        def leftovers(self, why_discarded, have):
            self.kept = (why_discarded, have)

    seen, sites = Seen(), shares.SITES
    shares.SITES = (*sites, ("share.example", share_site))
    try:
        saved = engine.download(jobs.Job(id="t", url=given, folder=str(box.dl), connections=4), seen)
    finally:
        shares.SITES = sites
    check("a share link whose file link changes each time (OneDrive's): the bytes from last "
          "time are kept (they belong to the link as given), and the file is right",
          seen.kept == (None, 1_000_000) and saved == str(box.dl / "shared.bin")
          and sha(box.dl / "shared.bin") == srv.sha(), (getattr(seen, "kept", None), saved))


@test
def a_link_that_ends_at_a_sign_in_page():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url("/redirect?to=" + srv.url("/login")))
    check("redirected to a sign-in page: AUTH_REQUIRED (not just a web page)",
          code != 0 and r.get("error", {}).get("code") == "AUTH_REQUIRED", r)


@test
def the_servers_full_file_name_wins():
    from email.message import Message

    def named(header):
        reply = FakeReply("", "https://x.org/download?id=1")
        reply.headers = Message()
        reply.headers["Content-Disposition"] = header
        return engine.pick_name(reply)

    full = "attachment; filename=\"ExampleSheet.xlsx\"; filename*=UTF-8''Example%20Sheet.xlsx"
    check("filename*= (spaces, any language) wins over the plain filename= for old browsers",
          named(full) == "Example Sheet.xlsx" and named("attachment; filename*=UTF-8''caf%C3%A9.pdf")
          == "café.pdf" and named('attachment; filename="plain.zip"') == "plain.zip",
          [named(full)])


# ---- what a review of the whole code found ------------------------------------------------------
def within(seconds, fn, *args):
    """fn(*args), or TimeoutError if it takes longer than `seconds` (it would never end)."""
    def too_long(*_):
        raise TimeoutError(f"{fn.__name__} took over {seconds} s")
    old = signal.signal(signal.SIGALRM, too_long)
    signal.alarm(seconds)
    try:
        return fn(*args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@test
def any_name_is_made_safe():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0." + "s" * 342       # a link that ends in a JWT
    name = within(5, engine.safe_name, token)
    check("a long name with dots in it: cut to size (not an endless loop)",
          len(name.encode()) <= 200 and name.startswith("eyJ"), name)
    names = [engine.safe_name(n) for n in ("../../escaped.sh", "/tmp/abs.sh", "..", "")]
    check("a name can't point outside the folder, or be hidden, or be empty",
          all("/" not in n and not n.startswith(".") and n for n in names), names)


@test
def a_share_links_name_stays_in_the_folder():
    srv, box = Server(size=1), Sandbox()
    box.dl.mkdir(parents=True)
    sites = shares.SITES
    shares.SITES = (*sites, ("share.example", lambda opener, url: (srv.url(), "../../escaped.bin")))
    try:
        saved = engine.download(jobs.Job(id="t", url="https://share.example/s/x", folder=str(box.dl)),
                                engine.Report())
    finally:
        shares.SITES = sites
    check("a share link named '../../escaped.bin': saved inside the download folder",
          Path(saved).parent == box.dl and sha(saved) == srv.sha(), saved)


@test
def an_empty_file_on_nginx():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url("/empty-416.bin"))
    check("an empty file whose 416 carries an error page (nginx): done, 0 bytes, nothing left over",
          code == 0 and r.get("bytes") == 0 and box.files() == ["empty-416.bin"], (r, box.files()))


@test
def a_resume_carries_on_under_its_earlier_name():
    srv, box = Server(size=4), Sandbox()
    box.dl.mkdir(parents=True)
    pattern = bytes((i + 1) % 251 for i in range(251))
    (box.dl / "halo test (1).bin.part").write_bytes((pattern * 4000)[:1_000_000])   # last time,
    (box.dl / "halo test (1).bin.part.json").write_text(json.dumps(                  # its name was
        {"url": srv.url(), "size": 4_000_000, "fingerprint": '"test-v1"'}))          # taken

    class Seen(engine.Report):
        def leftovers(self, why_discarded, have):
            self.kept = (why_discarded, have)

    seen = Seen()
    saved = engine.download(jobs.Job(id="t", url=srv.url(), folder=str(box.dl)), seen)
    check("its name is free now, but the bytes from last time are under 'halo test (1).bin': "
          "kept, and the file finished there", seen.kept == (None, 1_000_000)
          and saved == str(box.dl / "halo test (1).bin") and sha(saved) == srv.sha()
          and box.files() == ["halo test (1).bin"], (getattr(seen, "kept", None), box.files()))


@test
def next_keeps_the_jobs_own_options():
    job = jobs.Job(id="a1", url="https://www.youtube.com/watch?v=x", folder="/tmp/dl", audio=True,
                   video=True, limit=1_000_000, headers={"Referer": "https://r.org/p"})
    step = cli.next_step(job, {"state": "failed", "error": {"code": "AUTH_REQUIRED"}})
    args = cli.parser(cli.Output(True)).parse_args(shlex.split(step.replace("<cookies.txt>", "c.txt"))[1:])
    check("AUTH_REQUIRED's next runs as it is, and keeps the job's own options (only the login "
          "is new)", args.audio and args.limit == "1000000" and args.header == ["Referer: https://r.org/p"]
          and args.cookies == "c.txt", step)
    page = jobs.Job(id="b2", url="https://x.org/page", folder="/tmp/dl", cookies="/c.txt",
                    headers={"Authorization": "Bearer t"}, limit=500_000)
    step = cli.next_step(page, {"state": "failed", "error": {"code": "NOT_A_FILE",
                                                             "links": ["https://cdn.org/f.zip"]}})
    check("NOT_A_FILE's next (the one file on the page) keeps the cookies and the speed limit, "
          "but not an Authorization header meant for the page's server",
          "--cookies /c.txt" in step and "--limit 500000" in step and "Authorization" not in step, step)


@test
def a_dead_jobs_old_pid_is_left_alone():
    box, saved_home = Sandbox(), jobs.HOME
    jobs.HOME = box.root / "home"
    other = subprocess.Popen(["sleep", "60"])            # a process that now has a job's old pid
    LEFTOVERS.append(other.kill)
    try:
        ended = jobs.new("https://x.org/a.zip", str(box.dl))
        ended.set_status(state="failed", pid=other.pid)
        jobs.stop(ended)
        jobs.cancel(ended)
        check("pause or cancel of a job that isn't running: the process with its old pid is "
              "left alone", other.poll() is None)
        crashed = jobs.new("https://x.org/b.zip", str(box.dl))
        engine.write_json(crashed.dir / "status.json", {"state": "downloading", "pid": other.pid,
                                                        "updated": time.time() - 60})
        st = crashed.status()
        jobs.stop(crashed)
        check("a 'downloading' job whose worker is gone and whose pid is someone else's now "
              "(after a crash or a restart): INTERRUPTED, and that process is left alone",
              st.get("state") == "failed" and st["error"]["code"] == "INTERRUPTED"
              and other.poll() is None, st)
    finally:
        jobs.HOME = saved_home


@test
def a_download_cant_wait_for_itself():
    srv, box = Server(), Sandbox()
    code, r = box.start(srv.url(), "--in", "1h")
    job = r.get("job", "?")
    box.idm("pause", job)
    code, r = box.start(srv.url(), "--after", job)
    check("--after its own id: BAD_INPUT, and the download stays as it was",
          code == 2 and r.get("error", {}).get("code") == "BAD_INPUT"
          and box.status_file(job)["state"] == "paused", r)


@test
def again_while_it_runs_is_that_run():
    srv, box = Server(size=4, rate=1), Sandbox()
    code, first = box.start(srv.url())
    code, again = box.start(srv.url(), "--again")
    check("--again while the same download is running: that one (never two at once into one "
          ".part)", again.get("job") == first.get("job") and again.get("state") == "downloading",
          (first, again))


@test
def halo_says_what_is_wrong_before_the_first_byte():
    lines = halo.caption({"state": "downloading", "file": "/dl/a.zip", "bytes": None,
                          "trouble": {"message": "the server said 429 Too Many Requests", "retry_in": 2}})
    check("a server that pushes back from the start: its complaint shows, not 'starting…'",
          any("429" in text for text, _ in lines), lines)


@test
def watch_stops_on_q_while_nothing_downloads():
    box = Sandbox(**TRUE_COLOUR)
    started = time.time()
    code, shown = in_terminal(["watch"], box.env, keys=((1, "q"),), timeout=10)
    check("watch (no id), nothing downloading yet: q stops it", code == 0
          and time.time() - started < 5 and "q or Ctrl+C" in shown, shown[-300:])


@test
def video_options_and_failures():
    busy = video.problem("ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable")
    check("a video site's 503 (Service Unavailable): SERVER_ERROR, worth retrying, not NOT_FOUND",
          busy.code == "SERVER_ERROR", busy.code)
    sound = video.choices(FAKE_YTDLP, jobs.Job(id="v", url="https://youtu.be/x", folder="/dl", audio=True))
    named = video.choices(FAKE_YTDLP, jobs.Job(id="v", url="https://youtu.be/x", folder="/dl",
                                               name="my talk.mp4"))
    check("--audio falls back to the whole video's sound when a site has no sound-only stream",
          sound[sound.index("-f") + 1].endswith("/b"), sound)
    check("-o NAME names a video too (with the extension yt-dlp gives it)",
          named[named.index("-o") + 1] == "my talk.%(ext)s", named)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    started = time.time()
    for fn in TESTS:
        if only in fn.__name__:
            print(fn.__name__.replace("_", " "))
            try:
                fn()
            except Exception as e:                                       # a broken test is a failure
                check(f"{fn.__name__} ran without crashing", False, repr(e))
            finally:
                while LEFTOVERS:
                    LEFTOVERS.pop()()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed in {time.time() - started:.0f}s")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
