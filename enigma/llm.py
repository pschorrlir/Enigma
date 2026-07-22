"""Async LLM clients: Ollama for local work, Anthropic for cohesion passes."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("enigma.llm")


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self._cfg = cfg
        self._http = http
        self._base = cfg.ollama_host.rstrip("/")

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
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if format_json:
            payload["format"] = "json"
        try:
            r = await self._http.post(
                f"{self._base}/api/generate",
                json=payload,
                timeout=self._cfg.request_timeout_s,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"ollama generate failed ({model}): {e}") from e
        return r.json().get("response", "")

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


class AnthropicClient:
    """Minimal Messages API client; only used for escalation."""

    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self._cfg = cfg
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.anthropic_api_key)

    async def generate(self, prompt: str, *, system: str | None = None, max_tokens: int = 4096) -> str:
        if not self.enabled:
            raise LLMError("ANTHROPIC_API_KEY not set; cloud escalation unavailable")
        body: dict[str, Any] = {
            "model": self._cfg.cloud_model,
            "max_tokens": max_tokens,
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


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from model output."""
    text = text.strip()
    # Strip a thinking block if the model emits one.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    for candidate in (text, *_JSON_RE.findall(text)[:3]):
        try:
            obj = json.loads(candidate)
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
