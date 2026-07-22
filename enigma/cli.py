"""CLI: submit tasks, run the daemon (foreground or detached), inspect state.

  enigma start                 # detach daemon into the background
  enigma stop                  # stop the background daemon
  enigma daemon                # run daemon in the foreground
  enigma submit task.json      # or: enigma submit - < task.json, or --desc "..."
  enigma run task.json         # run one task synchronously, print result
  enigma status                # queue counts + recent tasks
  enigma result <task-id>      # fetch a task's result JSON
  enigma insights              # what the engine has learned
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
    return TaskSpec.from_json(raw)


def cmd_submit(args: argparse.Namespace) -> None:
    cfg = load_config()
    task = _read_spec(args)
    store = Store(cfg.db_path)
    store.enqueue(task.id, task.to_json())
    store.close()
    print(task.id)


def cmd_run(args: argparse.Namespace) -> None:
    cfg = load_config()
    task = _read_spec(args)

    async def _run() -> str:
        store = Store(cfg.db_path)
        async with httpx.AsyncClient() as http:
            engine = Engine(cfg, store, http)
            result = await engine.run_task(task)
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
    cfg.pid_path.write_text(str(proc.pid))
    time.sleep(0.7)
    if proc.poll() is not None:
        print(f"daemon exited immediately; see {cfg.log_path}")
        raise SystemExit(1)
    print(f"daemon started (pid {proc.pid}), log: {cfg.log_path}")


def cmd_stop(_: argparse.Namespace) -> None:
    cfg = load_config()
    pid = _daemon_pid(cfg)
    if pid is None:
        print("daemon not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _i in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    cfg.pid_path.unlink(missing_ok=True)
    print(f"daemon {pid} stopped")


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
        print(f"[{r['kind']}] (used {r['uses']}x) {r['lesson']}")


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

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
