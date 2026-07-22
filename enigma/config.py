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

    # Cloud frontier model (Anthropic), used only for cohesion passes
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    cloud_model: str = field(default_factory=lambda: _env("ENIGMA_CLOUD_MODEL", "claude-sonnet-5"))
    cloud_max_calls_per_task: int = field(default_factory=lambda: _env_int("ENIGMA_CLOUD_MAX_CALLS", 2))

    # Iteration loop
    max_iterations: int = field(default_factory=lambda: _env_int("ENIGMA_MAX_ITERATIONS", 8))
    candidates_per_iteration: int = field(default_factory=lambda: _env_int("ENIGMA_CANDIDATES", 3))
    target_score: float = field(default_factory=lambda: _env_float("ENIGMA_TARGET_SCORE", 0.95))
    # Escalate to the cloud after this many iterations without improvement.
    patience: int = field(default_factory=lambda: _env_int("ENIGMA_PATIENCE", 2))
    archive_size: int = field(default_factory=lambda: _env_int("ENIGMA_ARCHIVE_SIZE", 4))
    task_timeout_s: float = field(default_factory=lambda: _env_float("ENIGMA_TASK_TIMEOUT", 900.0))
    request_timeout_s: float = field(default_factory=lambda: _env_float("ENIGMA_REQUEST_TIMEOUT", 180.0))

    # Daemon
    concurrency: int = field(default_factory=lambda: _env_int("ENIGMA_CONCURRENCY", 2))
    poll_interval_s: float = field(default_factory=lambda: _env_float("ENIGMA_POLL_INTERVAL", 1.0))

    # Memory recall
    recall_top_k: int = field(default_factory=lambda: _env_int("ENIGMA_RECALL_K", 4))

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
    cfg.ensure_home()
    return cfg
