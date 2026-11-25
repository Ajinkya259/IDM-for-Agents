"""
Downloads that run in the background.

An agent's command must never sit waiting on a big file (its tool call would be cut off), so
every download runs as its own small background process, a "worker".  Commands only start,
watch and stop workers.

Each job lives in a folder, ~/.idm/jobs/<id>/:
    job.json      what to download (written by `idm get` / `idm start`)
    status.json   how it is going, rewritten by the worker about 5 times a second (what
                  replies and dotty are made from)
    worker.log    anything the worker printed, for debugging

Before it downloads, a worker waits its turn: for its time (--at, --in), for the download it
follows (--after), and for one of the MAX_RUNNING places (~/.idm/places/N.lock): at most that
many downloads run at once, and the rest wait in line, first come first served.
"""
import fcntl
import os
import secrets
import signal
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import engine, video
from .engine import read_json, write_json
from .errors import IDMError, classify

HOME = Path(os.environ.get("IDM_HOME") or "~/.idm").expanduser().absolute()
MAX_RUNNING = int(os.environ.get("IDM_MAX_DOWNLOADS", "3"))   # downloads at once; the rest wait
ACTIVE = ("scheduled", "queued", "downloading")        # a worker is on it (maybe waiting its turn)
STARTING = 10                                           # seconds a new worker has to take its lock
FINAL = ("done", "failed", "paused", "cancelled")       # nothing is happening
# and "new": created, its worker not started yet


@dataclass
class Job:
    """One download: what to fetch, where to, and with which options."""
    id: str
    url: str
    folder: str
    name: Optional[str] = None                     # None: the server's name for the file
    headers: dict = field(default_factory=dict)    # extra request headers, e.g. Authorization
    cookies: Optional[str] = None                  # a browser cookies.txt file
    allow_page: bool = False                       # save a web page instead of refusing it
    connections: int = 8                           # at most this many at once
    starts_at: Optional[float] = None              # --at / --in: not before this time
    after: Optional[str] = None                    # --after: not before this job has ended
    limit: Optional[int] = None                    # --limit: at most this many bytes a second
    video: bool = False                            # a video site's page: yt-dlp downloads it
    audio: bool = False                            # --audio: the video's sound alone
    created: float = field(default_factory=time.time)

    @property
    def dir(self):
        return HOME / "jobs" / self.id

    def save(self):
        write_json(self.dir / "job.json", asdict(self))

    def status(self):
        """How the job is going.  A worker that died without saying so is reported as failed."""
        st = read_json(self.dir / "status.json")
        if st.get("state") in ACTIVE and st.get("pid") and not working(self, st):
            st = self.set_status(state="failed", pid=None, error=IDMError(
                "INTERRUPTED", "the download process stopped unexpectedly").to_dict())
        return st

    def set_status(self, **changes):
        with locked(self.dir / "status.lock"):        # the worker, its connections and commands
            st = {**read_json(self.dir / "status.json"), **changes, "updated": time.time()}
            write_json(self.dir / "status.json", st)  # all write it: one at a time
        return st


