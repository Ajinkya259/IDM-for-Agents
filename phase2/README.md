# Phase 2: a CLI that AI agents can rely on

`idm` downloads files, and is built so an AI agent (Claude Code, Codex, Cursor...) can use it without anyone watching:

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
| `FILE_CHANGED` | the file changed on the server mid-download | yes (starts over) |
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
| `idm/engine.py` | The download itself: phase 1's engine, now quiet and reporting through `Report`. |
| `idm/errors.py` | The error codes, their hints, and `classify()`, which turns any exception into one. |

A job's folder holds:
- `job.json`: what to download.
- `status.json`: how it's going. The worker rewrites it about 5 times a second, and the animated face will read it in phase 4.
- `worker.log`: what the worker printed, for debugging.

## Tests

```
python3 tests/run_tests.py        # every command and error code, against a misbehaving local server
python3 tests/testserver.py       # the server on its own, to try things by hand
```
