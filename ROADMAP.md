# Next iteration roadmap

Synthesized from three parallel reviews (correctness, architecture/performance,
2025–26 research scout) of commit `99c49f8`.

## P0 — Correctness fixes (before anything else)

1. **Subprocess containment in `python_tests`** (`evaluators.py`)
   - `CancelledError` during `proc.communicate()` skips `proc.kill()` → orphaned
     `while True` children survive the daemon. Wrap in `try/finally`, kill + `await proc.wait()`.
   - Start the child in its own process group (`start_new_session=True`) and kill the
     group — candidate code that spawns subprocesses currently leaks grandchildren.
   - Redirect stdin to DEVNULL; guard the `@@ENIGMA@@` report parse (stray post-marker
     output crashes the evaluator); accept multi-line test statements (current
     line-splitting corrupts any `for`/multi-line assert — split on top-level
     statements via `ast.parse` instead).
2. **Transient LLM errors must not destroy a task** (`engine.py`)
   - One Anthropic 529 or Ollama restart mid-task currently marks the whole task
     `failed` and discards a possibly 0.85-scoring archive. Catch per-call, degrade:
     skip the failed candidate/escalation, keep iterating, fall back to archive best.
   - `gather` over evaluations needs `return_exceptions=True` (one bad evaluation
     cancels its siblings mid-subprocess).
3. **Task timeout must preserve the archive** (`engine.py`) — keep archive state
   visible outside `_iterate` so `wait_for` timeout returns `exhausted` + best, not
   `best=None, iterations=0`.
4. **Daemon single-instance safety** (`daemon.py`, `cli.py`)
   - `requeue_stale_running()` at startup steals a live daemon's tasks → duplicate
     execution. Guard with pidfile liveness / heartbeat column. Foreground
     `enigma daemon` must also take the pidfile (O_EXCL) — today it's invisible to
     `stop`/`status`.
   - `stop` unlinks the pidfile even when the daemon is still draining → second
     daemon launches. Only unlink after confirmed exit.
5. **Claim loop bursts the whole queue** (`daemon.py`) — no await between claims, so
   500 queued tasks are all marked `running` instantly. `await sem.acquire()` before
   `claim_next()`; stop reading `sem._value`.
6. **Input validation at submit time** (`task.py`, `cli.py`, `memory.py`) — reject
   non-dict `evaluator`; handle duplicate task `id` (IntegrityError); validate
   non-empty `ENIGMA_LOCAL_MODELS`; catch `re.error` from bad patterns (and run
   task-supplied regexes off the event loop or with a guard — catastrophic
   backtracking currently stalls the entire daemon).
7. **Smaller**: `_clean` splits on `OUTPUT:` for all styles (truncates legitimate
   content); greedy `_JSON_RE` (`\{.*\}` DOTALL) fails on two-object outputs → judge
   silently scores 0.5 and pollutes the bandit; mixed cosine/Jaccard scales in
   `recall` bias against non-embedded insights.

## P1 — Speed (goal: halve per-task wall clock)

1. **Stop Ollama model thrash** — bandit picks a model per *task*, not per iteration;
   judge/reflect/learn use the same resident model as generation; send `keep_alive`.
   On a single-GPU host this is the largest hidden cost.
2. **Set `options.num_ctx`** (config, default 8192) — prompts routinely exceed
   Ollama's default context; silent head-truncation burns iterations.
3. **Overlap the loop** — run `_reflect` as a background task (evaluator feedback is
   already available for the next prompt); skip reflection on the final iteration and
   for evaluators with precise feedback (`python_tests`).
4. **Move `_learn` out of the concurrency slot** — finish/store the result first,
   distill the lesson fire-and-forget.
5. **Per-candidate gen→eval chains with early exit** — don't barrier all N
   generations before evaluating; cancel remaining work once a candidate hits target.
6. **Concurrent cohesion** — run the cloud pass alongside a local batch, not instead
   of it; raise cloud `max_tokens`.
7. **Event-driven queue** — wake on worker completion + submit nudge instead of 1s
   polls; `busy_timeout` down from 30s (a CLI write can freeze the loop).
