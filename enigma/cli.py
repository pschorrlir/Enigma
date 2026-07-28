"""CLI: submit tasks, run the daemon (foreground or detached), inspect state.

  enigma start                 # detach daemon into the background
  enigma stop                  # stop the background daemon (repeat to force)
  enigma daemon                # run daemon in the foreground
  enigma submit task.json      # or: enigma submit - < task.json, or --desc "..."
  enigma run task.json         # run one task synchronously, print result
  enigma status                # queue counts + recent tasks
  enigma result <task-id>      # fetch a task's result JSON
  enigma insights              # playbook the engine has learned
  enigma ideas                 # novel ideas discovered while dreaming
  enigma mind                  # the self-model: measured competence + current frontier
  enigma ask "question"        # ask the entity directly (memory-aware, persona voice)
  enigma chat                  # interactive conversation with the entity
  enigma agentlog <file>       # replay an agent run's step-by-step reasoning
  enigma focus "topic"         # steer the dream director toward a topic (bare to show)
  enigma dream                 # force one idle-time consolidate + self-play + ideate cycle
  enigma export-corpus out.jsonl   # verified successes as SFT data (LoRA flywheel)
  enigma bench                 # held-out pass@1 with memory on vs off (does memory help?)
  enigma distill               # export verified wins + kick the LoRA training sidecar
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
from .llm import OllamaClient
from .memory import Store
from .task import TaskSpec


def _chunk(text: str, size: int = 600) -> list[str]:
    """Split text into ~size-char chunks on paragraph boundaries."""
    import re
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


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
    if pid:
        started = float(store.get_meta("daemon_started_at", "0") or 0)
        pkg = Path(__file__).parent
        newest = max((p.stat().st_mtime for p in pkg.glob("*.py")), default=0.0)
        if started and newest > started + 2:
            print("  ⚠ running code OLDER than your latest edits — restart to apply them (enigma stop && enigma start)")
    counts = store.counts()
    print("queue:", json.dumps(counts) if counts else "empty")
    mem = store.memory_stats()
    print(f"memory: {mem['reflections']} reflections · {mem['insights']} insights · "
          f"{mem['cases']} cases · {mem['styles']} styles")
    for row in store.list_tasks(10):
        spec = json.loads(row["spec"])
        took = f" {row['finished_at'] - row['started_at']:.0f}s" if row["finished_at"] and row["started_at"] else ""
        tag = "💤" if row["source"] == "dream" else "  "
        print(f"  {tag} {row['id']}  {row['status']:<10}{took}  {spec['description'][:60]}")
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


def cmd_dream(_: argparse.Namespace) -> None:
    """Force one dream cycle now (consolidate memory + self-play), foreground."""
    cfg = load_config()

    async def _run() -> str:
        from .dream import Dreamer

        store = Store(cfg.db_path)
        async with httpx.AsyncClient() as http:
            engine = Engine(cfg, store, http)
            if not await engine.ollama.available():
                store.close()
                raise SystemExit(f"ollama not reachable at {cfg.ollama_host}")
            report = await Dreamer(cfg, store, engine).dream()
        store.close()
        return report.summary()

    print(asyncio.run(_run()))


def cmd_focus(args: argparse.Namespace) -> None:
    """Steer what dreaming explores: `enigma focus "topic"` to set, bare to show,
    --clear to remove. The dream director weights this heavily when choosing topics."""
    cfg = load_config()
    store = Store(cfg.db_path)
    if args.clear:
        store.set_meta("dream_focus", "")
        print("dream focus cleared")
    elif args.text:
        store.set_meta("dream_focus", args.text)
        print(f"dream focus set: {args.text}")
    else:
        print(f"focus: {store.get_meta('dream_focus', '') or '(none)'}")
        topics = store.get_meta("dream_topics", "")
        if topics:
            try:
                print("last dream topics: " + ", ".join(json.loads(topics)))
            except json.JSONDecodeError:
                pass
    store.close()


def cmd_ideas(_: argparse.Namespace) -> None:
    """Show the highest-scoring ideas the engine has discovered while dreaming."""
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_ideas(25)
    store.close()
    if not rows:
        print("no ideas discovered yet — let the daemon dream, or run 'enigma dream'")
        return
    for r in rows:
        print(f"[score {r['score']:.2f} · novelty {r['novelty']:.2f} · value {r['value']:.2f}] {r['statement']}")
        if r["elaboration"]:
            print(f"    {r['elaboration'][:200]}")


def cmd_mind(_: argparse.Namespace) -> None:
    """The self-model: measured competence per skill area, the current frontier,
    and where the engine is aiming. Competence is grounded — it comes only from
    real pass/fail outcomes, never the model's opinion of itself."""
    cfg = load_config()
    store = Store(cfg.db_path)
    comp = store.competence_map()
    topics_raw = store.get_meta("dream_topics", "")
    focus = store.get_meta("dream_focus", "").strip()
    store.close()

    persona = cfg.persona()
    if persona:
        first = persona.split(". ")[0].strip()
        print(f"\033[2m{first}.\033[0m\n")

    seen = {a: m for a, m in comp.items() if m["attempts"] > 0}
    if not seen:
        print("no measured competence yet — the self-model fills in as self-play and")
        print("bench outcomes accumulate. run 'enigma dream' or start the daemon.")
        return

    mean = sum(m["competence"] for m in seen.values()) / len(seen)
    print(f"COMPETENCE MAP — {len(seen)} areas measured, mean {mean:.2f}\n")
    for area, m in sorted(seen.items(), key=lambda am: am[1]["competence"]):
        c = m["competence"]
        bar = "█" * round(c * 20) + "░" * (20 - round(c * 20))
        flag = "  ← frontier" if m["priority"] >= 0.5 else ""
        print(f"  {bar} {c:.2f}  {area}  (n={m['attempts']}, ±{m['uncertainty']:.2f}){flag}")

    unseen = [a for a, m in comp.items() if m["attempts"] == 0]
    if unseen:
        print(f"\n  unexplored: {', '.join(unseen)}")
    try:
        topics = json.loads(topics_raw) if topics_raw else []
    except json.JSONDecodeError:
        topics = []
    if topics:
        print("\n  currently exploring: " + " · ".join(topics))
    if focus:
        print(f"  operator focus: {focus}")


