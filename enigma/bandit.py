"""Thompson-sampling bandit over generation strategies.

An arm is (model, temperature, style). The model is chosen once per TASK
(Thompson over per-model aggregate posteriors) so a task stays on one
resident Ollama model instead of thrashing weights between iterations;
(temperature, style) is then sampled per iteration among that model's arms.

Styles include the built-ins plus GEPA-evolved hints ("evolved:<id>") loaded
from the store — the bandit is the guardrail that decides whether an evolved
prompt actually beats its ancestors.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from .memory import Store

BUILTIN_STYLES = ("direct", "plan_then_solve", "critique_revise")
TEMPERATURES = (0.2, 0.8)


@dataclass(frozen=True, slots=True)
class Arm:
    model: str
    temperature: float
    style: str  # builtin name or "evolved:<id>"

    @property
    def key(self) -> str:
        return json.dumps({"model": self.model, "temperature": self.temperature, "style": self.style}, sort_keys=True)


def build_arms(models: tuple[str, ...], styles: tuple[str, ...]) -> list[Arm]:
    return [Arm(m, t, s) for m in models for t in TEMPERATURES for s in styles]


class StrategyBandit:
    def __init__(self, store: Store):
        self._store = store
        # context -> {arm_key: (alpha, beta)}; write-through cache so per-
        # iteration selection never re-reads SQLite.
        self._cache: dict[str, dict[str, tuple[float, float]]] = {}

    def _stats(self, context: str) -> dict[str, tuple[float, float]]:
        if context not in self._cache:
            self._cache[context] = self._store.bandit_arms(context)
        return self._cache[context]

    def _draw(self, context: str, key: str) -> float:
        alpha, beta = self._stats(context).get(key, (1.0, 1.0))
        return random.betavariate(alpha, beta)

    def select(self, context: str, arms: list[Arm]) -> Arm:
        best_arm, best_draw = arms[0], -1.0
        for arm in arms:
            draw = self._draw(context, arm.key)
            if draw > best_draw:
                best_arm, best_draw = arm, draw
        return best_arm

    def select_model(self, context: str, models: tuple[str, ...]) -> str:
        """Thompson draw per model from its pooled arm posteriors."""
        if len(models) == 1:
            return models[0]
        stats = self._stats(context)
        best_model, best_draw = models[0], -1.0
        for model in models:
            alpha = beta = 1.0
            for key, (a, b) in stats.items():
                if json.loads(key).get("model") == model:
                    alpha += a - 1.0
                    beta += b - 1.0
            draw = random.betavariate(max(alpha, 0.01), max(beta, 0.01))
            if draw > best_draw:
                best_model, best_draw = model, draw
        return best_model

    def reward(self, context: str, arm: Arm, score: float) -> None:
        score = min(1.0, max(0.0, score))
        stats = self._stats(context)
        alpha, beta = stats.get(arm.key, (1.0, 1.0))
        stats[arm.key] = (alpha + score, beta + (1.0 - score))
        self._store.bandit_update(context, arm.key, score)
