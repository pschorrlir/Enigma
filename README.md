# Enigma

A background self-learning task engine for Python 3.13. Local Ollama models do the
iterative work; a cloud frontier model (Anthropic) is pulled in only when progress
stalls. Every finished task distills a reusable lesson that is recalled on future tasks.

## Research grounding

- **Reflexion** (Shinn et al., 2023) — failed attempts are critiqued verbally and the
  critique is injected into the next generation prompt; finished tasks distill
  transferable insights stored with embeddings (Voyager-style skill memory).
- **Evolutionary archive** (FunSearch / AlphaEvolve, 2024–25) — each iteration samples
  best-of-N candidates in parallel and keeps a scored top-k pool that seeds later prompts.
- **Thompson-sampling bandit** — per evaluator kind, the engine learns which
  (model × temperature × prompt-style) arm produces the best scores.
- **Cascade escalation** (FrugalGPT-style) — cheap local models first; one cloud
  "cohesion" pass synthesizing the candidate pool only after `patience` stalled iterations.

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
pipenv run enigma stop
pipenv run enigma run task.json               # or: run one task synchronously
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
| `ENIGMA_CANDIDATES` | `3` | parallel samples per iteration |
| `ENIGMA_MAX_ITERATIONS` | `8` | iteration budget per task |
| `ENIGMA_PATIENCE` | `2` | stalled iterations before escalating |
| `ENIGMA_CONCURRENCY` | `2` | tasks run in parallel by the daemon |
| `ENIGMA_TARGET_SCORE` | `0.95` | stop when reached |
| `ENIGMA_HOME` | `.enigma` | SQLite db, pidfile, daemon log |

State lives in `.enigma/enigma.db` (WAL). Killing the daemon requeues in-flight tasks
on next start.

## Note on `python_tests`

Candidate code runs in a subprocess with `-I` (isolated mode) and a timeout — that
contains accidents, not malice. Don't point it at untrusted task sources.
