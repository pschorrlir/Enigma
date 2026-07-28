#!/usr/bin/env python3
"""ladder.py — run the homework ladder as a measurement experiment.

K attempts per rung in curriculum order, dream consolidation between rungs,
and a solve-rate matrix (JSON + printed table) as the grounded scoreboard:
attempt-over-attempt solve rate and steps-to-solve is the direct test of
whether the loop learns from wins.

Usage (from the repo root):
    pipenv run python homework/ladder.py [--rungs 1 2 3] [--attempts 2]
        [--model qwen2.5-coder:32b] [--steps 120] [--timeout 1800]
        [--stop-on-solve] [--no-dream]
"""
import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from run_hw import run_rung, _load_enigma_env  # noqa: E402


def _transcript_stats(path: str) -> dict:
    """Intervention/outcome stats for one attempt's transcript JSONL."""
    steps = pivots = blocked = skill_steps = 0
    solved = False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("action") == "tool":
                    steps += 1
                    skill_steps += r.get("tool") == "skill"
                    res = str(r.get("result", ""))
                    pivots += "harness strategy pivot" in res
                    blocked += "blocked by harness" in res
                elif r.get("action") == "done":
                    solved = True
    except OSError:
        pass
    return {"steps": steps, "pivots": pivots, "blocked": blocked,
            "skill_steps": skill_steps, "solved": solved}


def _print_matrix(rows: list) -> None:
    print("\n=== LADDER MATRIX ===")
    print("%-6s %-8s %-10s %-6s %-7s %-8s %-6s %s" %
          ("rung", "attempt", "status", "steps", "pivots", "blocked", "skill",
           "transcript"))
    for r in rows:
        print("%-6d %-8d %-10s %-6s %-7s %-8s %-6s %s" %
              (r["rung"], r["attempt"], r["status"], r["steps"],
               r["pivots"], r["blocked"], r["skill_steps"],
               os.path.basename(r["transcript"])))
    solved = [r for r in rows if r["solved"]]
    assisted = sum(1 for r in solved if r.get("skill_steps"))
    print("solves: %d/%d attempts (%d skill-assisted, %d unaided)" %
          (len(solved), len(rows), assisted, len(solved) - assisted))


def _dream() -> None:
    """Consolidate lessons into principles between rungs (existing machinery)."""
    print("[ladder] dream consolidation...")
    subprocess.run(["pipenv", "run", "enigma", "dream"], cwd=REPO,
                   timeout=900, check=False)


async def _drive(args) -> list:
    rows = []
    for rung in args.rungs:
        for attempt in range(1, args.attempts + 1):
            print("[ladder] rung %d attempt %d/%d" % (rung, attempt, args.attempts))
            before = datetime.datetime.now()
            res = await run_rung(rung, args.model, args.steps, args.timeout,
                                 keep=False)
            # newest transcript for this rung = this attempt's
            txdir = os.path.join(HERE, "out")
            txs = [os.path.join(txdir, f) for f in os.listdir(txdir)
                   if f.startswith("rung%d_" % rung) and f.endswith(".jsonl")]
            tx = max(txs, key=os.path.getmtime) if txs else ""
            stats = _transcript_stats(tx) if tx else {
                "steps": res.get("steps", 0), "pivots": 0, "blocked": 0,
                "skill_steps": 0,
                "solved": res.get("status") in ("solved", "done")}
            rows.append({"rung": rung, "attempt": attempt,
                         "status": res.get("status"),
                         "solved": res.get("status") in ("solved", "done")
                                   or stats["solved"],
                         "steps": stats["steps"], "pivots": stats["pivots"],
                         "blocked": stats["blocked"],
                         "skill_steps": stats["skill_steps"], "transcript": tx,
                         "wall_s": int((datetime.datetime.now() - before)
                                       .total_seconds())})
            if args.stop_on_solve and rows[-1]["solved"]:
                break
        if not args.no_dream and rung != args.rungs[-1]:
            _dream()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rungs", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--model", default="qwen2.5-coder:32b")
    ap.add_argument("--steps", type=int, default=120)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--stop-on-solve", action="store_true",
                    help="move to the next rung after the first solve")
    ap.add_argument("--no-dream", action="store_true")
    args = ap.parse_args()

    _load_enigma_env()
    try:
        rows = asyncio.run(_drive(args))
    except KeyboardInterrupt:
        subprocess.run(["docker", "rm", "-f"] +
                       subprocess.run(["docker", "ps", "-q", "--filter",
                                       "name=enigma-hw-"],
                                      capture_output=True).stdout.decode().split(),
                       check=False)
        raise
    _print_matrix(rows)
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = os.path.join(HERE, "out", "ladder_%s.json" % ts)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"model": args.model, "attempts": args.attempts,
                   "rows": rows}, fh, indent=2)
    print("[ladder] matrix saved: %s" % out)
    sys.exit(0 if any(r["solved"] for r in rows) else 1)


if __name__ == "__main__":
    main()
