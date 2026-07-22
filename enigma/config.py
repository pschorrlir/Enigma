"""Configuration, all overridable via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(slots=True)
class Config:
    # Storage
    home: Path = field(default_factory=lambda: Path(_env("ENIGMA_HOME", ".enigma")))

    # Local models (Ollama)
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://localhost:11434"))
    local_models: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip() for m in _env("ENIGMA_LOCAL_MODELS", "qwen3:8b,llama3.2:3b").split(",") if m.strip()
        )
    )
    embed_model: str = field(default_factory=lambda: _env("ENIGMA_EMBED_MODEL", "nomic-embed-text"))
    num_ctx: int = field(default_factory=lambda: _env_int("ENIGMA_NUM_CTX", 8192))
    keep_alive: str = field(default_factory=lambda: _env("ENIGMA_KEEP_ALIVE", "15m"))

    # Cloud frontier model (Anthropic), used only for cohesion/rubric passes
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    cloud_model: str = field(default_factory=lambda: _env("ENIGMA_CLOUD_MODEL", "claude-sonnet-5"))
    cloud_max_calls_per_task: int = field(default_factory=lambda: _env_int("ENIGMA_CLOUD_MAX_CALLS", 2))
    cloud_max_tokens: int = field(default_factory=lambda: _env_int("ENIGMA_CLOUD_MAX_TOKENS", 8192))

    # Iteration loop
    max_iterations: int = field(default_factory=lambda: _env_int("ENIGMA_MAX_ITERATIONS", 8))
    # Adaptive best-of-N: start with candidates_min, extend toward candidates_max
    # when scores are dispersed and no candidate hit the target.
    candidates_min: int = field(default_factory=lambda: _env_int("ENIGMA_CANDIDATES_MIN", 2))
    candidates_max: int = field(default_factory=lambda: _env_int("ENIGMA_CANDIDATES", 4))
    target_score: float = field(default_factory=lambda: _env_float("ENIGMA_TARGET_SCORE", 0.95))
    patience: int = field(default_factory=lambda: _env_int("ENIGMA_PATIENCE", 2))
    archive_size: int = field(default_factory=lambda: _env_int("ENIGMA_ARCHIVE_SIZE", 4))
    novelty_threshold: float = field(default_factory=lambda: _env_float("ENIGMA_NOVELTY_THRESHOLD", 0.97))
    task_timeout_s: float = field(default_factory=lambda: _env_float("ENIGMA_TASK_TIMEOUT", 900.0))
    request_timeout_s: float = field(default_factory=lambda: _env_float("ENIGMA_REQUEST_TIMEOUT", 180.0))

    # Daemon
    concurrency: int = field(default_factory=lambda: _env_int("ENIGMA_CONCURRENCY", 2))
    poll_interval_s: float = field(default_factory=lambda: _env_float("ENIGMA_POLL_INTERVAL", 1.0))

    # PRM sidecar (step-level process reward model, see sidecar/)
    prm_url: str = field(default_factory=lambda: _env("ENIGMA_PRM_URL", "http://127.0.0.1:8799"))

    # Memory / self-learning
    recall_top_k: int = field(default_factory=lambda: _env_int("ENIGMA_RECALL_K", 4))
    evolved_styles_max: int = field(default_factory=lambda: _env_int("ENIGMA_EVOLVED_STYLES_MAX", 4))
    episode_retention_days: int = field(default_factory=lambda: _env_int("ENIGMA_EPISODE_RETENTION_DAYS", 30))

    @property
    def db_path(self) -> Path:
        return self.home / "enigma.db"

    @property
    def pid_path(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return self.home / "daemon.log"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    cfg = Config()
    if not cfg.local_models:
        raise SystemExit("ENIGMA_LOCAL_MODELS is empty — set at least one Ollama model name")
    if cfg.candidates_min < 1 or cfg.candidates_max < cfg.candidates_min:
        raise SystemExit("candidate bounds invalid: need 1 <= ENIGMA_CANDIDATES_MIN <= ENIGMA_CANDIDATES")
    cfg.ensure_home()
    return cfg
