"""CLI: submit tasks, run the daemon (foreground or detached), inspect state.

  enigma start                 # detach daemon into the background
  enigma stop                  # stop the background daemon (repeat to force)
  enigma daemon                # run daemon in the foreground
  enigma submit task.json      # or: enigma submit - < task.json, or --desc "..."
  enigma run task.json         # run one task synchronously, print result
  enigma status                # queue counts + recent tasks
  enigma result <task-id>      # fetch a task's result JSON
  enigma insights              # playbook the engine has learned
  enigma export-corpus out.jsonl   # verified successes as SFT data (LoRA flywheel)
  enigma web                   # interactive dashboard at http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import load_config
from .daemon import run_daemon
from .engine import Engine, result_to_json
from .memory import Store
from .task import TaskSpec


def _read_spec(args: argparse.Namespace) -> TaskSpec:
    if args.desc:
        spec: dict = {"description": args.desc}
        if args.input:
            spec["input"] = args.input
        if args.output:
            spec["output"] = {"kind": args.output}
        return TaskSpec.from_json(spec)
    if not args.file:
        raise SystemExit("provide a task file, '-' for stdin, or --desc")
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    try:
        return TaskSpec.from_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise SystemExit(f"invalid task spec: {e}")


def cmd_submit(args: argparse.Namespace) -> None:
    cfg = load_config()
    task = _read_spec(args)
    store = Store(cfg.db_path)
    task_id = store.enqueue(task.id, task.to_json())
    store.close()
    print(task_id)


def cmd_run(args: argparse.Namespace) -> None:
    cfg = load_config()
    task = _read_spec(args)

    async def _run() -> str:
        store = Store(cfg.db_path)
        async with httpx.AsyncClient() as http:
            engine = Engine(cfg, store, http)
            result = await engine.run_task(task)
            await engine.learn(task, result)
        store.close()
        return result_to_json(result)

    print(asyncio.run(_run()))


def cmd_daemon(_: argparse.Namespace) -> None:
    asyncio.run(run_daemon(load_config()))


def _daemon_pid(cfg) -> int | None:
    try:
        pid = int(cfg.pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def cmd_start(_: argparse.Namespace) -> None:
    cfg = load_config()
    if (pid := _daemon_pid(cfg)) is not None:
        print(f"daemon already running (pid {pid})")
        return
    with open(cfg.log_path, "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "enigma", "daemon"],
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # The daemon writes its own pidfile; wait for it to appear or the child to die.
    for _i in range(30):
        if proc.poll() is not None:
            print(f"daemon exited immediately; see {cfg.log_path}")
            raise SystemExit(1)
        if _daemon_pid(cfg) is not None:
            print(f"daemon started (pid {proc.pid}), log: {cfg.log_path}")
            return
        time.sleep(0.1)
    print(f"daemon did not come up within 3s; see {cfg.log_path}")
    raise SystemExit(1)


def cmd_stop(_: argparse.Namespace) -> None:
    cfg = load_config()
    pid = _daemon_pid(cfg)
    if pid is None:
        print("daemon not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _i in range(150):
        if _daemon_pid(cfg) is None:
            print(f"daemon {pid} stopped")
            return
        time.sleep(0.1)
    # Never unlink the pidfile here — the daemon owns it and is still draining.
    print(f"daemon {pid} is still draining in-flight tasks; run 'enigma stop' again to force-cancel")


def cmd_status(_: argparse.Namespace) -> None:
    cfg = load_config()
    store = Store(cfg.db_path)
    pid = _daemon_pid(cfg)
    print(f"daemon: {'running (pid %d)' % pid if pid else 'stopped'}")
    counts = store.counts()
    print("queue:", json.dumps(counts) if counts else "empty")
    for row in store.list_tasks(10):
        spec = json.loads(row["spec"])
        took = f" {row['finished_at'] - row['started_at']:.0f}s" if row["finished_at"] and row["started_at"] else ""
        print(f"  {row['id']}  {row['status']:<10}{took}  {spec['description'][:60]}")
    store.close()


def cmd_result(args: argparse.Namespace) -> None:
    cfg = load_config()
    store = Store(cfg.db_path)
    row = store.get_task(args.task_id)
    store.close()
    if row is None:
        raise SystemExit(f"no task {args.task_id}")
    if row["result"]:
        print(row["result"])
    else:
        print(json.dumps({"task_id": row["id"], "status": row["status"]}))


def cmd_insights(_: argparse.Namespace) -> None:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_insights(30)
    store.close()
    if not rows:
        print("no insights learned yet")
        return
    for r in rows:
        print(f"[{r['kind']}] (used {r['uses']}x, +{r['helpful']}/-{r['harmful']}) {r['lesson']}")


def cmd_export_corpus(args: argparse.Namespace) -> None:
    """Verified successes as (prompt, completion) JSONL — the data flywheel for
    LoRA self-distillation of the local models."""
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_succeeded_specs()
    store.close()
    n = 0
    with open(args.out, "w") as f:
        for row in rows:
            spec = json.loads(row["spec"])
            result = json.loads(row["result"] or "{}")
            output = result.get("output")
            if not output:
                continue
            # Only verifiable evaluator kinds make trustworthy training signal.
            if (spec.get("evaluator") or {}).get("kind") not in ("python_tests", "json_schema", "regex", "contains"):
                continue
            prompt = spec["description"]
            if spec.get("input") is not None:
                inp = spec["input"] if isinstance(spec["input"], str) else json.dumps(spec["input"])
                prompt += "\n\nINPUT:\n" + inp
            f.write(json.dumps({"prompt": prompt, "completion": output, "score": result.get("score")}) + "\n")
            n += 1
    print(f"wrote {n} verified examples to {args.out}")


def cmd_web(args: argparse.Namespace) -> None:
    from .web import serve

    serve(load_config(), args.host, args.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="enigma", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in (("submit", cmd_submit), ("run", cmd_run)):
        sp = sub.add_parser(name)
        sp.add_argument("file", nargs="?", help="task JSON file or '-' for stdin")
        sp.add_argument("--desc", help="shortcut: task description instead of a file")
        sp.add_argument("--input", help="shortcut: task input string")
        sp.add_argument("--output", choices=("text", "json", "code"), help="shortcut: output kind")
        sp.set_defaults(fn=fn)

    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("start").set_defaults(fn=cmd_start)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    rp = sub.add_parser("result")
    rp.add_argument("task_id")
    rp.set_defaults(fn=cmd_result)
    sub.add_parser("insights").set_defaults(fn=cmd_insights)
    ep = sub.add_parser("export-corpus")
    ep.add_argument("out", help="output JSONL path")
    ep.set_defaults(fn=cmd_export_corpus)
    wp = sub.add_parser("web")
    wp.add_argument("--host", default="127.0.0.1")
    wp.add_argument("--port", type=int, default=8765)
    wp.set_defaults(fn=cmd_web)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
