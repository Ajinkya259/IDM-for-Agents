"""
Downloads that run in the background.

An agent's command must never sit waiting on a big file (its tool call would be cut off), so
every download runs as its own small background process, a "worker".  Commands only start,
watch and stop workers.

Each job lives in a folder, ~/.tako/jobs/<id>/:
    job.json      what to download (written by `tako get` / `tako start`)
    status.json   how it is going, rewritten by the worker about 5 times a second.  It is
                  also what the animated face will read.
    worker.log    anything the worker printed, for debugging
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import engine
from .errors import TakoError, classify

HOME = Path(os.environ.get("TAKO_HOME") or "~/.tako").expanduser()
ACTIVE = ("queued", "downloading")                     # a worker is on it
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
    created: float = field(default_factory=time.time)

    @property
    def dir(self):
        return HOME / "jobs" / self.id

    def save(self):
        write_json(self.dir / "job.json", asdict(self))

    def status(self):
        """How the job is going.  A worker that died without saying so is reported as failed."""
        st = read_json(self.dir / "status.json")
        if st.get("state") in ACTIVE and st.get("pid") and not alive(st["pid"]):
            st = self.set_status(state="failed", error=TakoError(
                "INTERRUPTED", "the download process stopped unexpectedly").to_dict())
        return st

    def set_status(self, **changes):
        st = {**read_json(self.dir / "status.json"), **changes, "updated": time.time()}
        write_json(self.dir / "status.json", st)
        return st


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
        raise TakoError("NO_SUCH_JOB", f"no download with id {job_id!r}")
    return Job(**read_json(path))


def every():
    """All jobs, oldest first."""
    folders = (HOME / "jobs").glob("*/job.json") if HOME.exists() else []
    return sorted((Job(**read_json(p)) for p in folders), key=lambda j: j.created)


def find(url, folder, name=None):
    """The latest job for this link and destination (cancelled ones don't count)."""
    same = [j for j in every() if (j.url, j.folder, j.name) == (url, folder, name)
            and j.status().get("state") != "cancelled"]
    return same[-1] if same else None


# ---- starting and stopping workers ----------------------------------------------------------
_children = []                                        # workers this process started (to reap)


def start(job):
    """Start a worker for the job, in the background.  It outlives this command."""
    job.set_status(state="queued", pid=None, error=None, trouble=None, note=None)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    with open(job.dir / "worker.log", "a") as log:
        worker = subprocess.Popen([sys.executable, "-m", "tako", "_work", job.id],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  env=env, start_new_session=True)
    _children.append(worker)
    job.set_status(pid=worker.pid)                    # adds to whatever the worker already wrote


def stop(job, timeout=5):
    """Ask the worker to stop (it saves what it has and says 'paused'), and wait until it has."""
    pid = job.status().get("pid")
    if alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        if alive(pid):                                # it didn't listen: make it
            os.kill(pid, signal.SIGKILL)
    if read_json(job.dir / "status.json").get("state") in ACTIVE:
        job.set_status(state="paused")                # it stopped before it could say so


def cancel(job):
    """Stop the job and delete what it downloaded so far."""
    stop(job)
    final = job.status().get("file")
    for leftover in (f"{final}.part", f"{final}.part.json") if final else ():
        if os.path.exists(leftover):
            os.remove(leftover)
    job.set_status(state="cancelled")


def alive(pid):
    if not pid:
        return False
    for child in _children:                           # let our own finished workers go
        child.poll()
    try:
        os.kill(pid, 0)                               # signal 0: "are you there?"
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---- the worker ------------------------------------------------------------------------------
def work(job):
    """The worker process: run the download and keep status.json up to date until it ends."""
    signal.signal(signal.SIGTERM, _pause)
    status = StatusReport(job)
    try:
        path = engine.download(job, status)
    except KeyboardInterrupt:
        status.end("paused")
    except Exception as e:
        error = classify(e)
        if error is None:                             # a bug: say so, keep the details in worker.log
            traceback.print_exc()
            error = TakoError("INTERNAL", repr(e))
        status.end("failed", error=error.to_dict())
    else:
        status.end("done", file=path, bytes=os.path.getsize(path))


def _pause(signum, frame):
    raise KeyboardInterrupt                           # `tako pause`: stop where we are


class StatusReport(engine.Report):
    """Hears what the engine does and keeps status.json up to date: progress at most 5 times
    a second (speed is measured over the last 3 seconds), everything else at once."""

    def __init__(self, job):
        self.job, self.size, self.window, self.written = job, None, [], 0.0
        job.set_status(state="downloading", pid=os.getpid(), started=time.time(),
                       error=None, trouble=None, note=None)

    def found(self, remote, part):
        self.size = remote.size
        self.job.set_status(state="downloading", file=part.final, size=remote.size,
                            can_resume=remote.can_resume)

    def leftovers(self, why_discarded, have):
        note = f"starting over: {why_discarded}" if why_discarded else (
            f"resumed with {have:,} bytes already here" if have else None)
        self.job.set_status(bytes=have, note=note)

    def progress(self, done):
        now = time.time()
        if now - self.written < 0.2:
            return
        self.written = now
        self.window = [(t, b) for t, b in self.window if now - t <= 3 and b <= done] + [(now, done)]
        speed = self.speed()
        eta = round((self.size - done) / speed) if speed and self.size else None
        self.job.set_status(state="downloading", bytes=done, speed=round(speed), eta=eta,
                            trouble=None)

    def retrying(self, error, wait):
        self.job.set_status(trouble={"code": error.code, "message": str(error), "retry_in": wait})

    def speed(self):
        (t0, b0), (t1, b1) = self.window[0], self.window[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def end(self, state, **fields):
        self.job.set_status(state=state, speed=0, eta=None, trouble=None, finished=time.time(),
                            **fields)


# ---- small helpers -----------------------------------------------------------------------------
def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    """Write the whole file or nothing: readers (the face, `tako wait`) never see half of it."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
