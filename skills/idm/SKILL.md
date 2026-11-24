---
name: idm
description: Download files and videos from links with idm, a download manager made for agents. Use it whenever the user asks to download, fetch, grab or save something from a URL (a dataset, installer, PDF, archive, model weights, a YouTube video, an Instagram reel, a TikTok), or pastes a link to one (including Google Drive, Google Docs, Dropbox, OneDrive, GitHub and Hugging Face share links), instead of curl, wget or yt-dlp.
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
     A `stalled` number with a `trouble` means the server has sent nothing for that many
     seconds; idm keeps trying, and stops with `NETWORK` after 2 minutes of silence.
   - `"state": "scheduled"`: it starts at `starts_at` (`starts_in` from now). Tell the user
     when; there is nothing to run until then.
   - `"state": "queued"`: it is waiting its turn (`waiting_for` says for what). Run `next`.
   - `"ok": false`: look up `error.code` below, and run `next` when there is one.
3. Video links (YouTube, Instagram reels, TikTok, X, Vimeo, Facebook, Reddit...) work the same
   way: `idm get LINK -d FOLDER`. The file is named after the video's title. `--audio` gets the
   sound alone. A playlist link fails with `NOT_A_FILE` and `links` (its first videos): pick
   the one the user means, or ask. For a video on another site, add `--video`.
4. Share links work as they are: Google Drive, Google Docs/Sheets/Slides, Dropbox, OneDrive,
   and GitHub or Hugging Face file pages. Give idm the whole link the user pasted (Dropbox's
   `rlkey=` is what opens it). Docs come as .docx, Sheets as .xlsx, Slides as .pptx; for a
   PDF, give `https://docs.google.com/document/d/ID/export?format=pdf`. A Drive folder fails
   with `NOT_A_FILE` and `links` (the files in it): download the ones the user means, or ask.
5. Don't switch to curl or wget when idm reports an error: the error already says what is
   wrong, and the other tool would only hit the same wall (or save an error page as the file).

## When it fails

| `error.code` | What to do |
|---|---|
| `RATE_LIMITED`, `SERVER_ERROR`, `NETWORK` | idm already waited and retried. Run `next` (`idm resume ID`) once; if it fails again, tell the user the server is having trouble, and offer to try later: the same `idm start` with `--in 30m`. |
| `FILE_CHANGED` | The file changed on the server while it downloaded. Run `next`: it starts over with the new version. |
| `INTERRUPTED` | The background download stopped (killed, or the computer slept). Run `next`: it carries on from where it was. |
| `NOT_FOUND` | The link is wrong or has expired. Tell the user and ask for a fresh link. Don't guess other links. |
| `AUTH_REQUIRED` | The site wants a login (for videos: a private video, or Instagram, which often wants one). For a Drive, Dropbox or OneDrive link, it isn't shared with everyone: ask the user to share it as "anyone with the link" and send the new link. Ask the user to export their browser cookies as a `cookies.txt` file (a "Get cookies.txt" browser extension does it) and tell you where it is; then run `next` with that path in place of `<cookies.txt>`. A token works too: `--header 'Authorization: Bearer TOKEN'`. |
| `NOT_A_FILE` | The link opens a web page, not a file. `error.links` lists the files that page links to: when there is one, `next` downloads it; when there are several, pick the one the user means or ask. None? Look at the page yourself, or ask. Use `--allow-page` only if the user clearly wants the page itself. |
| `NEEDS_TOOL` | Video sites need yt-dlp. Ask the user to install it (`brew install yt-dlp ffmpeg`, or `pipx install yt-dlp`), or to update it if it's there (`yt-dlp -U`); then run `next`. |
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
- Later, or in order: `--at 02:00` (or `2am`, or `'2026-11-25 02:00'`), `--in 2h`, `--after ID`
  (when that download has ended). `idm resume ID --now` starts a waiting one now.
- Speed: `--limit 2M` (bytes a second; `500K`...). `idm limit ID 500K` changes it while the
  download runs; `idm limit ID 0` removes it. Use it when the user wants the network free for
  something else.
- At most 3 downloads run at once; the rest wait in line, first come first served.
- Other commands: `idm start URL` (start in the background, reply at once), `idm status ID`,
  `idm list` (the latest 20), `idm pause ID`, `idm resume ID`, `idm cancel ID` (stops it and
  deletes the partial file; a finished file stays).
- Options for get/start: `-o NAME`, `-d FOLDER`, `--header 'Name: value'`,
  `--cookies FILE`, `--connections N` (1-16, default 8), `--again` (download again),
  `--at`, `--in`, `--after`, `--limit`, `--audio`, `--video`.
- Exit codes: 0 fine, 1 failed, 2 bad command, 3 failed but worth retrying (run `next`).
- The person sees the download as halo, an animated face, in their terminal; you only need
  the JSON.
