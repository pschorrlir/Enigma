# Enigma

A background self-learning task engine for Python 3.13. Local Ollama models do the
iterative work; a cloud frontier model (Anthropic) is pulled in only when progress
stalls. Every finished task distills a reusable lesson that is recalled on future tasks.

## Research grounding

- **Reflexion** (Shinn et al., 2023) — background critique of the best failure feeds
  later prompts; evaluator feedback is used immediately.
- **Evolutionary archive with novelty gating** (FunSearch / AlphaEvolve / ShinkaEvolve) —
  adaptive best-of-N waves, near-duplicate candidates rejected before evaluation,
  prompt exemplars sampled by fitness + diversity from the top-k pool.
- **DeepConf** (arXiv:2508.15260) — collapsed-confidence generations (mean token
  logprob) are dropped before paying for evaluation; confidence breaks archive ties.
- **ACE playbook memory** (arXiv:2510.04618) — distilled lessons carry per-bullet
  helpful/harmful counters, get credited by task outcome, and are pruned when net-negative.
- **Contrastive distillation** (Training-Free GRPO, arXiv:2510.08191) — lessons are
  distilled by contrasting the best and worst candidates of the same task.
- **Memento case bank** (arXiv:2508.16153) — solved tasks are stored as exemplars and
  recalled as few-shot examples for similar future tasks.
- **GEPA-style prompt evolution** (arXiv:2507.19457) — on local plateau the engine
  mutates a new prompt-style instruction; the Thompson-sampling bandit arbitrates
  evolved styles against the built-ins.
- **Rubric-based judging** (Rubrics-as-Rewards, arXiv:2507.17746) — `llm_judge`
  decomposes criteria into a weighted binary checklist (cloud-generated when available)
  instead of one noisy holistic score.
- **Calibrated cascade** (UCCI-style) — cloud escalation timing is calibrated on
  episode history per evaluator kind, falling back to a patience rule when history is
  thin; the cohesion pass runs concurrently with local sampling. One local model is
  picked per task (pooled Thompson draw) so Ollama never thrashes weights.

## Setup

```bash
pipenv install            # Python 3.13.14, httpx only
ollama pull qwen3:8b llama3.2:3b nomic-embed-text
export ANTHROPIC_API_KEY=sk-...   # optional: enables cloud cohesion passes
```

## Usage

```bash
pipenv run enigma start                       # detach daemon into background
pipenv run enigma submit task.json            # returns a task id immediately
pipenv run enigma submit --desc "summarize X" --output text
pipenv run enigma status                      # queue + recent tasks
pipenv run enigma result <task-id>            # result JSON when finished
pipenv run enigma insights                    # what the engine has learned
pipenv run enigma stop                        # repeat to force-cancel a drain
pipenv run enigma run task.json               # or: run one task synchronously
pipenv run enigma web                         # dashboard at http://127.0.0.1:8765
pipenv run enigma export-corpus sft.jsonl     # verified successes for LoRA distillation
```

## Task format (flexible input/output)

```json
{
  "description": "Write a python function solve(x) returning the square of x",
  "input": {"anything": "string, object, list — passed to the model verbatim"},
  "output": {"kind": "code"},
  "evaluator": {
    "kind": "python_tests",
    "tests": "assert solve(2)==4\nassert solve(5)==25"
  },
  "max_iterations": 8,
  "target_score": 0.95
}
```

Output kinds: `text`, `json`, `code`.
Evaluators: `python_tests` (runs candidate code + asserts in an isolated subprocess,
partial credit per assert), `json_schema` (dependency-free subset validator), `regex`,
`contains`, and `llm_judge` (local model scores against your `criteria`).

## Configuration (env vars)

| Variable | Default | |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `ENIGMA_LOCAL_MODELS` | `qwen3:8b,llama3.2:3b` | comma-separated bandit model pool |
| `ENIGMA_EMBED_MODEL` | `nomic-embed-text` | insight-recall embeddings |
| `ENIGMA_CLOUD_MODEL` | `claude-sonnet-5` | frontier model for cohesion passes |
| `ENIGMA_CLOUD_MAX_CALLS` | `2` | cloud budget per task |
| `ENIGMA_CANDIDATES_MIN` / `ENIGMA_CANDIDATES` | `2` / `4` | adaptive best-of-N bounds |
| `ENIGMA_NUM_CTX` | `8192` | Ollama context window |
| `ENIGMA_KEEP_ALIVE` | `15m` | keep models resident between calls |
| `ENIGMA_MAX_ITERATIONS` | `8` | iteration budget per task |
| `ENIGMA_PATIENCE` | `2` | stalled iterations before escalating |
| `ENIGMA_CONCURRENCY` | `2` | tasks run in parallel by the daemon |
| `ENIGMA_TARGET_SCORE` | `0.95` | stop when reached |
| `ENIGMA_HOME` | `.enigma` | SQLite db, pidfile, daemon log |

State lives in `.enigma/enigma.db` (WAL). Killing the daemon requeues in-flight tasks
on next start.

## PRM sidecar (step-level verification)

The `prm` evaluator scores each reasoning step of an output with
**Qwen2.5-Math-PRM-7B** (best open ≤8B process reward model) served by a
sidecar in its own venv — the main environment stays httpx-only. Scoring is a
single forward pass, so CPU (bf16 + AVX-512) works.

```bash
# one-time: model weights into models/, then
sidecar/setup.sh            # CPU torch; use --cuda for GPU wheels
sidecar/run.sh              # serves http://127.0.0.1:8799
```

Task usage — best for multi-step reasoning/derivations:

```json
{"description": "Prove ... step by step.",
 "evaluator": {"kind": "prm", "aggregate": "min"}}
```

`aggregate` is one of `min` (default: weakest-step, strictest), `mean`, `prod`,
`last`. Feedback names the weakest step, which feeds the reflection loop.
Override the endpoint with `ENIGMA_PRM_URL` or per-task `"url"`.

## Note on `python_tests`

Candidate code runs in a subprocess with `-I` (isolated mode) and a timeout — that
contains accidents, not malice. Don't point it at untrusted task sources.
