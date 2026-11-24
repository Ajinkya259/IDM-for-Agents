# Phase 3: many connections at once

Phase 2's `idm`, now downloading a file over up to 8 connections at once, the way IDM does.

## How it works

The file isn't cut into fixed parts up front. It's cut as the download goes:

```
            0 MB                                                   40 MB
start       1▶──────────────────────────────────────────────────────────   1 has the whole file
2 joins     1▶────────────────────────────2▶────────────────────────────   2 takes the second half of it
3 joins     ██1▶──────────────────────────2▶─────────────3▶─────────────   3 takes half of the biggest piece left: 2's
later       ██████████████1▶──────────────████████2▶─────███████████████   3 is done...
3 is free   ██████████████1▶──────3▶──────████████2▶─────███████████████   ...so it takes half of what 1 has left

            █ saved   ─ still missing   N▶ connection N, writing there
```

- **A free connection takes the second half of the biggest piece still missing.** That piece's owner carries on with the first half. So no connection sits idle while there's work left, and the last pieces are small: nobody waits on one slow connection with a big piece.
- **Pieces under 2 MB aren't split,** so a small file uses one connection.
- **Each connection writes straight into its place in the `.part` file,** which is made full size up front. There's nothing to join at the end.
- **The `.part.json` notes list the pieces still missing** (`"missing": [[from, to], ...]`, updated every second). A pause, a crash or a killed process carries on exactly where it stopped. A `.part` file left by phase 2 resumes too.
- **If the server pushes back** (429, 503: too many connections), the extra connections step back and hand their pieces to the others. The last one never steps back: it waits and retries. So a server that allows only 2 connections still works.
- **A server that can't send parts of a file** (no Range support) gets one connection, start to end, as in phase 2.

`--connections N` sets the most at once (1 to 16, default 8). `status.json` has `connections`, the number working right now, for the face in phase 4.

**Is it faster?** Only when the server, not your line, is the limit. A server that sends each connection 2 MB/s sends 8 connections about 16 MB/s; a line that tops out at 4 MB/s stays at 4 MB/s. On the local test server, 6 MB took 4.0 s over one connection and 1.3 s over several. From proof.ovh.net over a home line, 10 MB took 3.8 s alone and 3.2 s with 8.

## The agent contract (from phase 2)

`idm` is built so an AI agent (Claude Code, Codex, Cursor...) can use it without anyone watching:

- **Every reply is one line of JSON.** It's short, so it costs few tokens.
- **Every failure has a fixed code,** says whether trying again can help, and gives a hint saying what to do.
- **Every reply includes `next`,** the exact command to run next, when there is one.
- **Downloads run in the background.** An agent's tool call is never cut off halfway (Claude Code cuts them off at about 2 minutes by default).

People get readable text and a progress line instead of JSON.

```
bin/idm get URL                 download it and wait (an agent gets a reply within 90 s)
bin/idm start URL               start in the background and reply at once
bin/idm wait ID [--max S]       wait for it to finish
bin/idm status ID               how it's going right now
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
| `idm/cli.py` | The commands, and what people and agents see. Start at `cmd_get()`. |
| `idm/jobs.py` | Background jobs. Each one has a folder under `~/.idm/jobs/<id>/`. |
| `idm/engine.py` | The download, step by step: `download()`. It talks to the server and keeps the `.part` file. |
| `idm/crew.py` | Many connections on one file: `Missing` (the pieces, and splitting the biggest) and `Crew` (the connections). |
| `idm/errors.py` | The error codes, their hints, `classify()` (any exception as a code), and the retry waits. |

A job's folder holds:
- `job.json`: what to download.
- `status.json`: how it's going. The worker rewrites it about 5 times a second, and the animated face will read it in phase 4.
- `worker.log`: what the worker printed, for debugging.

## Tests

```
python3 tests/run_tests.py        # 68 checks: every command, every error code, the pieces
python3 tests/testserver.py       # the misbehaving local server on its own, to try things by hand
python3 tests/testserver.py --max-connections 2     # ...one that allows only 2 connections at once
```
