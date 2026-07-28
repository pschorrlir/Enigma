"""Dreaming: idle-time consolidation + self-play.

When the daemon's queue is empty (or on `enigma dream`) the engine "sleeps"
and does two things a busy engine never has time for:

  1. Consolidate — cluster the episodic playbook (insights) by embedding
     similarity and distill each dense cluster into ONE higher-order
     principle stored in the `reflections` tier (semantic memory). Then
     dedup the case bank and prune net-harmful insights. Memory gets
     smaller, more general, and higher-signal.

  2. Self-play — take past solved cases of open-ended kinds, invent harder
     sibling tasks, solve them locally, and run the normal learning pass on
     the results. The engine practices against its own imagined curriculum,
     growing the case bank and playbook while idle.

A dream yields the moment real work arrives: the daemon cancels the cycle
and the interrupted self-play task is marked failed (never requeued).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field

from .config import Config
from .engine import Engine, result_to_json
from .evaluators import Evaluator
from .llm import LLMError, extract_json, is_degenerate
from .memory import Store, _cosine, _jaccard, _tokens
from .task import TaskSpec

log = logging.getLogger("enigma.dream")

# Self-play invents VERIFIABLE coding problems (reference solution + tests it
# actually passes) so grading is real execution, not an llm_judge vibe-check.
# Earlier llm_judge self-play produced meaningless 1.0s: it graded ill-posed
# problems and bare/absent answers as perfect. Coding problems across these
# sub-areas give varied, checkable skill practice.
_CODE_AREAS = (
    "string processing",
    "arithmetic or number theory",
    "list / array manipulation",
    "searching or sorting",
    "recursion or dynamic programming",
    "parsing structured text or simple expressions",
    "hash maps and counting",
    "greedy or simulation logic",
    "basic graph or tree traversal",
    "dates, times, or intervals",
)
_REFLECTION_DEDUP = 0.90

# Angles the engine looks through when forging an INSIGHT from two concepts.
# These ask for coherent, useful connections — not surreal juxtapositions.
# Variety of lens matters as much as variety of material.
_IDEA_LENSES = (
    "a non-obvious principle that genuinely connects them",
    "a testable hypothesis about how one influences the other",
    "an insight that explains something previously puzzling about either",
    "a concrete technique or method that transfers from one to the other",
    "a prediction or consequence that follows if both hold",
    "a synthesis that resolves a real tension between them",
)


@dataclass(slots=True)
class DreamReport:
    reflections_added: int = 0
    clusters_seen: int = 0
    cases_merged: int = 0
    insights_pruned: int = 0
    plays_attempted: int = 0
    plays_solved: int = 0
    ideas_added: int = 0
    topics: list[str] = field(default_factory=list)
    competence_delta: dict[str, float] = field(default_factory=dict)
    interrupted: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "dream interrupted by real work" if self.interrupted else "dream complete"
        line = (
            f"{head}: +{self.reflections_added} reflections from {self.clusters_seen} clusters, "
            f"{self.cases_merged} cases merged, {self.insights_pruned} insights pruned, "
            f"self-play {self.plays_solved}/{self.plays_attempted} solved, +{self.ideas_added} ideas"
        )
        if self.topics:
            line += "\n  topics: " + " · ".join(self.topics)
        if self.competence_delta:
            moved = sorted(self.competence_delta.items(), key=lambda kv: -abs(kv[1]))
            line += "\n  competence: " + " · ".join(
                f"{a} {d:+.2f}" for a, d in moved)
        return line + ("".join(f"\n  - {n}" for n in self.notes) if self.notes else "")


class Dreamer:
    def __init__(self, cfg: Config, store: Store, engine: Engine):
        self.cfg = cfg
        self.store = store
        self.engine = engine
        # Cheap meta-ops (abstract, invent) run on the fast utility model, not
        # the big solver model — consolidation over many clusters must stay cheap.
        self._model = cfg.utility_model or cfg.local_models[0]
        # The entity's voice — colors what it's curious about and how it narrates,
        # never how it grades. Loaded once per dreamer.
        self._persona = cfg.persona()

    def _as_persona(self, instruction: str) -> str:
        """Prepend the persona so generation carries the entity's voice. Used only
        for curiosity/creative steps (director, ideation) — never for grading."""
        if not self._persona:
            return instruction
        return (
            "You are the following entity. Let it shape what you find worth "
            "exploring and the voice of your reasoning, but obey the output format exactly:\n"
            f"{self._persona}\n\n---\n\n{instruction}"
        )

    async def dream(self, should_stop: asyncio.Event | None = None) -> DreamReport:
        report = DreamReport()
        before = self.store.competence_map()  # self-model snapshot for the delta
        await self._consolidate(report)
        if self._stopped(should_stop):
            report.interrupted = True
            return report
        # The director judges what this cycle should explore; self-play and
        # ideation are then steered toward those topics.
        report.topics = await self._direct()
        self.store.set_meta("dream_topics", json.dumps(report.topics))
        # Self-play stays UNthemed: honest, well-posed skill drills. Theming code
        # problems on abstract director topics is what produced contrived mashups
        # ("sort arrays as if money"). Topics steer ideation only.
        await self._self_play(report, should_stop)
        if not self._stopped(should_stop):
            await self._ideate(report, should_stop, report.topics)
        report.interrupted = self._stopped(should_stop)
        report.competence_delta = self._competence_delta(before, self.store.competence_map())
        return report

    @staticmethod
    def _competence_delta(before: dict, after: dict) -> dict[str, float]:
        """Which areas' measured competence moved this cycle — the entity's
        'did I get better' signal, grounded in real pass/fail outcomes."""
        moved: dict[str, float] = {}
        for area, a in after.items():
            d = a["competence"] - before.get(area, {}).get("competence", a["competence"])
            if abs(d) >= 0.01:
                moved[area] = round(d, 3)
        return moved

    # ---- director (chooses the topics each cycle explores) ------------------

    async def _direct(self) -> list[str]:
        """Judge which topics are worth exploring next, from what the engine has
        recently done, learned, and found promising — plus any operator focus."""
        if not self.cfg.dream_direct_enabled or self.cfg.dream_topics_per_cycle <= 0:
            return []
        recent = []
        for r in self.store.list_tasks(24):
            if r["source"] == "user":
                try:
                    recent.append(json.loads(r["spec"]).get("description", "")[:140])
                except Exception:
                    pass
        recent = recent[:6]
        idea_rows = self.store.list_ideas(8)
        liked = [r["statement"] for r in idea_rows if r["rating"] > 0][:4]
        ideas = [r["statement"] for r in idea_rows if r["rating"] >= 0][:5]
        principles = [r["lesson"] for r in self.store.list_reflections(6)]
        focus = self.store.get_meta("dream_focus", "").strip()
        weak = self._weakest_areas(4)

        parts = ["You direct a self-learning engine's idle-time exploration."]
        if focus:
            parts.append(f"THE OPERATOR WANTS YOU TO FOCUS ON: {focus}\nWeight this heavily.")
        if weak:
            parts.append("Your MEASURED weak spots (low competence / high uncertainty, from real "
                         "pass-fail outcomes) — bias exploration toward shoring these up:\n- "
                         + "\n- ".join(weak))
        if liked:
            parts.append("The operator LIKED these ideas — strongly favor related directions:\n- "
                         + "\n- ".join(i[:140] for i in liked))
        if recent:
            parts.append("Recent real user tasks:\n- " + "\n- ".join(recent))
        if ideas:
            parts.append("Other ideas found so far:\n- " + "\n- ".join(i[:140] for i in ideas))
        if principles:
            parts.append("Established principles:\n- " + "\n- ".join(p[:140] for p in principles))
        parts.append(
            f"Choose {self.cfg.dream_topics_per_cycle} specific TOPICS the engine should explore next "
            "to discover valuable new ideas and skills. Favor topics that are promising, underexplored, "
            "or connected to the operator's focus and the user's tasks; avoid ones already saturated. "
            "Each topic is a short noun phrase (3-8 words).\n"
            'Respond with ONLY JSON: {"topics": ["...", ...]}'
        )
        try:
            raw = (await self.engine.ollama.generate(
                self._model, self._as_persona("\n\n".join(parts)),
                temperature=0.7, format_json=True, num_predict=300
            )).text
        except LLMError:
            return []
        obj = extract_json(raw)
        items = obj.get("topics") if obj else None
        if not isinstance(items, list):
            return []
        topics = [str(t).strip()[:120] for t in items if isinstance(t, str) and t.strip()]
        return topics[: self.cfg.dream_topics_per_cycle]

    def _weakest_areas(self, k: int) -> list[str]:
        """Top-k frontier areas by learning priority, as human-readable labels
        for the director — the entity naming where it knows it's weak."""
        comp = self.store.competence_map()
        seen = [(a, m) for a, m in comp.items() if m["attempts"] > 0]
        seen.sort(key=lambda am: am[1]["priority"], reverse=True)
        return [f"{a} (competence {m['competence']:.2f}, n={m['attempts']})" for a, m in seen[:k]]

    @staticmethod
    def _stopped(ev: asyncio.Event | None) -> bool:
        return ev is not None and ev.is_set()

    # ---- consolidation --------------------------------------------------------

    async def _consolidate(self, report: DreamReport) -> None:
        await self._distill_reflections(report)
        report.cases_merged = self._dedup_cases()
        report.insights_pruned = self.store.prune_insights()
        self.store.prune_reflections()  # drop net-harmful principles (no longer immortal)
        # Optional per-tier ceilings (0 = unlimited), then the primary governor:
        # let memory grow until the disk budget bites, evicting weakest-first.
        self.store.cap_reflections(self.cfg.reflections_max)
        self.store.cap_insights(self.cfg.insights_max)
        self.store.cap_cases(self.cfg.cases_max)
        self.store.cap_ideas(self.cfg.ideas_max)
        evicted = self.store.enforce_memory_budget(
            self.cfg.memory_max_mb * 1024 * 1024, self.cfg.memory_evict_grace_hours * 3600
        )
        if evicted:
            report.notes.append("disk budget reached; evicted " +
                                ", ".join(f"{n} {k}" for k, n in evicted.items()))
            log.info("memory disk budget enforced: evicted %s", evicted)

    async def _distill_reflections(self, report: DreamReport) -> None:
        clusters = self._cluster_insights()
        if not clusters:
            return
        # Insights come newest-first, so greedy clusters are ordered by recency;
        # process only the freshest few per cycle to bound consolidation cost.
        clusters = clusters[: self.cfg.dream_max_clusters]
        existing = self.store.all_reflections()  # (id, lesson, emb)
        for cluster in clusters:
            report.clusters_seen += 1
            lessons = [lesson for _, lesson, _ in cluster]
            reflection = await self._abstract(lessons)
            if not reflection:
                continue
            emb = await self.engine.ollama.embed(reflection)
            if self._reflection_exists(reflection, emb, existing):
                continue  # already have this principle
            self.store.add_reflection(topic="", lesson=reflection, embedding=emb, support=len(cluster))
            existing.append((-1, reflection, emb))
            report.reflections_added += 1

    def _cluster_insights(self) -> list[list[tuple]]:
        """Cluster insights by embedding cosine, or by token overlap when the
        embed model is unavailable (embeddings are all None)."""
        insights = self.store.all_insights()
        embedded = [(i, l, e) for i, l, e, _k in insights if e]
        if len(embedded) >= self.cfg.dream_cluster_min:
            return _cluster(embedded, _cosine, self.cfg.dream_cluster_threshold, self.cfg.dream_cluster_min)
        toks = [(i, l, _tokens(l)) for i, l, _, _k in insights]
        return _cluster(toks, _jaccard, self.cfg.dream_cluster_token_threshold, min_size=2)

    @staticmethod
    def _reflection_exists(lesson: str, emb: list[float] | None, existing: list[tuple]) -> bool:
        toks = _tokens(lesson)
        for _, ex_lesson, ex_emb in existing:
            if emb is not None and ex_emb is not None:
                if _cosine(emb, ex_emb) > _REFLECTION_DEDUP:
                    return True
            elif _jaccard(toks, _tokens(ex_lesson)) > 0.6:
                return True
        return False

    async def _abstract(self, lessons: list[str]) -> str | None:
        joined = "\n".join(f"- {l}" for l in lessons[:12])
        prompt = (
            "These related lessons were each learned from a different task:\n"
            f"{joined}\n\n"
            "Write ONE sentence stating the single higher-level principle that generalizes "
            "them all — the reusable rule a solver should carry into future tasks. Be concrete "
            "and transferable. Output only the sentence."
        )
        try:
            text = (await self.engine.ollama.generate(self._model, prompt, temperature=0.3)).text.strip()
        except LLMError:
            return None
        text = text.splitlines()[0].strip().strip('"') if text else ""
        if len(text) < 20 or text.lower().startswith(("here", "sure", "okay", "the principle")):
            return None
        return text[:400]

    def _dedup_cases(self) -> int:
        """Merge near-identical solved exemplars, keeping the highest score."""
        rows = [r for r in self.store.all_cases() if r["embedding"]]
        by_kind: dict[str, list] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        to_delete: list[int] = []
        import json as _json

        for group in by_kind.values():
            decoded = [(r, _json.loads(r["embedding"])) for r in group]
            kept: list[tuple] = []
            for r, emb in decoded:
                dup_of = next((k for k in kept if _cosine(emb, k[1]) > self.cfg.dream_case_dedup_threshold), None)
                if dup_of is None:
                    kept.append((r, emb))
                    continue
                # Keep whichever scored higher; drop the other.
                loser = r if (r["score"] or 0) <= (dup_of[0]["score"] or 0) else dup_of[0]
                to_delete.append(loser["id"])
                if loser is dup_of[0]:
                    kept.remove(dup_of)
                    kept.append((r, emb))
        return self.store.delete_cases(to_delete)

    # ---- self-play ------------------------------------------------------------

    async def _self_play(self, report: DreamReport, should_stop: asyncio.Event | None) -> None:
        if self.cfg.dream_max_plays <= 0:
            return
        plays = attempts = 0
        # Inventions that fail validation (ill-posed, reference fails its own
        # tests) are discarded, so allow extra attempts to reach the play quota.
        max_attempts = self.cfg.dream_max_plays * 5
        comp = self.store.competence_map()
        while plays < self.cfg.dream_max_plays and attempts < max_attempts and not self._stopped(should_stop):
            # Frontier-targeting: practise where the self-model is weakest / least
            # certain, not uniformly. This is the goal-directed part of the entity.
            area = self._pick_frontier_area(comp)
            attempts += 1
            # Difficulty curriculum: push harder where the solver already wins,
            # ease off where it struggles — aim the frontier near ~50% solve rate.
            rate = comp.get(area, {}).get("competence")
            has_data = comp.get(area, {}).get("attempts", 0) > 0
            difficulty = ("challenging" if has_data and rate > 0.7
                          else "straightforward" if has_data and rate < 0.3
                          else "moderate")
            task = await self._invent_verifiable(None, area, difficulty)
            if task is None:
                continue
            plays += 1
            report.plays_attempted += 1
            solved = await self._play(task)
            self.store.record_area_outcome(area, solved)
            if solved:
                report.plays_solved += 1
        if not report.plays_attempted:
            report.notes.append("self-play invented no verifiable tasks this cycle")

    def _pick_frontier_area(self, comp: dict[str, dict[str, float]]) -> str:
        """Choose a code area to practise, biased toward the learning frontier
        (weak + uncertain areas have higher priority). Unseen areas get max
        priority so the map fills in; sampling stays stochastic to avoid ruts."""
        weights = [max(0.05, comp.get(a, {}).get("priority", 1.0)) for a in _CODE_AREAS]
        return random.choices(_CODE_AREAS, weights=weights, k=1)[0]

    async def _invent_verifiable(self, topic: str | None = None, area: str | None = None,
                                difficulty: str = "moderate") -> TaskSpec | None:
        """Invent a coding problem WITH a reference solution and tests, then keep
        it only if the reference solution actually passes those tests — proof the
        problem is well-posed and the tests are valid. The solver later attempts
        it fresh, graded by real execution (python_tests), not by opinion."""
        area = area or random.choice(_CODE_AREAS)
        about = f", themed around {topic}" if topic else ""
        prompt = (
            f"Invent ONE small, self-contained, {difficulty} Python coding problem about {area}{about}.\n"
            "Requirements:\n"
            "- The description must name the exact function to implement and its signature, and fully "
            "specify the behaviour with an example. It must be solvable from the description alone.\n"
            "- Provide a correct reference SOLUTION (the complete function).\n"
            "- Provide 3-6 assert-based TESTS that call that function by name and check it.\n"
            'Respond with ONLY JSON: {"description": "...", "solution": "<python code>", '
            '"tests": "assert f(...) == ...\\nassert f(...) == ..."}'
        )
        try:
            raw = (await self.engine.ollama.generate(
                self._model, prompt, temperature=0.7, format_json=True, num_predict=1200
            )).text
        except LLMError:
            return None
        obj = extract_json(raw)
        if not obj:
            return None
        desc, solution, tests = obj.get("description"), obj.get("solution"), obj.get("tests")
        if not all(isinstance(x, str) and x.strip() for x in (desc, solution, tests)):
            return None
        desc = desc.strip()
        if len(desc) < 20 or is_degenerate(desc) or "assert" not in tests:
            return None
        # Validate: the reference solution must pass its own tests. This filters
        # out ill-posed problems and inconsistent/hallucinated tests.
        evaluator = Evaluator({"kind": "python_tests", "tests": tests}, self.engine.ollama, self._model)
        try:
            check = await evaluator.evaluate(TaskSpec(description=desc, output_kind="code"), solution)
        except Exception:
            return None
        if check.score < 1.0:
            return None  # reference solution doesn't satisfy its own tests — discard
        return TaskSpec(
            description=desc[:2000],
            output_kind="code",
            evaluator={"kind": "python_tests", "tests": tests[:4000]},
        )

    async def _play(self, task: TaskSpec) -> bool:
        """Run one imagined task through the full engine, then learn from it.
        Mirrors the daemon worker so cases/insights/styles accrue as usual."""
        self.store.insert_dream_task(task.id, task.to_json())
        try:
            result = await self.engine.run_task(task)
        except asyncio.CancelledError:
            self.store.finish(task.id, "failed", '{"error": "dream interrupted"}')
            raise
        except Exception:
            log.exception("dream self-play task %s crashed", task.id)
            self.store.finish(task.id, "failed", '{"error": "internal error"}')
            return False
        self.store.finish(task.id, result.status, result_to_json(result))
        log.info("dream play %s -> %s (score %.2f)", task.id, result.status,
                 result.best.score if result.best else 0.0)
        try:
            await self.engine.learn(task, result)
        except Exception:
            log.exception("dream learning failed for %s", task.id)
        return result.status == "succeeded"

    # ---- ideation (discovery of novel ideas) --------------------------------

    async def _ideate(self, report: DreamReport, should_stop: asyncio.Event | None,
                     topics: list[str] | None = None) -> None:
        """Forge insights by bridging MODERATELY distant memory concepts (the
        band where a real, non-obvious connection exists), keeping only those
        that clear a novelty gate (not already known) and a value gate (a strict
        judge that rejects forced analogies). When the director set topics, ideas
        are anchored to them. Ranking is value-dominant, not novelty-dominant."""
        if not self.cfg.ideate_enabled or self.cfg.dream_max_ideas <= 0:
            return
        concepts = self._concept_pool()
        if len(concepts) < 2:
            report.notes.append("not enough embedded memory to ideate from yet")
            return
        existing = [(s, e) for _, s, e, _r in self.store.all_ideas() if e]  # (statement, embedding)
        lenses = list(_IDEA_LENSES)
        random.shuffle(lenses)
        kept = attempts = 0
        max_attempts = self.cfg.dream_max_ideas * 3
        while kept < self.cfg.dream_max_ideas and attempts < max_attempts and not self._stopped(should_stop):
            lens = lenses[attempts % len(lenses)]
            topic = topics[attempts % len(topics)] if topics else None
            attempts += 1
            pair = self._bridge_pair(concepts)
            sparked = await self._spark_idea(lens, pair, topic)
            if sparked is None:
                continue
            statement, elaboration = sparked
            emb = await self.engine.ollama.embed(statement)
            novelty = self._novelty(emb, existing, concepts)
            if novelty < self.cfg.idea_min_novelty:
                continue  # too close to something already known
            value, reason = await self._value_of(statement, elaboration)
            if value < self.cfg.idea_min_value:
                continue  # not worth keeping
            # Value-dominant ranking: usefulness/coherence outweighs raw novelty,
            # so a solid insight beats a weird-but-useless juxtaposition.
            score = round(0.7 * value + 0.3 * novelty, 4)
            if self.cfg.idea_ground:
                elaboration = await self._ground(statement, elaboration)
            parents = [p[0][:160] for p in pair] if pair else []
            self.store.add_idea(statement, elaboration, emb, round(novelty, 3), round(value, 3), score, parents)
            if emb is not None:
                existing.append((statement, emb))
            kept += 1
            report.ideas_added += 1
            log.info("dream idea (nov %.2f val %.2f): %s", novelty, value, statement[:120])
        self.store.cap_ideas(self.cfg.ideas_max)

    def _concept_pool(self) -> list[tuple[str, list[float]]]:
        """(text, embedding) drawn from every knowledge tier — the raw material
        for recombination."""
        pool: list[tuple[str, list[float]]] = []
        for _id, text, emb, _kind in self.store.all_insights():
            if emb:
                pool.append((text, emb))
        for _id, text, emb in self.store.all_reflections():
            if emb:
                pool.append((text, emb))
        for r in self.store.all_cases():
            if r["embedding"]:
                pool.append(((r["description"] or "")[:400], json.loads(r["embedding"])))
        # Operator-ingested documents — recombining the user's own material is
        # where genuine "I'd never have connected those" ideas come from.
        for text, emb in self.store.all_docs():
            if emb:
                pool.append((text[:400], emb))
        return pool

    def _bridge_pair(self, concepts: list[tuple[str, list[float]]]):
        """An anchor concept and a MODERATELY distant match — one that sits in the
        bridge band [lo, hi]: far enough to be non-obvious, close enough that a
        real connection exists. Insight lives here; the global-minimum (max
        distance) pairing only yields incoherent mashups. Falls back to the
        least-similar in-sample concept if nothing lands in the band."""
        if len(concepts) < 2:
            return None
        anchor = random.choice(concepts)
        sample = [c for c in random.sample(concepts, min(len(concepts), 24)) if c is not anchor]
        if not sample:
            return None
        lo, hi = self.cfg.idea_bridge_lo, self.cfg.idea_bridge_hi
        scored = [(c, _cosine(anchor[1], c[1])) for c in sample]
        in_band = [c for c, s in scored if lo <= s <= hi]
        if in_band:
            partner = random.choice(in_band)
        else:
            # Nothing in band: prefer the concept closest to the band, not the
            # single most-distant one (that's the mashup failure mode).
            mid = (lo + hi) / 2
            partner = min(scored, key=lambda cs: abs(cs[1] - mid))[0]
        return (anchor, partner)

    async def _spark_idea(self, lens: str, pair, topic: str | None = None):
        toward = f" Aim it at the topic of {topic}." if topic else ""
        if pair:
            (a, _), (b, _) = pair
            body = (
                f"Concept A: {a[:400]}\nConcept B: {b[:400]}\n\n"
                f"These two concepts are connected in a non-obvious way. Articulate {lens}.\n"
                "It must be a GENUINE, coherent insight that is useful and could be acted on — "
                "not a forced metaphor, pun, or surface analogy, and not a restatement of either "
                f"concept. If no real connection exists, say so instead of inventing one.{toward}"
            )
        else:
            body = f"Articulate {lens.replace('them', 'a domain').replace('either', 'it')}.{toward or ' Make it specific, useful, and non-obvious.'}"
        prompt = self._as_persona(
            body + "\n\nRespond with ONLY JSON: "
            '{"idea": "<one crisp sentence, or empty if no real insight>", '
            '"elaboration": "<2-3 sentences on why it holds and how it could be used>"}'
        )
        try:
            raw = (await self.engine.ollama.generate(
                self._model, prompt, temperature=0.9, format_json=True, num_predict=500
            )).text
        except LLMError:
            return None
        obj = extract_json(raw)
        if not obj or not isinstance(obj.get("idea"), str):
            return None
        idea = obj["idea"].strip()
        if len(idea) < 15 or is_degenerate(idea):
            return None
        elaboration = obj.get("elaboration") if isinstance(obj.get("elaboration"), str) else ""
        return idea[:1000], elaboration.strip()[:2000]

    def _novelty(self, emb, existing: list, concepts: list[tuple[str, list[float]]]) -> float:
        """1 - max cosine similarity to any prior idea or memory concept."""
        if emb is None:
            return 1.0  # can't measure; don't block
        sims = [_cosine(emb, e) for _s, e in existing]
        for _text, cemb in random.sample(concepts, min(len(concepts), 40)):
            sims.append(_cosine(emb, cemb))
        return 1.0 - (max(sims) if sims else 0.0)

    async def _ground(self, statement: str, elaboration: str) -> str:
        """Attach a real-world prior-art check to an idea (best-effort)."""
        try:
            found = await self.engine.tools.run("search", statement[:160])
        except Exception:
            return elaboration
        if not found or "no results" in found.lower() or "failed" in found.lower():
            return elaboration
        return (elaboration + "\n\nPRIOR ART (web search):\n" + found[:800]).strip()

    async def _value_of(self, statement: str, elaboration: str):
        prompt = (
            f"Idea: {statement}\n"
            + (f"Detail: {elaboration}\n" if elaboration else "")
            + "\nRate this idea from 0.0 to 1.0 as a strict, skeptical evaluator. You are hunting "
            "for genuine INSIGHT — a coherent, useful, non-obvious connection. Reserve scores above "
            "0.7 for ideas that are specific, plausible, AND actually useful.\n"
            "Score BELOW 0.3 if the idea is a forced or surface-level analogy, a pun, a strained "
            "metaphor that mashes unrelated things together, or a cliche/restatement. An idea that "
            "sounds clever but wouldn't help anyone do or understand anything is worthless.\n"
            'Respond with ONLY JSON: {"score": <0.0-1.0>, "reason": "<one short clause>"}'
        )
        try:
            raw = (await self.engine.ollama.generate(
                self._model, prompt, temperature=0.0, format_json=True, num_predict=200
            )).text
        except LLMError:
            return 0.0, ""
        obj = extract_json(raw)
        if not obj or "score" not in obj:
            return 0.0, ""
        try:
            return max(0.0, min(1.0, float(obj["score"]))), str(obj.get("reason", ""))[:200]
        except (TypeError, ValueError):
            return 0.0, ""


def _cluster(items: list[tuple], sim, threshold: float, min_size: int) -> list[list[tuple]]:
    """Greedy single-pass clustering: each item joins the first cluster whose
    seed it is `sim`-similar enough to (>= threshold), else seeds a new one.
    `sim` compares the third element of each item tuple (embedding or tokens)."""
    clusters: list[list[tuple]] = []
    for item in items:
        for cluster in clusters:
            if sim(item[2], cluster[0][2]) >= threshold:
                cluster.append(item)
                break
        else:
            clusters.append([item])
    return [c for c in clusters if len(c) >= min_size]
