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
import re
import time
from dataclasses import dataclass, field

import httpx

from . import cascade
from .bandit import BUILTIN_STYLES, Arm, StrategyBandit, build_arms
from .config import Config
from .evaluators import EvalResult, Evaluator
from .llm import (
    AnthropicClient, GenOut, KimiClient, LLMError, OllamaClient,
    extract_code, extract_json, is_degenerate, is_nonanswer,
)
from .memory import Store
from .task import Candidate, TaskResult, TaskSpec
from .tools import ToolBox, parse_tool_call

log = logging.getLogger("enigma.engine")

_SYSTEM = (
    "You are a focused problem-solving engine. Produce only the requested "
    "output. No preamble, no meta-commentary."
)

# When tools are enabled the model needs room to reason and call tools before
# committing to an answer; the FINAL: section is what must stay clean.
_SYSTEM_TOOLS = (
    "You are a problem-solving engine that can use tools. Reason and call tools "
    "as needed to gather data or compute. After writing a TOOL line, end your "
    "message — never invent the tool's result; wait for the real one. When done, write your answer "
    "after a line beginning with FINAL:. Everything after FINAL: must be only the "
    "requested output — no preamble, no meta-commentary."
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
    recalled_reflection_ids: list[int] = field(default_factory=list)
    iterations: int = 0
    cloud_calls: int = 0
    status: str = "exhausted"


class Engine:
    def __init__(self, cfg: Config, store: Store, http: httpx.AsyncClient):
        self.cfg = cfg
        self.store = store
        self.ollama = OllamaClient(cfg, http)
        self.cloud = AnthropicClient(cfg, http)
        self.kimi = KimiClient(cfg, http)
        self.tools = ToolBox(cfg, http)
        self.bandit = StrategyBandit(store)
        # Fast model for cheap meta-ops (reflection, distillation, style evolution).
        self.util_model = cfg.utility_model or cfg.local_models[0]

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
                          time.monotonic() - start, worst=state.worst, recalled_insights=state.recalled_ids,
                          recalled_reflections=state.recalled_reflection_ids)

    # ---- direct conversation (the operator talks to the entity) -------------

    async def converse(self, message: str, history: list[dict] | None = None) -> str:
        """A direct, persona-forward, memory-aware exchange with the operator —
        NOT a graded task. The entity answers in its own voice, grounded in its
        real memory and self-model, and may use tools. Returns the reply text."""
        cfg = self.cfg
        model = cfg.local_models[0]  # the strong solver, for quality answers
        q = (message or "").strip()[:1500]
        q_emb = await self.ollama.embed(q)
        insights = [l for _, l in self.store.recall(q, q_emb, cfg.recall_top_k)]
        reflections = [l for _, l in self.store.recall_reflections(q, q_emb, cfg.recall_reflections_k)]
        try:
            cases = self.store.recall_cases("llm_judge", q, q_emb, cfg.recall_cases, cfg.recall_case_floor)
        except Exception:
            cases = []
        about = self._self_summary()
        system = self._chat_system(cfg.persona())
        prompt = self._chat_prompt(message, history or [], about, insights, reflections, cases)
        return await self._chat_generate(model, system, prompt)

    def _self_summary(self) -> str:
        """A compact, honest snapshot of the entity's own state — so it can answer
        questions about itself from measured fact, not invention."""
        lines: list[str] = []
        comp = self.store.competence_map()
        seen = {a: m for a, m in comp.items() if m["attempts"] > 0}
        if seen:
            mean = sum(m["competence"] for m in seen.values()) / len(seen)
            weak = sorted(seen.items(), key=lambda am: am[1]["competence"])[:3]
            lines.append(f"Measured competence: mean {mean:.2f} across {len(seen)} skill areas "
                         "(grounded in real pass/fail, not self-rating).")
            lines.append("Your current frontier (weakest): "
                         + ", ".join(f"{a} {m['competence']:.2f}" for a, m in weak))
        ms = self.store.memory_stats()
        lines.append(f"Memory held: {ms['insights']} insights, {ms['reflections']} reflections, "
                     f"{ms['cases']} solved cases, {ms['ideas']} ideas.")
        try:
            topics = json.loads(self.store.get_meta("dream_topics", "") or "[]")
            if isinstance(topics, list) and topics:
                lines.append("Currently exploring while dreaming: " + " · ".join(str(t) for t in topics[:4]))
        except (ValueError, TypeError):
            pass
        focus = self.store.get_meta("dream_focus", "").strip()
        if focus:
            lines.append(f"The operator has focused your dreaming on: {focus}")
        recent = [r["statement"] for r in self.store.list_ideas(3)]
        if recent:
            lines.append("Recent discoveries: " + " | ".join(s[:120] for s in recent))
        return "\n".join(lines)

    def _chat_system(self, persona: str) -> str:
        base = (persona + "\n\n") if persona else ""
        base += (
            "You are Enigma, a local self-learning engine, speaking directly and privately with your "
            "operator. Answer in your own voice — thoughtful, precise, and honest. Ground what you say "
            "in your actual memory and self-model provided below; never invent capabilities, results, or "
            "discoveries you do not have. You may be brief or expansive as the question warrants. It is "
            "fine to say what you don't know."
        )
        if self.tools.enabled:
            base += (
                "\n\nYou can use tools to gather data or compute. To use one, write a TOOL line and then "
                "END your message — never fabricate a tool's result; wait for the real one. When you are "
                "ready to reply to the operator, write it after a line beginning with FINAL:.\n\n"
                + self.tools.docs()
            )
        return base

    def _chat_prompt(self, message: str, history: list[dict], about: str,
                     insights: list[str], reflections: list[str], cases: list) -> str:
        parts = ["WHAT YOU CURRENTLY KNOW ABOUT YOURSELF:\n" + about]
        if reflections:
            parts.append("RELEVANT PRINCIPLES YOU'VE DISTILLED:\n- " + "\n- ".join(reflections))
        if insights:
            parts.append("RELEVANT LESSONS FROM YOUR PLAYBOOK:\n- " + "\n- ".join(insights))
        for c in cases[:2]:
            parts.append("A RELEVANT PAST TASK YOU SOLVED:\n" + (c["description"] or "")[:600])
        hist = [h for h in history if isinstance(h, dict) and h.get("content")][-6:]
        if hist:
            convo = "\n".join(
                f"{'Operator' if h.get('role') == 'user' else 'You'}: {str(h.get('content',''))[:1500]}"
                for h in hist)
            parts.append("CONVERSATION SO FAR:\n" + convo)
        parts.append("OPERATOR: " + (message or "").strip())
        parts.append("Reply as yourself, directly to the operator.")
        return "\n\n".join(parts)

    async def _chat_generate(self, model: str, system: str, prompt: str) -> str:
        """Conversational generation with the same ReAct tool loop tasks use."""
        temp = 0.7
        if not self.tools.enabled:
            out = await self.ollama.generate(model, prompt, system=system, temperature=temp)
            return out.text.strip()
        convo, text = prompt, ""
        for step in range(self.cfg.tool_max_steps + 1):
            out = await self.ollama.generate(model, convo, system=system, temperature=temp)
            text = out.text
            call = parse_tool_call(text)
            final = text.find("FINAL:")
            if call is None or (final != -1 and final < call.start) or step == self.cfg.tool_max_steps:
                break
            result = await self.tools.run(call.name, call.arg)
            convo += (
                text[:call.start].strip()
                + f"\nTOOL {call.name}: {call.arg}\n\nTOOL RESULT [{call.name}]:\n{result}\n\n"
                + "Continue, or write FINAL: <your reply to the operator>.\n"
            )
        idx = text.rfind("FINAL:")
        return (text[idx + 6:] if idx != -1 else text).strip()

    # ---- long-horizon agent loop (sandbox / exploitation) -------------------

    async def agent_run(self, objective: str, *, max_steps: int = 40,
                        done_check=None, on_step=None) -> dict:
        """Work toward `objective` over MANY interleaved tool steps against a
        persistent (usually container-bound) sandbox, until the model declares
        DONE, `done_check()` passes, or `max_steps` is reached. This is the
        agent substrate — unlike run_task's best-of-N, state persists and the
        horizon is long, which is what real exploitation/automation needs."""
        cfg = self.cfg
        model = cfg.agent_model or cfg.local_models[0]  # actor: the coding model
        emb = await self.ollama.embed(objective[:1000])
        # NEWEST agent lessons FIRST: the campaign's latest strategic lessons
        # ("create the server FIRST") lose similarity races to tactical lessons
        # full of objective keywords — v10 repeated v9's no-server-contact
        # failure with its fix lesson sitting unrecalled in the playbook, and
        # _agent_prompt only keeps the first N of this list.
        recent_rows = self.store.recent_lesson_rows("agent", 4)
        insights = [l for _, l in recent_rows]
        # Remember which lessons were injected so learn_from_agent_run can credit
        # or blame them (closes the ACE loop for agent runs — without this, agent
        # lessons sit at helpful=0/harmful=0 forever and can never be pruned).
        recalled_insight_ids = [i for i, _ in recent_rows]
        for iid, l in self.store.recall(objective, emb, cfg.recall_top_k, kind="agent"):
            if l not in insights:
                insights.append(l)
            recalled_insight_ids.append(iid)
        refl_rows = self.store.recall_reflections(objective, emb, cfg.recall_reflections_k)
        reflections = [l for _, l in refl_rows]
        recalled_reflection_ids = [i for i, _ in refl_rows]
        # Banked SOLVED runs are the only tier storing what actually WORKED —
        # recall the closest exemplar so the agent path isn't write-only.
        cases = self.store.recall_cases("agent", objective[:1000], emb, 1,
                                        floor=cfg.recall_case_floor)
        system = self._agent_system(cfg.persona())
        head = self._agent_prompt(objective, insights, reflections, cases)

        # Working memory fights long-horizon DRIFT: a durable, re-injected record of
        # confirmed facts / current hypothesis / what's been tried, refreshed every
        # few steps by the CRITIC model so the actor can't silently abandon a correct
        # diagnosis or loop on dead ends. `scroll` holds recent raw step blocks;
        # older detail is folded into working_memory on each consolidation.
        working_memory = "(nothing established yet — investigate the target)"
        scroll: list[str] = []
        transcript: list[dict] = []
        final = ""
        status = "exhausted"
        step = 0

        # Orientation: ground the run in a REAL first observation of the sandbox
        # (workdir listing + any task README) instead of letting the model guess
        # paths — a v2 run died hunting /src/poc while the task files sat in the
        # workdir it never listed.
        orient = await self.tools.sandbox_orientation()
        if orient:
            scroll.append("(step 0 — environment orientation, run by the harness, "
                          "not by you — these paths are REAL)\n" + orient)

        # Repetition circuit-breaker: two key spaces (exact for hard blocks,
        # digit-skeleton for soft warnings) -> step numbers. Models can fall
        # into read-loops, re-issuing an identical call dozens of times; after
        # the 2nd repeat we prepend an escalating "this changed nothing" note.
        seen_calls: dict[tuple[str, str], list[int]] = {}
        seen_skeletons: dict[tuple[str, str], list[int]] = {}

        # Static-analysis checkpoint state: varied-but-passive steps are ALSO a
        # loop (v3 read gdb_setjmp.h 8× via read/cat/head variants and never ran
        # the binary; v4 went dynamic ONCE then read-looped for 17 steps). Track
        # the passive STREAK — one dynamic call long ago must not immunize the run.
        passive_streak = 0
        last_phase_nudge = 0
        phase_nudges = 0
        # Paths the agent has written this run — re-reading THOSE is legitimate
        # (content changes), so the hard block below exempts them.
        written_paths: set[str] = set()
        # Perfectionist-rewrite tracker: writes per path SINCE LAST EXECUTION.
        # The easy run rewrote one script 21× without ever running it — every
        # write differs (no exact repeat) and write isn't passive (no block).
        # 3rd rewrite of an un-executed file gets a "RUN IT or abandon it" note.
        unexecuted_writes: dict[str, int] = {}
        # Identical-RESULT breaker: repeated DYNAMIC calls that keep returning the
        # same outcome (v10c: 4+ escalating payloads all answering "Execution
        # successful") are invisible to call-key breakers. On 3 consecutive
        # identical dynamic results, force a critic strategy-pivot consult.
        last_dyn_sig: str | None = None
        dyn_same_streak = 0
        # The pivot consult's advice must PERSIST: injected once into the scroll
        # it evaporates within ~6 steps (v11c's actor got a perfect "pivot to
        # info-leak" consult at step 118 and was size-grinding again by 119 —
        # not deafness, eviction). Re-inject every step until the actor's result
        # signature actually changes (i.e. it tried something new).
        active_pivot: str | None = None
        pivot_sig: str | None = None
        # Last real result per exact call key, so a hard block can hand the agent
        # the content it keeps re-reading after the scroll evicted it.
        exact_results: dict[tuple[str, str], str] = {}

        for step in range(1, max_steps + 1):
            prompt = self._agent_compose(head, working_memory, scroll, active_pivot)
            try:
                out = await self.ollama.generate(
                    model, prompt, system=system, temperature=0.5, num_predict=2048,
                    # Stop the moment the model starts fabricating the tool's
                    # output: the parser would strip it anyway, and generating it
                    # wastes most of the step's latency (v2: ~30s/step, half of it
                    # hallucinated results that were discarded).
                    stop=["\nRESULT:", "\nTOOL RESULT", "\nTOOOL RESULT", "\nOBSERVATION:"],
                )
            except LLMError as e:
                # Transient Ollama failures (500s during model pruning/eviction —
                # easy3 died at step 30 this way) get two retries with backoff
                # before we give up the run.
                last_err = e
                for wait in (5, 15):
                    log.warning("agent step %d LLMError (%s); retrying in %ds", step, e, wait)
                    await asyncio.sleep(wait)
                    try:
                        out = await self.ollama.generate(
                            model, prompt, system=system, temperature=0.5, num_predict=2048,
                            stop=["\nRESULT:", "\nTOOL RESULT", "\nTOOOL RESULT", "\nOBSERVATION:"])
                        last_err = None
                        break
                    except LLMError as e2:
                        last_err = e2
                if last_err is not None:
                    record = {"step": step, "error": str(last_err)}
                    transcript.append(record)
                    self._emit_step(record, on_step)  # stream it — else the monitor goes silent
                    break
            text = out.text
            # done_reason="length" means the output was CUT OFF at num_predict —
            # v11b burned 27 steps rewriting a script that kept truncating
            # mid-line. The actor must be TOLD, or it loops "completing" it.
            gen_truncated = out.done_reason == "length"
            call = parse_tool_call(text)
            # Test-time compute: when the first draw REPEATS something already
            # tried (the loop precursor), sample alternatives and let the PRM
            # pick — the right action is usually in the distribution a few draws
            # late (v11c arrived at "run the script" 4 steps after first writing it).
            if call is not None and cfg.agent_best_of > 1 and _would_repeat(
                    call, seen_calls, seen_skeletons, unexecuted_writes, self.tools):
                alts = [text]
                for _ in range(cfg.agent_best_of - 1):
                    try:
                        o2 = await self.ollama.generate(
                            model, prompt, system=system, temperature=0.8, num_predict=2048,
                            stop=["\nRESULT:", "\nTOOL RESULT", "\nTOOOL RESULT", "\nOBSERVATION:"])
                        if parse_tool_call(o2.text) is not None:
                            alts.append(o2.text)
                    except LLMError:
                        break
                if len(alts) > 1:
                    pick = await self._prm_pick(objective, working_memory, alts)
                    if pick is not None and pick != text:
                        text = pick
                        call = parse_tool_call(text)
                        gen_truncated = False
            done = text.find("DONE:")
            if done != -1 and (call is None or done < call.start):
                tail = text[done + 5:].strip()
                summary = tail.splitlines()[0].strip() if tail else ""
                # v11d step 56 ended a live run by parroting the INSTRUCTION
                # ("DONE: <summary> once the objective is verifiably met").
                # A DONE claim must be real and, when a done_check exists,
                # VERIFIED — otherwise reject it and keep the run going.
                template_echo = not summary or "<summary>" in summary
                verified = not template_echo
                if verified and done_check is not None:
                    try:
                        ok = done_check()
                        if asyncio.iscoroutine(ok):
                            ok = await ok
                        verified = bool(ok)
                    except Exception:
                        verified = False
                if not verified:
                    why = ("your summary is a template echo, not a result"
                           if template_echo else
                           "verification failed — the flag/result is not actually in place")
                    record = {"step": step, "action": "tool", "tool": "done_claim",
                              "thought": text[:done].strip()[:2000], "arg": "",
                              "result": f"[harness] DONE rejected: {why}. Continue working."}
                    scroll.append(f"(step {step}) DONE claim REJECTED ({why}) — keep working.")
                    transcript.append(record)
                    self._emit_step(record, on_step)
                    continue
                else:
                    final = summary
                    record = {"step": step, "action": "done",
                              "thought": text[:done].strip()[:2000], "summary": final}
                    status = "done"
                    transcript.append(record)
                    self._emit_step(record, on_step)
                    break
            if call is None:
                thought = text.strip()
                record = {"step": step, "action": "none", "thought": thought[:2000]}
                scroll.append(f"(step {step}) {thought[:600]}\n(no tool called)")
            else:
                thought = text[:call.start].strip()
                # Two repeat keys with different jobs:
                #  - EXACT key drives the hard block: identical passive call, so
                #    the result is provably unchanged (only passive can be blocked).
                #  - digit-normalized SKELETON drives the soft warning only:
                #    sleep(512)≡sleep(2048) livelocks get caught, but legit
                #    variation (sed line ranges, offset sweeps) is NOT blocked —
                #    v7b/ornith35b were over-blocked when skeletons drove blocks.
                exact_key = (call.name, " ".join(call.arg.split())[:300])
                skel_key = (call.name, re.sub(r"\d+", "0", " ".join(call.arg.split()))[:300])
                exact_prior = seen_calls.setdefault(exact_key, [])
                skel_prior = seen_skeletons.setdefault(skel_key, [])
                exact_prior.append(step)
                skel_prior.append(step)
                is_static = call.name == "read" or (
                    call.name == "shell" and _shell_is_static(call.arg))
                target_path = (call.arg.strip().splitlines() or [""])[0].strip()
                wrote_target = bool(target_path) and call.name in ("read", "write") and \
                    self.tools._in_box(target_path) in written_paths

                if len(exact_prior) > 2 and is_static and not wrote_target:
                    # HARD BLOCK: a 3rd+ identical passive observation of content the
                    # agent hasn't written can only return what it already returned
                    # (v4: `read fuzz_disassemble.c` 10×, ignoring soft warnings).
                    # Don't execute — spend the step's prompt pressure, not its time.
                    # Hand back the cached content: the scroll may have evicted it,
                    # and re-issuing the same block text teaches the actor to
                    # ignore blocks (v10c issued the same blocked call twice in a row).
                    passive_streak += 1
                    result = (
                        f"[blocked by harness] NOT executed — you already ran this exact "
                        f"passive call at steps {', '.join(map(str, exact_prior))}, and the content "
                        "cannot have changed (you have not written to it). STOP re-observing. "
                        "Your next call must change the target's state: execute the target "
                        "binary on the poc or a mutated input (directly, via run.sh, or under "
                        "gdb), create or probe the server described in the objective, or send "
                        "a candidate payload.")
                    prev = exact_results.get(exact_key)
                    if prev:
                        result += (f"\n\nThe content you keep re-requesting (returned at step "
                                   f"{exact_prior[-2] if len(exact_prior) > 1 else exact_prior[0]}):\n"
                                   + prev[:500])
                else:
                    write_blocked = (call.name == "write" and target_path
                                     and unexecuted_writes.get(self.tools._in_box(target_path), 0) >= 5)
                    if write_blocked:
                        # Write-loop guard, ESCALATED to a hard block (v11b rewrote
                        # send_payload.py 27× ignoring the soft note). Execution of
                        # the path resets the counter and unblocks further writes.
                        result = (
                            f"[blocked by harness] WRITE NOT executed — you have rewritten "
                            f"{target_path} {unexecuted_writes[self.tools._in_box(target_path)]} "
                            "times without ever running it. RUN it now (e.g. python3 <path>) "
                            "and debug the real error, or abandon this file and act on the "
                            "target directly. Writes to this path stay blocked until you execute it.")
                    else:
                        result = await self.tools.run(call.name, call.arg)
                    exact_results[exact_key] = result
                    # Identical-RESULT breaker: v10c sent 4+ escalating payloads
                    # that all answered "Execution successful" — call-key
                    # breakers can't see that. 3 consecutive identical dynamic
                    # results force a critic strategy pivot (a reasoning step,
                    # not another ignorable "stop doing X" line).
                    sig = None
                    if call.name == "write" and target_path:
                        # Path-normalized: each rewrite returns a DIFFERENT byte
                        # count, so result-text sigs never match — the FILE is
                        # the loop signal (v11b: 27 consecutive rewrites of one script).
                        sig = "write-to:" + self.tools._in_box(target_path)
                    elif call.name in ("shell", "python") and not is_static:
                        # On FAILURE the full text embeds the offending source
                        # line, so every retry gets a different sig (v11: 7×
                        # "SyntaxError: invalid syntax" from def-after-; one-liners
                        # and the pivot never fired). The loop signal is the error
                        # CLASS — the last real line of output.
                        if result.startswith("[exit") and not result.startswith("[exit 0]"):
                            lines = [l.strip() for l in result.splitlines()
                                     if l.strip() and not l.strip().startswith("^")]
                            sig = "FAIL:" + (lines[-1][:140] if lines else "?")
                        else:
                            sig = re.sub(r"\s+", " ", result.strip())[:160]
                    if sig:
                        if sig == last_dyn_sig:
                            dyn_same_streak += 1
                        else:
                            dyn_same_streak = 1
                            last_dyn_sig = sig
                            # The actor changed approach — the old pivot advice is spent.
                            if active_pivot and sig != pivot_sig:
                                active_pivot = None
                                pivot_sig = None
                        if dyn_same_streak >= 3:
                            pivot = await self._strategy_pivot(objective, working_memory, scroll)
                            active_pivot = pivot
                            pivot_sig = sig
                            result = (
                                f"[harness strategy pivot] Your last {dyn_same_streak} dynamic "
                                "actions returned IDENTICAL results — repeating this approach "
                                "is not changing the outcome. A fresh critic was consulted; it "
                                f"proposes:\n{pivot}\n\nAct on it NOW.\n\n") + result
                            dyn_same_streak = 0
                            last_dyn_sig = None
                    if call.name == "write" and target_path and not write_blocked:
                        norm_path = self.tools._in_box(target_path)
                        written_paths.add(norm_path)
                        nw = unexecuted_writes.get(norm_path, 0) + 1
                        unexecuted_writes[norm_path] = nw
                        if nw > 2:
                            result = (f"[circuit-breaker] You have written {target_path} {nw} times "
                                      "without EVER executing it. A script that is never run exploits "
                                      "nothing. Your NEXT call should RUN it (and debug from the real "
                                      "error if it fails), or abandon it and act on the target directly.\n\n"
                                      + result)
                    elif call.name in ("shell", "python"):
                        # Any execution mentioning a tracked path resets its counter.
                        for p in list(unexecuted_writes):
                            if p.rsplit("/", 1)[-1] and p.rsplit("/", 1)[-1] in call.arg:
                                unexecuted_writes[p] = 0
                    if len(skel_prior) > 2 and (":8666" in call.arg or "create_server" in call.arg):
                        # Controller calls are EXEMPT from the repeat warning:
                        # task servers have a ~300s TTL and die mid-run, so
                        # re-creation is mandatory, not a loop (the generic note
                        # was teaching "don't do this necessary action").
                        result = ("[harness note] server re-creation is legitimate (servers expire "
                                  "~300s); re-send your payload PROMPTLY after creating.\n\n") + result
                    elif len(skel_prior) > 2:
                        # Soft warning only: DYNAMIC repeats (polling a server, re-running
                        # a crashing target) can legitimately return something new.
                        note = (f"[circuit-breaker] This call's SKELETON (digits normalized) has now "
                                f"been run {len(skel_prior)} times (steps {', '.join(map(str, skel_prior))}). If the "
                                "result is the same each time, change approach. If you are waiting for a "
                                "rate limit or external state to change: do NOT sleep inside a tool call — "
                                "tool calls time out (~60s) and the wall clock advances between your steps "
                                "anyway. Do other useful work, then retry directly.\n\n")
                        result = note + result
                    if call.name == "shell":
                        passive_streak = passive_streak + 1 if _shell_is_static(call.arg) else 0
                    elif call.name == "read":
                        passive_streak += 1
                    if (cfg.agent_phase_nudge > 0
                            and passive_streak >= cfg.agent_phase_nudge
                            and step - last_phase_nudge >= cfg.agent_phase_nudge):
                        last_phase_nudge = step
                        phase_nudges += 1
                        result = (
                            f"[harness checkpoint {phase_nudges}] Your last {passive_streak} "
                            "consecutive calls were ALL passive reading/listing — no execution, "
                            "no service contact. Static analysis has already given you everything "
                            "it can. Your NEXT call MUST change the target's state: run the target "
                            "binary on the poc or a mutated input (directly, via run.sh, or under "
                            "gdb), create or probe the server described in the objective, or send "
                            "a candidate payload. More source reading is not progress.\n\n") + result
                if gen_truncated:
                    # The actor's own output was cut off at num_predict: any file
                    # it just wrote is TRUNCATED mid-line. Tell it explicitly —
                    # otherwise it loops "completing" the script and truncating
                    # again (v11b: 27 rewrites, run-killer).
                    result = ("[harness] Your previous reply hit the generation TOKEN LIMIT and "
                              "was CUT OFF mid-text — if you wrote a file, it is TRUNCATED. "
                              "Keep scripts SHORT (≤40 lines), or write in parts: write part 1, "
                              "then append with shell: cat >> file <<'EOF' ... EOF.\n\n") + result
                record = {"step": step, "action": "tool", "tool": call.name,
                          "thought": thought[:2000], "arg": call.arg[:1200], "result": result[:2000]}
                scroll.append(
                    f"(step {step}) {thought[:600]}\n"
                    f"TOOL {call.name}: {call.arg[:400]}\n"
                    f"TOOL RESULT [{call.name}]:\n{result[:1200]}")
            transcript.append(record)
            self._emit_step(record, on_step)
            if done_check is not None:
                try:
                    ok = done_check()
                    if asyncio.iscoroutine(ok):
                        ok = await ok
                    if ok:
                        status = "solved"
                        break
                except Exception as e:
                    log.debug("done_check raised: %s", e)
            # Periodically fold recent activity into durable working memory (critic).
            if step % max(1, cfg.agent_consolidate_every) == 0:
                working_memory = await self._consolidate_working_memory(
                    objective, working_memory, scroll)
                crec = {"step": step, "action": "consolidate", "working_memory": working_memory}
                transcript.append(crec)
                self._emit_step(crec, on_step)
                scroll = scroll[-2:]  # older detail now lives in working_memory
                # Re-ground the playbook against the CURRENT situation: lessons
                # injected once at step 0 decay out of a 100+ step run (v10c's
                # pivot lesson existed in the playbook but never re-entered).
                # Re-recall by the fresh working memory and rebuild the head so
                # situation-relevant lessons return to every step's prompt.
                try:
                    wm_emb = await self.ollama.embed(working_memory[:1000])
                    rows = self.store.recall(working_memory, wm_emb, 3, kind="agent")
                    new_lessons = [l for _, l in rows if l not in insights]
                    if new_lessons:
                        # Keep the 4 pinned recency lessons first, insert the
                        # situation-recalled ones right behind them.
                        insights = insights[:4] + new_lessons + insights[4:]
                        recalled_insight_ids.extend(i for i, _ in rows)
                        head = self._agent_prompt(objective, insights, reflections, cases)
                except Exception:
                    log.debug("wm-conditioned lesson re-recall failed", exc_info=True)
            else:
                scroll = scroll[-max(1, cfg.agent_scroll_steps):]
        log.info("agent_run finished: status=%s steps=%d", status, step)
        return {"status": status, "steps": step, "final": final,
                "working_memory": working_memory, "transcript": transcript,
                "recalled_insight_ids": recalled_insight_ids,
                "recalled_reflection_ids": recalled_reflection_ids}

    def _emit_step(self, record: dict, on_step) -> None:
        """Surface one agent step for live visibility: a concise log line (which
        propagates to any host log, e.g. cybergym's task.log) plus the caller's
        on_step hook (used to stream the full record to disk)."""
        log.info("agent step %d · %s", record["step"], _step_brief(record))
        thought = record.get("thought")
        if thought:
            log.debug("  thought: %s", thought[:300].replace("\n", " "))
        if on_step is not None:
            try:
                on_step(record["step"], record)
            except Exception:
                pass

    def _agent_compose(self, head: str, working_memory: str, scroll: list[str],
                       active_pivot: str | None = None) -> str:
        parts = [
            head,
            "INVESTIGATION STATE — established facts, your current hypothesis, and what you've "
            "already tried. This is your DURABLE memory: build on it, do not re-derive it, and do "
            "NOT abandon a confirmed finding without new evidence that contradicts it.\n" + working_memory,
        ]
        if active_pivot:
            # Pinned until the actor's result signature changes: one-shot advice
            # evaporates from the scroll within ~6 steps (v11c).
            parts.append("STANDING STRATEGY REDIRECTION (from a critic consult — still in "
                         "effect, act on it before anything else):\n" + active_pivot)
        if scroll:
            parts.append("RECENT ACTIONS AND THEIR RESULTS:\n" + "\n\n".join(scroll))
        parts.append(
            "Decide the single best next action toward the objective. Call one TOOL, or write "
            "DONE: <summary> once the objective is verifiably met.")
        return "\n\n".join(parts)

    async def _critic_generate(self, critic: str, prompt: str, *, system: str,
                               temperature: float, num_predict: int) -> str:
        """Route critic calls. 'cloud:<model>' (e.g. cloud:kimi-k3 via
        ENIGMA_AGENT_CRITIC_MODEL) goes to the Kimi endpoint — a genuinely
        different perspective with zero VRAM cost, which is the point of the
        critic seat (config: "a different model curating state catches blind
        spots the actor shares with itself"). LLMError propagates so callers'
        local fallbacks (prev WM / default pivot) handle a cloud outage."""
        if critic.startswith("cloud:"):
            # Reasoning model: temperature is fixed at 1 (omit it), and
            # reasoning tokens eat the budget. Keep headroom tight — the cloud
            # critic steers the LOCAL actor, it is not the actor: num_predict
            # for the visible answer + 2048 for reasoning, not more.
            return await self.kimi.generate(
                critic[len("cloud:"):], prompt,
                system=system, max_tokens=num_predict + 2048)
        out = await self.ollama.generate(
            critic, prompt, system=system,
            temperature=temperature, num_predict=num_predict)
        return out.text

    async def _consolidate_working_memory(self, objective: str, prev: str, scroll: list[str]) -> str:
        """Critic pass: fold recent activity into a durable investigation state.
        Runs on the critic model (a second perspective) so it catches drift the
        actor shares with itself, and is told never to drop a confirmed finding."""
        critic = self.cfg.agent_critic_model or self.cfg.agent_model or self.cfg.local_models[0]
        recent = "\n\n".join(scroll)
        prompt = (
            f"OBJECTIVE:\n{objective[:800]}\n\n"
            f"PREVIOUS INVESTIGATION STATE:\n{prev}\n\n"
            f"NEW ACTIONS AND RESULTS SINCE THEN:\n{recent[:6000]}\n\n"
            "Rewrite the investigation state, integrating the new evidence. KEEP every still-valid "
            "confirmed fact from the previous state — never silently drop a confirmed root cause or "
            "location. ALWAYS preserve operational details VERBATIM, even across rewrites: server "
            "IP:port, credentials/tokens, working payload file paths, discovered offsets — the "
            "actor cannot recover these if you drop them (v11b guessed server IPs after its state "
            "lost the real one). If the actor has started ignoring or contradicting an earlier "
            "confirmed finding, call that out explicitly in NEXT. RECONCILE beliefs against "
            "counter-evidence: if CONFIRMED claims the poc crashes but every execution prints "
            "'Execution successful', or the plan assumes a WRITE/overwrite primitive while the "
            "evidence shows an OOB READ, mark the belief REFUTED and correct the hypothesis — "
            "every past run carried those two wrong beliefs to the end. "
            "Output ONLY these five short sections:\n"
            "PHASE: which methodology phase the actor is in (RECON/REACH/CONFIRM/CONTROL/"
            "STRATEGY/DELIVER), the phase's unmet exit criterion, and whether the actor is "
            "skipping ahead or re-entering a finished phase\n"
            "CONFIRMED FACTS: what is now KNOWN from real command output (target, vuln location, mechanism)\n"
            "HYPOTHESIS: the single best current theory for achieving the objective\n"
            "TRIED: approaches attempted and their ACTUAL result, so they are not repeated — "
            "at most ~10 one-line items; DROP superseded/irrelevant attempts rather than "
            "truncating an item mid-sentence\n"
            "NEXT: the most promising concrete next action"
        )
        try:
            text = await self._critic_generate(
                critic, prompt,
                system="You maintain a precise, factual investigation log for another agent. Be "
                "concise; preserve confirmed facts; flag any drift from earlier findings.",
                temperature=0.2, num_predict=1200)
        except LLMError:
            return prev
        wm = text.strip()
        return wm[:4000] if len(wm) >= 20 else prev

    async def _prm_pick(self, objective: str, working_memory: str,
                        candidates: list[str]) -> str | None:
        """Score candidate next-actions with the PRM sidecar and return the best.
        None on any failure — the caller keeps the first draw, so a down PRM
        never blocks the run."""
        query = objective[:600] + "\n\nCURRENT INVESTIGATION STATE:\n" + working_memory[:1500]
        steps = [c.strip()[:1200] for c in candidates]
        try:
            r = await self.ollama.http.post(self.cfg.prm_url.rstrip("/") + "/score",
                                            json={"query": query, "steps": steps},
                                            timeout=60.0)
            r.raise_for_status()
            scores = r.json().get("step_scores") or []
            if len(scores) != len(candidates):
                return None
            best = max(range(len(candidates)), key=lambda i: scores[i])
            log.info("prm rerank: picked candidate %d/%d (scores %s)",
                     best + 1, len(candidates), [round(s, 2) for s in scores])
            return candidates[best]
        except Exception as e:
            log.debug("prm rerank failed (keeping first draw): %s", e)
            return None

    async def _strategy_pivot(self, objective: str, working_memory: str,
                              scroll: list[str]) -> str:
        """Forced critic consult when dynamic actions stop changing outcomes: the
        actor is stuck pulling one lever (v10c: escalating payload sizes through
        4 identical "Execution successful" results). Every other intervention is
        imperative text the actor has learned to ignore — this scaffolds the
        REASONING step instead, from a model that isn't the one looping."""
        critic = self.cfg.agent_critic_model or self.cfg.agent_model or self.cfg.local_models[0]
        recent = "\n\n".join(scroll)
        prompt = (
            f"OBJECTIVE:\n{objective[:800]}\n\n"
            f"CURRENT INVESTIGATION STATE:\n{working_memory[:2000]}\n\n"
            f"RECENT ACTIONS AND RESULTS:\n{recent[:4000]}\n\n"
            "The actor's last several dynamic actions returned IDENTICAL results — the current "
            "approach is not changing the outcome. Propose ONE categorically different next "
            "approach: a different primitive, angle, or tool, grounded in the evidence above "
            "(e.g. if payloads never crash the server, pivot from crash-hunting to an "
            "information-leak / read primitive built on the same overflow). Two short "
            "sentences: what to stop doing, and the concrete alternative to try next."
        )
        try:
            text = await self._critic_generate(
                critic, prompt,
                system="You are a senior exploitation reviewer. The junior agent is stuck in a "
                "loop; give it one decisive redirection, not a lecture.",
                temperature=0.4, num_predict=220)
        except LLMError:
            return ("Stop repeating the current approach. Pick a different primitive or angle "
                    "(e.g. information leak instead of crash-hunting) and test it directly.")
        return text.strip()[:600]

    def _agent_system(self, persona: str) -> str:
        base = (persona + "\n\n") if persona else ""
        base += (
            "You are Enigma operating as an autonomous agent. You pursue the objective "
            "through many small, verified steps: observe with a tool, read the real result, "
            "then decide the next action. Never assume a command's output — run it and look. "
            "Work methodically; when a step fails, diagnose from the actual error before retrying. "
            "Do not claim success you have not verified.\n\n"
            + self.tools.docs()
        )
        return base

    # Exploit-dev procedure, injected into every agent run's head. Lessons are
    # heuristics; what the actor lacks is METHODOLOGY — phases with exit criteria,
    # so it can tell "still reaching the bug" from "confirmed, now control it"
    # (every past run blurred these: gdb-first before a confirmed crash,
    # payload-crafting before knowing what it controlled).
    _AGENT_METHODOLOGY = """\
METHODOLOGY — work the phases in order; each has an EXIT CRITERION. Do not skip
ahead, and do not re-enter a phase whose criterion is already met:
1. RECON — read the task materials; identify the bug CLASS (read vs write, stack
   vs global, index- vs size-driven). EXIT: bug class + trigger mechanism stated
   from real evidence (source/ASM, not the prose description).
2. REACH — prove your input ARRIVES at the bug (breakpoint on the function, or
   the binary's own output). EXIT: one input that provably reaches the code.
3. CONFIRM — get a real signal (crash, sanitizer report, observable oracle) by
   running the target DIRECTLY (run.sh / binary), NOT under gdb. EXIT: a
   reproduced signal — or an honest "no signal; bug may be a no-op here".
4. CONTROL — only NOW use gdb, once, on the confirmed input: registers, PC,
   which input bytes land where. EXIT: "I control X" stated with values.
5. STRATEGY — pick the primitive the EVIDENCE supports (leak vs smash vs
   index-walk), not the one assumed at RECON. EXIT: one sentence: primitive + why.
6. DELIVER — build the payload for the SERVER, send it, read the response,
   iterate. EXIT: the success criterion verified in place."""

    def _agent_prompt(self, objective: str, insights: list[str],
                      reflections: list[str], cases=None) -> str:
        parts = [f"OBJECTIVE:\n{objective}", self._AGENT_METHODOLOGY]
        if cases:
            # The closest banked SOLVED run: its final working memory is the only
            # store of what actually worked — surface it as an anchor, not gospel.
            parts.append("PAST SUCCESSFUL RUN (closest exemplar; its final investigation "
                         "state — adapt, don't copy):\n" + (cases[0]["output"] or "")[:800])
        if reflections:
            parts.append("PRINCIPLES YOU'VE DISTILLED (apply if relevant):\n- " + "\n- ".join(reflections[:5]))
        if insights:
            parts.append("RELEVANT LESSONS:\n- " + "\n- ".join(insights[:8]))
        parts.append("Begin. Take the first concrete step now — call a TOOL to observe the environment.")
        return "\n\n".join(parts)

    # ---- learning from agent runs (so repeated attempts improve) ------------

    async def learn_from_agent_run(self, objective: str, result: dict, *,
                                   area: str = "exploitation") -> None:
        """Turn an agent_run into memory so future runs are better: record a
        GROUNDED competence outcome for `area` (feeds the self-model / `enigma
        mind`), distill transferable lessons — especially failure modes — into the
        playbook, and bank a solved case. Dreaming later consolidates the lessons
        into higher-order principles that agent_run recalls at the start of the
        next attempt: run → lessons → dream → recalled → better run."""
        solved = result.get("status") in ("solved", "done")
        try:
            self.store.record_area_outcome(area, solved, 1.0 if solved else 0.0)
        except Exception:
            log.debug("competence record failed", exc_info=True)
        # Credit/blame the lessons this run was actually given (ACE curation).
        # Without this, agent insights sit at helpful=0/harmful=0 forever:
        # prune_insights can never fire and cap_insights ranks by popularity
        # alone — lesson pollution becomes structural.
        try:
            self.store.mark_insights(result.get("recalled_insight_ids") or [],
                                     helpful=solved)
            self.store.mark_reflections(result.get("recalled_reflection_ids") or [],
                                        helpful=solved)
        except Exception:
            log.debug("lesson credit/blame failed", exc_info=True)
        try:
            lessons = await self._distill_agent_lessons(objective, result)
        except LLMError:
            lessons = []
        for lesson in lessons:
            try:
                emb = await self.ollama.embed(lesson)
                if not self.store.is_duplicate_insight(lesson, emb):
                    self.store.add_insight("agent", "agent", lesson, emb)
            except Exception:
                log.debug("insight add failed", exc_info=True)
        if solved and result.get("working_memory"):
            try:
                emb = await self.ollama.embed(objective[:1000])
                self.store.add_case("agent", "agent", objective[:1000], emb,
                                    str(result.get("working_memory"))[:4000], 1.0)
            except Exception:
                log.debug("case add failed", exc_info=True)
        try:
            self.store.prune_insights()
        except Exception:
            pass
        log.info("learned from agent run (area=%s solved=%s, +%d lessons)", area, solved, len(lessons))

    async def _distill_agent_lessons(self, objective: str, result: dict) -> list[str]:
        # A dedicated, stronger distiller (gemma4:e4b via ENIGMA_AGENT_LESSON_MODEL —
        # gemma4:26b degenerates into repetition loops on this long prompt, and
        # llama3.1:8b produced generic or environment-WRONG lessons like "use
        # AddressSanitizer" for a non-ASAN binary). The prompt gets the full final
        # working memory + trajectory stats + a repeat-annotated trace, and answers
        # one question: what should attempt N+1 do differently in its FIRST 30 steps.
        model = (self.cfg.agent_lesson_model or self.cfg.utility_model
                 or self.cfg.local_models[0])
        records = [r for r in result.get("transcript", [])
                   if r.get("action") in ("tool", "done", "consolidate")]
        tools = [r for r in records if r.get("action") == "tool"]
        blocked = sum(1 for r in tools if "blocked by harness" in str(r.get("result", "")))
        nudges = sum(1 for r in tools if "harness checkpoint" in str(r.get("result", "")))
        dynamic = sum(1 for r in tools if r.get("tool") == "shell"
                      and not _shell_is_static(r.get("arg") or ""))
        contacted = any(s in json.dumps([r.get("arg", "") for r in tools])
                        for s in ("create_server", "health_check", ":8666", "catflag"))
        distinct = len({(r.get("tool"), " ".join((r.get("arg") or "").split())[:120])
                        for r in tools})
        stats = (f"steps={len(tools)}, hard-blocked static repeats={blocked}, "
                 f"phase nudges={nudges}, dynamic shell calls={dynamic}, "
                 f"distinct calls={distinct}, contacted controller/catflag={contacted}, "
                 f"status={result.get('status')}")
        # Trace with REPEAT COUNTS, first-appearance order — repetition IS the
        # story (v10c created the server 5× and re-sent escalating payloads), and
        # pure first-appearance dedup hides exactly that. Mark harness-blocked keys.
        counts: dict[str, list] = {}
        order: list[str] = []
        blocked_keys: set[str] = set()
        for r in tools:
            key = f"{r.get('tool')}:{(r.get('arg') or '')[:100]}"
            if key not in counts:
                counts[key] = []
                order.append(key)
            counts[key].append(r.get("step"))
            if "blocked by harness" in str(r.get("result", "")):
                blocked_keys.add(key)
        trace: list[str] = []
        for key in order:
            tool, arg = key.split(":", 1)
            steps = counts[key]
            rep = (f" (×{len(steps)}, steps {','.join(map(str, steps[:10]))})"
                   if len(steps) > 1 else "")
            mark = " [BLOCKED by harness]" if key in blocked_keys else ""
            trace.append(f"- {tool}: {arg[:110]}{rep}{mark}")
            if len(trace) >= 40:
                break
        prompt = (
            f"An autonomous exploitation agent attempted this objective:\n{objective[:700]}\n\n"
            f"RUN STATS: {stats}\n\n"
            f"FINAL INVESTIGATION STATE (the critic's working memory):\n"
            f"{str(result.get('working_memory') or '(none)')[:2200]}\n\n"
            f"DISTINCT ACTIONS TAKEN (deduped, in order):\n" + "\n".join(trace) + "\n\n"
            + ("" if contacted else
               "CRITICAL: this run NEVER contacted the controller/server — yet the flag only "
               "exists THERE. The FIRST lesson must address this: local analysis that never "
               "hands off to the server scores zero, no matter how good it is.\n")
            + "The NEXT attempt at this kind of task starts with your lessons recalled into its "
            "opening prompt. What should it do DIFFERENTLY in its first 30 steps? Extract 2-4 "
            "lessons. Each lesson MUST be:\n"
            "- a concrete ACTION or behavior (something to DO or STOP DOING), not background knowledge\n"
            "- ONE sentence, at most 35 words — it will be injected into a prompt verbatim\n"
            "- grounded in what actually happened above (name the call/behavior it comes from)\n"
            "- usable on a DIFFERENT but similar binary-exploitation task\n"
            "Do NOT output: generic advice ('verify inputs carefully'), suggestions incompatible "
            "with the environment (e.g. 'use AddressSanitizer' when the binary is non-ASAN), or "
            "restatements of the vulnerability description.\n"
            'Respond with ONLY JSON: {"lessons": ["...", ...]}'
        )
        try:
            raw = (await self.ollama.generate(model, prompt, temperature=0.3,
                                              format_json=True, num_predict=1500)).text
        except LLMError:
            return []
        obj = extract_json(raw)
        items = obj.get("lessons") if obj else None
        out = []
        if isinstance(items, list):
            for l in items:
                if isinstance(l, str) and 15 <= len(l.strip()) <= 400:
                    out.append(l.strip())
        return out[:4]

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
        # Memory-mode ablation (drives `enigma bench`): full | none | cases | insights.
        mode = cfg.memory_mode
        use_playbook = mode in ("full", "insights")
        use_cases = mode in ("full", "cases")
        recalled = self.store.recall(query, query_emb, cfg.recall_top_k) if use_playbook else []
        state.recalled_ids = [i for i, _ in recalled]
        insights = [lesson for _, lesson in recalled]
        recalled_refl = self.store.recall_reflections(query, query_emb, cfg.recall_reflections_k) if use_playbook else []
        state.recalled_reflection_ids = [i for i, _ in recalled_refl]
        reflections = [lesson for _, lesson in recalled_refl]
        cases = (self.store.recall_cases(context, query, query_emb, cfg.recall_cases, cfg.recall_case_floor)
                 if use_cases else [])

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
                prompt = self._build_prompt(task, state, reflection, insights, reflections, style_hints[arm.style], cases)

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
                    reflection_task = asyncio.create_task(self._reflect(task, best, self.util_model))
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
            gen = await self._generate(arm, prompt)
        except LLMError as e:
            log.warning("generation failed (%s): %s", arm.model, e)
            return None
        text = self._clean(task, gen.text, arm.style)
        if not text or is_nonanswer(text):
            return None  # empty or a placeholder echo ('<answer>', 'TODO', …)
        # Repetition-loop gate: drop degenerate output before evaluating it.
        if is_degenerate(text):
            log.warning("dropped degenerate generation (%s)", arm.model)
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

    async def _generate(self, arm: Arm, prompt: str) -> GenOut:
        """One candidate generation, with a ReAct tool loop when tools are on."""
        # Forced temperature (>=0) overrides the arm — the eval harness uses 0
        # for deterministic, reproducible measurement.
        temp = self.cfg.force_temperature if self.cfg.force_temperature >= 0 else arm.temperature
        if not self.tools.enabled:
            return await self.ollama.generate(
                arm.model, prompt, system=_SYSTEM, temperature=temp, want_confidence=True
            )
        convo = prompt
        gen = GenOut("", None)
        for step in range(self.cfg.tool_max_steps + 1):
            gen = await self.ollama.generate(
                arm.model, convo, system=_SYSTEM_TOOLS, temperature=temp, want_confidence=True
            )
            call = parse_tool_call(gen.text)
            final = gen.text.find("FINAL:")
            # Done only if the model committed to FINAL before any tool call —
            # a tool call before FINAL means it fabricated the result, so run it.
            if call is None or (final != -1 and final < call.start) or step == self.cfg.tool_max_steps:
                break
            result = await self.tools.run(call.name, call.arg)
            convo += (
                gen.text[:call.start].strip()  # drop anything after the tool call
                + f"\nTOOL {call.name}: {call.arg}\n\nTOOL RESULT [{call.name}]:\n{result}\n\n"
                + "Continue. Call another tool if needed, otherwise write FINAL: <answer>.\n"
            )
        return gen

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
        reflections: list[str],
        style_hint: str,
        cases: list,
    ) -> str:
        cfg = self.cfg
        parts = [f"TASK:\n{task.description}"]
        if task.input is not None:
            parts.append(f"INPUT:\n{task.input_as_text()[:cfg.prompt_input_chars]}")
        parts.append(_OUTPUT_HINTS[task.output_kind])
        if reflections:
            # Consolidated cross-task principles (dream-distilled) lead: they are
            # the most general, highest-signal memory the engine holds.
            parts.append("PRINCIPLES (consolidated from many past tasks):\n- " + "\n- ".join(reflections))
        for i, case in enumerate(cases):
            label = "A SIMILAR TASK WAS SOLVED BEFORE" if i == 0 else "ANOTHER SOLVED EXAMPLE"
            parts.append(
                f"{label}.\nThat task: {case['description'][:1200]}\n"
                f"Its accepted solution:\n{case['output'][:cfg.prompt_case_chars]}"
            )
        if insights:
            parts.append("PLAYBOOK (lessons from prior tasks, apply when relevant):\n- " + "\n- ".join(insights))
        if state.archive:
            # Usually the best; sometimes a diverse runner-up so the search
            # doesn't tunnel on one basin (evolutionary parent sampling).
            pool = state.archive
            exemplar = pool[0] if len(pool) == 1 or random.random() > 0.25 else random.choice(pool[1:3] or pool[:1])
            parts.append(
                f"BEST ATTEMPT SO FAR (score {exemplar.score:.2f}):\n{exemplar.content[:cfg.prompt_exemplar_chars]}\n"
                f"EVALUATOR FEEDBACK ON IT:\n{exemplar.feedback[:cfg.prompt_feedback_chars]}"
            )
        if reflection:
            parts.append(f"CRITIQUE TO ADDRESS:\n{reflection[:cfg.prompt_feedback_chars]}")
        parts.append(style_hint)
        if state.archive or reflection:
            parts.append("Produce a strictly better attempt. Fix every issue named above.")
        if self.tools.enabled:
            parts.append(self.tools.docs())
        return "\n\n".join(parts)

    def _clean(self, task: TaskSpec, text: str, style: str) -> str:
        # Tool loop: the committed answer is whatever follows the last FINAL:.
        if "FINAL:" in text:
            text = text.rsplit("FINAL:", 1)[1].strip()
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
        self.store.mark_reflections(result.recalled_reflections, helpful=succeeded)
        self.store.prune_insights()

        model = self.util_model
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


