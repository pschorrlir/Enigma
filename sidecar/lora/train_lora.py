"""LoRA fine-tuning sidecar: turn Enigma's verified successes into a new local model.

This is the "weight-training flywheel". Enigma's `export-corpus` command emits
execution-verified successes as JSONL (one object per line):

    {"prompt": "<task description + optional INPUT>",
     "completion": "<the winning output>",
     "score": <float>}

Only verifiable evaluator kinds (python_tests, json_schema, regex, contains) are
exported, so every label is trustworthy. This script SFT-trains a LoRA adapter on
those pairs. The adapter is then converted to an Ollama model (see to_ollama.py)
and added to `ENIGMA_LOCAL_MODELS`, where Enigma's Thompson-sampling bandit picks
it up as a fresh arm and A/Bs it against the incumbents.

Runs on the HOST (needs a GPU + base weights), in its own venv, never in CI.

  python train_lora.py --corpus corpus.jsonl \
      --base meta-llama/Llama-3.1-8B-Instruct \
      --out adapters/enigma-lora

See README.md for the end-to-end workflow. Requires: transformers, peft, trl,
datasets, accelerate (see requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Instruction / response formatting
# ---------------------------------------------------------------------------
#
# We format every (prompt, completion) pair with the base model's own chat
# template (tokenizer.apply_chat_template). That keeps the LoRA aligned with the
# special tokens / role markers the base instruct model was trained on — the same
# formatting Ollama will apply at serve time. We only train on the assistant's
# response tokens (completion), not on the prompt, via TRL's completion-only
# collator with the model's response template.
#
# A short, fixed system message frames the model as Enigma's solver so the
# adapter learns Enigma's task style, not a generic assistant persona.

SYSTEM_PROMPT = (
    "You are Enigma's local solver. Complete the task exactly and return only "
    "the requested output with no preamble, explanation, or code fences unless "
    "the task asks for them."
)


def build_messages(prompt: str) -> list[dict[str, str]]:
    """The chat turns for a single training example (system + user)."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def load_corpus(path: Path, min_score: float) -> list[dict[str, str]]:
    """Read the JSONL corpus, filter by score, return [{prompt, completion}, ...].

    Rows missing prompt/completion, or whose score is below --min-score, are
    skipped. A null/absent score is treated as 0.0 so it only survives when
    --min-score is 0.
    """
    if not path.exists():
        raise SystemExit(f"corpus not found: {path}\n"
                         f"Generate it first with:  enigma export-corpus {path}")

    examples: list[dict[str, str]] = []
    skipped_score = 0
    skipped_empty = 0
    bad_lines = 0
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            prompt = (row.get("prompt") or "").strip()
            completion = (row.get("completion") or "")
            if not prompt or not completion.strip():
                skipped_empty += 1
                continue
            score = row.get("score")
            score = float(score) if isinstance(score, (int, float)) else 0.0
            if score < min_score:
                skipped_score += 1
                continue
            examples.append({"prompt": prompt, "completion": completion})

    print(f"corpus: {len(examples)} usable examples "
          f"(skipped {skipped_score} below --min-score={min_score}, "
          f"{skipped_empty} empty, {bad_lines} malformed lines)", flush=True)

    if not examples:
        raise SystemExit(
            "no usable training examples after filtering.\n"
            "Either the corpus is empty (run more verified tasks, then "
            "`enigma export-corpus`) or --min-score is too strict "
            f"(currently {min_score}; try --min-score 0.8)."
        )
    return examples


