#!/usr/bin/env python3
"""
A stand-in for yt-dlp in the tests: it answers the few options idm uses the way yt-dlp does,
for made-up videos, so the video path is tested without the internet.

The video's id is the link's v= (or its last part):
    ok, anything else   "Test clip": a 2 MB picture stream and a 1 MB sound stream, sent at
                        IDM_FAKE_RATE MB/s (default 4), then joined into one .mp4
    unavailable         ERROR: Video unavailable
    private             ERROR: Private video. Sign in if you've been granted access
    busy                ERROR: HTTP Error 429: Too Many Requests
    playlist            a playlist of 3 videos

Like yt-dlp, it carries on from a stream's .part file, and IDM_FAKE_LOG (if set) gets a
line per stream it starts, with the byte it started from.
"""
import json
import os
import sys
import time
import urllib.parse

STREAMS = (("f1.mp4", 2_000_000, 251), ("f2.m4a", 1_000_000, 241))   # name, size, byte pattern
ERRORS = {"unavailable": "Video unavailable",
          "private": "Private video. Sign in if you've been granted access to this video",
          "busy": "Unable to download webpage: HTTP Error 429: Too Many Requests"}


def stream_bytes(size, period):
    pattern = bytes(i % period for i in range(period))
    return (pattern * (size // period + 1))[:size]


def main(argv):
    if "--version" in argv:
        print("2026.10.26")
        return 0
    opts = options(argv)
    if "--load-info-json" in opts:
        return download(opts, json.load(open(opts["--load-info-json"])))
    url = argv[-1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    vid = (query.get("v") or query.get("list") or [url.rstrip("/").split("/")[-1]])[0]
    if vid in ERRORS:
        print(f"ERROR: [youtube] {vid}: {ERRORS[vid]}", file=sys.stderr)
        return 1
    videos = [f"{vid}{n}" for n in range(1, 4)] if vid == "playlist" else [vid]
    for v in videos:
        info = {"id": v, "title": "Test clip", "ext": "audio" if audio(opts) else "mp4",
                "webpage_url": f"https://www.youtube.com/watch?v={v}",
                "requested_formats": [{"filesize": size} for name, size, _ in chosen(opts)]}
        print(final_path(opts, info))
        print(json.dumps(info))
    return 0


def download(opts, info):
    """Each stream into its .part (carrying on from one that's there), then the joined file."""
    rate = min(float(os.environ.get("IDM_FAKE_RATE", "4")) * 1e6, float(opts.get("-r") or 1e12))
    template = opts["--progress-template"].split(":", 1)[1]
    paths = []
    for name, size, period in chosen(opts):
        path = final_path(opts, info).rsplit(".", 1)[0] + "." + name
        data, have = stream_bytes(size, period), os.path.getsize(path + ".part") if os.path.exists(path + ".part") else 0
        log(f"{name} from {have}")
        with open(path + ".part", "ab") as f:
            while have < size:
                n = min(64 * 1024, size - have)
                f.write(data[have:have + n])
                f.flush()
                have += n
                report(template, {"status": "downloading", "filename": path, "tmpfilename": path + ".part",
                                  "downloaded_bytes": have, "total_bytes": size})
                time.sleep(n / rate)
        os.replace(path + ".part", path)
        report(template, {"status": "finished", "filename": path, "downloaded_bytes": size, "total_bytes": size})
        paths.append(path)
    final = final_path(opts, info)
    with open(final, "wb") as out:                          # "join" them: one after the other
        for path in paths:
            out.write(open(path, "rb").read())
            os.remove(path)
    print(opts["--print"].split(":", 1)[1].replace("%(filepath)s", final), flush=True)
    return 0


def report(template, progress):
    print(template.replace("%(progress)j", json.dumps(progress)), flush=True)


def options(argv):
    """The options that take a value, as a dict (the last one wins)."""
    takes = {"-P", "-o", "-f", "-S", "-r", "-N", "--print", "--progress-template", "--load-info-json",
             "--merge-output-format", "--cookies", "--add-headers", "--js-runtimes", "--playlist-items"}
    opts, i = {}, 0
    while i < len(argv):
        if argv[i] in takes:
            opts[argv[i]] = argv[i + 1]
            i += 1
        i += 1
    return opts


def audio(opts):
    return opts.get("-f", "").startswith("ba")


def chosen(opts):
    return STREAMS[1:] if audio(opts) else STREAMS


def final_path(opts, info):
    ext = "m4a" if audio(opts) else "mp4"
    return os.path.join(opts.get("-P", "."), f"{info['title']} [{info['id']}].{ext}")


def log(line):
    if os.environ.get("IDM_FAKE_LOG"):
        with open(os.environ["IDM_FAKE_LOG"], "a") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