@contextmanager
def locked(path):
    """Hold a lock on `path` (made if need be) while the block runs: one process or thread at
    a time, whoever they are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


# ---- finding jobs ------------------------------------------------------------------------------
def new(url, folder, name=None):
    job = Job(id=secrets.token_hex(3), url=url, folder=folder, name=name)
    job.dir.mkdir(parents=True, mode=0o700)           # private: job.json may hold cookies
    job.save()
    job.set_status(job=job.id, url=url, state="new")
    return job


def load(job_id):
    path = HOME / "jobs" / job_id / "job.json"
    if not path.is_file():
        raise IDMError("NO_SUCH_JOB", f"no download with id {job_id!r}")
    return Job(**read_json(path))


def every():
    """All jobs, oldest first.  (A job folder that can't be read is left out.)"""
    found = []
    for path in (HOME / "jobs").glob("*/job.json") if HOME.exists() else []:
        try:
            found.append(Job(**read_json(path)))
        except TypeError:
            pass
    return sorted(found, key=lambda j: j.created)


def find(url, folder, name=None):
    """The latest job for this link and destination (cancelled ones don't count)."""
    same = [j for j in every() if (j.url, j.folder, j.name) == (url, folder, name)
            and j.status().get("state") != "cancelled"]
    return same[-1] if same else None


# ---- starting and stopping workers ----------------------------------------------------------
_children = []                                        # workers this process started (to reap)


def start(job):
    """Start a worker for the job, in the background.  It outlives this command."""
    later = job.starts_at and job.starts_at > time.time()
    job.set_status(state="scheduled" if later else "queued", starts_at=job.starts_at if later else None,
                   limit=job.limit, pid=None, error=None, trouble=None, note=None, speed=None,
                   eta=None, stalled=None, waiting_for=None, in_line_since=None)
    home = str(Path(__file__).resolve().parent.parent)  # idm's own folder: run this idm, never
    with open(job.dir / "worker.log", "a") as log:      # an idm/ folder in the current one
        worker = subprocess.Popen([sys.executable, "-m", "idm", "_work", job.id], cwd=home,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  start_new_session=True)
    _children.append(worker)
    job.set_status(pid=worker.pid)                    # adds to whatever the worker already wrote


def stop(job):
    """Ask the worker to stop (it saves what it has and says 'paused'), and wait until it has."""
    st = job.status()                                 # (a pid in it is this job's worker's)
    pid = st.get("pid") if st.get("state") in ACTIVE else None
    if alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5
        while alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        if alive(pid):                                # it didn't listen: make it
            os.kill(pid, signal.SIGKILL)
    if read_json(job.dir / "status.json").get("state") in ACTIVE:
        job.set_status(state="paused")                # it stopped before it could say so


def cancel(job):
    """Stop the job and delete what it downloaded so far.  (A finished download stays.)"""
    if job.status().get("state") == "done":
        return
    stop(job)
    st = job.status()
    final = st.get("file")
    mine = video.leftovers(st.get("parts")) if job.video else \
        (path for path in (f"{final}.part", f"{final}.part.json") if final and os.path.exists(path))
    for leftover in list(mine):
        os.remove(leftover)
    job.set_status(state="cancelled")


def working(job, st):
    """Is a worker on this job right now?  Its pid alone can't say: after a crash or a restart,
    another process may have that number.  So unless it wrote its status a moment ago (it may
    be starting up), it must also hold worker.lock, which goes with its process however it ends."""
    if not alive(st.get("pid")):
        return False
    return time.time() - st.get("updated", 0) < STARTING or held(job.dir / "worker.lock")


def held(path):
    """Does a process hold the lock on `path`?"""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False                                      # it was free (and closing the file freed it)


def alive(pid):
    if not pid:
        return False
    for child in _children:                           # let our own finished workers go
        child.poll()
    try:
        os.kill(pid, 0)                               # signal 0: "are you there?"
        return True
    except (ProcessLookupError, PermissionError):     # (a worker is always this user's)
        return False


# ---- the worker ------------------------------------------------------------------------------
def work(job):
    """The worker process: run the download and keep status.json up to date until it ends."""
    with open(job.dir / "worker.lock", "w") as claim:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)   # held until this worker ends
        except BlockingIOError:
            return                                    # another worker is already on this job
        signal.signal(signal.SIGTERM, _pause)
        status = StatusReport(job)
        try:
            job = wait_for_turn(job, status)
            with a_place(job, status):
                status.begin()
                path = (video.download if job.video else engine.download)(job, status)
        except KeyboardInterrupt:
            status.end("paused")
        except Exception as e:
            error = classify(e)
            if error is None:                         # a bug: say so, keep the details in worker.log
                traceback.print_exc()
                error = IDMError("INTERNAL", repr(e))
            status.end("failed", error=error.to_dict())
        else:
            status.end("done", file=path, bytes=os.path.getsize(path))


def _pause(signum, frame):
    raise KeyboardInterrupt                           # `idm pause`: stop where we are


def wait_for_turn(job, status):
    """Wait for the job's time (--at, --in) and for the job it follows (--after) to end.
    Returns the job as it is then: `idm resume --now` may have cleared both meanwhile."""
    while True:
        job = load(job.id)
        if job.starts_at and time.time() < job.starts_at:
            status.waiting("scheduled", starts_at=job.starts_at)
        elif job.after and load(job.after).status().get("state") in ACTIVE:
            status.waiting("queued", waiting_for=f"download {job.after} to end")
        else:
            return job
        time.sleep(1)               # short naps, on the wall clock: it jumps when the Mac sleeps