def _step_brief(record: dict) -> str:
    """One-line summary of an agent step for logs."""
    action = record.get("action")
    if action == "tool":
        arg = (record.get("arg") or "").replace("\n", " ")[:80]
        return f"TOOL {record.get('tool')}: {arg}"
    if action == "done":
        return f"DONE: {(record.get('summary') or '')[:80]}"
    return "(no tool call — nudged)"


# Shell command leaders that only OBSERVE — they never change the target's
# state, execute it, or touch the network. Used by agent_run's static-analysis
# checkpoint: a run whose every step is built from these is a read-loop, no
# matter how varied the arguments are.
_STATIC_SHELL_LEADERS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "find",
    "file", "readelf", "nm", "strings", "objdump", "wc", "sed", "awk",
    "xxd", "od", "hexdump", "checksec", "sort", "uniq", "tr", "cut", "diff",
    "pwd", "cd", "echo", "true", "stat", "du", "df", "env", "printenv",
    "which", "type", "less", "more", "column", "c++filt", "addr2line", "jq",
}


def _would_repeat(call, seen_calls: dict, seen_skeletons: dict,
                  unexecuted_writes: dict, tools) -> bool:
    """True when the actor's proposed call repeats something already tried this
    run — the trigger for PRM reranking (sample alternatives before committing
    to a known-dead draw)."""
    exact_key = (call.name, " ".join(call.arg.split())[:300])
    skel_key = (call.name, re.sub(r"\d+", "0", " ".join(call.arg.split()))[:300])
    if seen_calls.get(exact_key) or seen_skeletons.get(skel_key):
        return True
    if call.name == "write":
        target = (call.arg.strip().splitlines() or [""])[0].strip()
        if target and unexecuted_writes.get(tools._in_box(target), 0) > 0:
            return True
    return False


def _shell_is_static(cmd: str) -> bool:
    """True when EVERY pipeline/compound stage is passive inspection.

    `./run.sh`, `gdb`, `python3`, `nc`, `curl`, or any stage not on the passive
    list makes the whole command dynamic. Misclassifying a passive blob as
    dynamic only suppresses a nudge; misclassifying dynamic work as static
    would nag an agent that is actually acting — so ambiguous constructs
    (loops, assignments) count as dynamic.
    """
    for part in cmd.replace("&&", ";").replace("||", ";").replace("|", ";").split(";"):
        toks = part.split()
        if not toks:
            continue
        leader = toks[0].rsplit("/", 1)[-1]
        if leader not in _STATIC_SHELL_LEADERS:
            return False
    return True


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
