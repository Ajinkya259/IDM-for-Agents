#!/usr/bin/env python3
"""
Tests for tako, phase 2: every command, every error code, and the background jobs.

    python3 tests/run_tests.py          (about 2 minutes; uses free local ports)
    python3 tests/run_tests.py pause    (only the tests with "pause" in their name)

Each test runs the real `bin/tako` against tests/testserver.py, with its own TAKO_HOME and
download folder, and checks the JSON replies, the exit codes and the saved bytes.
"""
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAKO = [sys.executable, str(HERE.parent / "bin" / "tako")]
TESTS, RESULTS = [], []
LEFTOVERS = []                     # servers and sandboxes to clean up after each test, pass or fail


def test(fn):
    TESTS.append(fn)
    return fn


def check(what, ok, detail=""):
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

    def sha(self):
        pattern = bytes((i + self.version) % 251 for i in range(251))
        size = int(self.size * 1e6)
        return hashlib.sha256((pattern * (size // 251 + 1))[:size]).hexdigest()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()


class Sandbox:
    """A TAKO_HOME and a download folder of its own, and a way to run tako in them."""

    def __init__(self, **env):
        self.root = Path(tempfile.mkdtemp(prefix="tako-test-"))
        self.dl = self.root / "dl"
        self.env = {**os.environ, "TAKO_HOME": str(self.root / "home"), **env}
        LEFTOVERS.append(self.clean)

    def tako(self, *args, timeout=60):
        """Run tako as an agent would (output is a pipe).  Returns (exit code, the JSON reply)."""
        p = subprocess.run([*TAKO, *args], env=self.env, capture_output=True, text=True,
                           timeout=timeout)
        lines = p.stdout.strip().splitlines()
        try:
            reply = json.loads(lines[-1]) if len(lines) == 1 else {"_not_one_line": p.stdout}
        except ValueError:
            reply = {"_not_json": p.stdout + p.stderr}
        return p.returncode, reply

    def get(self, url, *args, **kw):
        return self.tako("get", url, "-d", str(self.dl), *args, **kw)

    def start(self, url, *args):
        return self.tako("start", url, "-d", str(self.dl), *args)

    def status_file(self, job):
        return json.loads((self.root / "home" / "jobs" / job / "status.json").read_text())

    def files(self):
        return sorted(os.listdir(self.dl)) if self.dl.exists() else []

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


def in_terminal(args, env, interrupt_after=None, timeout=60):
    """Run tako in a pseudo-terminal, as a person would.  Returns (exit code, what was shown)."""
    master, slave = pty.openpty()
    p = subprocess.Popen([*TAKO, *args], env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    shown, start, sent = b"", time.time(), False
    while True:
        if interrupt_after and not sent and time.time() - start > interrupt_after:
            p.send_signal(signal.SIGINT)
            sent = True
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
    check("--cookies with no such file: BAD_INPUT, exit 1",
          code == 1 and r.get("error", {}).get("code") == "BAD_INPUT", r)


@test
def web_page_instead_of_file():
    srv, box = Server(), Sandbox()
    code, r = box.get(srv.url("/page.html"))
    check("a web page: NOT_A_FILE, exit 1, `next` adds --allow-page",
          code == 1 and r.get("error", {}).get("code") == "NOT_A_FILE"
          and "--allow-page" in r.get("next", ""), r)
    code, r = box.get(srv.url("/page.html"), "--allow-page")
    check("--allow-page: the page is saved", code == 0 and box.files() == ["page.html"], (r, box.files()))


@test
def bad_input():
    box = Sandbox()
    code, r = box.get("not a link")
    check("bad link: BAD_URL, exit 1, and no job created",
          code == 1 and r.get("error", {}).get("code") == "BAD_URL"
          and box.tako("list")[1].get("jobs") == [], r)
    code, r = box.get("http://127.0.0.1:9/x", "--header", "no colon")
    check("bad --header: BAD_INPUT", code == 1 and r.get("error", {}).get("code") == "BAD_INPUT", r)
    code, r = box.tako("get")
    check("missing argument: still one line of JSON, BAD_INPUT, exit 2",
          code == 2 and r.get("error", {}).get("code") == "BAD_INPUT", r)
    for command in ("status", "wait", "pause", "resume", "cancel"):
        code, r = box.tako(command, "nope42")
        check(f"{command} with an unknown id: NO_SUCH_JOB, exit 1",
              code == 1 and r.get("error", {}).get("code") == "NO_SUCH_JOB", r)


@test
def no_server_gives_up_with_next():
    box = Sandbox(TAKO_TRIES="2")
    code, r = box.get(f"http://127.0.0.1:{free_port()}/x")
    e = r.get("error", {})
    check("nothing listening: NETWORK, retryable, exit 3, after retrying",
          code == 3 and e.get("code") == "NETWORK" and e.get("retryable") is True
          and "gave up after 2 tries" in e.get("message", ""), r)
    check("nothing listening: `next` resumes the same job",
          r.get("next") == f"tako resume {r.get('job')}", r)


@test
def server_errors():
    srv, box = Server(), Sandbox(TAKO_TRIES="1")
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
    wait_until(lambda: seen.add((box.tako("status", r["job"])[1].get("trouble") or {}).get("code"))
               or "RATE_LIMITED" in seen, timeout=6)
    check("429 twice: while waiting, status shows trouble RATE_LIMITED", "RATE_LIMITED" in seen, seen)
    code, done = box.tako("wait", r["job"])
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
    code, r = box.tako("get", srv.url(), "-d", str(locked))
    check("read-only folder: NO_PERMISSION, exit 1",
          code == 1 and r.get("error", {}).get("code") == "NO_PERMISSION", r)


# ---- background jobs ---------------------------------------------------------------------------------
@test
def start_replies_at_once_then_wait():
    srv, box = Server(size=6, rate=3), Sandbox()
    t = time.time()
    code, r = box.start(srv.url())
    took = time.time() - t
    check("start: replies at once (under 1.5 s) with a job id and `next`: tako wait",
          code == 0 and took < 1.5 and r.get("state") in ("queued", "downloading")
          and r.get("next") == f"tako wait {r.get('job')}", (took, r))
    code, mid = box.tako("wait", r["job"], "--max", "1")
    check("wait --max 1: replies while still downloading, with percent, speed and eta",
          code == 0 and mid.get("state") == "downloading" and 0 < mid.get("percent", 0) < 100
          and mid.get("speed", 0) > 0 and "eta" in mid, mid)
    code, done = box.tako("wait", r["job"])
    check("wait: done, right bytes", done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def get_with_max_hands_over_to_wait():
    srv, box = Server(size=6, rate=3), Sandbox()
    t = time.time()
    code, r = box.get(srv.url(), "--max", "0.5")
    check("get --max 0.5: replies in time, still downloading, `next`: tako wait",
          time.time() - t < 2 and r.get("state") == "downloading" and r.get("next", "").startswith("tako wait"), r)
    code, done = box.tako("wait", r["job"])
    check("then wait: done", code == 0 and done.get("state") == "done", done)


@test
def pause_and_resume():
    srv, box = Server(size=8, rate=2), Sandbox()
    _, r = box.start(srv.url())
    job = r["job"]
    wait_until(lambda: box.status_file(job).get("bytes", 0) > 500_000)
    pid = box.status_file(job)["pid"]
    code, p = box.tako("pause", job)
    check("pause: state paused, some percent, `next`: tako resume",
          code == 0 and p.get("state") == "paused" and 0 < p.get("percent", 0) < 100
          and p.get("next") == f"tako resume {job}", p)
    check("pause: the worker has stopped, the .part file stays",
          not alive(pid) and "halo test.bin.part" in box.files(), box.files())
    code, again = box.tako("resume", job)
    check("resume: downloading again", code == 0 and again.get("state") in ("queued", "downloading"), again)
    code, done = box.tako("wait", job)
    check("resume: finishes with the right bytes",
          done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def cancel_deletes_partial():
    srv, box = Server(size=8, rate=2), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 200_000)
    code, c = box.tako("cancel", r["job"])
    check("cancel: state cancelled, nothing left on disk",
          code == 0 and c.get("state") == "cancelled" and box.files() == [], (c, box.files()))


@test
def worker_killed_is_reported_and_resumable():
    srv, box = Server(size=8, rate=2), Sandbox()
    _, r = box.start(srv.url())
    job = r["job"]
    wait_until(lambda: box.status_file(job).get("bytes", 0) > 500_000)
    os.kill(box.status_file(job)["pid"], signal.SIGKILL)
    time.sleep(0.2)
    code, s = box.tako("status", job)
    check("worker killed: INTERRUPTED, retryable, exit 3, `next` resumes",
          code == 3 and s.get("error", {}).get("code") == "INTERRUPTED"
          and s.get("next") == f"tako resume {job}", s)
    box.tako("resume", job)
    code, done = box.tako("wait", job)
    check("worker killed: resume finishes with the right bytes",
          done.get("state") == "done" and sha(box.dl / "halo test.bin") == srv.sha(), done)


@test
def agent_cut_off_then_same_get_reattaches():
    srv, box = Server(size=6, rate=2), Sandbox()
    p = subprocess.Popen([*TAKO, "get", srv.url(), "-d", str(box.dl)], env=box.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    p.kill()                                                   # the agent's tool call was cut off
    p.wait()
    code, r = box.get(srv.url())
    jobs = box.tako("list")[1].get("jobs", [])
    check("cut off, then the same get: one job, carried on to done",
          code == 0 and r.get("state") == "done" and len(jobs) == 1, (r, jobs))
    check("cut off, then the same get: one file, right bytes",
          box.files() == ["halo test.bin"] and sha(box.dl / "halo test.bin") == srv.sha(), box.files())


@test
def pause_resume_on_server_that_cannot_resume():
    srv, box = Server("--no-ranges", size=6, rate=2), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 300_000)
    box.tako("pause", r["job"])
    box.tako("resume", r["job"])
    code, done = box.tako("wait", r["job"])
    note = box.status_file(r["job"]).get("note", "")
    check("no-ranges server: resume starts over (and says so), right bytes",
          done.get("state") == "done" and "can't resume" in (note or "")
          and sha(box.dl / "halo test.bin") == srv.sha(), (done, note))


@test
def file_changed_while_paused():
    srv, box = Server(size=6, rate=2), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("bytes", 0) > 300_000)
    box.tako("pause", r["job"])
    port = srv.port
    srv.stop()
    srv = Server("--version", "2", size=6, rate=20, port=port)       # same link, new file
    box.tako("resume", r["job"])
    code, done = box.tako("wait", r["job"])
    note = box.status_file(r["job"]).get("note", "")
    check("file changed while paused: noticed, starts over, gets the NEW bytes",
          done.get("state") == "done" and "changed" in (note or "")
          and sha(box.dl / "halo test.bin") == srv.sha(), (done, note))


@test
def status_file_has_what_the_face_needs():
    srv, box = Server(size=6, rate=2), Sandbox()
    _, r = box.start(srv.url())
    wait_until(lambda: box.status_file(r["job"]).get("speed", 0) > 0)
    st = box.status_file(r["job"])
    needed = {"job", "url", "file", "state", "bytes", "size", "speed", "eta", "pid", "updated"}
    check("status.json: has state, bytes, size, speed, eta, file... and is fresh",
          needed <= st.keys() and st["state"] == "downloading" and time.time() - st["updated"] < 2,
          sorted(needed - st.keys()))
    box.tako("cancel", r["job"])


@test
def list_is_short():
    srv, box = Server(), Sandbox()
    box.get(srv.url())
    box.get(srv.url("/status/404"))
    code, r = box.tako("list")
    rows = r.get("jobs", [])
    check("list: one row per job, with job, state, url (and the error code, no hint)",
          code == 0 and len(rows) == 2 and all({"job", "state", "url"} <= row.keys() for row in rows)
          and rows[1].get("error") == "NOT_FOUND" and "hint" not in json.dumps(rows), rows)


# ---- what a person sees ------------------------------------------------------------------------------
@test
def person_sees_progress_and_result():
    srv, box = Server(size=4, rate=4), Sandbox()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env)
    check("in a terminal: a progress line, then '✓ saved', no JSON",
          code == 0 and "MB/s" in shown and "✓ saved" in shown and '"ok"' not in shown, shown[-300:])


@test
def person_ctrl_c_pauses():
    srv, box = Server(size=8, rate=2), Sandbox()
    code, shown = in_terminal(["get", srv.url(), "-d", str(box.dl)], box.env, interrupt_after=1.5)
    jobs = box.tako("list")[1].get("jobs", [])
    check("Ctrl+C in a terminal: says paused, the job is paused, `tako resume` shown",
          "paused at" in shown and "tako resume" in shown and jobs and jobs[0]["state"] == "paused",
          (shown[-300:], jobs))


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
