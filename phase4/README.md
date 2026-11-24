# Phase 4: halo, the face

Phase 3's `idm`, now with **halo**: a face that shows a download while it runs. It appears in a terminal, and in Claude Code right under the agent's command.

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

## Where you see it

**In a terminal.** `idm get` and `idm wait` show halo instead of the progress line, in a true-colour terminal at least 56 × 23. A smaller one still gets the line. `idm watch` is for a second terminal while an agent works:

```
bin/idm watch                   every download, as it happens, until Ctrl+C
bin/idm watch ID                that one, until it ends (a link works too)
```

Ctrl+C in `get` pauses the download. In `wait` or `watch` it only stops watching.

**In Claude Code.** This folder is also a Claude Code plugin (`.claude-plugin/`, `hooks/`). While the agent runs `idm get` or `idm wait`, halo plays under its command:

```
claude --plugin-dir ~/Desktop/untitled/IDM/phase4
```

```
the agent runs:   idm get https://…/100Mb.dat
                        │
the mod sees it ────────┤   (hooks/halo.tsx, a tool.call hook on Bash)
and starts:       idm watch https://…/100Mb.dat --cells
                        │   reads ~/.idm/jobs/<id>/status.json 10 times a second and
                        │   prints each frame of halo as terminal cells, one JSON line each
                        ▼
the mod paints each frame under the agent's command (a Raster element, repainted in place)
```

- **The agent spends no tokens on it.** It still gets its one line of JSON. The face is drawn beside it, from the same `status.json` the worker keeps.
- **One face, drawn in one place.** `idm/halo.py` draws halo as a grid of cells. A terminal gets them as text with colour codes, Claude Code as packed cells. The mod (about 100 lines) only starts the watcher and paints what it sends.
- **When the command ends,** the face stays if the download finished (happy, 100%). Otherwise it goes away, and the next `idm wait` brings it back.
- **The desktop and mobile apps,** which can't draw cells, get a progress bar with the same words.

## How halo is drawn

`Halo.draw(status, time)` makes one picture from a job's `status.json`:

- **The ring:** 32 dots. Dot *i* stands for the *i*-th 32nd of the file. It floats, bobbing gently, until that much of the file has arrived. Then it flies home in 0.8 s (quick, then settling) and stays there. The next dot to come in twinkles.
- **The face:** a disc of chunky pixels (two columns by one row each, so they look square), with blush and two eyes. The eyes and mouth are characters (`● ◡`, `╥ ⌒`, `^ ▽`), so they stay thin. The % goes under the mouth.
- **The words:** the file name, how much is done, the speed and connections, and the time left. They become the server's complaint while it pushes back, and how to carry on while paused or after a failure.

The colours are the "lavender" set picked from `mockups/experiments.py`. The look is the one approved there: beads ring, glyph face.

## Many connections (from phase 3)

A free connection takes the second half of the biggest piece still missing, and every connection writes straight into its place in the `.part` file. The `.part.json` notes list the missing pieces, so a pause or crash resumes exactly. `--connections N` sets the most at once (1 to 16, default 8). See [phase 3](../phase3/README.md#how-it-works) for the diagram.

## The agent contract (from phase 2)

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
| `NOT_A_FILE` | the link is a web page, not a file | no (`next` adds `--allow-page`) |
| `HTTP_ERROR` | any other 4xx | no |
| `RATE_LIMITED` | 429, after idm's own retries | yes |
| `SERVER_ERROR` | 5xx, after idm's own retries | yes |
| `NETWORK` | offline, DNS, timeouts, a cut connection, after retries | yes |
| `TLS` | the certificate can't be verified | no (tell the user) |
| `FILE_CHANGED` | the file changed on the server mid-download (or got smaller: 416) | yes (starts over) |
| `DISK_FULL`, `NO_PERMISSION`, `WRITE_FAILED` | our side | full: yes; the others: no |
| `INTERRUPTED` | the background process died (killed, machine slept) | yes (resumes) |
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
| `idm/cli.py` | The commands, and what people and agents see. Start at `cmd_get()`; `cmd_watch()` is new. |
| `idm/halo.py` | **New:** the face. `Halo.draw()` turns a status into a `Picture` of cells; `ansi()` and `packed()` put it on screen. |
| `idm/jobs.py` | Background jobs. Each one has a folder under `~/.idm/jobs/<id>/`. |
| `idm/engine.py` | The download, step by step: `download()`. It talks to the server and keeps the `.part` file. |
| `idm/crew.py` | Many connections on one file: `Missing` (the pieces, and splitting the biggest) and `Crew` (the connections). |
| `idm/errors.py` | The error codes, their hints, `classify()` (any exception as a code), and the retry waits. |
| `hooks/halo.tsx` | **New:** the Claude Code mod. It starts `idm watch --cells` for the agent's command and paints the frames. |
| `.claude-plugin/plugin.json` | **New:** what makes this folder a Claude Code plugin. |

A job's folder holds:
- `job.json`: what to download.
- `status.json`: how it's going, rewritten about 5 times a second. halo is drawn from it alone.
- `worker.log`: what the worker printed, for debugging.

## Tests

```
python3 tests/run_tests.py        # every command, every error code, the pieces, and halo
claude plugin test .              # the mod: which commands it watches, the face under them
python3 tests/testserver.py       # the misbehaving local server on its own, to try things by hand
```
