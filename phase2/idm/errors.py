"""
What can go wrong, said so that an AI agent can act on it.

Every failure becomes a IDMError with a fixed `code`.  Its JSON form tells the agent whether
trying again can help (`retryable`) and what to do about it (`hint`).
"""
import errno
import http.client
import socket
import ssl
import urllib.error

HINTS = {
    "BAD_URL": "Check the link: it should start with http:// or https://.",
    "BAD_INPUT": "Check the command's options.",
    "NOT_FOUND": "The link is wrong or has expired. Find a fresh link to the file.",
    "AUTH_REQUIRED": "The server wants a login. Ask the user for a cookies.txt file exported "
                     "from their browser and add --cookies FILE, or add --header 'Authorization: ...'.",
    "NOT_A_FILE": "This link opens a web page, not a file. Find the direct download link on that "
                  "page, or use yt-dlp for a video page. To save the page itself, add --allow-page.",
    "HTTP_ERROR": "The server refused the request. Check the link, or ask the user.",
    "RATE_LIMITED": "The server is limiting requests. idm already waited and retried; "
                    "run `next` after a while.",
    "SERVER_ERROR": "The server is having trouble. idm already retried; run `next` later.",
    "NETWORK": "Could not reach the server (offline, DNS or timeouts). Check the connection, "
               "then run `next`.",
    "TLS": "The server's security certificate could not be verified. Don't work around this; "
           "tell the user.",
    "FILE_CHANGED": "The file changed on the server during the download. Run `next` to "
                    "download it again from the start.",
    "DISK_FULL": "The disk is full. Free some space, then run `next`.",
    "NO_PERMISSION": "idm can't write to that folder. Pick another one with -d.",
    "WRITE_FAILED": "idm couldn't write the file. Pick another folder with -d.",
    "INTERRUPTED": "The download process stopped unexpectedly. Run `next` to carry on from "
                   "where it stopped.",
    "NO_SUCH_JOB": "There is no download with that id. Run `idm list` to see them.",
    "INTERNAL": "This is a bug in idm. Tell the user; the details are in the job's worker.log.",
}

AUTO_RETRY = {"RATE_LIMITED", "SERVER_ERROR", "NETWORK"}                  # idm retries these itself
RETRYABLE = AUTO_RETRY | {"FILE_CHANGED", "DISK_FULL", "INTERRUPTED"}      # worth the agent trying again


class IDMError(Exception):
    """A failure with a fixed code, a plain message, and (sometimes) how long the server asked
    us to wait before trying again."""

    def __init__(self, code, message, retry_after=None):
        assert code in HINTS, code
        super().__init__(message)
        self.code, self.retry_after = code, retry_after

    def to_dict(self):
        d = {"code": self.code, "message": str(self), "retryable": self.code in RETRYABLE,
             "hint": HINTS[self.code]}
        if self.retry_after:
            d["retry_after"] = self.retry_after
        return d


NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError,
                  ssl.SSLError, http.client.HTTPException)


def classify(e):
    """Any exception from a download, as a IDMError.  None if it is a bug in idm itself."""
    if isinstance(e, IDMError):
        return e
    if isinstance(e, urllib.error.HTTPError):                 # the server answered, with an error
        return from_status(e.code, e.reason, e.headers.get("Retry-After"))
    cert = e if isinstance(e, ssl.SSLCertVerificationError) else getattr(e, "reason", None)
    if isinstance(cert, ssl.SSLCertVerificationError):        # URLError wraps it during the handshake
        return IDMError("TLS", f"bad certificate: {cert.verify_message or cert}")
    if isinstance(e, NETWORK_ERRORS):                         # no answer, or the answer stopped
        return IDMError("NETWORK", describe(e))
    if isinstance(e, OSError):                                # our side: the disk
        return from_disk(e)
    if isinstance(e, ValueError):                             # urllib didn't understand the link
        return IDMError("BAD_URL", f"that isn't a download link ({e})")
    return None


def from_status(status, reason, retry_after=None):
    said = f"the server said {status} {reason}"
    wait = seconds(retry_after)
    if status == 429:
        return IDMError("RATE_LIMITED", said, wait)
    if status in (401, 403, 407):
        return IDMError("AUTH_REQUIRED", said)
    if status in (404, 410):
        return IDMError("NOT_FOUND", said)
    if status >= 500:
        return IDMError("SERVER_ERROR", said, wait)
    return IDMError("HTTP_ERROR", said)


def from_disk(e):
    if e.errno == errno.ENOSPC:
        return IDMError("DISK_FULL", "the disk is full")
    if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return IDMError("NO_PERMISSION", f"no permission to write {e.filename or 'there'}")
    return IDMError("WRITE_FAILED", f"couldn't write the file: {e.strerror or e}")


def seconds(retry_after):
    """Retry-After is usually a number of seconds (anything else: None).  At most 60."""
    return min(60, int(retry_after)) if retry_after and retry_after.strip().isdigit() else None


def describe(e):
    """A short reason for a network error, like 'Connection refused' or 'timed out'."""
    e = getattr(e, "reason", e)                               # URLError wraps the real error
    if isinstance(e, socket.gaierror):
        return "couldn't look up the server's name (DNS)"
    return getattr(e, "strerror", None) or str(e) or type(e).__name__
