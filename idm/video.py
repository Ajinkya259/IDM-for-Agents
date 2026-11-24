"""
Videos from sites like YouTube, Instagram (reels), TikTok, X and Vimeo.

Their links open a page with a player, not a file.  The video itself sits behind each
site's own machinery: signed links that expire, the picture and the sound as separate
streams, and changes every few weeks.  yt-dlp knows every site's machinery and keeps up,
so for these links idm hands the downloading to yt-dlp, and stays the part the agent
talks to: the same JSON, error codes and `next`, the background job, pause, resume,
cancel, the schedule, the queue, the speed limit, and halo.

    1. ask yt-dlp what the video is: its title, size and file name (saved as video.json)
    2. let yt-dlp download it from that answer, reading its progress into status.json

A video's picture and sound are joined into one .mp4 (yt-dlp uses ffmpeg for that; without
ffmpeg idm asks for the best video that comes as one file).  --audio gets the sound alone.
"""
import functools
import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

from .engine import read_json
from .errors import IDMError

VIDEO_SITES = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "x.com", "twitter.com",
               "vimeo.com", "facebook.com", "fb.watch", "reddit.com", "v.redd.it", "twitch.tv",
               "dailymotion.com", "threads.net", "threads.com")
NAME = "%(title).150B [%(id)s].%(ext)s"     # 'Me at the zoo [jNQXAC9IVRw].mp4'
DESCRIBE_TIMEOUT = 120                      # seconds for yt-dlp to say what the video is
NEW_LIMIT = "new limit"                     # (a run stopped because `idm limit` changed it)

# What yt-dlp says when it fails, as idm's error codes: the first that matches wins.
PROBLEMS = (
    ("AUTH_REQUIRED", r"sign in|log ?in|cookies|private video|members.only|age.restrict|confirm your age"),
    ("NEEDS_TOOL", r"unable to extract|nsig extraction|please update|is outdated|requested format is not avail"),
    ("NOT_FOUND", r"unavailable|not available|has been removed|does not exist|no longer|404"),
    ("RATE_LIMITED", r"HTTP Error 429|too many requests"),
    ("SERVER_ERROR", r"HTTP Error 5\d\d"),
    ("HTTP_ERROR", r"HTTP Error 4\d\d"),
    ("NOT_A_FILE", r"unsupported url|no video formats|no video could be found|there's no video"),
    ("NETWORK", r"unable to download|timed out|connection (reset|refused|aborted)|temporary failure"
                r"|nodename nor servname|network is unreachable|getaddrinfo"),
)


def is_video(url):
    """Is this a link to a video site's page (where yt-dlp is the way to the video)?"""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == site or host.endswith("." + site) for site in VIDEO_SITES)


def download(job, report):
    """Download the video at job.url into job.folder and return the saved file's path.  Raises
    IDMError when it fails, and KeyboardInterrupt when paused (yt-dlp's .part files stay, and
    it carries on from them next time)."""
    tool = yt_dlp()
    about = describe(tool, job)                                          # 1. what is it?
    report.video(about["title"], about["file"], about["size"])
    have = {}                                                            # bytes of each stream
    while True:                                                          # 2. download it (again,
        saved = fetch(tool, job, report, have)                           #    when the limit changes)
        if saved != NEW_LIMIT:
            return saved or about["file"]          # (a video already there prints no path)


def yt_dlp():
    tool = os.environ.get("IDM_YT_DLP") or shutil.which("yt-dlp")
    if not tool or not os.path.isfile(tool):
        raise IDMError("NEEDS_TOOL", "video sites need yt-dlp, and it isn't installed")
    return tool


