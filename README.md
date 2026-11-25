# IDM for Agents

A download manager for AI agents. The agent runs `idm get URL` and gets one line of JSON back: the file's path, or an error code with the command to run next.

<img src="site/assets/dotty-download.gif" width="520" alt="dotty, the download face: a ring of 32 dots fills as the file arrives, with MB done, speed, connections and time left underneath.">

**[Project page](https://ajinkya259.github.io/IDM-for-Agents/)** · MIT license

## Install

```
pipx install git+https://github.com/Ajinkya259/IDM-for-Agents
```

Python 3.9+. On 3.11+ it also brings yt-dlp, ffmpeg and deno, so videos work too. `uv tool install` works the same way.

As a Claude Code plugin (adds a skill and shows dotty under each download):

```
/plugin marketplace add Ajinkya259/IDM-for-Agents
/plugin install idm-for-agents@idm-for-agents
```

## Use

```
$ idm get https://example.com/data.zip
{"ok": true, "job": "3f9a1c", "state": "done",
 "file": "/Users/you/Downloads/data.zip", "bytes": 52428800, "seconds": 6.1}

$ idm get https://example.com/members/report.pdf
{"ok": false, "job": "77b0e2", "state": "failed",
 "error": {"code": "AUTH_REQUIRED", "message": "the server said 403 Forbidden", "retryable": false, "hint": "..."},
 "next": "idm get https://example.com/members/report.pdf -d /Users/you --cookies <cookies.txt>"}
```

- **Files, share links, videos.** Google Drive, Dropbox, OneDrive, GitHub and Hugging Face links point at the file itself. YouTube, Instagram, TikTok and more go through yt-dlp.
- **Fast and safe.** Up to 16 connections, resumes after any stop, and never saves an error page as the file.
- **Errors an agent can act on.** 18 codes, each with a hint and the next command to run.

## Commands

```
idm get URL [-d FOLDER]       download and wait (replies within 90 s)
idm start URL                 start in the background (--at 02:00, --after ID, --limit 2M)
idm wait | status | pause | resume | cancel ID
idm watch [ID]                show dotty
idm list                      the latest 20 downloads

options: --cookies FILE   --header 'NAME: VALUE'   -o NAME   --connections N   --audio
```

## dotty

The face you watch while a download runs. Each of its 32 dots is 1/32 of the file.

<img src="site/assets/dotty-faces.svg" width="520" alt="dotty's five faces: starting, downloading, paused, trouble, done.">

starting · downloading · paused · trouble · done

## Tested

Real Claude Code agents (Sonnet) downloading from a server that fails on purpose, 7 ways, 5 runs each, checked byte for byte:

| | Runs that ended right | 10 MB from a slow server |
|---|---|---|
| idm | 35 / 35 | 113 s |
| curl, wget… | 33 / 35 | 206 s |

Both curl misses were a server answering 429 Too Many Requests: curl saved the empty reply as a 0-byte file, and the agent took it for an empty file. `python3 tests/run_tests.py` runs 186 checks, no internet needed.

## License

MIT. Made by [Ajinkya Sambare](https://ajinkya.site). Not affiliated with Internet Download Manager (IDM) or Tonec Inc.
