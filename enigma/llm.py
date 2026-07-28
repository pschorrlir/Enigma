"""Async LLM clients: Ollama for local work, Anthropic for cohesion/rubric passes."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("enigma.llm")


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class GenOut:
    text: str
    # DeepConf-style signal: mean token logprob when the server reports logprobs,
    # else None. Higher (closer to 0) = more confident.
    confidence: float | None = None
    # Ollama's stop cause: "stop" (natural end), "length" (hit num_predict), etc.
    # "length" means the output was CUT OFF mid-stream — callers writing files
    # from this text must know the content is truncated.
    done_reason: str | None = None


class OllamaClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self._cfg = cfg
        self._http = http
        self._base = cfg.ollama_host.rstrip("/")

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def available(self) -> bool:
        try:
            r = await self._http.get(f"{self._base}/api/tags", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        r = await self._http.get(f"{self._base}/api/tags", timeout=10.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        format_json: bool = False,
        want_confidence: bool = False,
        num_predict: int | None = None,
        stop: list[str] | None = None,
    ) -> GenOut:
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_ctx": self._cfg.num_ctx,
            "num_predict": self._cfg.num_predict if num_predict is None else num_predict,
        }
        if stop:
            # Hard generation stops: keep the model from role-playing the
            # environment (fabricating tool output) past its actual action.
            options["stop"] = list(stop)
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self._cfg.keep_alive,
            "options": options,
        }
        if system:
            payload["system"] = system
        if format_json:
            payload["format"] = "json"
        if want_confidence:
            # Honored by Ollama >= 0.12; older servers ignore unknown fields.
            payload["logprobs"] = True
        try:
            r = await self._http.post(
                f"{self._base}/api/generate",
                json=payload,
                timeout=self._cfg.request_timeout_s,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"ollama generate failed ({model}): {e}") from e
        data = r.json()
        return GenOut(data.get("response", ""), _mean_logprob(data),
                      data.get("done_reason"))

    async def embed(self, text: str) -> list[float] | None:
        """Return an embedding, or None if the embed model is unavailable."""
        try:
            r = await self._http.post(
                f"{self._base}/api/embed",
                json={"model": self._cfg.embed_model, "input": text[:8000]},
                timeout=30.0,
            )
            r.raise_for_status()
            vecs = r.json().get("embeddings") or []
            return vecs[0] if vecs else None
        except httpx.HTTPError:
            return None


def _mean_logprob(data: dict[str, Any]) -> float | None:
    lp = data.get("logprobs")
    if not isinstance(lp, list) or not lp:
        return None
    vals = [t["logprob"] for t in lp if isinstance(t, dict) and isinstance(t.get("logprob"), (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


class AnthropicClient:
    """Minimal Messages API client; used for cohesion passes and rubric generation."""

    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self._cfg = cfg
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.anthropic_api_key)

    async def generate(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
        if not self.enabled:
            raise LLMError("ANTHROPIC_API_KEY not set; cloud escalation unavailable")
        body: dict[str, Any] = {
            "model": self._cfg.cloud_model,
            "max_tokens": max_tokens or self._cfg.cloud_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        try:
            r = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers={
                    "x-api-key": self._cfg.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=self._cfg.request_timeout_s,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"anthropic generate failed: {e}") from e
        parts = r.json().get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def is_degenerate(text: str) -> bool:
    """Detect repetition-loop / low-entropy generations (e.g. 'based-based-based…',
    a token or phrase spammed thousands of times) that small models fall into.
    Uses char-level shingles so it catches runs whether or not they're
    whitespace-separated. Short texts are exempt — repetition is only
    pathological at length."""
    t = text.strip()
    if len(t) < 200:
        return False
    # Overlapping 16-char windows. Two repetition signatures, either is fatal:
    #  - low global variety (the whole text is a loop), or
    #  - one window dominating (a long repeated run diluted by other text, e.g.
    #    a varied intro followed by 'based-based-based…' for a thousand chars).
    k, step = 16, 4
    shingles = [t[i:i + k] for i in range(0, len(t) - k, step)]
    total = len(shingles)
    if not total:
        return False
    counts: dict[str, int] = {}
    top = 0
    for sh in shingles:
        c = counts[sh] = counts.get(sh, 0) + 1
        if c > top:
            top = c
    if len(counts) / total < 0.15:
        return True
    return top / total > 0.08


def is_nonanswer(text: str) -> bool:
    """True if the text is empty or a placeholder echo (e.g. '<answer>', 'your
    answer here', 'TODO', 'N/A') rather than a real answer — the model produced
    nothing of substance. Such output must never be scored as a success."""
    t = text.strip()
    if not t:
        return True
    if not re.search(r"[A-Za-z0-9]", t):  # only punctuation / symbols, no content
        return True
    if re.fullmatch(r"[<\[{(][^>\]})]{0,40}[>\]})]\.?", t):  # <answer>, <your code here>, [...]
        return True
    if re.fullmatch(
        r"[<\[{(]?\s*(?:your|the|final|insert|put)?\s*"
        r"(?:answer|output|result|response|solution|code|text|value|todo|tbd|n/?a|none|placeholder|\.\.\.)"
        r"(?:\s+(?:here|goes here))?\s*[>\]})]?[.:]?",
        t, re.IGNORECASE,
    ):
        return True
    return False


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction: whole text, then each balanced {...} span."""
    text = re.sub(r"<think>.*?</think>", "", text.strip(), flags=re.DOTALL).strip()
    candidates = [text]
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start : i + 1])
    for candidate in candidates[:6]:
        try:
            # strict=False tolerates literal newlines/control chars inside
            # strings — common in model-produced JSON (esp. long feedback).
            obj = json.loads(candidate, strict=False)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def extract_code(text: str) -> str:
    """Pull the largest fenced code block, or return the text as-is."""
    blocks = re.findall(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()
