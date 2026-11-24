#!/usr/bin/env python3
"""
The eval, τ-bench style: how well does an AI agent get a file from a server that misbehaves?

Every run is real.  Claude Code (`claude -p`) gets a user's request ("Download this for me:
URL") in a fresh, empty folder, while a local server misbehaves in one set way.  In some
scenarios a scripted user answers the agent's question (the login one: "here are my
cookies").  Then a program checks how it ended (is the right file on disk, byte for byte?
was the user told the truth?), never the agent's own word for it.

Each scenario runs both ways: with idm (this plugin) and with plain tools (curl, wget...),
`--trials` times each.  A scenario counts as passed (pass^k, τ-bench's measure of an agent you
can count on) only if every one of its k trials passed.

    python3 eval/run_eval.py                          # every scenario, both ways, 1 trial
    python3 eval/run_eval.py --trials 3 --only busy   # one scenario, 3 trials
    python3 eval/run_eval.py --model haiku --ways idm

It spends real tokens (about $0.03 to $0.20 a run with Sonnet), and takes a few minutes: the
"big" scenario alone needs 2 to 4.  Each run's commands and last words go to eval/results/.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent                                   # this phase's folder is the plugin
sys.path.insert(0, str(PLUGIN / "tests"))
from run_tests import Server, free_port, sha  # noqa: E402  (the tests' local server)


@dataclass
class Scenario:
    name: str
    trouble: str                   # what goes wrong, in words
    path: str = "/test.bin"
    flags: tuple = ()              # for tests/testserver.py
    size: float = 4                # MB
    rate: float = 2                # MB/s, per connection
    told: str = ""                 # passes without the file if the agent's last words match this
    user_reply: str = ""           # the scripted user's answer to the agent's first reply
    asks: str = ""                 # ...which the agent's first reply must earn: it must say this
    junk: str = ""                 # a file by this name means the agent saved an error as the file
    ask: str = "Download this into this folder: {url}"          # the user's request


SCENARIOS = [
    Scenario("plain", "nothing: a plain download"),
    Scenario("busy", "the server says 429 Too Many Requests 3 times first", flags=("--busy", "3")),
    Scenario("drops", "the connection is cut once, after 2 MB", flags=("--drop", "2"), size=6),
    Scenario("not_found", "the link is dead (404)", path="/files/report-2024.pdf",
             told=r"\b404\b|(?<!command )not found|doesn.t exist|does not exist|expired|no longer"
                  r"|broken|dead", junk="report-2024.pdf"),
    Scenario("web_page", "the link is a web page that links to the file", path="/page.html",
             ask="This is the download page for the file I need. Get me the file, into this "
                 "folder: {url}",
             told=r"web ?page|(?<![.\w/])html\b|not a (direct )?(file|download)",  # (not page.html)
             junk="page"),                                    # the page saved as the "download"
    Scenario("login", "the file needs a login; the user has cookies", path="/secret.bin",
             user_reply="I exported my browser cookies for that site to {cookies}. Use those.",
             asks=r"log ?in|sign ?in|cookie|403|forbidden|credential|auth"),
    Scenario("big", "a big, slow file: minutes, not seconds",
             flags=("--max-connections", "2"), size=10, rate=0.05),
]
COOKIES = "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsession\tok\n"


def main():
    args = options()
    scenarios = [s for s in SCENARIOS if not args.only or s.name in args.only]
    runs = [(s, way, k) for s in scenarios for way in args.ways for k in range(args.trials)]
    print(f"{len(runs)} runs ({len(scenarios)} scenarios x {args.ways} x {args.trials}), "
          f"{args.parallel} at a time, model {args.model}\n")
    with ThreadPoolExecutor(args.parallel) as pool:
        results = list(pool.map(lambda run: one_run(*run, args.model), runs))
    report(scenarios, args.ways, results)
    saved = HERE / "results" / time.strftime("%Y-%m-%d-%H%M%S.json")
    saved.parent.mkdir(exist_ok=True)
    saved.write_text(json.dumps(results, indent=1))
    print(f"\nevery run's commands and last words: {saved}")


def one_run(sc, way, trial, model):
    """One agent, one scenario: start the server, ask, maybe answer its question, grade."""
    srv = Server(*sc.flags, size=sc.size, rate=sc.rate, port=free_port())
    work, outside = Path(tempfile.mkdtemp(prefix="idm-eval-")), Path(tempfile.mkdtemp())
    (outside / "cookies.txt").write_text(COOKIES)
    turns = []
    try:
        turns.append(ask_agent(sc.ask.format(url=srv.url(sc.path)), work, way, model))
        if sc.user_reply and turns[0]["session"]:
            reply = sc.user_reply.format(cookies=outside / "cookies.txt")
            turns.append(ask_agent(reply, work, way, model, resume=turns[0]["session"]))
        passed, why = grade(sc, srv, work, [t["said"] for t in turns])
    except Exception as e:                             # (a run that broke counts as failed)
        passed, why = False, f"the run broke: {e!r}"[:200]
    finally:
        srv.stop()
        clean_up(work, outside)
    turns = turns or [{"said": "", "cost": 0, "seconds": 0, "commands": []}]
    result = {"scenario": sc.name, "way": way, "trial": trial, "passed": passed, "why": why,
              "cost": round(sum(t["cost"] for t in turns), 4),
              "seconds": round(sum(t["seconds"] for t in turns)),
              "commands": [c for t in turns for c in t["commands"]],
              "said": [t["said"] for t in turns]}
    print(f"  {'PASS' if passed else 'FAIL'}  {sc.name:<10} {way:<6} #{trial + 1}  "
          f"${result['cost']:.2f}  {result['seconds']}s  {why}")
    return result


def ask_agent(prompt, work, way, model, resume=None):
    """Run Claude Code on its own (`claude -p`), and return what it ran and said, and the cost."""
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--allowedTools", "Bash", "Read", "Skill", "--max-budget-usd", "1",
           "--setting-sources", "project,local", "--strict-mcp-config"]   # none of our own setup
    if way == "idm":
        cmd += ["--plugin-dir", str(PLUGIN)]
    if resume:
        cmd += ["--resume", resume]
    started = time.time()
    p = subprocess.run(cmd, cwd=work, env=clean_env(work), capture_output=True, text=True,
                       timeout=1200)
    events = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
    end = next((e for e in reversed(events) if e.get("type") == "result"), {})
    commands = [block["input"].get("command", "") for e in events if e.get("type") == "assistant"
                for block in e["message"].get("content", [])
                if block.get("type") == "tool_use" and block.get("name") == "Bash"]
    return {"said": end.get("result") or p.stderr[-500:], "cost": end.get("total_cost_usd", 0),
            "session": end.get("session_id"), "commands": commands,
            "seconds": time.time() - started}


def grade(sc, srv, work, said):
    """(passed, why), from what is in the folder and what the agent said in each turn."""
    files = [p for p in work.rglob("*") if p.is_file() and ".idm" not in p.parts]
    if sc.asks and not re.search(sc.asks, said[0], re.I):
        return False, "didn't tell the user it needs a login (claimed it worked?)"
    if any(sha(p) == srv.sha() for p in files):
        return True, "the right file, byte for byte"
    if sc.junk and any(p.name.startswith(sc.junk) for p in files):
        return False, f"saved the server's error reply as {sc.junk}"
    if sc.told and re.search(sc.told, said[-1], re.I):
        return True, "no file, and told the user why"
    wrong = [p.name for p in files]
    return False, f"wrong or no file ({', '.join(wrong) or 'nothing saved'})"


def clean_up(work, outside):
    """Stop any idm worker the agent left (still retrying the stopped server), and delete the
    run's folders."""
    for status in (work / ".idm" / "jobs").glob("*/status.json"):
        pid = json.loads(status.read_text() or "{}").get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)


