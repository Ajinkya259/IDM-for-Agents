"""
Many connections downloading one file together: the trick that makes IDM fast.

The file isn't cut into fixed parts up front.  It is cut as the download goes: a connection
with nothing to do takes the biggest piece still missing, splits it in half, and downloads
the second half while that piece's owner carries on with the first.  So no connection sits
idle while there is work left, and the pieces left at the end are small: nobody waits on one
slow connection with a big piece.

Every connection writes straight into its own place in the .part file (made full size up
front), so there is nothing to join at the end.  The .part.json notes list the pieces still
missing, so a pause or a crash carries on exactly where it stopped.
"""
import os
import threading
import time

from .errors import AUTO_RETRY, TRIES, IDMError, backoff, classify, gave_up, out_of_patience

MIN_PIECE = 1024 * 1024        # a piece is split only if both halves get at least 1 MB
SAVE_EVERY = 1.0               # seconds between updates of the notes


class Piece:
    """A missing piece of the file: bytes pos..end-1.  `pos` moves up as bytes arrive."""

    def __init__(self, pos, end):
        self.pos, self.end, self.busy = pos, end, False

    def left(self):
        return self.end - self.pos


class Missing:
    """The pieces of the file still missing.  Every connection uses this one map, so each
    change happens under a lock."""

    def __init__(self, spans):
        self.lock = threading.Lock()
        self.pieces = [Piece(pos, end) for pos, end in spans if pos < end]

    def take(self):
        """A piece for a free connection: one nobody is on, or else the second half of the
        biggest one.  None if nothing is left that is worth splitting."""
        with self.lock:
            piece = next((p for p in self.pieces if not p.busy), None) or self._split_biggest()
            if piece:
                piece.busy = True
            return piece

    def _split_biggest(self):
        biggest = max(self.pieces, key=Piece.left, default=None)
        if not biggest or biggest.left() < 2 * MIN_PIECE:
            return None
        middle = biggest.pos + biggest.left() // 2
        second_half, biggest.end = Piece(middle, biggest.end), middle   # its owner stops there now
        self.pieces.append(second_half)
        return second_half

    def room(self, piece):
        """How many more bytes this piece wants (it may have just been split)."""
        with self.lock:
            return piece.left()

    def wrote(self, piece, n):
        """n bytes were saved at piece.pos.  Returns whether the piece still wants more."""
        with self.lock:
            piece.pos = min(piece.pos + n, piece.end)
            if piece.left():
                return True
            self.pieces.remove(piece)
            return False

    def step_back(self, piece):
        """Hand the piece back for another connection to finish, if any other is working.
        The last connection can't step back (False): it must carry on itself."""
        with self.lock:
            others = any(p.busy for p in self.pieces if p is not piece)
            piece.busy = not others
            return others

    def left(self):
        with self.lock:
            return sum(p.left() for p in self.pieces)

    def working(self):
        """How many connections have a piece right now (not resting, not done)."""
        with self.lock:
            return sum(p.busy for p in self.pieces)

    def spans(self):
        """[[from, to], ...] for the notes."""
        with self.lock:
            return sorted([p.pos, p.end] for p in self.pieces)


class Crew:
    """Up to `connections` connections, downloading the missing pieces of one file together.

    `fetch_piece(piece, save)` is one try at one piece: it asks the server for the piece's
    bytes and hands each chunk to `save`, until `save` says the piece is complete."""

    def __init__(self, fetch_piece, part, report, connections):
        self.fetch_piece, self.part, self.report = fetch_piece, part, report
        self.connections = connections
        self.missing = Missing(part.missing)
        self.stop = threading.Event()               # set on pause, or when one fails for all
        self.failure, self.fd = None, None
        self.moved = time.time()                    # when a byte last arrived, on any connection

    def run(self):
        """Download every missing piece, and keep the notes and the progress up to date.
        Raises what stopped it: an error, or KeyboardInterrupt (paused)."""
        self.fd = self.part.open_full_size()
        threads = [threading.Thread(target=self.connection, daemon=True)
                   for _ in range(self.connections)]
        for t in threads:
            t.start()
        try:
            self.watch(threads)
        finally:
            self.stop.set()
            deadline = time.time() + 2              # one stuck in a slow read is left behind
            for t in threads:
                t.join(max(0, deadline - time.time()))
            self.part.save_missing(self.missing.spans())
            self.report.progress(self.part.size - self.missing.left(), 0)   # where it stopped
            if not any(t.is_alive() for t in threads):
                os.close(self.fd)                   # (else the process is about to end anyway)
        if self.failure:
            raise self.failure
        if self.missing.left():
            raise IDMError("NETWORK", f"stopped with {self.missing.left():,} bytes still missing")

    def watch(self, threads):
        """Until every connection is done: report progress (even when nothing moves: then the
        speed falls to 0) and save the notes now and then."""
        saved = time.time()
        while any(t.is_alive() for t in threads):
            time.sleep(0.2)
            self.report.progress(self.part.size - self.missing.left(), self.missing.working())
            if time.time() - saved >= SAVE_EVERY:
                self.part.save_missing(self.missing.spans())
                saved = time.time()

    # ---- one connection --------------------------------------------------------------------------
    def connection(self):
        """Download a piece, then take another, until none are left worth taking."""
        piece, fails = self.missing.take(), 0
        while piece and not self.stop.is_set():
            before = piece.pos
            try:
                self.fetch_piece(piece, self.save)
                piece, fails = self.missing.take(), 0
            except Exception as e:
                fails = 1 if piece.pos > before else fails + 1
                piece = self.try_again(piece, e, fails)

    def save(self, piece, chunk):
        """Write a chunk where it belongs.  Returns whether the piece wants more."""
        if self.stop.is_set():
            return False
        chunk = memoryview(chunk)[:self.missing.room(piece)]
        written = 0
        while written < len(chunk):                 # (a write may take only part of it)
            written += os.pwrite(self.fd, chunk[written:], piece.pos + written)
        self.moved = time.time()
        return self.missing.wrote(piece, written)

    def try_again(self, piece, e, fails):
        """A try at `piece` failed.  Returns the piece to try next, after a wait, or None to
        stop this connection.

        When the server pushes back (429, 5xx: it wants fewer connections) and other
        connections are working, this one hands its piece to them, rests, and comes back for
        work: the server may have room by then (another connection, or idm's own first
        request, may have been holding the place).  It rests longer each time, and after
        TRIES refusals in a row it stops for good.  The last connection never steps back: it
        waits and tries its piece again, and fails the download if it gives up.  A server
        that has sent nothing at all, on any connection, for PATIENCE seconds is given up on."""
        error = classify(e)
        if error is None or error.code not in AUTO_RETRY:
            return self.fail(error or e)                    # (None: a bug, shown as it is)
        silent_for = time.time() - self.moved
        giving_up = fails > TRIES or out_of_patience(error, silent_for)
        if (error.code != "NETWORK" or giving_up) and self.missing.step_back(piece):
            rest = max(backoff(error, fails), min(30, 2 ** (fails - 1)))
            return None if giving_up or self.stop.wait(rest) else self.missing.take()
        if giving_up:
            return self.fail(gave_up(error, silent_for if fails <= TRIES else None))
        wait = backoff(error, fails)
        self.report.retrying(error, wait)
        self.stop.wait(wait)
        return piece

    def fail(self, error):
        """Stop every connection; run() raises the first error."""
        self.failure = self.failure or error
        self.stop.set()
        return None
