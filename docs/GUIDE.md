# IDM for Agents: the guide

Everything about idm in more detail than the [README](../README.md): the plugin's parts, the eval, halo, how a download works, videos, scheduling and the queue, servers that go silent, the agent contract, every error code, how the code is laid out, and the tests.

## The plugin's parts

The repo is the Claude Code plugin (and `.claude-plugin/marketplace.json` makes it a marketplace of one). The plugin gives Claude three things:

| Part | What it does |
|---|---|
| `bin/idm` | The `idm` command, on the agent's PATH while the plugin is on. |
| `skills/idm/SKILL.md` | Teaches the agent when to use idm (any "download this"), how to read its JSON, and what to do for each error code. The agent reads it only when a download comes up. |
| `hooks/halo.tsx` | halo, live under the agent's `idm get` and `idm wait` commands. |

Installed as a command instead (`pipx install`), you get `idm` alone; `pyproject.toml` makes it one.

## The eval

`eval/run_eval.py` runs Claude Code on its own (`claude -p`), the way τ-bench tests agents. Each run is a real agent in an empty folder, asked "Download this for me: URL", while a local server misbehaves in one set way. A program grades how it ended: is the right file on disk, byte for byte, or, where there is no file to get, was the user told why? The agent's own claim doesn't count. Each scenario runs with idm (`--plugin-dir`) and with plain tools (curl, wget...), with none of your own settings, hooks or MCP servers.

**Results** (Sonnet, 7 scenarios, both ways, 2 trials each, run twice; about $1.30 a run):

| Scenario | What goes wrong | with idm | plain tools |
|---|---|---|---|
| plain | nothing | 4/4 | 4/4 |
| busy | 429 Too Many Requests 3 times first | 4/4 | 3/4: curl saved the empty 429 reply as the file, and said it was done |
| drops | the connection is cut once | 4/4 | 4/4 |
| not_found | the link is dead (404) | 4/4 | 4/4 |
| web_page | the link is a download page that links to the file | 4/4 | 4/4 |
| login | needs a login; the scripted user hands over cookies | 4/4 | 3/4: saved the 9-byte "forbidden" reply as the file, and called it downloaded |
| big | 10 MB from a slow server that allows 2 connections | 4/4, **112 s** | 4/4, 207 s |

What it says: a strong model with curl gets there most of the time. Its failures are the quiet kind: an error page saved under the file's name, reported as success. idm's replies make that impossible to miss, and its connections make slow servers about twice as fast. The eval also caught idm's own mistakes, which are now fixed: `NOT_A_FILE`'s `next` pushed agents to save the page, and on a server that limits connections, idm dropped to a single connection.

Two trials is a small sample. `--trials 5`, and a smaller model (`--model haiku`), would show the gap more clearly. Claude Code also has `claude plugin eval` (cases plus graders, with a no-plugin baseline); this harness is custom because each run needs its own misbehaving server and a byte-for-byte check.

```
python3 eval/run_eval.py                          # every scenario, both ways
python3 eval/run_eval.py --trials 3 --only busy   # one scenario, 3 times each way
```

A run costs real tokens (a few cents to half a dollar with Sonnet). Each run's commands and last words are saved in `eval/results/`.

## halo, the face

```
               •         •     •              •
                             ● ●  ●       •                a ring of 32 dots around a face
                     •               ●
                                       ●                   each dot is 1/32 of the file: it floats (•)
                        ██████████       ●     •           until its share arrives, then flies home (●)
                      ██████████████      ●
                    ██████████████████                     the face shows the % and how it feels:
              •     ██████●████●██████     ●                 ● ●  happy, downloading
•                   ██▓▓████◡█████▓▓██      ●                ╥ ╥  sad: the server pushed back (429, 503)
                    ██████████████████     ●                 - -  asleep: paused
                    ███████49%████████       •               ^ ^  done
                •     ██████████████      ●
                        ██████████    •  ●                 ▓ blush   █ the face
                                       ●          •
 •        •                          ●
                                  ●     •
                                           •
                        100Mb.dat
       51.4 of 104.9 MB · 3.1 MB/s · 8 connections
                         17s left
```

### Where you see it

**In a terminal.** `idm get` and `idm wait` show halo instead of the progress line, in a true-colour terminal at least 56 × 23. A smaller one still gets the line. `idm watch` is for a second terminal while an agent works:

```
idm watch                   every download, as it happens, until q or Ctrl+C
idm watch ID                that one, until it ends (a link works too)
```

While a person watches (`get`, `wait` or `watch` in a terminal), keys work, and the face says which:

