"""Background worker: polls the SQLite queue and runs tasks concurrently."""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from .config import Config
from .engine import Engine, result_to_json
from .memory import Store
from .task import TaskSpec

log = logging.getLogger("enigma.daemon")


async def run_daemon(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = Store(cfg.db_path)
    recovered = store.requeue_stale_running()
    if recovered:
        log.info("requeued %d stale running task(s)", recovered)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    limits = httpx.Limits(max_connections=cfg.concurrency * cfg.candidates_per_iteration + 4)
    async with httpx.AsyncClient(limits=limits) as http:
        engine = Engine(cfg, store, http)
        if not await engine.ollama.available():
            log.warning("ollama not reachable at %s — will keep polling; tasks will fail until it is up", cfg.ollama_host)
        sem = asyncio.Semaphore(cfg.concurrency)
        inflight: set[asyncio.Task] = set()
        log.info("daemon up: models=%s cloud=%s concurrency=%d",
                 cfg.local_models, cfg.cloud_model if engine.cloud.enabled else "disabled", cfg.concurrency)

        async def worker(task_id: str, spec_json: str) -> None:
            async with sem:
                try:
                    task = TaskSpec.from_json(spec_json)
                    task.id = task_id
                    result = await engine.run_task(task)
                    store.finish(task_id, result.status, result_to_json(result))
                    log.info("task %s -> %s (%.1fs)", task_id, result.status, result.elapsed_s)
                except Exception:
                    log.exception("task %s crashed", task_id)
                    store.finish(task_id, "failed", "{}")

        while not stop.is_set():
            if sem._value > 0:  # only claim when a slot is free
                row = store.claim_next()
                if row is not None:
                    t = asyncio.create_task(worker(row["id"], row["spec"]))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
                    continue  # claim eagerly while queue is non-empty
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval_s)
            except asyncio.TimeoutError:
                pass

        if inflight:
            log.info("draining %d in-flight task(s)...", len(inflight))
            await asyncio.gather(*inflight, return_exceptions=True)
    store.close()
    log.info("daemon stopped")
