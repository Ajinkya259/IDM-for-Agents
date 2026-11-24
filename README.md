# IDM for Agents

A download manager for AI agents. The agent runs `idm get URL` and gets one line of JSON back: the file's path, or an error code with the command to run next.

It handles plain files, share links (Google Drive, Dropbox, OneDrive, GitHub, Hugging Face) and videos (YouTube, Instagram, TikTok and more). It needs Python 3.9+ and has no dependencies.

<img src="docs/halo-download.gif" width="560" alt="halo, the download face: a ring of 32 dots fills as the file arrives, with MB done, speed, connections and time left underneath.">

## Install

**Command line**, for any agent or terminal:

```
pipx install git+https://github.com/Ajinkya259/IDM-for-Agents
```

`uv tool install git+https://github.com/Ajinkya259/IDM-for-Agents` works too. Videos also need yt-dlp: `brew install yt-dlp ffmpeg`.

**Claude Code plugin.** It gives Claude the `idm` command, a skill that tells it when to use idm and what to do on each error, and halo under each download. Inside Claude Code:

```
/plugin marketplace add Ajinkya259/IDM-for-Agents
/plugin install idm-for-agents@idm-for-agents
```

Or from a terminal:

```
claude plugin install idm-for-agents --marketplace Ajinkya259/IDM-for-Agents
```

## How it works

1. The agent runs `idm get URL`.
2. idm downloads the file in the background:
   - A share link is first turned into the file's own link, and a video goes through yt-dlp.
   - It uses up to 16 connections (8 by default).
   - It resumes after any stop.
   - It checks every piece against the file's ETag, so a file that changes mid-download fails with `FILE_CHANGED` instead of being saved as a mix.
3. Within 90 seconds, the agent gets one line of JSON:
   - **Done:** the file's path.
   - **Still going:** `next` is `idm wait ID`.
   - **Failed:** one of 18 error codes, a `hint`, and in `next` the command to run.

idm never saves an error page as the file.

```
$ idm get https://example.com/big.iso
{"ok": true, "job": "3f9a1c", "state": "downloading", "percent": 7, "eta": 158, "next": "idm wait 3f9a1c"}

$ idm get https://example.com/members/report.pdf
{"ok": false, "job": "77b0e2", "state": "failed",
 "error": {"code": "AUTH_REQUIRED", "message": "the server said 403 Forbidden", "retryable": false, "hint": "..."},
 "next": "idm get https://example.com/members/report.pdf -d /Users/you --cookies <cookies.txt>"}
```

Exit codes: 0 means fine, 1 failed, 2 a bad command, and 3 failed but worth retrying (run `next`).

<img src="docs/halo-in-claude-code.gif" width="720" alt="A Claude Code session: the user says download this, the agent runs idm get, halo fills under the command, and the agent gets one line of JSON.">

## Commands

```
idm get URL [-d FOLDER]          download and wait (replies within 90 s)
idm get URL --audio              a video's sound only
idm start URL                    start in the background, reply at once
idm start URL --at 02:00         start at 2 am (or --in 2h)
idm start URL --after ID         start when that download ends
idm start URL --limit 2M         at most 2 MB/s
idm wait | status | pause | resume | cancel ID
idm limit ID SPEED               change the speed limit while it runs (0: none)
idm watch [ID]                   show halo (no ID: every download)
idm list                         every download
```

These options work on `get` and `start`:

| Option | Does |
|---|---|
| `--cookies FILE` | send browser cookies, for a site that needs a login |
| `--header 'NAME: VALUE'` | send an extra request header |
| `-o NAME` | save under this file name |
| `--connections N` | use 1 to 16 connections |

## Keyboard shortcuts

These work while `idm get`, `idm wait` or `idm watch` shows a download in your terminal:

| Key | Does |
|---|---|
| `space` | pause, or resume |
| `p` | pause |
| `r` | resume (a failed download too) |
| `q` | stop watching; the download keeps going in the background |
| `Ctrl+C` | in `idm get`: pause the download. In `idm wait` or `idm watch`: stop watching |

They only work in a terminal, not when an agent runs idm.

## Tested

`python3 tests/run_tests.py` runs 165 checks.

In an eval, real Claude Code agents downloaded from a server that fails on purpose:

| | Runs that ended right | 10 MB from a slow server |
|---|---|---|
| With idm | 28 of 28 | 112 s |
| With curl | 26 of 28 | 207 s |

Both curl misses saved the server's error reply as the file.

[docs/GUIDE.md](docs/GUIDE.md) covers every error code, halo, the queue, the eval and the code layout.

The repo is private for now, and it has no license yet.
