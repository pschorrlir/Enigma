# Enigma LoRA sidecar — the weight-training flywheel

Enigma solves tasks with local Ollama models and **execution-verifies** the
winners (python_tests, json_schema, regex, contains). Those verified successes
are free, trustworthy training data. This sidecar closes the loop: it distills
them into a LoRA adapter, registers the result as a new Ollama model, and hands
that model to Enigma's Thompson-sampling bandit as a **new arm** — which then
measures it against the incumbents on real tasks.

```
   verified successes                LoRA SFT              new Ollama model
   (execution-checked)  ──────────►  (this sidecar) ─────► enigma-distilled-vN
          ▲                                                        │
          │                                                        ▼
   eval harness / bandit  ◄───────────────────────────  added to ENIGMA_LOCAL_MODELS
   (measures the new arm, A/Bs it safely against qwen3/llama)
```

**Why this is the flywheel:** every task the engine verifies makes the next
model a little better, and the bandit guarantees a regression can't win — a bad
adapter simply loses draws and gets sampled less. Verified successes become
weights; weights become a measured bandit arm; the harness decides if it stays.

This runs on the **host** with a GPU. It is not part of CI and not part of the
main httpx-only Enigma environment. Like the PRM sidecar, it lives in its own
venv.

## Requirements

- A single GPU with **24–48 GB VRAM** for an 8B base.
  - ~40–48 GB comfortably trains an 8B in bf16 with the defaults.
  - On 24 GB, add `--load-4bit` (QLoRA) and/or lower `--batch-size`/`--max-seq-len`.
- The base model weights. `meta-llama/Llama-3.1-8B-Instruct` is gated on HF
  (accept the license + `huggingface-cli login`); `unsloth/llama-3.1-8b-instruct`
  is an ungated drop-in.
- For the Ollama conversion step: a [llama.cpp](https://github.com/ggml-org/llama.cpp)
  checkout (provides `convert_lora_to_gguf.py`) and `ollama`.

## One-time setup

```bash
cd sidecar/lora
python3 -m venv .venv && . .venv/bin/activate
pip install --upgrade pip
# install a CUDA-matched torch first, e.g. for CUDA 12.4:
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

## End-to-end workflow

### 1. Export the corpus (from the Enigma project root)

```bash
enigma export-corpus corpus.jsonl
```

Writes one JSON object per line: `{"prompt", "completion", "score"}`. Only
verifiable evaluator kinds are exported, so the labels are trustworthy.

### 2. Train the LoRA adapter

```bash
cd sidecar/lora
python train_lora.py \
    --corpus ../../corpus.jsonl \
    --base meta-llama/Llama-3.1-8B-Instruct \
    --out adapters/enigma-lora \
    --min-score 1.0
```

- `--min-score 1.0` (default) trains only on fully-verified examples. Lower it
  (e.g. `0.8`) if the corpus is small and you want partial-credit examples too.
- Each pair is formatted with the base model's own **chat template** (a fixed
  Enigma system prompt + the task as the user turn + the winning output as the
  assistant turn). Loss is computed **only on the response** tokens via TRL's
  completion-only collator, so the model learns to *produce* winning outputs, not
  to echo prompts.
- LoRA defaults: `r=16, alpha=32, dropout=0.05`, targeting attention + MLP
  projections (`q,k,v,o,gate,up,down`). Training uses bf16, gradient
  checkpointing, 3 epochs, effective batch 16 (`--batch-size 2 × --grad-accum 8`).
- Output: the adapter + tokenizer + `enigma_lora_meta.json` (provenance) in
  `adapters/enigma-lora/`.

Tune with `--epochs`, `--batch-size`, `--grad-accum`, `--lr`, `--max-seq-len`,
`--lora-r`, `--lora-alpha`, `--load-4bit`. Run `python train_lora.py --help` for
the full list.

### 3. Convert + register with Ollama

Ollama serves adapters via the Modelfile `ADAPTER` directive, but the adapter
must be **GGUF** — PEFT/safetensors won't load directly. `to_ollama.py` writes
the Modelfile and prints the exact commands; pass `--llama-cpp` to also run the
conversion.

```bash
python to_ollama.py \
    --adapter adapters/enigma-lora \
    --base meta-llama/Llama-3.1-8B-Instruct \
    --from-model llama3.1:8b \
    --version 1 \
    --llama-cpp ~/src/llama.cpp
```

What it does:

1. **GGUF conversion** (llama.cpp) — equivalent to running by hand:
   ```bash
   python ~/src/llama.cpp/convert_lora_to_gguf.py adapters/enigma-lora \
       --base meta-llama/Llama-3.1-8B-Instruct \
       --outtype f16 --outfile adapters/enigma-lora/enigma-lora-f16.gguf
   ```
   Omit `--llama-cpp` and the script just prints this command instead of running it.

2. **Writes a Modelfile** (`adapters/enigma-lora/Modelfile`):
   ```
   FROM llama3.1:8b
   ADAPTER ./enigma-lora-f16.gguf
   PARAMETER temperature 0.7
   PARAMETER num_ctx 8192
   ```
   > **The base must match.** `FROM llama3.1:8b` must be the *same weights and
   > architecture* the adapter was trained on (`Llama-3.1-8B-Instruct` HF ↔
   > `llama3.1:8b` Ollama). A mismatched base makes the adapter tensors line up
   > against the wrong layers and produces garbage. `pull` the base first if
   > needed: `ollama pull llama3.1:8b`.

3. **Prints the create command**:
   ```bash
   ollama create enigma-distilled-v1 -f adapters/enigma-lora/Modelfile
   ```
   Run it. Verify with `ollama run enigma-distilled-v1 "..."`.

### 4. Add the new model to the bandit pool

Append the new model name to `ENIGMA_LOCAL_MODELS` so Enigma's Thompson-sampling
bandit treats it as a new arm and safely A/Bs it against the incumbents:

```bash
export ENIGMA_LOCAL_MODELS="qwen3:8b,llama3.2:3b,enigma-distilled-v1"
enigma stop && enigma start   # restart the daemon to pick up the new pool
```

The bandit starts sampling the new arm, the eval harness scores its outputs on
real tasks, and its win-rate decides how often it gets drawn. Ship `-v2`, `-v3`,
… as the corpus grows; each is just a new arm the harness has to be convinced by.

## Files

| File | Purpose |
|---|---|
| `train_lora.py` | SFT a LoRA adapter on the verified-success corpus (peft + trl). |
| `to_ollama.py`  | Convert the adapter to GGUF + write the Modelfile + print `ollama create`. |
| `requirements.txt` | Training deps (torch, transformers, peft, trl, datasets, accelerate; bitsandbytes optional). |

## Notes & honesty about the conversion path

- `convert_lora_to_gguf.py` reads the **base** model's config/tokenizer to name
  tensors correctly, which is why `--base` is required for conversion. The
  adapter records its base in `enigma_lora_meta.json`, so `to_ollama.py` can
  recover it automatically.
- Ollama's GGUF LoRA support expects standard Llama-family tensor names. The
  attention+MLP target set used here is exactly that, so conversion is
  well-trodden. If you switch to an exotic base, verify llama.cpp supports its
  LoRA conversion before assuming this works.
- No step here runs in CI or needs network access at serve time — training and
  conversion are host-side, one-off operations.
