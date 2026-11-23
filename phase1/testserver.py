#!/usr/bin/env python3
"""
A tiny local server for testing download.py.  It serves one made-up 20 MB file, slowly
(4 MB/s), so there is time to press Ctrl+C.  Flags make it misbehave on purpose:

    python3 testserver.py                 # normal: supports Range, so downloads can resume
    python3 testserver.py --drop 5        # cuts the connection after 5 MB (once)
    python3 testserver.py --busy          # answers the first request with 429 "too busy"
    python3 testserver.py --no-ranges     # ignores Range, like a server that can't resume
    python3 testserver.py --version 2     # a different file at the same URL (as if it changed)

Then, in another terminal:   python3 download.py http://127.0.0.1:8000/test.bin

Byte i of the file is (i + version) % 251, so a resume that puts bytes in the wrong place
gives a different checksum.  The server prints the right one when it starts.
"""
import argparse
import hashlib
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIZE = 20 * 1000 * 1000
RATE = 4 * 1000 * 1000          # bytes per second
CHUNK = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        opts = self.server.opts
        self.server.count += 1
        if opts.busy and self.server.count == 1:
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/test.bin":
            self.send_error(404)
            return
        start, end, status = 0, SIZE - 1, 200
        m = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range", ""))
        if m and not opts.no_ranges:
            start, status = int(m.group(1)), 206
            end = min(int(m.group(2)), SIZE - 1) if m.group(2) else SIZE - 1
            if start >= SIZE:
                self.send_error(416)
                return
        self.send_response(status)
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, SIZE))
        self.send_header("Accept-Ranges", "none" if opts.no_ranges else "bytes")
        self.send_header("ETag", '"test-v%d"' % opts.version)
        self.send_header("Content-Disposition", 'attachment; filename="halo test.bin"')
        self.end_headers()
        pos, sent = start, 0
        while pos <= end:
            n = min(CHUNK, end + 1 - pos)
            self.wfile.write(self.server.data[pos:pos + n])
            pos, sent = pos + n, sent + n
            if opts.drop and not self.server.dropped and sent >= opts.drop * 1e6:
                self.server.dropped = True                 # cut the line, once
                self.connection.close()
                return
            time.sleep(n / RATE)

    def log_message(self, fmt, *args):
        print("  %s  %s" % (self.headers.get("Range", "no Range"), fmt % args), flush=True)


def main():
    ap = argparse.ArgumentParser(description="Misbehaving test server for download.py")
    ap.add_argument("--drop", type=float, help="cut the connection after this many MB (once)")
    ap.add_argument("--busy", action="store_true", help="answer the first request with 429")
    ap.add_argument("--no-ranges", action="store_true", help="ignore Range requests")
    ap.add_argument("--version", type=int, default=1, help="change the file's content (and ETag)")
    ap.add_argument("--port", type=int, default=8000)
    opts = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", opts.port), Handler)
    srv.opts, srv.count, srv.dropped = opts, 0, False
    pattern = bytes((i + opts.version) % 251 for i in range(251))
    srv.data = (pattern * (SIZE // 251 + 1))[:SIZE]
    print("serving http://127.0.0.1:%d/test.bin  (20 MB, sha256 %s…)" % (
        opts.port, hashlib.sha256(srv.data).hexdigest()[:16]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