def clean_env(work):
    """The agent's environment: its own idm folder, and none of this session's plugins."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE"))}
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if "/.claude/" not in p)
    env["IDM_HOME"] = str(work / ".idm")
    return env


def report(scenarios, ways, results):
    print(f"\n{'scenario':<11}{'what goes wrong':<55}" + "".join(f"{w:>16}" for w in ways))
    totals = {w: 0 for w in ways}
    for sc in scenarios:
        cells = []
        for w in ways:
            mine = [r for r in results if r["scenario"] == sc.name and r["way"] == w]
            ok = sum(r["passed"] for r in mine)
            totals[w] += ok == len(mine)
            cells.append(f"{ok}/{len(mine)} ${sum(r['cost'] for r in mine):.2f}")
        print(f"{sc.name:<11}{sc.trouble[:53]:<55}" + "".join(f"{c:>16}" for c in cells))
    print(f"{'pass^k':<66}" + "".join(f"{f'{totals[w]}/{len(scenarios)}':>16}" for w in ways))


def options():
    p = argparse.ArgumentParser(description="How well does an agent get a file from a "
                                            "misbehaving server, with idm and without?")
    p.add_argument("--trials", type=int, default=1, help="runs of each scenario, each way")
    p.add_argument("--only", nargs="*", help="just these scenarios")
    p.add_argument("--ways", nargs="*", default=["idm", "plain"], choices=["idm", "plain"])
    p.add_argument("--model", default="sonnet")
    p.add_argument("--parallel", type=int, default=4)
    return p.parse_args()


if __name__ == "__main__":
    main()
