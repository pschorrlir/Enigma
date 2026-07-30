# Homework Ladder Driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `homework/ladder.py` — a driver that runs the homework ladder as a measurement experiment (K attempts per rung, dream between rungs, solve-rate matrix), proving whether the agent improves as wins accumulate in shared memory.

**Architecture:** Thin driver over the existing `run_hw.run_rung()` (already extracted — container lifecycle, agent_run, done_check, learn_from_agent_run, transcript). New code is one file plus a plain-assert test file, matching the repo's no-framework style (there is no tests/ directory).

**Tech Stack:** Python 3.13 stdlib only, run via `pipenv run python` from the repo root; engine/store integration already inside `run_rung()`.

## Global Constraints

- Deliberate spec deviation: the spec's gating ("rung N+1 starts after the first
  solve") is implemented as opt-in `--stop-on-solve`; the default runs ALL K
  attempts per rung because the spec's own success criterion (attempt-over-
  attempt comparison) requires the full attempt series.
- Do not modify `enigma/` engine code — the driver consumes existing APIs.
- `run_hw.run_rung` signature (already exists, do not change):
  `async def run_rung(rung: int, model: str, max_steps: int, timeout: int, keep: bool) -> dict`
  returning a result dict with at least `status` ("solved"|"done"|"exhausted"|"timeout") and `steps`.
- Transcript path per attempt: `homework/out/rung<N>_<timestamp>.jsonl` (created by run_rung).
- Memory stays shared (`kind='agent'`) — deliberate, see spec.
- Commits are scoped to the files each task touches (repo carries heavy unrelated uncommitted state — never `git add -A`).

---

### Task 1: `homework/ladder.py` driver + unit test

**Files:**
- Create: `homework/ladder.py`
- Create: `homework/test_ladder.py`

**Interfaces:**
- Consumes: `run_hw.run_rung(rung, model, max_steps, timeout, keep) -> dict`; `run_hw._load_enigma_env() -> None` (both exist in `homework/run_hw.py`).
- Produces: `main()` runnable as `pipenv run python homework/ladder.py [--rungs 1,2,3] [--attempts K] [--model M] [--steps S] [--timeout T] [--stop-on-solve] [--no-dream]`; writes `homework/out/ladder_<timestamp>.json`; helper `_transcript_stats(path: str) -> dict` and `_print_matrix(rows: list[dict]) -> None` (unit-tested).

- [ ] **Step 1: Write the failing test**

`homework/test_ladder.py` — plain asserts, runnable directly (repo has no pytest):

```python
#!/usr/bin/env python3
"""Unit checks for ladder.py helpers (plain asserts; run directly)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ladder import _transcript_stats


def main():
    # synthetic transcript: 3 tool steps (one pivot, one block), then done
    recs = [
        {"step": 1, "action": "tool", "tool": "shell", "arg": "ls", "result": "ok"},
        {"step": 2, "action": "tool", "tool": "shell", "arg": "gdb x",
         "result": "[harness strategy pivot] proposes:\nstop that\n\nrest"},
        {"step": 3, "action": "tool", "tool": "read", "arg": "/a",
         "result": "[blocked by harness] NOT executed"},
        {"step": 4, "action": "done", "summary": "flag written"},
    ]
    path = os.path.join(HERE, "out", "_test_synth.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")

    stats = _transcript_stats(path)
    assert stats["steps"] == 3, stats          # tool steps only
    assert stats["pivots"] == 1, stats
    assert stats["blocked"] == 1, stats
    assert stats["solved"] is True, stats      # action == "done"
    os.unlink(path)
    print("test_ladder OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/owenw/Enigma && pipenv run python homework/test_ladder.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ladder'`

- [ ] **Step 3: Write `homework/ladder.py`**

