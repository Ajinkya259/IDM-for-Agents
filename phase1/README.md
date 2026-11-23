# Phase 1: one connection, done properly

`download.py` downloads one file over one connection. It doesn't use parallel connections yet. This step gets the basics right first, because every later step builds on them.

```
python3 download.py https://proof.ovh.net/files/10Mb.dat
python3 download.py URL -o clip.mp4 -d ~/Downloads
```

## What happens, step by step

Start reading at `download()`. It is the whole job in five lines, and each line calls one function:

| Step | Function | What it does |
|---|---|---|
| 1. What is this file? | `probe()` | Asks for "bytes 0 onwards" and reads only the answer's headers. They give the name, the size, a fingerprint (ETag), and whether the server can resume. |
| 2. Where does it go? | `PartFile(...)` | Picks `NAME`. If `NAME` is already taken, it uses `NAME (1)`. |
| 3. Anything from last time? | `report_leftovers()` | Keeps an earlier `NAME.part` only if it belongs to this exact file. |
| 4. Get the missing bytes | `fetch_rest()` | Asks for "bytes N onwards", where N is how much is already on disk, and writes them into `NAME.part`. |
| 5. Finish | `PartFile.finish()` | Renames `NAME.part` to `NAME`. |

Steps 1 and 4 run inside `with_retries()`. It handles everything that can go wrong, using `sorted_errors()`, which sorts each failure into one of two kinds:
- **`TryAgain`:** a problem that may pass, such as a dropped connection, a timeout or a busy server.
- **`Fatal`:** a problem waiting won't fix, such as a 404 or a full disk.

**The status code to know:** when we ask for part of a file, **206 Partial Content** means the server sent exactly that part, so it can resume. **200 OK** means it ignored the request and is sending the whole file.

## When things go wrong

| What happens | What it does |
|---|---|
| You press Ctrl+C | Keeps `NAME.part`. Run the same command again and it continues from the last byte. |
| The connection drops or times out | Waits 1, 2, 4, 8… seconds, then asks for the rest with `Range: bytes=<where it stopped>-`. |
| The server says 429 or 503 (too busy) | Waits as long as the server's `Retry-After` header says. |
| The file changed on the server since you paused | Notices the size or fingerprint changed and starts over. Otherwise you'd get half of the old file glued to half of the new one. |
| The server can't resume | Says so, and starts over when it has to. |
| 404 or 403 | Says what the server said and stops. Nothing is left behind. |
| 8 failures in a row with no progress | Gives up, keeping `.part` so you can try later. |

## Try the bad situations yourself

`testserver.py` serves a made-up 20 MB file slowly (4 MB/s), so you have time to press Ctrl+C. Run it in one terminal and `download.py` in another:

```
python3 testserver.py                # normal
python3 testserver.py --drop 5       # cuts the connection after 5 MB
python3 testserver.py --busy         # first answer is 429 "too busy"
python3 testserver.py --no-ranges    # a server that can't resume
python3 testserver.py --version 2    # the same link, but the file changed

python3 download.py http://127.0.0.1:8000/test.bin
```

## Not in this phase yet

Many connections at once (phase 2), the halo animation, and agent / Claude Code output.
