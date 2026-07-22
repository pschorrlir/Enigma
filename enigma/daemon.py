"""Background worker: owns the pidfile, polls the SQLite queue, runs tasks
concurrently, and schedules post-task learning outside the concurrency slots."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import httpx

from .config import Config
from .engine import Engine, result_to_json
from .memory import Store
from .task import TaskSpec

log = logging.getLogger("enigma.daemon")


def _acquire_pidfile(cfg: Config) -> bool:
    """Atomically claim the pidfile; only one daemon per ENIGMA_HOME."""
    while True:
        try:
            fd = os.open(cfg.pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(cfg.pid_path.read_text().strip())
            except (ValueError, FileNotFoundError):
                pid = None
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    return False  # a live daemon owns it
                except ProcessLookupError:
                    pass
                except PermissionError:
                    return False
            # Stale pidfile from a crashed daemon — remove and retry the
            # atomic create (another starter may win the race; that's fine).
            cfg.pid_path.unlink(missing_ok=True)


async def run_daemon(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not _acquire_pidfile(cfg):
        log.error("another daemon is already running for %s", cfg.home)
        raise SystemExit(1)
    try:
        await _run(cfg)
    finally:
        cfg.pid_path.unlink(missing_ok=True)


async def _run(cfg: Config) -> None:
    store = Store(cfg.db_path)
    # Safe: we hold the pidfile, so no other daemon's tasks can be stolen.
    recovered = store.requeue_stale_running()
    if recovered:
        log.info("requeued %d stale running task(s)", recovered)
    pruned = store.prune_episodes(cfg.episode_retention_days)
    if pruned:
        log.info("pruned %d old episode(s)", pruned)

    stop = asyncio.Event()
    force = asyncio.Event()
    signal_count = 0

    def _on_signal() -> None:
        nonlocal signal_count
        signal_count += 1
        stop.set()
        if signal_count > 1:
            force.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    limits = httpx.Limits(max_connections=cfg.concurrency * (cfg.candidates_max + 2) + 4)
    async with httpx.AsyncClient(limits=limits) as http:
        engine = Engine(cfg, store, http)
        if not await engine.ollama.available():
            log.warning("ollama not reachable at %s — will keep polling; tasks will fail until it is up", cfg.ollama_host)
        sem = asyncio.Semaphore(cfg.concurrency)
        wake = asyncio.Event()
        inflight: set[asyncio.Task] = set()
        learners: set[asyncio.Task] = set()
        log.info("daemon up: models=%s cloud=%s concurrency=%d",
                 cfg.local_models, cfg.cloud_model if engine.cloud.enabled else "disabled", cfg.concurrency)

        async def worker(task_id: str, spec_json: str) -> None:
            # The claim loop acquired our slot; release it when done.
            try:
                try:
                    task = TaskSpec.from_json(spec_json)
                    task.id = task_id
                except ValueError as e:
                    store.finish(task_id, "failed", result_to_json_error(task_id, str(e)))
                    return
                result = await engine.run_task(task)
                store.finish(task_id, result.status, result_to_json(result))
                log.info("task %s -> %s (%.1fs)", task_id, result.status, result.elapsed_s)
                # Learning happens outside the slot so the queue keeps moving.
                lt = asyncio.create_task(engine.learn(task, result))
                learners.add(lt)
                lt.add_done_callback(learners.discard)
            except asyncio.CancelledError:
                store.finish(task_id, "failed", result_to_json_error(task_id, "daemon shutdown"))
                raise
            except Exception:
                log.exception("task %s crashed", task_id)
                store.finish(task_id, "failed", result_to_json_error(task_id, "internal error"))
            finally:
                sem.release()
                wake.set()

        async def wait_signal(timeout: float) -> None:
            waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(wake.wait())]
            try:
                await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            wake.clear()

        while not stop.is_set():
            await sem.acquire()
            if stop.is_set():
                sem.release()
                break
            row = store.claim_next()
            if row is None:
                sem.release()
                await wait_signal(cfg.poll_interval_s)
                continue
            t = asyncio.create_task(worker(row["id"], row["spec"]))
            inflight.add(t)
            t.add_done_callback(inflight.discard)

        if inflight or learners:
            log.info("draining %d task(s) and %d learner(s)... (second signal cancels)", len(inflight), len(learners))
            drain = asyncio.create_task(asyncio.gather(*inflight, *learners, return_exceptions=True))
            forced = asyncio.create_task(force.wait())
            done, _ = await asyncio.wait({drain, forced}, return_when=asyncio.FIRST_COMPLETED)
            if forced in done and not drain.done():
                log.warning("force shutdown: cancelling in-flight work")
                for t in list(inflight) + list(learners):
                    t.cancel()
                await asyncio.gather(*inflight, *learners, return_exceptions=True)
            forced.cancel()
            drain.cancel()
    store.close()
    log.info("daemon stopped")


def result_to_json_error(task_id: str, message: str) -> str:
    import json

    return json.dumps({"task_id": task_id, "status": "failed", "error": message})