def main() -> None:
    ap = argparse.ArgumentParser(
        description="SFT a LoRA adapter on Enigma's verified-success corpus.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--corpus", type=Path, required=True,
                    help="JSONL from `enigma export-corpus` (prompt/completion/score)")
    ap.add_argument("--base", default="meta-llama/Llama-3.1-8B-Instruct",
                    help="base HF instruct model to adapt "
                         "(e.g. unsloth/llama-3.1-8b-instruct for gated-free weights)")
    ap.add_argument("--out", type=Path, default=Path("./adapters/enigma-lora"),
                    help="directory to write the trained LoRA adapter")
    ap.add_argument("--min-score", type=float, default=1.0,
                    help="only train on examples with score >= this (1.0 = fully verified)")

    # LoRA hyperparameters
    ap.add_argument("--lora-r", type=int, default=16, help="LoRA rank")
    ap.add_argument("--lora-alpha", type=int, default=32, help="LoRA alpha (scaling)")
    ap.add_argument("--lora-dropout", type=float, default=0.05, help="LoRA dropout")

    # Training hyperparameters (defaults sized for a single 24-48GB GPU on an 8B base)
    ap.add_argument("--epochs", type=float, default=3.0, help="number of training epochs")
    ap.add_argument("--batch-size", type=int, default=2, help="per-device train batch size")
    ap.add_argument("--grad-accum", type=int, default=8,
                    help="gradient accumulation steps (effective batch = batch-size * grad-accum)")
    ap.add_argument("--lr", type=float, default=2e-4, help="learning rate")
    ap.add_argument("--max-seq-len", type=int, default=2048,
                    help="max tokenized sequence length (prompt+completion; longer is truncated)")
    ap.add_argument("--warmup-ratio", type=float, default=0.03, help="LR warmup ratio")
    ap.add_argument("--seed", type=int, default=42, help="random seed")
    ap.add_argument("--load-4bit", action="store_true",
                    help="load the base in 4-bit (QLoRA) to fit smaller GPUs (needs bitsandbytes)")
    args = ap.parse_args()

    # Heavy imports are deferred so --help and the corpus guard work without a
    # GPU stack installed.
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    examples = load_corpus(args.corpus, args.min_score)

    print(f"loading tokenizer: {args.base}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if tokenizer.pad_token is None:
        # Causal LMs often ship without a pad token; reuse EOS for padding.
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    if tokenizer.chat_template is None:
        raise SystemExit(
            f"{args.base} has no chat template; pick an -Instruct/-chat base model "
            "so the LoRA aligns with the same formatting Ollama serves."
        )

    # Render each example to a single training string with the model's own chat
    # template. add_generation_prompt=False because the completion follows the
    # assistant header we emit ourselves below via full-conversation rendering.
    def to_text(ex: dict[str, str]) -> dict[str, str]:
        messages = build_messages(ex["prompt"]) + [
            {"role": "assistant", "content": ex["completion"]}
        ]
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
        return {"text": text}

    dataset = Dataset.from_list(examples).map(to_text, remove_columns=["prompt", "completion"])

    # Completion-only loss: mask everything up to and including the assistant
    # response header so gradients flow only through the response tokens. We
    # derive the response template from the chat template so it matches the base.
    response_template = _assistant_response_template(tokenizer)

    print(f"loading base model: {args.base} "
          f"({'4-bit QLoRA' if args.load_4bit else 'bf16'})", flush=True)

    quant_config = None
    if args.load_4bit:
        from transformers import BitsAndBytesConfig
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.base,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        quantization_config=quant_config,
    )
    model.config.use_cache = False  # incompatible with gradient checkpointing

    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        # Attention + MLP projections: the standard "all linear" LoRA target set
        # for Llama-family models. Adapting the MLP (gate/up/down) as well as
        # attention gives the adapter enough capacity to shift task behavior.
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )

    args.out.mkdir(parents=True, exist_ok=True)

    sft_config = SFTConfig(
        output_dir=str(args.out),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        warmup_ratio=args.warmup_ratio,
        lr_scheduler_type="cosine",
        optim="paged_adamw_8bit" if args.load_4bit else "adamw_torch",
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_seq_length=args.max_seq_len,
        packing=False,  # off: completion-only masking needs example boundaries intact
        logging_steps=5,
        save_strategy="epoch",
        report_to=[],
        seed=args.seed,
        dataset_text_field="text",
    )

    # TRL's collator masks the prompt so loss is computed on the response only.
    from trl import DataCollatorForCompletionOnlyLM
    collator = DataCollatorForCompletionOnlyLM(
        response_template=response_template, tokenizer=tokenizer
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=dataset,
        peft_config=lora_config,
        processing_class=tokenizer,
        data_collator=collator,
    )

    print(f"training: {len(dataset)} examples, {args.epochs} epochs, "
          f"effective batch = {args.batch_size * args.grad_accum}", flush=True)
    trainer.train()

    # Save the adapter (LoRA weights + config) and the tokenizer alongside it, so
    # the conversion step has everything it needs.
    trainer.save_model(str(args.out))
    tokenizer.save_pretrained(str(args.out))

    # Record provenance so a converted Ollama model is traceable to its base.
    (args.out / "enigma_lora_meta.json").write_text(json.dumps({
        "base_model": args.base,
        "corpus": str(args.corpus),
        "min_score": args.min_score,
        "examples": len(dataset),
        "epochs": args.epochs,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout},
    }, indent=2))

    print(f"\ndone. adapter written to {args.out}", flush=True)
    print("next: convert + register with Ollama:", flush=True)
    print(f"    python to_ollama.py --adapter {args.out} --base {args.base}", flush=True)


def _assistant_response_template(tokenizer) -> str:
    """Return the token string that immediately precedes the assistant's reply.

    We ask the chat template to render an empty conversation with a generation
    prompt and, separately, without one; the difference is exactly the assistant
    header (e.g. Llama-3's "<|start_header_id|>assistant<|end_header_id|>\n\n").
    That string is what the completion-only collator uses to find where the
    response begins. Falls back to a Llama-3 literal if the diff is empty.
    """
    probe = [{"role": "user", "content": "x"}]
    with_gen = tokenizer.apply_chat_template(probe, tokenize=False, add_generation_prompt=True)
    without_gen = tokenizer.apply_chat_template(probe, tokenize=False, add_generation_prompt=False)
    if with_gen.startswith(without_gen) and len(with_gen) > len(without_gen):
        return with_gen[len(without_gen):]
    return "<|start_header_id|>assistant<|end_header_id|>\n\n"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
