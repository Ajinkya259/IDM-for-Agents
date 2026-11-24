---
name: idm
description: Download files from links with idm, a download manager made for agents. Use it whenever the user asks to download, fetch, grab or save a file from a URL (a dataset, video, installer, PDF, archive, model weights), or pastes a link to a file, instead of curl or wget.
---

# Downloading with idm

`idm` is on the PATH. Every command replies with **one line of JSON**, and on failure says
what went wrong (`error.code`), whether trying again helps, and a `hint`. When there is
something to run next, the reply has `next`: the exact command.

## The usual flow

1. Run `idm get URL -d FOLDER` (FOLDER: where the user wants it; the current folder if they
   didn't say). It downloads, and replies within 90 seconds.
2. Read the reply:
   - `"state": "done"`: the file is at `file`. Tell the user where.
   - `"state": "downloading"`: a big file. It carries on in the background, so run `next`
     (`idm wait ID`) until it is done. Tell the user how far along it is (`percent`, `eta`).
   - `"ok": false`: look up `error.code` below, and run `next` when there is one.
3. Don't switch to curl or wget when idm reports an error: the error already says what is
   wrong, and the other tool would only hit the same wall (or save an error page as the file).

## When it fails

| `error.code` | What to do |
|---|---|
| `RATE_LIMITED`, `SERVER_ERROR`, `NETWORK` | idm already waited and retried. Run `next` (`idm resume ID`) once; if it fails again, tell the user the server is having trouble. |
| `FILE_CHANGED` | The file changed on the server while it downloaded. Run `next`: it starts over with the new version. |
| `INTERRUPTED` | The background download stopped (killed, or the computer slept). Run `next`: it carries on from where it was. |
| `NOT_FOUND` | The link is wrong or has expired. Tell the user and ask for a fresh link. Don't guess other links. |
| `AUTH_REQUIRED` | The site wants a login. Ask the user to export their browser cookies as a `cookies.txt` file (a "Get cookies.txt" browser extension does it) and tell you where it is; then run `next` with that path in place of `<cookies.txt>`. A token works too: `--header 'Authorization: Bearer TOKEN'`. |
| `NOT_A_FILE` | The link opens a web page, not a file. `error.links` lists the files that page links to: when there is one, `next` downloads it; when there are several, pick the one the user means or ask. None? Look at the page yourself, or ask. Use `--allow-page` only if the user clearly wants the page itself. |
| `TLS` | The site's certificate can't be verified. Tell the user; don't work around it. |
| `DISK_FULL` | Tell the user; after they free space, run `next`. |
| `NO_PERMISSION`, `WRITE_FAILED` | Pick another folder with `-d`. |
| `BAD_URL`, `BAD_INPUT` | Fix the command. |
| `INTERNAL` | A bug in idm: tell the user (details in the job's worker.log). |

## Good to know

- Running the same `idm get` (same link, same folder) twice, even at once, never downloads
  twice: it re-attaches to the running download, or replies `"already": true`.
- `idm get` and `idm wait ID` wait up to 90 s a call (`--max SECONDS` to change it): don't
  add `sleep` loops.
- A `note` in a reply says something worth telling the user, like "starting over: the file
  changed".
- Other commands: `idm start URL` (start in the background, reply at once), `idm status ID`,
  `idm list` (the latest 20), `idm pause ID`, `idm resume ID`, `idm cancel ID` (stops it and
  deletes the partial file; a finished file stays).
- Options for get/start: `-o NAME`, `-d FOLDER`, `--header 'Name: value'`,
  `--cookies FILE`, `--connections N` (1-16, default 8), `--again` (download again).
- Exit codes: 0 fine, 1 failed, 2 bad command, 3 failed but worth retrying (run `next`).
- The person sees the download as halo, an animated face, in their terminal; you only need
  the JSON.