@contextmanager
def a_place(job, status):
    """Hold one of the MAX_RUNNING places while the job downloads.  Jobs take them in the
    order they asked; a place frees itself when its worker ends, however it ends (the lock
    goes with the process)."""
    asked = time.time()
    status.waiting("queued", in_line_since=asked)     # so the jobs behind see this one
    while True:
        place = None if ahead_in_line(job, asked) else free_place()
        if place:
            break
        status.waiting("queued", in_line_since=asked,
                       waiting_for=f"a free place ({MAX_RUNNING} downloads are running)")
        time.sleep(0.5)
    with place:
        yield


def ahead_in_line(job, asked):
    """Is another job waiting for a place since before this one asked?"""
    for other in every():
        st = read_json(other.dir / "status.json")
        if other.id != job.id and st.get("state") == "queued" and working(other, st) \
                and (st.get("in_line_since") or asked) < asked:
            return True
    return False


def free_place():
    """A place's lock, taken; None if every place is taken."""
    (HOME / "places").mkdir(parents=True, exist_ok=True)
    for n in range(MAX_RUNNING):
        place = open(HOME / "places" / f"{n}.lock", "w")
        try:
            fcntl.flock(place, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return place
        except BlockingIOError:
            place.close()
    return None


class StatusReport(engine.Report):
    """Hears what the engine does and keeps status.json up to date: progress at most 5 times
    a second (speed is measured over the last 3 seconds), everything else at once.  When no
    byte arrives, it says so: the speed falls to 0 and `stalled` counts the seconds."""

    def __init__(self, job):
        self.job, self.size, self.window, self.written = job, None, [], 0.0
        self.done, self.moved, self.waited = None, time.time(), None
        job.set_status(pid=os.getpid(), error=None, trouble=None, note=None)

    def waiting(self, state, **why):
        """Not downloading yet: scheduled, or queued (`why` says for what)."""
        if (state, why) != self.waited:
            self.waited = (state, why)
            self.job.set_status(state=state, **{"starts_at": None, "waiting_for": None, **why})

    def begin(self):
        self.moved = time.time()
        self.job.set_status(state="downloading", started=self.moved, starts_at=None,
                            waiting_for=None, in_line_since=None)

    def found(self, remote, part):
        self.size = remote.size
        self.job.set_status(state="downloading", file=part.final, size=remote.size)

    def video(self, file, size):
        self.size = size
        self.job.set_status(file=file, size=size)

    def parts(self, paths):
        self.job.set_status(parts=paths)              # (what `idm cancel` deletes)

    def leftovers(self, why_discarded, have):
        note = f"starting over: {why_discarded}" if why_discarded else (
            f"resumed with {have:,} bytes already here" if have else None)
        self.done = have
        self.job.set_status(bytes=have, note=note)

    def progress(self, done, connections=1):
        now = time.time()
        if done != self.done:
            self.done, self.moved = done, now
        if now - self.written < 0.2:
            return
        self.written = now
        self.window = [(t, b) for t, b in self.window if now - t <= 3 and b <= done] + [(now, done)]
        speed = self.speed()
        eta = round((self.size - done) / speed) if speed and self.size else None
        moving = {"trouble": None} if now - self.moved < 1 else {}   # (trouble stays till bytes come)
        self.job.set_status(state="downloading", bytes=done, speed=round(speed), eta=eta,
                            connections=connections, stalled=self.stalled(), **moving)

    def retrying(self, error, wait):
        quiet = {"speed": 0, "eta": None} if time.time() - self.moved >= 3 else {}
        self.job.set_status(trouble={"code": error.code, "message": str(error), "retry_in": wait},
                            stalled=self.stalled(), **quiet)

    def stalled(self):
        """Seconds since the last byte arrived, once there have been a few; else None."""
        silent = time.time() - self.moved
        return round(silent) if silent >= 5 else None

    def speed(self):
        (t0, b0), (t1, b1) = self.window[0], self.window[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def end(self, state, **fields):
        self.job.set_status(state=state, pid=None, speed=0, eta=None, trouble=None, connections=0,
                            stalled=None, waiting_for=None, in_line_since=None, finished=time.time(),
                            **{"bytes": self.done, **fields})
