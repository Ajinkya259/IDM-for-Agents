# Runs download.py against testserver.py in every bad situation and checks each file byte for byte.
#     python3 run_tests.py      (takes about 2 minutes; uses port 8123)
import hashlib, os, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = "/usr/bin/python3"
URL = "http://127.0.0.1:8123/test.bin"
SIZE = 20 * 1000 * 1000


def expected(version=1):
    pattern = bytes((i + version) % 251 for i in range(251))
    return hashlib.sha256((pattern * (SIZE // 251 + 1))[:SIZE]).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def server(*flags):
    p = subprocess.Popen([PY, os.path.join(HERE, "testserver.py"), "--port", "8123", *flags],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    p.stdout.readline()                      # "serving ..." means it is listening
    return p


def run(folder, url=URL, interrupt_after=None, extra=()):
    p = subprocess.Popen([PY, os.path.join(HERE, "download.py"), url, "-d", folder, *extra],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if interrupt_after:
        time.sleep(interrupt_after)
        p.send_signal(signal.SIGINT)
    out, _ = p.communicate(timeout=120)
    return p.returncode, out


results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + detail if detail and not ok else ""))


def files(folder):
    return sorted(os.listdir(folder))


def case(title, flags, fn):
    d = tempfile.mkdtemp(prefix="p1-")
    srv = server(*flags)
    try:
        fn(d, srv)
    finally:
        srv.terminate(); srv.wait()
        shutil.rmtree(d)


def basic(d, srv):
    code, out = run(d)
    f = os.path.join(d, "halo test.bin")
    check("basic: exit 0", code == 0, out)
    check("basic: name from Content-Disposition, .part and notes gone", files(d) == ["halo test.bin"], str(files(d)))
    check("basic: bytes correct", os.path.exists(f) and sha(f) == expected(), out)
    code, out = run(d)
    check("again: saved as 'halo test (1).bin'", "halo test (1).bin" in files(d), str(files(d)))


def pause_resume(d, srv):
    code, out = run(d, interrupt_after=1.5)
    part = os.path.join(d, "halo test.bin.part")
    have = os.path.getsize(part) if os.path.exists(part) else 0
    check("pause: exit 130 and says paused", code == 130 and "paused at" in out, out)
    check("pause: .part kept with some bytes", 0 < have < SIZE, "have=%d" % have)
    code, out = run(d)
    f = os.path.join(d, "halo test.bin")
    check("resume: says resuming", "resuming at" in out, out)
    check("resume: bytes correct", code == 0 and sha(f) == expected(), out)


def drop(d, srv):
    code, out = run(d)
    f = os.path.join(d, "halo test.bin")
    check("drop: notices and retries", "trying again" in out, out)
    check("drop: bytes correct after carrying on", code == 0 and sha(f) == expected(), out)


def busy(d, srv):
    t = time.time()
    code, out = run(d)
    f = os.path.join(d, "halo test.bin")
    check("429: waits as asked (Retry-After 2s)", "429" in out and "trying again in 2s" in out, out)
    check("429: then downloads fine", code == 0 and sha(f) == expected(), out)


def no_ranges(d, srv):
    code, out = run(d, interrupt_after=1.5)
    check("no-ranges: says it can't resume", "can't resume" in out, out)
    code, out = run(d)
    f = os.path.join(d, "halo test.bin")
    check("no-ranges: rerun starts over and says so", "starting over: this server can't resume" in out, out)
    check("no-ranges: bytes correct", code == 0 and sha(f) == expected(), out)


def changed(d, srv):
    code, out = run(d, interrupt_after=1.5)
    srv.terminate(); srv.wait()
    srv2 = server("--version", "2")
    try:
        code, out = run(d)
        f = os.path.join(d, "halo test.bin")
        check("changed: notices the file changed", "changed since last time" in out, out)
        check("changed: gets the NEW file, correct bytes", code == 0 and sha(f) == expected(2), out)
    finally:
        srv2.terminate(); srv2.wait()


def not_found(d, srv):
    code, out = run(d, url="http://127.0.0.1:8123/nope.bin")
    check("404: clear message, exit 1, no junk files", code == 1 and "404" in out and files(d) == [], out + str(files(d)))


def no_server(d, srv):
    srv.terminate(); srv.wait()
    p = subprocess.Popen([PY, os.path.join(HERE, "download.py"), URL, "-d", d],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(4)
    p.send_signal(signal.SIGINT)
    out, _ = p.communicate(timeout=30)
    check("no server: retries with growing waits, Ctrl+C stops it", "trying again in 1s" in out and "trying again in 2s" in out and p.returncode == 130, out)


def output_name(d, srv):
    code, out = run(d, extra=("-o", "mine.bin"))
    check("-o: saves under the given name", code == 0 and files(d) == ["mine.bin"] and sha(os.path.join(d, "mine.bin")) == expected(), out)


case("basic", [], basic)
case("pause/resume", [], pause_resume)
case("drop", ["--drop", "5"], drop)
case("busy", ["--busy"], busy)
case("no ranges", ["--no-ranges"], no_ranges)
case("changed", [], changed)
case("404", [], not_found)
case("no server", [], no_server)
case("-o", [], output_name)
print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