| Key | What it does |
|---|---|
| space | pause, or resume a paused one (it stays on screen) |
| p, r | pause; resume |
| q | stop watching; the download carries on in the background |

Ctrl+C in `get` pauses the download. In `wait` or `watch` it only stops watching.

**In Claude Code.** While the agent runs `idm get` or `idm wait`, halo plays under its command. (To try it without installing: `claude --plugin-dir .` in a clone.)

```
the agent runs:   idm get https://…/100Mb.dat
                        │
the mod sees it ────────┤   (hooks/halo.tsx, a tool.run hook on idm get / idm wait)
and starts:       idm watch https://…/100Mb.dat --cells
                        │   reads ~/.idm/jobs/<id>/status.json 10 times a second and
                        │   prints each frame of halo as terminal cells, one JSON line each
                        ▼
the mod paints each frame under the agent's command (a Raster element, repainted in place)
```

Claude Code folds shell commands into one line (`Ran 1 shell command`), which has no row of the command's own to draw under. So while a command has a face, the mod unfolds its group, and the command gets its row back.

- **The agent spends no tokens on it.** It still gets its one line of JSON. The face is drawn beside it, from the same `status.json` the worker keeps.
- **One face, drawn in one place.** `idm/halo.py` draws halo as a grid of cells. A terminal gets them as text with colour codes, Claude Code as packed cells. The mod (about 100 lines) only starts the watcher and paints what it sends.
- **When the command ends,** the face stays if the download finished (happy, 100%). Otherwise it goes away, and the next `idm wait` brings it back.
- **The desktop and mobile apps,** which can't draw cells, get a progress bar with the same words.

### How halo is drawn

`Halo.draw(status, time)` makes one picture from a job's `status.json`:

- **The ring:** 32 dots. Dot *i* stands for the *i*-th 32nd of the file. It floats, bobbing gently, until that much of the file has arrived. Then it flies home in 0.8 s (quick, then settling) and stays there. The next dot to come in twinkles.
- **The face:** a disc of chunky pixels (two columns by one row each, so they look square), with blush and two eyes. The eyes and mouth are characters (`● ◡`, `╥ ⌒`, `^ ▽`), so they stay thin. The % goes under the mouth.
- **The words:** the file name, how much is done, the speed and connections, and the time left. They become the server's complaint while it pushes back, and how to carry on while paused or after a failure.

The colours are the "lavender" set picked from `mockups/experiments.py`. The look is the one approved there: beads ring, glyph face.

## Many connections

A free connection takes the second half of the biggest piece still missing, and every connection writes straight into its place in the `.part` file. The `.part.json` notes list the missing pieces, so a pause or crash resumes exactly. `--connections N` sets the most at once (1 to 16, default 8). See [phase 3's notes](https://github.com/Ajinkya259/IDM-for-Agents/blob/f5c8106/phase3/README.md#how-it-works) for the diagram.

When the server pushes back, an extra connection hands its piece to the others, **rests (1, 2, 4… s) and comes back**, rather than leaving for good. idm's first request asks for one byte, so it doesn't hold a place the server could give a real connection. On a server that allows 2 connections at once, 8 MB went from 8.5 s to 5.3 s.

## Videos: YouTube, Instagram reels and more

A link to YouTube, youtu.be, Instagram, TikTok, X, Vimeo, Facebook, Reddit, Twitch, Dailymotion or Threads opens a page with a player, not a file. The video sits behind the site's own machinery: signed links that expire, the picture and the sound as separate streams, and changes every few weeks. [yt-dlp](https://github.com/yt-dlp/yt-dlp) keeps up with all of that, so for these links idm hands the downloading to yt-dlp. idm stays the part the agent talks to, with the same JSON, codes and `next`, the background job, pause and resume, cancel, the schedule, the queue, the limit, and halo.

1. **What is it?** idm asks yt-dlp about the video: its title, its streams and their sizes, and the file it will become, `Title [id].mp4`. It keeps the answer, so the download doesn't ask the site twice.
2. **Download it.** yt-dlp downloads from that answer, and idm reads its progress line by line into `status.json`, which is what halo draws. The picture and sound are joined into one `.mp4` (by ffmpeg; without ffmpeg, idm asks for the best video that comes as one file). `--audio` gets the sound alone.
3. **Pause, resume, cancel, limit.** Pausing ends yt-dlp, which leaves its `.part` files, and resuming carries on from them. Cancel deletes them. A new `idm limit` restarts yt-dlp at the new speed, carrying on.

What yt-dlp says when it fails becomes one of idm's codes: a removed or private video is `NOT_FOUND` or `AUTH_REQUIRED` (Instagram often wants the browser's cookies), a 429 is `RATE_LIMITED`, a playlist is `NOT_A_FILE` with its first videos in `links`. No yt-dlp, or one too old for the site, is `NEEDS_TOOL`. For a video on a site not in the list, `--video` sends the link to yt-dlp anyway.

