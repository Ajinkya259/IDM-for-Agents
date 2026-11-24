# IDM for Agents

A download manager made for AI agents: files, and videos from YouTube, Instagram and more. `idm` gives an agent one line of JSON per step, a fixed error code with the exact command to run next, and never saves an error page as your file. Written from scratch in Python 3.9+, with nothing else to install.

<img src="docs/halo-download.gif" width="560" alt="halo during a download: a lavender face inside a ring of 32 dots. The dots fly home one by one as the percent rises, the caption shows MB done, speed, 8 connections and time left, and at 100% the face cheers.">

## Install

As a command, for any agent or terminal:

```
pipx install git+https://github.com/Ajinkya259/IDM-for-Agents
```

As a Claude Code plugin, which adds a skill (when to use idm, what to do on each error) and halo, the face above, under every download:

```
/plugin install idm-for-agents --marketplace Ajinkya259/IDM-for-Agents
```

## Use

```
$ idm get https://example.com/big.iso -d ~/Downloads
{"ok": true, "job": "3f9a1c", "state": "downloading", "percent": 7, "eta": 158, "next": "idm wait 3f9a1c"}

idm get https://youtu.be/ID       a video (Instagram reels, TikTok, X... too); --audio for the sound
idm start URL --at 02:00          start at 2 am (or --in 2h)
idm start URL --after 3f9a1c      start once that download has ended
idm start URL --limit 2M          at most 2 MB a second (idm limit ID changes it as it runs)
idm wait | status | pause | resume | cancel ID      idm list      idm watch
```

Watching in a terminal, space pauses and resumes, and q leaves it running. Videos need [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp ffmpeg`).

<img src="docs/halo-in-claude-code.gif" width="720" alt="A Claude Code session. The user says: download this. The agent loads the idm skill and runs idm get; halo fills its ring under the command, then the command returns one JSON object and the agent tells the user where the file is.">

## What it does for an agent

- **Clear failures.** 18 error codes (`NOT_FOUND`, `AUTH_REQUIRED`, `RATE_LIMITED`...), each with a `hint` and, where there is one, `next`: the exact command to run.
- **Fast and safe.** Up to 16 connections on one file, resume after any stop, and an ETag check on every piece, so a file that changes mid-download is never saved as a mix.
- **Never cut off.** Big downloads run in the background; `idm get` replies within 90 seconds and `idm wait` picks up from there.
- **Patient, not stuck.** A silent server shows up as `stalled`, and after 2 minutes with no byte idm stops and says so.

## Tested

156 checks in `tests/run_tests.py`, and an eval in which real Claude Code agents download from a server that misbehaves on purpose. With idm, 28 of 28 runs got the right file or told the user why; with plain curl, 26 of 28 (twice it saved the server's error reply as the file and called it done). On a slow server, idm took 112 s where curl took 207 s.

## More

- [docs/GUIDE.md](docs/GUIDE.md): everything in detail. Covers the agent contract, every error code, halo, the queue, the eval and how the code is laid out.
- How it was built: one phase at a time, each tested before the next ([phases 1 to 5 as they were](https://github.com/Ajinkya259/IDM-for-Agents/tree/f5c8106)).

Private for now. No license yet.
