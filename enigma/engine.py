"""The self-learning iteration engine.

Per task:
  1. Pick ONE local model for the whole task (pooled Thompson draw) so Ollama
     never thrashes weights between iterations; judge/reflect/distill reuse it.
  2. Recall playbook insights and a solved-case exemplar from memory.
  3. Each iteration: bandit picks (temperature, style) — styles include
     GEPA-evolved hints; sample an adaptive wave of candidates, each running
     its own generate→novelty-gate→evaluate chain concurrently with early
     exit the moment one hits target. DeepConf: collapsed-confidence
     generations are dropped before paying for evaluation.
  4. Evolutionary archive keeps the top-k across iterations; the prompt
     exemplar is usually the best but sometimes a diverse runner-up
     (ShinkaEvolve-style parent sampling).
  5. Reflexion critique runs in the background and lands in a later prompt;
     evaluator feedback is used immediately.
  6. Cascade escalation is calibrated on episode history (when history is
     thin, falls back to the patience rule); the cloud cohesion pass runs
     CONCURRENTLY with a local wave, never instead of it.
  7. Transient LLM errors degrade (skip the candidate, keep the archive) —
     they never destroy a task that has partial results.

Post-task (engine.learn, run outside the daemon's concurrency slot):
  credit/blame recalled playbook bullets, store solved cases, distill a
  contrastive lesson (best-vs-worst, Training-Free-GRPO-style), and on local
  plateau evolve a new prompt style for the bandit to arbitrate.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from . import cascade
from .bandit import BUILTIN_STYLES, Arm, StrategyBandit, build_arms
from .config import Config
from .evaluators import EvalResult, Evaluator
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

# Evaluators whose feedback is already precise; reflection only helps them
# once progress stalls.
_PRECISE_FEEDBACK = {"python_tests", "json_schema", "regex", "contains"}

# DeepConf: drop generations whose mean token logprob signals collapse.
_CONFIDENCE_FLOOR = -3.0


@dataclass(slots=True)
class _RunState:
    archive: list[Candidate] = field(default_factory=list)
    worst: Candidate | None = None
    seen_texts: list[str] = field(default_factory=list)
    recalled_ids: list[int] = field(default_factory=list)
    iterations: int = 0
    cloud_calls: int = 0
    status: str = "exhausted"


class Engine:
    def __init__(self, cfg: Config, store: Store, http: httpx.AsyncClient):
        self.cfg = cfg
        self.store = store
        self.ollama = OllamaClient(cfg, http)
        self.cloud = AnthropicClient(cfg, http)
        self.bandit = StrategyBandit(store)

    # ---- public entry ----------------------------------------------------

    async def run_task(self, task: TaskSpec) -> TaskResult:
        start = time.monotonic()
        state = _RunState()
        try:
            await asyncio.wait_for(self._iterate(task, state), timeout=self.cfg.task_timeout_s)
        except asyncio.TimeoutError:
            # Timeout preserves the archive: best-so-far is still the result.
            state.status = "exhausted" if state.archive else "failed"
        except LLMError as e:
            log.error("task %s failed: %s", task.id, e)
            if not state.archive:
                return TaskResult(task.id, "failed", Candidate(str(e), 0.0, "engine error"),
                                  state.iterations, state.cloud_calls, time.monotonic() - start)
            state.status = "exhausted"
        best = state.archive[0] if state.archive else None
        return TaskResult(task.id, state.status, best, state.iterations, state.cloud_calls,
                          time.monotonic() - start, worst=state.worst, recalled_insights=state.recalled_ids)

    # ---- iteration loop -----------------------------------------------------

    async def _iterate(self, task: TaskSpec, state: _RunState) -> None:
        cfg = self.cfg
        target = task.target_score if task.target_score is not None else cfg.target_score
        max_iters = task.max_iterations if task.max_iterations is not None else cfg.max_iterations
        context = task.evaluator.get("kind", "llm_judge")

        # One model for the whole task: no Ollama weight thrash.
        model = self.bandit.select_model(context, cfg.local_models)
        style_hints = dict(_STYLE_HINTS)
        for row in self.store.list_styles(context):
            style_hints[f"evolved:{row['id']}"] = row["hint"]
        arms = build_arms((model,), tuple(style_hints))
        evaluator = Evaluator(task.evaluator, self.ollama, model, self.cloud, self.cfg.prm_url)
        history = self.store.episode_history(context)

        query = task.description + " " + task.input_as_text()[:500]
        query_emb = await self.ollama.embed(query)
        recalled = self.store.recall(query, query_emb, cfg.recall_top_k)
        state.recalled_ids = [i for i, _ in recalled]
        insights = [lesson for _, lesson in recalled]
        case = self.store.recall_case(context, query, query_emb)

        reflection = ""
        reflection_task: asyncio.Task | None = None
        best_seen = -1.0
        stall = 0
        gen_failures = 0

        try:
            for iteration in range(1, max_iters + 1):
                state.iterations = iteration
                escalate = (
                    state.cloud_calls < cfg.cloud_max_calls_per_task
                    and self.cloud.enabled
                    and cascade.should_escalate(history, iteration, max(best_seen, 0.0), stall, cfg.patience)
                )
                arm = self.bandit.select(context, arms)
                if reflection_task is not None and reflection_task.done():
                    reflection = reflection_task.result() if not reflection_task.cancelled() else ""
                    reflection_task = None
                prompt = self._build_prompt(task, state, reflection, insights, style_hints[arm.style], case)

                candidates = await self._wave(task, evaluator, arm, prompt, escalate, target, state)
                if not candidates:
                    gen_failures += 1
                    if gen_failures >= 2 and not state.archive:
                        raise LLMError("no candidates produced in two consecutive iterations")
                    stall += 1
                    continue
                gen_failures = 0

                local_best = max((c.score for c in candidates if c.origin == "local"), default=None)
                if local_best is not None:
                    self.bandit.reward(context, arm, local_best)

                state.archive = sorted(
                    state.archive + candidates,
                    key=lambda c: (c.score, c.confidence if c.confidence is not None else -999.0),
                    reverse=True,
                )[: cfg.archive_size]
                for c in candidates:
                    if state.worst is None or c.score < state.worst.score:
                        state.worst = c
                best = state.archive[0]
                self.store.log_episode(
                    task.id, iteration, arm.key, best.score, best.feedback,
                    "cloud" if any(c.origin == "cloud" for c in candidates) else "local",
                )
                log.info("task %s iter %d [%s%s] best=%.2f", task.id, iteration, arm.model,
                         "+cloud" if escalate else "", best.score)

                if best.score >= target:
                    state.status = "succeeded"
                    return

                stall = stall + 1 if best.score <= best_seen + 1e-9 else 0
                best_seen = max(best_seen, best.score)

                # Reflexion in the background; skip when the evaluator's own
                # feedback is precise and we're still making progress.
                want_reflection = iteration < max_iters and (context not in _PRECISE_FEEDBACK or stall >= 1)
                if want_reflection and reflection_task is None:
                    reflection_task = asyncio.create_task(self._reflect(task, best, model))
            state.status = "exhausted"
        finally:
            if reflection_task is not None:
                reflection_task.cancel()

    # ---- candidate wave ---------------------------------------------------

    async def _wave(
        self,
        task: TaskSpec,
        evaluator: Evaluator,
        arm: Arm,
        prompt: str,
        escalate: bool,
        target: float,
        state: _RunState,
    ) -> list[Candidate]:
        cfg = self.cfg
        chains = {
            asyncio.create_task(self._gen_eval(task, evaluator, arm, prompt, state))
            for _ in range(cfg.candidates_min)
        }
        if escalate:
            state.cloud_calls += 1
            chains.add(asyncio.create_task(self._cohesion_eval(task, evaluator, state)))
        results: list[Candidate] = []
        extra_budget = cfg.candidates_max - cfg.candidates_min
        pending = chains
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    cand = t.result() if not t.cancelled() and t.exception() is None else None
                    if t.exception() is not None:
                        log.warning("candidate chain error: %s", t.exception())
                    if cand is not None:
                        results.append(cand)
                        if cand.score >= target:
                            return results  # early exit; finally cancels the rest
                # Adaptive best-of-N: extend the wave only when it looks worth it.
                if not pending and extra_budget > 0 and results:
                    scores = [c.score for c in results]
                    dispersed = max(scores) - min(scores) > 0.15
                    weak = max(scores) < 0.5
                    if len(results) < 2 or dispersed or weak:
                        n = min(extra_budget, 2)
                        extra_budget -= n
                        pending = {
                            asyncio.create_task(self._gen_eval(task, evaluator, arm, prompt, state))
                            for _ in range(n)
                        }
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return results

    async def _gen_eval(
        self, task: TaskSpec, evaluator: Evaluator, arm: Arm, prompt: str, state: _RunState
    ) -> Candidate | None:
        try:
            gen = await self.ollama.generate(
                arm.model, prompt, system=_SYSTEM, temperature=arm.temperature, want_confidence=True
            )
        except LLMError as e:
            log.warning("generation failed (%s): %s", arm.model, e)
            return None
        text = self._clean(task, gen.text, arm.style)
        if not text:
            return None
        # DeepConf gate: don't pay for evaluating a collapsed generation.
        if gen.confidence is not None and gen.confidence < _CONFIDENCE_FLOOR:
            return None
        # Novelty gate: near-duplicates of already-evaluated candidates add
        # no information — skip the evaluation cost (ShinkaEvolve rejection).
        if _too_similar(text, state.seen_texts, self.cfg.novelty_threshold):
            return None
        state.seen_texts.append(text)
        ev = await self._safe_evaluate(evaluator, task, text)
        return Candidate(text, ev.score, ev.feedback, "local", gen.confidence)

    async def _cohesion_eval(self, task: TaskSpec, evaluator: Evaluator, state: _RunState) -> Candidate | None:
        try:
            text = await self._cohesion_pass(task, state)
        except LLMError as e:
            log.warning("cloud cohesion failed, continuing locally: %s", e)
            return None
        if not text:
            return None
        ev = await self._safe_evaluate(evaluator, task, text)
        return Candidate(text, ev.score, ev.feedback, "cloud")

    async def _safe_evaluate(self, evaluator: Evaluator, task: TaskSpec, text: str) -> EvalResult:
        try:
            return await evaluator.evaluate(task, text)
        except LLMError as e:
            log.warning("evaluation failed transiently: %s", e)
            return EvalResult(0.0, f"evaluation unavailable: {e}")

    # ---- prompt construction ------------------------------------------------

    def _build_prompt(
        self,
        task: TaskSpec,
        state: _RunState,
        reflection: str,
        insights: list[str],
        style_hint: str,
        case,
    ) -> str:
        parts = [f"TASK:\n{task.description}"]
        if task.input is not None:
            parts.append(f"INPUT:\n{task.input_as_text()[:8000]}")
        parts.append(_OUTPUT_HINTS[task.output_kind])
        if case is not None:
            parts.append(
                f"A SIMILAR TASK WAS SOLVED BEFORE.\nThat task: {case['description'][:800]}\n"
                f"Its accepted solution:\n{case['output'][:2500]}"
            )
        if insights:
            parts.append("PLAYBOOK (lessons from prior tasks, apply when relevant):\n- " + "\n- ".join(insights))
        if state.archive:
            # Usually the best; sometimes a diverse runner-up so the search
            # doesn't tunnel on one basin (evolutionary parent sampling).
            pool = state.archive
            exemplar = pool[0] if len(pool) == 1 or random.random() > 0.25 else random.choice(pool[1:3] or pool[:1])
            parts.append(
                f"BEST ATTEMPT SO FAR (score {exemplar.score:.2f}):\n{exemplar.content[:4000]}\n"
                f"EVALUATOR FEEDBACK ON IT:\n{exemplar.feedback[:1500]}"
            )
        if reflection:
            parts.append(f"CRITIQUE TO ADDRESS:\n{reflection[:1500]}")
        parts.append(style_hint)
        if state.archive or reflection:
            parts.append("Produce a strictly better attempt. Fix every issue named above.")
        return "\n\n".join(parts)

    def _clean(self, task: TaskSpec, text: str, style: str) -> str:
        if style == "plan_then_solve" and "OUTPUT:" in text:
            text = text.split("OUTPUT:", 1)[1].strip()
        if task.output_kind == "code":
            return extract_code(text)
        return text.strip()

    # ---- reflexion ------------------------------------------------------------

    async def _reflect(self, task: TaskSpec, best: Candidate, model: str) -> str:
        prompt = (
            f"Task: {task.description}\n\nBest attempt (score {best.score:.2f}):\n{best.content[:3000]}\n\n"
            f"Evaluator feedback: {best.feedback[:1000]}\n\n"
            "In 3 short bullet points, state the concrete root causes of failure and "
            "exactly what the next attempt must change. Be specific, not generic."
        )
        try:
            return (await self.ollama.generate(model, prompt, temperature=0.3)).text[:1500]
        except LLMError:
            return best.feedback

    # ---- cloud cohesion pass -----------------------------------------------------

    async def _cohesion_pass(self, task: TaskSpec, state: _RunState) -> str:
        pool = "\n\n---\n\n".join(
            f"[attempt score {c.score:.2f}] feedback: {c.feedback[:400]}\n{c.content[:2500]}"
            for c in state.archive[:3]
        )
        prompt = (
            f"Local models are stuck on this task. Synthesize the strongest possible answer.\n\n"
            f"TASK:\n{task.description}\n\n"
            + (f"INPUT:\n{task.input_as_text()[:8000]}\n\n" if task.input is not None else "")
            + f"{_OUTPUT_HINTS[task.output_kind]}\n\n"
            + (f"PRIOR ATTEMPTS:\n{pool}\n\n" if pool else "")
            + "Merge what worked, fix what the feedback flagged, and output only the final answer."
        )
        text = await self.cloud.generate(prompt, system=_SYSTEM)
        return self._clean(task, text, "direct")

    # ---- post-task learning ------------------------------------------------------

    async def learn(self, task: TaskSpec, result: TaskResult) -> None:
        """Run OUTSIDE the concurrency slot: playbook credit, case bank,
        contrastive lesson distillation, and style evolution."""
        if result.status == "failed" or result.best is None:
            return
        kind = task.evaluator.get("kind", "llm_judge")
        succeeded = result.status == "succeeded"

        # ACE-style playbook curation: credit or blame what was recalled.
        self.store.mark_insights(result.recalled_insights, helpful=succeeded)
        self.store.prune_insights()

        model = self.cfg.local_models[0]
        if succeeded:
            emb = await self.ollama.embed(task.description)
            self.store.add_case(task.id, kind, task.description, emb, result.best.content, result.best.score)
        else:
            await self._evolve_style(task, result, kind, model)

        lesson = await self._distill(task, result, model)
        if lesson:
            emb = await self.ollama.embed(lesson)
            if not self.store.is_duplicate_insight(lesson, emb):
                self.store.add_insight(task.id, kind, lesson, emb)

    async def _distill(self, task: TaskSpec, result: TaskResult, model: str) -> str | None:
        best, worst = result.best, result.worst
        if worst is not None and worst.content != best.content and best.score - worst.score >= 0.3:
            # Contrastive (training-free-GRPO-style): why did the winner win?
            prompt = (
                f"Two attempts at the same task.\nTask: {task.description[:1200]}\n\n"
                f"STRONG attempt (score {best.score:.2f}):\n{best.content[:2000]}\n\n"
                f"WEAK attempt (score {worst.score:.2f}, feedback: {worst.feedback[:400]}):\n{worst.content[:2000]}\n\n"
                "Write ONE sentence stating the general, transferable principle that "
                "distinguishes the strong attempt from the weak one. No task-specific details."
            )
        else:
            outcome = "succeeded" if result.status == "succeeded" else "did not fully succeed"
            prompt = (
                f"A task {outcome} after {result.iterations} iterations.\n"
                f"Task: {task.description[:1500]}\n"
                f"Final feedback: {result.best.feedback[:800]}\n\n"
                "Write ONE sentence stating a general, transferable lesson for solving similar "
                "future tasks (a tactic, pitfall, or format rule). No task-specific details."
            )
        try:
            lesson = (await self.ollama.generate(model, prompt, temperature=0.2)).text.strip()
        except LLMError:
            return None
        if not lesson or len(lesson) < 15:
            return None
        lesson = lesson.splitlines()[0][:400]
        if lesson.lower().startswith(("here", "sure", "okay", "i ", "certainly")) or lesson.endswith(":"):
            return None
        return lesson

    async def _evolve_style(self, task: TaskSpec, result: TaskResult, kind: str, model: str) -> None:
        """GEPA-lite: on local plateau, mutate a prompt style; the bandit
        arbitrates whether the mutant beats the incumbents."""
        current = "\n".join(f"- {h}" for h in _STYLE_HINTS.values())
        prompt = (
            f"An automated solver plateaued at score {result.best.score:.2f} on this task kind ({kind}).\n"
            f"Task example: {task.description[:800]}\n"
            f"Final evaluator feedback: {result.best.feedback[:600]}\n\n"
            f"Its current prompt-style instructions:\n{current}\n\n"
            "Write ONE new 1-2 sentence prompt-style instruction, different from the above, "
            "that would plausibly avoid this failure mode. Output only the instruction."
        )
        try:
            hint = (await self.ollama.generate(model, prompt, temperature=0.7)).text.strip()
        except LLMError:
            return
        hint = hint.splitlines()[0].strip().strip('"')
        if 20 <= len(hint) <= 500:
            self.store.add_style(kind, hint, self.cfg.evolved_styles_max)


def _too_similar(text: str, seen: list[str], threshold: float) -> bool:
    for s in seen[-12:]:
        if difflib.SequenceMatcher(None, text[:2000], s[:2000]).quick_ratio() >= threshold and \
           difflib.SequenceMatcher(None, text[:2000], s[:2000]).ratio() >= threshold:
            return True
    return False


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