8. **Bandit hygiene** — reward the arm actually played (temperature perturbation
   currently mis-attributes credit); cache arm stats in memory, write-through.
9. **Memory scalability** — cache decoded embeddings (recall currently JSON-decodes
   500 embeddings on the event loop per task); dedupe insights on insert
   (cosine > 0.95); rank recall by usefulness + recency; prune episodes.

## P2 — Bleeding-edge capabilities (ranked by evidence × fit)

Quick wins (days, share existing infrastructure):

1. **DeepConf** (arXiv:2508.15260, NeurIPS 2025) — request logprobs from Ollama,
   attach mean-confidence to candidates, drop low-confidence samples *before* paying
   for evaluation; up to ~85% token reduction reported on Ollama-class models.
2. **Adaptive best-of-N** (ESC/adaptive-consistency lineage) — sample in pairs, stop
   early on agreement/target, raise N on dispersion. Replaces constant
   `candidates_per_iteration=3`.
3. **Training-free GRPO–style contrastive distillation** (arXiv:2510.08191) — the
   engine already produces N scored candidates per iteration; distill lessons by
   contrasting best vs worst in-group instead of the current "one transferable
   sentence" prompt. Sharper insights at zero extra sampling cost.
4. **ShinkaEvolve archive upgrades** (arXiv:2509.19349) — embed candidates, reject
   near-duplicates pre-evaluation, and sample prompt exemplars from the archive by
   fitness + novelty instead of always `archive[0]` (current pure hill-climb).

Core upgrades (share the reflection/memory substrate, compound with each other):

5. **ACE playbook memory** (arXiv:2510.04618) — replace one-sentence insights with an
   itemized playbook: per-bullet helpful/harmful counters, delta updates
   (add/edit/deprecate), prune negatives. Fixes the append-forever decay mode.
6. **GEPA prompt evolution** (arXiv:2507.19457, ICLR 2026 oral) — evolve the
   hard-coded `_STYLE_HINTS`/`_SYSTEM` via reflective mutation with Pareto-frontier
   selection; evolved styles become new bandit arms so Thompson sampling guards
   against regressions. Highest-leverage single upgrade.
7. **Rubric-based judging** (Rubrics-as-Rewards, arXiv:2507.17746) — generate a
   weighted checklist per task once (good use of one cloud call), have the local
   judge score items binary. De-noises the reward every other mechanism depends on.
   Promote to quick-wins if `llm_judge` tasks dominate.
8. **Calibrated cascade deferral** (UCCI arXiv:2605.18796, speculative cascades) —
   learn *when* cloud escalation pays from episode history (task embedding, iter-1
   score, confidence → did cloud help) instead of the fixed patience counter; add a
   cheap cloud verify-only mode.

Longer arcs:

9. **Memento case bank** (arXiv:2508.16153) — store solved (task, output) exemplars;
   recall as few-shot for similar tasks. High payoff when task families recur.
10. **Process reward verification** (ThinkPRM arXiv:2504.16828, or
    Qwen2.5-Math-PRM-7B via Ollama) — step-level verification feeding `_reflect` for
    plan-style arms. Payoff concentrated on multi-step reasoning tasks.
11. **LoRA self-distillation** (Absolute Zero arXiv:2505.03335, SDFT) — rejection-
    sampling SFT on the engine's own verified successes (esp. `python_tests`), train
    LoRA in a sidecar, register the adapter as a new Ollama model = new bandit arm.
    The only path that permanently improves the local models; do last — items 5/3/9
    build its data flywheel.

Deprioritized with reasons: Darwin Gödel Machine (self-modifying source — needs a
benchmark harness first), TextGrad (subsumed by GEPA), full A-MEM (covered by
ACE + Memento), SEAL (training cluster out of scope).

## API-shape prep (cheap now, expensive later)

- LLM clients: accept `messages: list`, return a `Response` (text, blocks,
  stop_reason) — the seam for tool use, multi-turn, streaming. The Anthropic client
  currently silently drops non-text blocks.
- Registries instead of hard-coded dispatch for output kinds and evaluators.
- Queue: `priority` column + `enigma wait <id>`; groundwork for task dependencies.
