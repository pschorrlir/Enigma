"""Convert a trained Enigma LoRA adapter into an Ollama model the host can serve.

Ollama serves LoRA adapters through the Modelfile `ADAPTER` directive, but it
expects the adapter in **GGUF** format — not the PEFT/safetensors that
train_lora.py writes. So the honest path has two steps:

  1. Convert the PEFT adapter -> GGUF with llama.cpp's convert_lora_to_gguf.py.
  2. Write a Modelfile (FROM <base> + ADAPTER <gguf>) and `ollama create`.

This script does step 2 unconditionally (Modelfile + printed commands) and can
do step 1 for you when you point it at a llama.cpp checkout (--llama-cpp), or
just print the exact conversion command when you don't.

  # print everything, convert if --llama-cpp is given:
  python to_ollama.py --adapter adapters/enigma-lora \
      --base meta-llama/Llama-3.1-8B-Instruct \
      --from-model llama3.1:8b \
      --version 1 \
      --llama-cpp ~/src/llama.cpp

The base the adapter was trained on and the Ollama `FROM` base MUST be the same
architecture and weights (Llama-3.1-8B-Instruct HF <-> llama3.1:8b in Ollama),
or the adapter tensors won't line up. See README.md for the full workflow.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# Default mapping from HF base repo -> the equivalent Ollama base tag. Used only
# to suggest a --from-model when you don't pass one; always verify it matches the
# weights you actually trained on.
_HF_TO_OLLAMA = {
    "meta-llama/Llama-3.1-8B-Instruct": "llama3.1:8b",
    "unsloth/llama-3.1-8b-instruct": "llama3.1:8b",
    "meta-llama/Meta-Llama-3.1-8B-Instruct": "llama3.1:8b",
}


def guess_from_model(base: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if base in _HF_TO_OLLAMA:
        return _HF_TO_OLLAMA[base]
    raise SystemExit(
        f"cannot infer the Ollama base tag for HF base '{base}'.\n"
        "Pass --from-model explicitly, e.g. --from-model llama3.1:8b "
        "(it MUST be the same weights the LoRA was trained on)."
    )


def convert_to_gguf(adapter: Path, llama_cpp: Path, outfile: Path) -> None:
    """Run llama.cpp's convert_lora_to_gguf.py on the PEFT adapter directory."""
    script = llama_cpp / "convert_lora_to_gguf.py"
    if not script.exists():
        # Older checkouts spelled it with hyphens.
        alt = llama_cpp / "convert-lora-to-gguf.py"
        if alt.exists():
            script = alt
        else:
            raise SystemExit(
                f"convert_lora_to_gguf.py not found under {llama_cpp}.\n"
                "Clone llama.cpp (https://github.com/ggml-org/llama.cpp) and point "
                "--llama-cpp at it."
            )
    cmd = [
        sys.executable, str(script),
        str(adapter),
        "--base", _read_base_from_meta(adapter),
        "--outtype", "f16",
        "--outfile", str(outfile),
    ]
    print("running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _read_base_from_meta(adapter: Path) -> str:
    """The adapter records its base model; convert_lora_to_gguf needs it to read
    the base config/tokenizer for tensor naming."""
    meta = adapter / "enigma_lora_meta.json"
    if meta.exists():
        return json.loads(meta.read_text()).get("base_model", "")
    # Fall back to PEFT's own adapter_config.json.
    cfg = adapter / "adapter_config.json"
    if cfg.exists():
        return json.loads(cfg.read_text()).get("base_model_name_or_path", "")
    raise SystemExit(f"cannot determine base model for adapter {adapter}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Package an Enigma LoRA adapter as an Ollama model.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--adapter", type=Path, required=True,
                    help="the adapter directory written by train_lora.py")
    ap.add_argument("--base", default=None,
                    help="HF base the adapter was trained on "
                         "(defaults to the value recorded in the adapter's metadata)")
    ap.add_argument("--from-model", default=None,
                    help="Ollama base tag for the Modelfile FROM line "
                         "(e.g. llama3.1:8b); inferred from --base when omitted")
    ap.add_argument("--version", type=int, default=1,
                    help="version N -> model name enigma-distilled-vN")
    ap.add_argument("--name", default=None,
                    help="override the model name (default enigma-distilled-vN)")
    ap.add_argument("--llama-cpp", type=Path, default=None,
                    help="path to a llama.cpp checkout; if given, runs the GGUF "
                         "conversion for you")
    ap.add_argument("--gguf-name", default="enigma-lora-f16.gguf",
                    help="filename for the converted GGUF adapter (inside --adapter)")
    args = ap.parse_args()

    if not args.adapter.exists():
        raise SystemExit(f"adapter directory not found: {args.adapter}")

    base = args.base or _read_base_from_meta(args.adapter)
    from_model = guess_from_model(base, args.from_model)
    name = args.name or f"enigma-distilled-v{args.version}"
    gguf_path = args.adapter / args.gguf_name

    # Step 1: GGUF conversion (run it, or print the command).
    if args.llama_cpp:
        convert_to_gguf(args.adapter, args.llama_cpp, gguf_path)
        print(f"converted adapter -> {gguf_path}", flush=True)
    else:
        print("# Step 1 - convert the PEFT adapter to GGUF (run once, needs a "
              "llama.cpp checkout):", flush=True)
        print(f"    python /path/to/llama.cpp/convert_lora_to_gguf.py {args.adapter} \\", flush=True)
        print(f"        --base {base} --outtype f16 --outfile {gguf_path}", flush=True)
        print("  (or re-run this script with --llama-cpp /path/to/llama.cpp to do "
              "it automatically)\n", flush=True)

    # Step 2: write the Modelfile. ADAPTER references the GGUF by a path relative
    # to the Modelfile so `ollama create` resolves it regardless of cwd.
    modelfile_path = args.adapter / "Modelfile"
    modelfile = (
        f"# Enigma distilled model: {name}\n"
        f"# LoRA trained on verified successes, base {base}.\n"
        f"FROM {from_model}\n"
        f"ADAPTER ./{args.gguf_name}\n"
        "\n"
        "PARAMETER temperature 0.7\n"
        "PARAMETER num_ctx 8192\n"
    )
    modelfile_path.write_text(modelfile)
    print(f"# Wrote Modelfile -> {modelfile_path}\n", flush=True)
    print("# Step 2 - register the model with Ollama:", flush=True)
    print(f"    ollama create {name} -f {modelfile_path}\n", flush=True)
    print("# Step 3 - add it to Enigma's bandit pool so it becomes a new arm:", flush=True)
    print(f'    export ENIGMA_LOCAL_MODELS="qwen3:8b,llama3.2:3b,{name}"', flush=True)
    print("    # then restart the daemon:  enigma stop && enigma start", flush=True)


if __name__ == "__main__":
    main()