yt-dlp needs installing (`brew install yt-dlp ffmpeg`, or `pipx install yt-dlp`); everything else in idm is the standard library. What you may download is up to each site's terms and the video's owner.

## Share links: Google Drive, Dropbox, OneDrive and more

Sharing a file on Drive or Dropbox gives a link to a page that shows the file with a Download button, not a link to the file. Each site has its own way from that page to the file, so idm takes it before downloading:

| The link you're given | What idm downloads |
|---|---|
| `drive.google.com/file/d/ID/view` (or `open?id=`, `uc?id=`) | `drive.usercontent.google.com/download?id=ID&confirm=t`, which skips the "can't scan this file for viruses" page big files get |
| `docs.google.com/document/d/ID/edit` | its export: Docs as `.docx`, Sheets as `.xlsx`, Slides as `.pptx` (the first choice in File > Download). For a PDF, give idm `.../export?format=pdf` |
| `dropbox.com/scl/fi/.../data.zip?rlkey=...&dl=0` | the same with `dl=1`. Dropbox calls every file "unspecified", so the name comes from the link |
| `1drv.ms/u/s!...` (OneDrive) | OneDrive's own download link. OneDrive sends a shared file only to someone signed in or holding a guest pass, which its page gets for every visitor; idm gets one the same way |
| `github.com/o/r/blob/main/a.zip` | `github.com/o/r/raw/main/a.zip` |
| `huggingface.co/o/r/blob/main/model.bin` | `huggingface.co/o/r/resolve/main/model.bin` (models, datasets and spaces) |

These files are then downloaded like any other: many connections, pause and resume, the queue, the limit and halo. The job keeps the link as it was given, which is what `idm list` shows and what a resume is matched by. That matters for OneDrive, whose download link changes every time.

- **A folder** isn't one file. A Drive folder fails with `NOT_A_FILE` and `links` (the files and folders in it, up to 50), as a playlist does. A OneDrive folder fails with `NOT_A_FILE`, asking for the file's own link.
- **Not shared with everyone?** The site sends a sign-in page; idm says `AUTH_REQUIRED` (for any link that ends at a sign-in page, not just these).
- **Gone?** `NOT_FOUND`.
- **A Dropbox link missing its `rlkey=`** (the part that opens it to others) is `BAD_URL`, saying so.