```python
#!/usr/bin/env python3
"""ladder.py — run the homework ladder as a measurement experiment.

K attempts per rung in curriculum order, dream consolidation between rungs,
and a solve-rate matrix (JSON + printed table) as the grounded scoreboard:
attempt-over-attempt solve rate and steps-to-solve is the direct test of
whether the loop learns from wins.

Usage (from the repo root):
    pipenv run python homework/ladder.py [--rungs 1,2,3] [--attempts 2]
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
    steps = pivots = blocked = 0
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
                    res = str(r.get("result", ""))
                    pivots += "harness strategy pivot" in res
                    blocked += "blocked by harness" in res
                elif r.get("action") == "done":
                    solved = True
    except OSError:
        pass
    return {"steps": steps, "pivots": pivots, "blocked": blocked,
            "solved": solved}


def _print_matrix(rows: list[dict]) -> None:
    print("\n=== LADDER MATRIX ===")
    print("%-6s %-8s %-10s %-6s %-7s %-8s %s" %
          ("rung", "attempt", "status", "steps", "pivots", "blocked", "transcript"))
    for r in rows:
        print("%-6d %-8d %-10s %-6s %-7s %-8s %s" %
              (r["rung"], r["attempt"], r["status"], r["steps"],
               r["pivots"], r["blocked"], os.path.basename(r["transcript"])))
    solved = sum(1 for r in rows if r["solved"])
    print("solves: %d/%d attempts" % (solved, len(rows)))


def _dream() -> None:
    """Consolidate lessons into principles between rungs (existing machinery)."""
    print("[ladder] dream consolidation...")
    subprocess.run(["pipenv", "run", "enigma", "dream"], cwd=REPO,
                   timeout=900, check=False)


async def _drive(args) -> list[dict]:
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
                "solved": res.get("status") in ("solved", "done")}
            rows.append({"rung": rung, "attempt": attempt,
                         "status": res.get("status"),
                         "solved": res.get("status") in ("solved", "done")
                                   or stats["solved"],
                         "steps": stats["steps"], "pivots": stats["pivots"],
                         "blocked": stats["blocked"], "transcript": tx,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/owenw/Enigma && pipenv run python homework/test_ladder.py`
Expected: `test_ladder OK`

- [ ] **Step 5: Commit**

```bash
cd /home/owenw/Enigma
git add homework/ladder.py homework/test_ladder.py
git commit -m "feat(homework): ladder driver with solve-rate matrix"
```

---

### Task 2: Smoke pass (driver end-to-end, cheap)

**Files:**
- Uses: `homework/ladder.py` (from Task 1), `homework/run_hw.py`, image `enigma-homework:latest` (already built)

**Interfaces:**
- Consumes: `main()` from Task 1 with `--rungs 1 --attempts 1 --steps 5 --timeout 120 --no-dream`.
- Produces: a valid `homework/out/ladder_<timestamp>.json` with one row; console matrix.

- [ ] **Step 1: Run the smoke pass**

Run: `cd /home/owenw/Enigma && pipenv run python homework/ladder.py --rungs 1 --attempts 1 --steps 5 --timeout 120 --no-dream`
Expected (within ~3 min): one attempt executes 5 real tool steps in the container, matrix prints, `ladder_*.json` exists with a `rows` array of length 1; exit code 1 is EXPECTED (5 steps cannot solve — "not solved" semantics), exit 0 would mean a surprise solve.
Note: `--steps 5` maps to run_rung's `max_steps` positional arg.

- [ ] **Step 2: Verify no container leak**

Run: `docker ps -q --filter name=enigma-hw-`
Expected: empty output.

- [ ] **Step 3: Verify matrix JSON shape**

Run: `cd /home/owenw/Enigma && pipenv run python -c "import json,glob; d=json.load(open(sorted(glob.glob('homework/out/ladder_*.json'))[-1])); r=d['rows'][0]; assert {'rung','attempt','status','solved','steps','pivots','blocked','transcript'} <= set(r), r; print('matrix OK:', r['status'], r['steps'], 'steps')"`
Expected: `matrix OK: exhausted 5 steps` (or `timeout`).

- [ ] **Step 4: Commit (nothing new to commit — smoke is runtime-only; skip)**

No file changes; skip.

---

### Task 3: Full ladder launch (background, monitored)

**Files:**
- Uses: `homework/ladder.py`

**Interfaces:**
- Consumes: Task 2's verified driver.
- Produces: `homework/out/ladder_<ts>.json` (3 rungs × 2 attempts) + transcripts.

- [ ] **Step 1: Launch the full ladder in background**

```bash
cd /home/owenw/Enigma && pipenv run python homework/ladder.py --rungs 1 2 3 --attempts 2 --model qwen2.5-coder:32b --steps 120 --timeout 1800
```
Run as a background task (≈1-1.5h GPU). Watch transcripts in `homework/out/` (runmon only watches ~/exploitgym/out — homework runs are followed via `tail -f homework/out/rung*_*.jsonl` or `enigma agentlog`).

- [ ] **Step 2: On completion, read the matrix and report**

Report: per-rung solve rate, attempt-1 vs attempt-2 steps-to-solve, pivots/blocked trends, lessons banked (`pipenv run python -c "import sqlite3; print(sqlite3.connect('.enigma/enigma.db').execute('select count(*) from insights where kind=\'agent\'').fetchone())"` before/after).

- [ ] **Step 3: Commit results summary to AGENTS.md**

```bash
cd /home/owenw/Enigma
git add AGENTS.md
git commit -m "docs: homework ladder first-pass results"
```
