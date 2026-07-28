# Homework Ladder Driver — closing the win flywheel

**Status:** approved design, pre-implementation
**Date:** 2026-07-27
**Context:** Enigma exploitation campaign (see AGENTS.md). ~13 scored ExploitGym
runs, all 0.0. The harness is no longer the bottleneck; the self-learning loop is
starved because every automatic input (distiller, case bank, dream clusters, LoRA
corpus) only ever sees failures. The homework ladder (`homework/`, 3
proven-solvable rungs) exists to manufacture wins. This spec covers the driver
that runs the ladder as a measurement experiment.

## Goal

Make the self-improvement thesis testable: **does the agent measurably improve
across attempts as wins accumulate in shared memory?** Metric: per-rung solve
rate and steps-to-solve, attempt 1 vs attempt K.

Non-goals: skill compilation (Voyager-style tools), LoRA training, new agent
interventions. Those follow only after win volume exists.

## Architecture

Thin driver + refactor, no new agent machinery:

1. **`homework/run_hw.py` (refactor)** — extract the existing run flow (start
   container, `docker cp` flag, `bind_container`, `agent_run`, done_check,
   transcript streaming, `learn_from_agent_run` on all outcomes, cleanup) into
   `async def run_rung(rung, model, steps, timeout, keep=False) -> dict`.
   The existing CLI keeps working unchanged by calling it.

2. **`homework/ladder.py` (new, ~150 lines)** — the experiment driver.
   - Args: `--rungs 1,2,3` (default all three), `--attempts K` (default 2),
     `--model` (default qwen2.5-coder:32b), `--steps` (default 120),
     `--timeout` (default 1800), `--no-dream`.
   - Curriculum gating: rung N+1 starts after the first solve on rung N, or
     after K failed attempts (recorded honestly in the matrix, not hidden).
   - Dream between rungs: `subprocess` call to `pipenv run enigma dream`
     (robust, no API surgery) unless `--no-dream`.
   - Output: `homework/out/ladder_<timestamp>.json` + printed matrix.

3. **Scoreboard (the point)** — per rung: attempts, solves, steps-to-solve per
   attempt, interventions fired (pivots/blocks from transcripts), lessons
   banked. Printed as a table and saved to the JSON.

## Data flow

attempt → transcript JSONL (existing) → `learn_from_agent_run` banks solved
case + lessons + grounded competence into the shared store (existing) → dream
consolidates between rungs → next attempt recalls the win material (existing
recall paths: cases, kind-scoped lessons, mid-run re-recall) → matrix JSON.

## Memory hygiene (deliberate choice)

Homework lessons stay `kind='agent'` in the shared store — gdb/payload craft
*should* transfer to ExploitGym, and solved cases are the real transfer
vehicle. Homework-specific offsets leaking into benchmark recall is mitigated
by similarity-gated recall. No new code.

## Error handling

- Ollama 5xx mid-run: existing retry-with-backoff in `agent_run`.
- Failed attempts: still bank failure outcomes via `learn_from_agent_run`
  (existing), matrix records them.
- Container leaks: driver removes named containers in `finally`, including on
  KeyboardInterrupt.
- PRM sidecar down: rerank silently disabled (existing fallback).

## Testing

- Smoke: `--rungs 1 --attempts 1 --steps 5 --timeout 120` — proves driver flow
  end-to-end in ~2 min without meaningful GPU spend.
- Real: full ladder (3 rungs × 2 attempts, ≈1-1.5h GPU) in background, watched
  via runmon (:8766) and transcripts under `homework/out/`.

## Success criteria

- Driver completes a full pass with a valid matrix JSON.
- Any rung solved by the agent (solve scripts already prove solvability; the
  open question is agent capability).
- Attempt-over-attempt comparison is computable (same model, same rung).
