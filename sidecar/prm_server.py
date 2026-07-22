"""PRM sidecar: serves Qwen2.5-Math-PRM-7B step-level scoring over HTTP.

Runs in its own venv (torch + transformers) so the main Enigma environment
stays httpx-only. Scoring is a single forward pass — no generation — so CPU
inference is fine (bf16 on AVX-512).

  POST /score  {"query": "...", "steps": ["step 1", "step 2", ...]}
    -> {"step_scores": [0.98, 0.42, ...], "min": .., "mean": .., "prod": .., "last": ..}
  GET  /health -> {"ok": true, "model": "...", "device": "cpu|cuda"}

Usage: python prm_server.py [--model PATH] [--port 8799]
"""

from __future__ import annotations

import argparse
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

SYSTEM = "Please reason step by step, and put your final answer within \\boxed{}."
MAX_TOKENS = 3584
MAX_STEPS = 32

_LOCK = threading.Lock()  # one inference at a time


class PRM:
    def __init__(self, model_path: str):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"loading {model_path} on {self.device} (bf16)...", flush=True)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map=self.device,
            trust_remote_code=True,
        ).eval()
        self.step_sep_id = self.tokenizer.encode("<extra_0>")[0]
        print("model ready", flush=True)

    @torch.no_grad()
    def score(self, query: str, steps: list[str]) -> list[float]:
        steps = [s.strip()[:2000] for s in steps if s.strip()][:MAX_STEPS]
        if not steps:
            return []
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": query[:6000]},
            {"role": "assistant", "content": "<extra_0>".join(steps) + "<extra_0>"},
        ]
        conversation = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
        input_ids = self.tokenizer.encode(conversation, return_tensors="pt")
        if input_ids.shape[1] > MAX_TOKENS:
            return []  # too long to score faithfully; caller falls back
        input_ids = input_ids.to(self.model.device)
        outputs = self.model(input_ids=input_ids)
        token_masks = input_ids == self.step_sep_id
        # Official extraction: softmax over the 2-class head at each <extra_0>,
        # keep P(step is correct).
        probabilities = F.softmax(outputs[0], dim=-1) * token_masks.unsqueeze(-1)
        sample = probabilities[0]
        positive = sample[sample != 0].view(-1, 2)[:, 1]
        return [float(x) for x in positive.cpu()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(Path(__file__).parent.parent / "models" / "Qwen2.5-Math-PRM-7B"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    prm = PRM(args.model)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json({"ok": True, "model": args.model, "device": prm.device})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/score":
                self._json({"error": "not found"}, 404)
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                query = str(data.get("query", ""))
                steps = data.get("steps", [])
                if not query or not isinstance(steps, list) or not steps:
                    self._json({"error": "need query and non-empty steps list"}, 400)
                    return
                with _LOCK:
                    scores = prm.score(query, [str(s) for s in steps])
                if not scores:
                    self._json({"error": "input too long or no scoreable steps"}, 422)
                    return
                self._json({
                    "step_scores": scores,
                    "min": min(scores),
                    "mean": sum(scores) / len(scores),
                    "prod": math.prod(scores),
                    "last": scores[-1],
                })
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"PRM sidecar: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
