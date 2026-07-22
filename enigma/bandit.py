"""Thompson-sampling bandit over generation strategies.

An arm is (model, temperature, style). Per evaluator-kind context, the
engine samples from Beta(alpha, beta) posteriors and plays the best draw,
then feeds the achieved score back as a fractional reward. Over many tasks
the engine learns which local setup works for which kind of problem.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from .memory import Store

STYLES = ("direct", "plan_then_solve", "critique_revise")
TEMPERATURES = (0.2, 0.8)


@dataclass(frozen=True, slots=True)
class Arm:
    model: str
    temperature: float
    style: str

    @property
    def key(self) -> str:
        return json.dumps({"model": self.model, "temperature": self.temperature, "style": self.style}, sort_keys=True)

    @classmethod
    def from_key(cls, key: str) -> "Arm":
        d = json.loads(key)
        return cls(d["model"], d["temperature"], d["style"])


def build_arms(models: tuple[str, ...]) -> list[Arm]:
    return [Arm(m, t, s) for m in models for t in TEMPERATURES for s in STYLES]


class StrategyBandit:
    def __init__(self, store: Store, arms: list[Arm]):
        self._store = store
        self._arms = arms

    def select(self, context: str) -> Arm:
        stats = self._store.bandit_arms(context)
        best_arm, best_draw = self._arms[0], -1.0
        for arm in self._arms:
            alpha, beta = stats.get(arm.key, (1.0, 1.0))
            draw = random.betavariate(alpha, beta)
            if draw > best_draw:
                best_arm, best_draw = arm, draw
        return best_arm

    def reward(self, context: str, arm: Arm, score: float) -> None:
        self._store.bandit_update(context, arm.key, min(1.0, max(0.0, score)))