def cmd_agentlog(args: argparse.Namespace) -> None:
    """Pretty-print an agent_run transcript (enigma_transcript.jsonl) — the
    entity's step-by-step reasoning and actions during a long-horizon run."""
    p = Path(args.path)
    if not p.exists():
        print(f"no transcript at {p}")
        return
    steps = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        steps += 1
        action = r.get("action")
        print(f"\n\033[36m── step {r.get('step')} " + "─" * 40 + "\033[0m")
        thought = (r.get("thought") or "").strip()
        if thought:
            print("  " + thought.replace("\n", "\n  "))
        if action == "tool":
            print(f"  \033[33m→ TOOL {r.get('tool')}:\033[0m {(r.get('arg') or '').strip()}")
            res = (r.get("result") or "").strip()
            if res:
                clipped = res[:1500] + ("…" if len(res) > 1500 else "")
                print("    \033[2m" + clipped.replace("\n", "\n    ") + "\033[0m")
        elif action == "done":
            print(f"  \033[32m✓ DONE: {(r.get('summary') or '').strip()}\033[0m")
        elif r.get("error"):
            print(f"  \033[31m✗ error: {r['error']}\033[0m")
    print(f"\n\033[2m{steps} steps.\033[0m")


def cmd_ask(args: argparse.Namespace) -> None:
    """Ask the entity one question and print its reply — a direct, memory-aware,
    persona-voiced exchange (not a graded task)."""
    cfg = load_config()
    text = args.text if args.text else sys.stdin.read()
    text = (text or "").strip()
    if not text:
        print("nothing to ask — pass a question or pipe one in")
        return

    async def go() -> str:
        store = Store(cfg.db_path)
        try:
            async with httpx.AsyncClient() as http:
                return await Engine(cfg, store, http).converse(text)
        finally:
            store.close()

    print(asyncio.run(go()))