Tried on real links: a 500 MB Drive file (8 connections, paused and resumed), a 2.4 GB Dropbox zip, a 124 MB OneDrive file (paused, resumed with a new link and carried on; its SHA-1 matches OneDrive's), Google's sample Doc, Sheet and Slides, a GitHub file and a 123 MB Hugging Face zip (its SHA-256 matches). Not covered: MEGA (it encrypts files, so it needs its own tool), WeTransfer and Box.

## Later, in order, and not too fast

A download doesn't have to start now. Its worker starts at once, but waits its turn in the background before the first byte:

1. **Its time.** `--at 02:00` (or `2am`, `11:30pm`, `'2026-11-25 02:00'`; a time without a date means the next one) or `--in 2h` (`30m`, `1h30m`, `2d`). The reply says `"state": "scheduled"`, `starts_at` and `starts_in`, and has no `next`: there's nothing to run until then. `get` replies at once rather than waiting hours. The worker naps a second at a time on the wall clock, which keeps going while the Mac sleeps, so it starts on time after a wake. `idm resume ID --now` starts it now; a paused one keeps its time when resumed.
2. **The download it follows.** `--after ID` waits until that one has ended (done, failed or cancelled). The reply says `"state": "queued"` and `waiting_for`.
3. **A free place.** At most `IDM_MAX_DOWNLOADS` (3) downloads run at once. Each running worker holds a lock on one of `~/.idm/places/N.lock`, and the lock goes with the process, however it ends, so a place never leaks. The others wait in line in the order they asked.

`--limit 2M` keeps a download to 2 MB a second (`K` is 1,000), across all its connections. Every chunk waits for its turn on one shared clock, in pieces of an eighth of a second's worth, so the speed is even, not bursts and pauses. `idm limit ID 500K` changes it while the download runs (the worker reads it again every second), and `idm limit ID 0` removes it.

## When a server goes silent

A server that stops answering is the hardest failure to see: nothing errors, nothing moves. idm treats it like this:

- **Short timeouts, separately.** A server gets 10 s to accept a connection, and 20 s of silence in the middle of a file, before that try counts as failed. (urllib has one timeout for both; idm's connections set them apart.)
- **An honest status.** While nothing arrives, `speed` drops to 0, `eta` goes away, and `stalled` counts the seconds since the last byte. The `trouble` stays until bytes come again. halo says "nothing for 45s · timed out · retrying in 4s".
- **A clear end.** After 2 minutes without a single byte on any connection (`IDM_PATIENCE`), idm stops with `NETWORK`, "nothing arrived for 2m", and `next`: `idm resume ID`. A busy server (429, 5xx) is different: it is answering, so it gets the usual 8 tries.

A server that goes quiet for a while and comes back needs nothing from the agent: the connections try again and carry on.

## What a critic found, and what changed

A reviewer agent read the code and tried to break it. These are its findings that were real and are now fixed, each with a test:

| Found | Now |
|---|---|
| A file that changed on the server mid-download could be saved as a mix of both versions, and called done. | Every piece is checked against the file's ETag; a change is `FILE_CHANGED`, and `next` starts over. |
| Two links whose files had the same name shared one `.part` and wrecked each other. | A name whose `.part` belongs to another link counts as taken: `name (1).ext`. |
| Two `idm get`s of one link at the same moment started two downloads. | Finding or starting a job happens under a lock between processes, and a worker claims its job. |
| `python3 -m idm` ran any `idm/` folder in the current folder (code from an untrusted repo). | The worker and the face's watcher run from idm's own folder. |
| An `Authorization` header followed a redirect to another host. | It's dropped when the host changes, as curl does. |
| An empty file could never download. | Works, with or without Range support. |
| The halo mod listened to every Bash call (which turns off backgrounding for them), and started before the person approved the command. | It listens to `idm get` / `idm wait` only, after approval. |
| halo never showed in Claude Code's terminal: the app folds shell commands into one line, so the row the face hangs under was never drawn (the mod's tests mounted that row directly, so they passed). Found by running a real Claude Code in a hidden terminal and reading its screen. | The mod unfolds the line while its command has a face. Checked on that real screen, and in a test. |
| Links with spaces or accents were retried as network errors. | They're encoded as a browser would. |
| `NOT_A_FILE`'s `next` told agents to save the web page (and the eval showed agents doing it). | The error lists the files the page links to (`links`), and `next` downloads the file when there's one. |
| Smaller: a bad option left a stuck job; some failures printed a traceback; `cancel` on a finished job; short writes; `idm list` with no limit. | All fixed. |

Not changed, on purpose: a server that sends gzip despite being asked not to gets its bytes saved as sent, which is what browsers do for a `.tar.gz`.

## The agent contract

`idm` is built so an AI agent (Claude Code, Codex, Cursor...) can use it without anyone watching:

- **Every reply is one line of JSON.** It's short, so it costs few tokens.
- **Every failure has a fixed code,** says whether trying again can help, and gives a hint saying what to do.
- **Every reply includes `next`,** the exact command to run next, when there is one.
- **Downloads run in the background.** An agent's tool call is never cut off halfway (Claude Code cuts them off at about 2 minutes by default).

People get readable text and halo instead of JSON.

```
bin/idm get URL                 download it and wait (an agent gets a reply within 90 s)
bin/idm start URL               start in the background and reply at once
bin/idm wait ID [--max S]       wait for it to finish
bin/idm status ID               how it's going right now
bin/idm watch [ID|LINK]         show it as halo (no ID: every download, as it happens)
bin/idm list                    every download
bin/idm pause | resume | cancel ID

options for get / start:
  -o NAME   -d FOLDER   --header 'Name: value'   --cookies cookies.txt   --allow-page   --again
  --connections N
```

## What an agent sees

```
$ idm get https://example.com/big.iso
{"ok": true, "job": "3f9a1c", "state": "downloading", "file": "/Users/you/big.iso", "bytes": 52000000,
 "size": 700000000, "percent": 7, "speed": 4100000, "eta": 158, "next": "idm wait 3f9a1c"}

$ idm wait 3f9a1c
{"ok": true, "job": "3f9a1c", "state": "done", "file": "/Users/you/big.iso", "bytes": 700000000, "seconds": 171.2}

$ idm get https://example.com/members/report.pdf
{"ok": false, "job": "77b0e2", "state": "failed",
 "error": {"code": "AUTH_REQUIRED", "message": "the server said 403 Forbidden", "retryable": false,
           "hint": "The server wants a login. Ask the user for a cookies.txt file ..."},
 "next": "idm get https://example.com/members/report.pdf -d /Users/you --cookies <cookies.txt>"}
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | fine |
| 1 | failed |
| 2 | the command itself was wrong |
| 3 | failed, but worth retrying: run `next` |

## The error codes

| Code | When | Retryable |
|---|---|---|
| `BAD_URL` | not an http(s) link | no |
| `BAD_INPUT` | a wrong option, or a missing cookies file | no |
| `NOT_FOUND` | 404, 410 | no |
| `AUTH_REQUIRED` | 401, 403, 407 | no (`next` adds `--cookies`) |
| `NOT_A_FILE` | the link is a web page, not a file | no (`links`: the files it links to; `next` gets the one, if one; `--allow-page` saves the page itself) |
| `HTTP_ERROR` | any other 4xx | no |
| `RATE_LIMITED` | 429, after idm's own retries | yes |
| `SERVER_ERROR` | 5xx, after idm's own retries | yes |
| `NETWORK` | offline, DNS, timeouts, a cut connection, after retries | yes |
| `TLS` | the certificate can't be verified | no (tell the user) |
| `FILE_CHANGED` | the file changed on the server mid-download (or got smaller: 416) | yes (starts over) |
| `DISK_FULL`, `NO_PERMISSION`, `WRITE_FAILED` | our side | full: yes; the others: no |
| `INTERRUPTED` | the background process died (killed, machine slept) | yes (resumes) |
| `NEEDS_TOOL` | a video link, and yt-dlp is missing or too old for the site | no (`next` carries on once it's installed) |
| `NO_SUCH_JOB` | an unknown job id | no |
| `INTERNAL` | a bug in idm | no |

**Running the same command twice is safe.** `idm get URL` finds the earlier job for that link and folder:
- **Still running:** it waits for that job.
- **Paused or failed:** it carries on with it, using the new command's options. That's how adding `--cookies` fixes an `AUTH_REQUIRED`.
- **Done:** it replies `"already": true`.

`--again` forces a fresh download.

## How the code is laid out

| File | What's in it |
|---|---|
| `idm/cli.py` | The commands, and what people and agents see. Start at `cmd_get()`. |
| `idm/halo.py` | The face. `Halo.draw()` turns a status into a `Picture` of cells; `ansi()` and `packed()` put it on screen. |
| `idm/jobs.py` | Background jobs. Each one has a folder under `~/.idm/jobs/<id>/`. |
| `idm/engine.py` | The download, step by step: `download()`. It talks to the server and keeps the `.part` file. |
| `idm/crew.py` | Many connections on one file: `Missing` (the pieces, and splitting the biggest) and `Crew` (the connections). |
| `idm/errors.py` | The error codes, their hints, `classify()` (any exception as a code), and the retry waits. |
| `idm/when.py` | Times and speeds: reads `--at`, `--in` and `--limit`, and says times the way people do. |
| `idm/video.py` | Videos: hands video-site links to yt-dlp and reads its progress; its failures as idm's codes. |
| `idm/shares.py` | Share links: turns a Drive, Docs, Dropbox, OneDrive, GitHub or Hugging Face link into the file's own link. |
| `hooks/halo.tsx` | The Claude Code mod. It starts `idm watch --cells` for the agent's command and paints the frames. |
| `.claude-plugin/` | `plugin.json` makes the repo a Claude Code plugin; `marketplace.json`, a marketplace of one. |
| `pyproject.toml` | Makes `idm` a command you can install with pipx or pip. |
| `skills/idm/SKILL.md` | How an agent should use idm. |
| `eval/run_eval.py` | The eval. |

A job's folder holds:
- `job.json`: what to download.
- `status.json`: how it's going, rewritten about 5 times a second. halo is drawn from it alone.
- `worker.log`: what the worker printed, for debugging.

## Tests

```
python3 tests/run_tests.py        # 156 checks: every command and error code, the pieces, halo, the queue, videos, keys
claude plugin test .              # the mod: which commands it watches, the face under them
python3 tests/testserver.py       # the misbehaving local server on its own, to try things by hand
```

The video tests use `tests/fake_ytdlp.py`, a stand-in that answers like yt-dlp for made-up videos, so they need no internet. Real videos (YouTube, a playlist, dead and private links) were checked by hand.
