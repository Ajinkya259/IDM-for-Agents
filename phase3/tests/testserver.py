#!/usr/bin/env python3
"""
A small local server that misbehaves on purpose, for testing idm.

    python3 tests/testserver.py [--size MB] [--rate MB/s] [--port N] [--drop MB] [--busy N]
                                [--no-ranges] [--version N] [--max-connections N]

    /test.bin       a made-up file (20 MB, sent at 4 MB/s per connection, so there is time
                    to pause it)
                      --drop MB     cuts the connection after that many MB (once)
                      --busy N      answers the first N requests with 429 "too busy"
                      --no-ranges   ignores Range, like a server that can't resume
                      --version N   a different file at the same link (as if it changed)
                      --max-connections N
                                    answers 503 to a connection beyond N at once
    /secret.bin     the same file, but only with the cookie session=ok or the header
                    "Authorization: Bearer ok"; otherwise 403
    /page.html      a web page, not a file
    /status/CODE    answers with that status code (429 and 503 say Retry-After: 1)
    /stats          {"peak": most connections at once on /test.bin, "requests": how many}

Byte i of the file is (i + version) % 251, so bytes saved in the wrong place give the wrong
checksum.  The server prints the right one when it starts.
"""
import argparse
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass                                            # the client hung up (idm's probe does)

    def route(self):
        route = self.path.split("?")[0]
        if route == "/test.bin":
            self.server.requests += 1
            if self.server.requests <= self.server.opts.busy:
                return self.reply(429, b"", {"Retry-After": "1"})
            return self.send_counted()
        if route == "/secret.bin":
            if "session=ok" in self.headers.get("Cookie", "") or \
                    self.headers.get("Authorization") == "Bearer ok":
                return self.send_file()
            return self.reply(403, b"forbidden")
        if route == "/page.html":
            return self.reply(200, b"<html><body>Click <a href='/test.bin'>here</a></body></html>",
                              {"Content-Type": "text/html; charset=utf-8"})
        if route == "/stats":
            stats = {"peak": self.server.peak, "requests": self.server.requests}
            return self.reply(200, json.dumps(stats).encode(), {"Content-Type": "application/json"})
        m = re.fullmatch(r"/status/(\d{3})", route)
        if m:
            code = int(m.group(1))
            return self.reply(code, b"", {"Retry-After": "1"} if code in (429, 503) else {})
        self.reply(404, b"not found")

    def reply(self, code, body, headers=None):
        self.send_response(code)
        for name, value in {"Content-Length": str(len(body)), **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_counted(self):
        """send_file(), counting the connections at once (and refusing too many)."""
        srv = self.server
        with srv.lock:
            if srv.opts.max_connections and srv.active >= srv.opts.max_connections:
                return self.reply(503, b"", {"Retry-After": "1"})
            srv.active += 1
            srv.peak = max(srv.peak, srv.active)
        try:
            self.send_file()
        finally:
            with srv.lock:
                srv.active -= 1

    def send_file(self):
        opts, data = self.server.opts, self.server.data
        size, start, end, status = len(data), 0, len(data) - 1, 200
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if m and not opts.no_ranges:
            start, status = int(m.group(1)), 206
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            if start >= size:
                return self.reply(416, b"", {"Content-Range": f"bytes */{size}"})
        self.send_response(status)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "none" if opts.no_ranges else "bytes")
        self.send_header("ETag", f'"test-v{opts.version}"')
        self.send_header("Content-Disposition", 'attachment; filename="halo test.bin"')
        self.end_headers()
        pos, sent = start, 0
        while pos <= end:
            n = min(CHUNK, end + 1 - pos)
            self.wfile.write(data[pos:pos + n])
            pos, sent = pos + n, sent + n
            if opts.drop and not self.server.dropped and sent >= opts.drop * 1e6:
                self.server.dropped = True                  # cut the line, once
                self.close_connection = True
                self.connection.close()
                return
            time.sleep(n / (opts.rate * 1e6))

    def log_message(self, fmt, *args):
        print(f"  {self.headers.get('Range', '-'):>16}  {fmt % args}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="A test server that misbehaves on purpose")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--size", type=float, default=20, help="file size in MB")
    ap.add_argument("--rate", type=float, default=4, help="sending speed in MB/s")
    ap.add_argument("--drop", type=float, help="cut the connection after this many MB (once)")
    ap.add_argument("--busy", type=int, default=0, help="answer the first N requests with 429")
    ap.add_argument("--no-ranges", action="store_true", help="ignore Range requests")
    ap.add_argument("--version", type=int, default=1, help="change the file's content and ETag")
    ap.add_argument("--max-connections", type=int, default=0, help="refuse more at once (503)")
    opts = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", opts.port), Handler)
    srv.daemon_threads = True
    srv.opts, srv.requests, srv.dropped = opts, 0, False
    srv.lock, srv.active, srv.peak = threading.Lock(), 0, 0
    srv.data = file_bytes(int(opts.size * 1e6), opts.version)
    print(f"serving http://127.0.0.1:{opts.port}/test.bin  ({opts.size:g} MB at {opts.rate:g} MB/s, "
          f"sha256 {hashlib.sha256(srv.data).hexdigest()[:16]}…)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def file_bytes(size, version):
    pattern = bytes((i + version) % 251 for i in range(251))
    return (pattern * (size // 251 + 1))[:size]


if __name__ == "__main__":
    main()