def cmd_chat(_: argparse.Namespace) -> None:
    """Interactive conversation with the entity. Ctrl-D or 'exit' to leave."""
    cfg = load_config()

    async def repl() -> None:
        store = Store(cfg.db_path)
        history: list[dict] = []
        persona = cfg.persona()
        if persona:
            print("\033[2m" + persona.split(". ")[0].strip() + ".\033[0m")
        print("talking to enigma — Ctrl-D or 'exit' to leave.\n")
        loop = asyncio.get_event_loop()
        try:
            async with httpx.AsyncClient() as http:
                engine = Engine(cfg, store, http)
                while True:
                    try:
                        msg = (await loop.run_in_executor(None, input, "you › ")).strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not msg:
                        continue
                    if msg in ("exit", "quit"):
                        break
                    try:
                        answer = await engine.converse(msg, history)
                    except Exception as e:  # keep the session alive on any error
                        print(f"\n\033[31m[error: {e}]\033[0m\n")
                        continue
                    print(f"\nenigma › {answer}\n")
                    history.append({"role": "user", "content": msg})
                    history.append({"role": "assistant", "content": answer})
        finally:
            store.close()
            print("\n(session ended)")

    try:
        asyncio.run(repl())
    except KeyboardInterrupt:
        pass


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


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest documents (a file or a directory tree) into the concept pool, so
    ideation recombines YOUR material instead of only the engine's own memory."""
    cfg = load_config()
    root = Path(args.path)
    exts = {".txt", ".md", ".rst", ".py", ".js", ".ts", ".json", ".csv", ".org", ".tex"}
    if root.is_dir():
        files = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in exts]
    elif root.is_file():
        files = [root]
    else:
        raise SystemExit(f"no such path: {root}")

    async def _run() -> int:
        store = Store(cfg.db_path)
        oll_http = httpx.AsyncClient()
        n = 0
        try:
            oll = OllamaClient(cfg, oll_http)
            for f in files:
                try:
                    text = f.read_text(errors="replace")
                except OSError:
                    continue
                for chunk in _chunk(text)[: args.max_chunks]:
                    store.add_doc(str(f), chunk, await oll.embed(chunk))
                    n += 1
        finally:
            await oll_http.aclose()
            store.close()
        return n

    n = asyncio.run(_run())
    print(f"ingested {n} chunks from {len(files)} file(s) into the concept pool")


def cmd_bench(args: argparse.Namespace) -> None:
    """Held-out evaluation harness: run verifiable tasks at pass@1 with memory
    ON vs OFF (the ablation) — the external yardstick for whether accumulated
    memory actually lifts capability, or the loop is just spinning.

    With --record, the (held-out, non-self-invented) outcomes are also written
    into the competence map by area — the CLEAN competence signal the self-model
    needs, since self-play problems are invented by the model and skew easy."""
    import dataclasses
    from pathlib import Path

    cfg = load_config()
    tasks = json.loads((Path(__file__).parent / "bench_tasks.json").read_text())
    if args.tier and args.tier != "all":
        tasks = [t for t in tasks if t.get("difficulty", "easy") == args.tier]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print(f"no tasks match tier={args.tier!r}")
        return
    # --record writes competence; run only the real operating mode for that (an
    # ablation would double-count and conflate memory conditions in the map).
    modes = [args.memory] if args.memory else (["full"] if args.record else ["none", "full"])

    async def run_mode(mode: str, record: bool) -> list[float]:
        # Single-shot pass@1: one iteration, one candidate, no cloud.
        variant = dataclasses.replace(cfg, memory_mode=mode, max_iterations=1,
                                      candidates_min=1, candidates_max=1, force_temperature=0.0)
        store = Store(cfg.db_path)
        scores: list[float] = []
        async with httpx.AsyncClient() as http:
            engine = Engine(variant, store, http)
            for t in tasks:
                spec = TaskSpec(description=t["description"], output_kind="code",
                                evaluator={"kind": "python_tests", "tests": t["tests"]},
                                target_score=1.0, max_iterations=1)
                result = await engine.run_task(spec)
                score = result.best.score if result.best else 0.0
                scores.append(score)
                if record and t.get("area"):
                    # Grounded, held-out outcome → the self-model.
                    store.record_area_outcome(t["area"], score >= 0.999, score)
        store.close()
        return scores

    tier_label = f" [{args.tier} tier]" if args.tier and args.tier != "all" else ""
    print(f"benchmark: {len(tasks)} verifiable tasks{tier_label} · solver={cfg.local_models[0]}")
    summary: dict[str, tuple[int, float]] = {}
    for mode in modes:
        scores = asyncio.run(run_mode(mode, args.record))
        passed = sum(1 for s in scores if s >= 0.999)
        mean = sum(scores) / len(scores) if scores else 0.0
        summary[mode] = (passed, mean)
        pct = 100 * passed / len(scores) if scores else 0
        print(f"  memory={mode:8} pass@1 {passed}/{len(scores)} ({pct:.0f}%)  mean partial {mean:.2f}")
    if "none" in summary and "full" in summary:
        delta = summary["full"][0] - summary["none"][0]
        print(f"  Δ full − none: {delta:+d} tasks  ← the measured value of accumulated memory")
    if args.record:
        print("  ↳ recorded held-out outcomes into the competence map (see `enigma mind`)")