def describe(tool, job):
    """Ask yt-dlp about the video, keep its answer for the download, and return its title,
    the file it will be saved as, and its size (the sum of its streams; None if unknown)."""
    try:
        p = subprocess.run([tool, *choices(tool, job), "--simulate", "--flat-playlist",
                            "--playlist-items", "1:5", "--print", "filename", "--print", "%()j",
                            job.url],
                           capture_output=True, text=True, timeout=DESCRIBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise IDMError("NETWORK", "yt-dlp took too long to find the video") from None
    if p.returncode != 0:
        raise problem(p.stderr)
    lines = p.stdout.splitlines()
    if len(lines) > 2:                                       # a filename and a JSON per video
        videos = [json.loads(line) for line in lines[1::2]]
        raise IDMError("NOT_A_FILE", "this link is a playlist; idm downloads one video at a time",
                       links=[v.get("webpage_url") or v.get("url") for v in videos])
    file, info_json = (lines + ["", ""])[:2]
    info = json.loads(info_json)
    with open(job.dir / "video.json", "w") as f:
        f.write(info_json)
    streams = info.get("requested_formats") or [info]
    sizes = [s.get("filesize") or s.get("filesize_approx") for s in streams]
    return {"title": info.get("title") or info.get("id"), "file": os.path.abspath(file),
            "size": sum(sizes) if all(sizes) else None}


def fetch(tool, job, report, have):
    """One run of yt-dlp downloading the video described in video.json.  Returns the saved
    file's path, or NEW_LIMIT if `idm limit` changed the speed limit (run it again with it)."""
    limit = current_limit(job)
    args = [*choices(tool, job), "--load-info-json", str(job.dir / "video.json"), "--no-simulate",
            "--newline", "--progress", "--progress-template", "download:IDMP %(progress)j",
            "--print", "after_move:IDMF %(filepath)s", "-N", str(job.connections)]
    with open(job.dir / "yt-dlp.log", "w") as log:
        proc = subprocess.Popen([tool, *args, *(["-r", str(limit)] if limit else [])],
                                stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            saved = follow(proc, job, report, limit, have)
        finally:
            stop(proc)
    if saved == NEW_LIMIT or proc.returncode == 0:
        return saved
    with open(job.dir / "yt-dlp.log") as log:
        raise problem(log.read())


def follow(proc, job, report, limit, have):
    """Read yt-dlp's lines until it ends: progress into the status (`have`: the bytes of each
    stream so far), and the saved file's path.  NEW_LIMIT if `idm limit` changed the limit."""
    saved, checked = None, time.time()
    for line in proc.stdout:
        if line.startswith("IDMP "):
            step = json.loads(line[5:])
            if step.get("filename") not in have:
                report.parts(list(have) + [step.get("filename")])
            have[step.get("filename")] = step.get("downloaded_bytes") or 0
            report.progress(sum(have.values()))
        elif line.startswith("IDMF "):
            saved = line[5:].rstrip("\n")
        if time.time() - checked >= 1:
            checked = time.time()
            if current_limit(job) != limit:
                return NEW_LIMIT
    proc.wait()
    return saved


def current_limit(job):
    return read_json(job.dir / "job.json").get("limit")


def stop(proc):
    """End yt-dlp if it's still running (it leaves its .part files, to carry on from)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def choices(tool, job):
    """yt-dlp's options for this job: where and under what name, which streams, its login."""
    args = ["--no-playlist", "-P", job.folder, "-o", NAME]
    if job.audio:
        args += ["-f", "ba[ext=m4a]/ba"]                     # the sound alone, as it comes
    elif shutil.which("ffmpeg"):
        args += ["-S", "res:1080,ext:mp4:m4a", "--merge-output-format", "mp4"]
    else:
        args += ["-f", "b[ext=mp4]/b"]                       # the best that needs no joining
    if job.cookies:
        args += ["--cookies", job.cookies]
    for name, value in job.headers.items():
        args += ["--add-headers", f"{name}:{value}"]
    if not shutil.which("deno") and shutil.which("node") and new_enough(tool):
        args += ["--js-runtimes", "node"]                    # YouTube needs a JavaScript runtime
    return args


@functools.lru_cache(maxsize=None)
def new_enough(tool):
    """Does this yt-dlp know --js-runtimes (2025.11 and later)?"""
    try:
        version = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return version.strip() >= "2025.11"


def problem(said):
    """yt-dlp's complaint (its last ERROR line) as an IDMError with idm's code for it."""
    errors = [line for line in said.splitlines() if line.startswith("ERROR:")]
    words = errors[-1] if errors else (said.strip().splitlines() or ["yt-dlp failed"])[-1]
    words = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", words).strip()
    for code, pattern in PROBLEMS:
        if re.search(pattern, words, re.I):
            return IDMError(code, f"yt-dlp: {words}"[:300])
    return IDMError("HTTP_ERROR", f"yt-dlp: {words}"[:300])


def leftovers(parts):
    """The files a stopped video download leaves: each stream's .part, its pieces and notes."""
    for part in parts or []:
        yield from (path for path in (part, part + ".part", part + ".ytdl") if os.path.exists(path))
        yield from glob.glob(glob.escape(part) + ".part-Frag*")
