"""The self-learning iteration engine.

Per task:
  1. Recall relevant insights from prior tasks (embedding similarity).
  2. Bandit picks a strategy arm (local model, temperature, prompt style).
  3. Sample N candidates in parallel; evaluate all concurrently.
  4. Keep an evolutionary archive of the top-k candidates across iterations.
  5. Reflexion: critique the best failure, feed critique + archive into the
     next generation prompt.
  6. If the score plateaus for `patience` iterations, escalate once to the
     cloud frontier model for a cohesion pass that synthesizes the archive.
  7. On finish, reward the bandit and distill a reusable insight.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .bandit import Arm, StrategyBandit, build_arms
from .config import Config
from .evaluators import Evaluator
from .llm import AnthropicClient, LLMError, OllamaClient, extract_code
from .memory import Store
from .task import Candidate, TaskResult, TaskSpec

log = logging.getLogger("enigma.engine")

_SYSTEM = (
    "You are a focused problem-solving engine. Produce only the requested "
    "output. No preamble, no meta-commentary."
)

_STYLE_HINTS = {
    "direct": "Answer directly and concisely.",
    "plan_then_solve": "First write a 3-line PLAN:, then write the final OUTPUT: below it.",
    "critique_revise": "Draft an answer, list its two biggest weaknesses, then output only the improved final version.",
}

_OUTPUT_HINTS = {
    "text": "Respond in plain text.",
    "json": "Respond with a single valid JSON value and nothing else.",
    "code": "Respond with a single fenced code block containing complete, runnable code.",
}


class Engine:
    def __init__(self, cfg: Config, store: Store, http: httpx.AsyncClient):
        self.cfg = cfg
        self.store = store
        self.ollama = OllamaClient(cfg, http)
        self.cloud = AnthropicClient(cfg, http)
        self.bandit = StrategyBandit(store, build_arms(cfg.local_models))

    # ---- public entry ----------------------------------------------------

    async def run_task(self, task: TaskSpec) -> TaskResult:
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(self._iterate(task, start), timeout=self.cfg.task_timeout_s)
        except asyncio.TimeoutError:
            result = TaskResult(task.id, "exhausted", None, 0, 0, time.monotonic() - start)
        except LLMError as e:
            log.error("task %s failed: %s", task.id, e)
            result = TaskResult(task.id, "failed", Candidate(str(e), 0.0, "engine error"), 0, 0, time.monotonic() - start)
        await self._learn(task, result)
        return result

    # ---- iteration loop -----------------------------------------------------

    async def _iterate(self, task: TaskSpec, start: float) -> TaskResult:
        cfg = self.cfg
        target = task.target_score if task.target_score is not None else cfg.target_score
        max_iters = task.max_iterations if task.max_iterations is not None else cfg.max_iterations
        context = task.evaluator.get("kind", "llm_judge")
        judge_model = cfg.local_models[0]
        evaluator = Evaluator(task.evaluator, self.ollama, judge_model)

        insights = await self._recall(task)
        archive: list[Candidate] = []  # evolutionary pool, sorted best-first
        reflection = ""
        best_score_seen = -1.0
        stall = 0
        cloud_calls = 0
        iteration = 0
        arm: Arm | None = None

        for iteration in range(1, max_iters + 1):
            escalate = (
                stall >= cfg.patience
                and cloud_calls < cfg.cloud_max_calls_per_task
                and self.cloud.enabled
            )
            if escalate:
                cloud_calls += 1
                candidates = [await self._cohesion_pass(task, archive, reflection, insights)]
                origin = "cloud"
                stall = 0
            else:
                arm = self.bandit.select(context)
                prompt = self._build_prompt(task, archive, reflection, insights, arm.style)
                gens = await asyncio.gather(
                    *(
                        self.ollama.generate(
                            arm.model, prompt, system=_SYSTEM,
                            temperature=arm.temperature + 0.1 * i,
                        )
                        for i in range(cfg.candidates_per_iteration)
                    ),
                    return_exceptions=True,
                )
                candidates = [Candidate(self._clean(task, g)) for g in gens if isinstance(g, str) and g.strip()]
                origin = "local"
                if not candidates:
                    errs = [str(g) for g in gens if isinstance(g, Exception)]
                    raise LLMError("all generations failed: " + (errs[0] if errs else "empty outputs"))

            evals = await asyncio.gather(*(evaluator.evaluate(task, c.content) for c in candidates))
            for cand, ev in zip(candidates, evals):
                cand.score, cand.feedback, cand.origin = ev.score, ev.feedback, origin

            archive = sorted(archive + candidates, key=lambda c: c.score, reverse=True)[: cfg.archive_size]
            best = archive[0]
            if arm is not None and origin == "local":
                self.bandit.reward(context, arm, max(c.score for c in candidates))
            self.store.log_episode(task.id, iteration, arm.key if arm and origin == "local" else "cloud",
                                   best.score, best.feedback, origin)
            log.info("task %s iter %d [%s] best=%.2f", task.id, iteration, origin, best.score)

            if best.score >= target:
                return TaskResult(task.id, "succeeded", best, iteration, cloud_calls, time.monotonic() - start)

            stall = stall + 1 if best.score <= best_score_seen + 1e-9 else 0
            best_score_seen = max(best_score_seen, best.score)
            reflection = await self._reflect(task, best)

        best = archive[0] if archive else None
        return TaskResult(task.id, "exhausted", best, iteration, cloud_calls, time.monotonic() - start)

    # ---- prompt construction ------------------------------------------------

    def _build_prompt(self, task: TaskSpec, archive: list[Candidate], reflection: str,
                      insights: list[str], style: str) -> str:
        parts = [f"TASK:\n{task.description}"]
        if task.input is not None:
            parts.append(f"INPUT:\n{task.input_as_text()[:8000]}")
        parts.append(_OUTPUT_HINTS[task.output_kind])
        if insights:
            parts.append("LESSONS FROM PRIOR TASKS (apply when relevant):\n- " + "\n- ".join(insights))
        if archive:
            top = archive[0]
            parts.append(
                f"BEST ATTEMPT SO FAR (score {top.score:.2f}):\n{top.content[:4000]}\n"
                f"EVALUATOR FEEDBACK ON IT:\n{top.feedback[:1500]}"
            )
        if reflection:
            parts.append(f"CRITIQUE TO ADDRESS:\n{reflection[:1500]}")
        parts.append(_STYLE_HINTS[style])
        if archive or reflection:
            parts.append("Produce a strictly better attempt. Fix every issue named above.")
        return "\n\n".join(parts)

    def _clean(self, task: TaskSpec, text: str) -> str:
        # plan_then_solve style: keep only the final output section if present.
        if "OUTPUT:" in text:
            text = text.split("OUTPUT:", 1)[1].strip()
        if task.output_kind == "code":
            return extract_code(text)
        return text.strip()

    # ---- reflexion ------------------------------------------------------------

    async def _reflect(self, task: TaskSpec, best: Candidate) -> str:
        prompt = (
            f"Task: {task.description}\n\nBest attempt (score {best.score:.2f}):\n{best.content[:3000]}\n\n"
            f"Evaluator feedback: {best.feedback[:1000]}\n\n"
            "In 3 short bullet points, state the concrete root causes of failure and "
            "exactly what the next attempt must change. Be specific, not generic."
        )
        try:
            return (await self.ollama.generate(self.cfg.local_models[0], prompt, temperature=0.3))[:1500]
        except LLMError:
            return best.feedback

    # ---- cloud cohesion pass -----------------------------------------------------

    async def _cohesion_pass(self, task: TaskSpec, archive: list[Candidate],
                             reflection: str, insights: list[str]) -> Candidate:
        pool = "\n\n---\n\n".join(
            f"[attempt score {c.score:.2f}] feedback: {c.feedback[:400]}\n{c.content[:2500]}" for c in archive[:3]
        )
        prompt = (
            f"Local models are stuck on this task. Synthesize the strongest possible answer.\n\n"
            f"TASK:\n{task.description}\n\n"
            + (f"INPUT:\n{task.input_as_text()[:8000]}\n\n" if task.input is not None else "")
            + f"{_OUTPUT_HINTS[task.output_kind]}\n\n"
            + (f"PRIOR ATTEMPTS:\n{pool}\n\n" if pool else "")
            + (f"CURRENT CRITIQUE:\n{reflection}\n\n" if reflection else "")
            + (("KNOWN LESSONS:\n- " + "\n- ".join(insights) + "\n\n") if insights else "")
            + "Merge what worked, fix what the feedback flagged, and output only the final answer."
        )
        text = await self.cloud.generate(prompt, system=_SYSTEM)
        return Candidate(self._clean(task, text), origin="cloud")

    # ---- self-learning ------------------------------------------------------------

    async def _recall(self, task: TaskSpec) -> list[str]:
        query = task.description + " " + task.input_as_text()[:500]
        emb = await self.ollama.embed(query)
        # Store calls stay on the event-loop thread: sqlite3's serialized mode
        # doesn't make cross-thread statement sequences on one connection safe.
        return self.store.recall(query, emb, self.cfg.recall_top_k)

    async def _learn(self, task: TaskSpec, result: TaskResult) -> None:
        """Distill a transferable lesson from the finished task."""
        if result.best is None or result.status == "failed":
            return
        outcome = "succeeded" if result.status == "succeeded" else "did not fully succeed"
        prompt = (
            f"A task {outcome} after {result.iterations} iterations.\n"
            f"Task: {task.description[:1500]}\n"
            f"Final feedback: {result.best.feedback[:800]}\n\n"
            "Write ONE sentence stating a general, transferable lesson for solving similar "
            "future tasks (a tactic, pitfall, or format rule). No task-specific details."
        )
        try:
            lesson = (await self.ollama.generate(self.cfg.local_models[0], prompt, temperature=0.2)).strip()
        except LLMError:
            return
        if not lesson or len(lesson) < 15:
            return
        lesson = lesson.splitlines()[0][:400]
        # Reject preamble/meta output masquerading as a lesson.
        if lesson.lower().startswith(("here", "sure", "okay", "i ", "certainly")) or lesson.endswith(":"):
            return
        emb = await self.ollama.embed(lesson)
        self.store.add_insight(task.id, task.evaluator.get("kind", "llm_judge"), lesson, emb)


def result_to_json(result: TaskResult) -> str:
    return json.dumps(
        {
            "task_id": result.task_id,
            "status": result.status,
            "score": result.best.score if result.best else None,
            "output": result.best.content if result.best else None,
            "feedback": result.best.feedback if result.best else None,
            "origin": result.best.origin if result.best else None,
            "iterations": result.iterations,
            "cloud_calls": result.cloud_calls,
            "elapsed_s": round(result.elapsed_s, 2),
        }
    )