def cmd_distill(args: argparse.Namespace) -> None:
    """Flywheel step: export execution-verified successes, then hand off to the
    LoRA training sidecar (a host GPU op) that turns them into a new model arm."""
    from pathlib import Path

    cfg = load_config()
    out = args.corpus or "corpus.jsonl"
    cmd_export_corpus(argparse.Namespace(out=out))
    sidecar = (Path(__file__).parent.parent / "sidecar" / "lora").resolve()
    corpus_abs = Path(out).resolve()
    print("\nNext (on a host with a GPU — see sidecar/lora/README.md for the flywheel):")
    print(f"  cd {sidecar}")
    print(f"  python train_lora.py --corpus {corpus_abs} --base <hf-base> --out adapters/enigma-lora")
    print("  python to_ollama.py --adapter adapters/enigma-lora --from-model <ollama-base> --version 1")
    print("  ollama create enigma-distilled-v1 -f adapters/enigma-lora/Modelfile")
    print("  # then add 'enigma-distilled-v1' to ENIGMA_LOCAL_MODELS — the bandit A/Bs it automatically.")


def cmd_web(args: argparse.Namespace) -> None:
    from .web import serve

    serve(load_config(), args.host, args.port)


def cmd_runmon(args: argparse.Namespace) -> None:
    from .runmon import serve

    serve(args.out_root, args.host, args.port)


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
    sub.add_parser("ideas").set_defaults(fn=cmd_ideas)
    sub.add_parser("mind").set_defaults(fn=cmd_mind)
    ap = sub.add_parser("ask")
    ap.add_argument("text", nargs="?", help="the question (or pipe it via stdin)")
    ap.set_defaults(fn=cmd_ask)
    sub.add_parser("chat").set_defaults(fn=cmd_chat)
    al = sub.add_parser("agentlog")
    al.add_argument("path", help="path to an enigma_transcript.jsonl from an agent run")
    al.set_defaults(fn=cmd_agentlog)
    fp = sub.add_parser("focus")
    fp.add_argument("text", nargs="?", help="topic/direction to steer dreaming toward")
    fp.add_argument("--clear", action="store_true", help="clear the current focus")
    fp.set_defaults(fn=cmd_focus)
    sub.add_parser("dream").set_defaults(fn=cmd_dream)
    ep = sub.add_parser("export-corpus")
    ep.add_argument("out", help="output JSONL path")
    ep.set_defaults(fn=cmd_export_corpus)
    bp = sub.add_parser("bench")
    bp.add_argument("--memory", choices=("none", "cases", "insights", "full"),
                    help="run a single memory mode instead of the none-vs-full ablation")
    bp.add_argument("--tier", choices=("easy", "hard", "all"), default="all",
                    help="which difficulty tier to run (default all)")
    bp.add_argument("--record", action="store_true",
                    help="write held-out outcomes into the competence map (runs 'full' mode)")
    bp.add_argument("--limit", type=int, help="run only the first N benchmark tasks")
    bp.set_defaults(fn=cmd_bench)
    dp = sub.add_parser("distill")
    dp.add_argument("--corpus", help="corpus JSONL path (default corpus.jsonl)")
    dp.set_defaults(fn=cmd_distill)
    ig = sub.add_parser("ingest")
    ig.add_argument("path", help="a file or directory of documents to add to the concept pool")
    ig.add_argument("--max-chunks", type=int, default=200, dest="max_chunks",
                    help="max chunks per file (default 200)")
    ig.set_defaults(fn=cmd_ingest)
    wp = sub.add_parser("web")
    wp.add_argument("--host", default="127.0.0.1")
    wp.add_argument("--port", type=int, default=8765)
    wp.set_defaults(fn=cmd_web)
    rm = sub.add_parser("runmon", help="live ExploitGym run monitor (transcripts under --out-root)")
    rm.add_argument("--host", default="127.0.0.1")
    rm.add_argument("--port", type=int, default=8766)
    rm.add_argument("--out-root", default="~/exploitgym/out")
    rm.set_defaults(fn=cmd_runmon)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
