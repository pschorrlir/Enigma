"""Calibrated cascade deferral: decide WHEN a cloud cohesion pass pays.

Instead of a fixed "no improvement for `patience` iterations" rule, consult
episode history for this evaluator kind: among tasks that eventually
succeeded with local models only, how often were they at-or-below the
current best score at this iteration? If almost none were (local recovery
from this state is historically rare), escalate now rather than burning
more local iterations. Falls back to the patience heuristic until enough
history accumulates (UCCI-style calibrated deferral, poor-man's edition).
"""

from __future__ import annotations

import sqlite3

MIN_HISTORY_EPISODES = 30
RECOVERY_FLOOR = 0.2  # escalate early when < 20% of successful runs looked this bad


def should_escalate(
    history: list[sqlite3.Row],
    iteration: int,
    best_score: float,
    stall: int,
    patience: int,
) -> bool:
    if stall >= patience:
        return True
    if stall == 0 or len(history) < MIN_HISTORY_EPISODES:
        return False
    # Episodes at this iteration or earlier, from tasks that finished locally.
    succeeded_low = succeeded_total = 0
    for row in history:
        if row["iteration"] > iteration or row["score"] is None:
            continue
        if row["status"] == "succeeded":
            succeeded_total += 1
            if row["score"] <= best_score + 0.05:
                succeeded_low += 1
    if succeeded_total < MIN_HISTORY_EPISODES // 2:
        return False
    recovery_rate = succeeded_low / succeeded_total
    return recovery_rate < RECOVERY_FLOOR
