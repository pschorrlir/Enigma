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
    # Fast model for cheap meta-ops (reflection/distillation/style/abstraction/
    # task-invention). These don't need the big solver model. Empty = reuse the
    # primary local model.
    utility_model: str = field(default_factory=lambda: _env("ENIGMA_UTILITY_MODEL", ""))
    # Model for the long-horizon agent loop (agent_run) — exploitation/automation
    # want a strong CODING model (e.g. qwen3-coder:30b). Empty = reuse local_models[0].
    agent_model: str = field(default_factory=lambda: _env("ENIGMA_AGENT_MODEL", ""))
    # Anti-drift working memory: every N steps, fold recent activity into a durable
    # "confirmed facts / hypothesis / tried / next" state that is re-injected each
    # step, so a long run can't forget a correct diagnosis or loop on dead ends.
    agent_consolidate_every: int = field(default_factory=lambda: _env_int("ENIGMA_AGENT_CONSOLIDATE_EVERY", 8))
    agent_scroll_steps: int = field(default_factory=lambda: _env_int("ENIGMA_AGENT_SCROLL", 6))
    # Static-analysis checkpoint: after this many CONSECUTIVE passive calls
    # (read/ls/grep/objdump… — no execution, no service contact), inject an
    # escalating "act on the target NOW" nudge, repeated while the streak
    # continues. Breaks the "perfect writeup, zero dynamic contact" failure
    # mode (v3/v4 both read-looped past step 40). 0 disables.
    agent_phase_nudge: int = field(default_factory=lambda: _env_int("ENIGMA_AGENT_PHASE_NUDGE", 15))
    # Second model, used as a CRITIC/supervisor that maintains the working memory
    # and flags drift while the actor model (agent_model) does the hands-on work.
    # A different model curating state catches blind spots the actor shares with
    # itself. Empty = reuse the actor model. e.g. gemma4:26b alongside qwen3-coder:30b.
    agent_critic_model: str = field(default_factory=lambda: _env("ENIGMA_AGENT_CRITIC_MODEL", ""))
    # Distills post-run lessons from agent transcripts (learn_from_agent_run).
    # Wants a strong REASONING model, not the cheap utility model — weak
    # distillers bank generic or environment-wrong lessons that mislead the
    # next run. Empty = utility_model = local_models[0]. e.g. gemma4:26b.
    agent_lesson_model: str = field(default_factory=lambda: _env("ENIGMA_AGENT_LESSON_MODEL", ""))
    # Test-time compute for the agent loop: when the actor's first draw repeats
    # something already tried (about to loop), sample this many candidates total
    # and let the PRM pick. The correct action is usually IN the distribution a
    # few draws late — reranking finds it on step 1. 1 = off.
    agent_best_of: int = field(default_factory=lambda: _env_int("ENIGMA_AGENT_BEST_OF", 3))
    # PRM sidecar (sidecar/prm_server.py) for step-level candidate scoring.
    prm_url: str = field(default_factory=lambda: _env("ENIGMA_PRM_URL", "http://127.0.0.1:8799"))
    # The entity's persona — colors generation/self-narration (director, ideation,
    # `enigma mind`) but never the grader or competence measurement. See persona().
    persona_path: str = field(default_factory=lambda: _env("ENIGMA_PERSONA_PATH", "enigma/persona.txt"))
    embed_model: str = field(default_factory=lambda: _env("ENIGMA_EMBED_MODEL", "nomic-embed-text"))
    num_ctx: int = field(default_factory=lambda: _env_int("ENIGMA_NUM_CTX", 16384))
    # Max output tokens per generation. Some Ollama builds default this low
    # (128); set it high so long design/answer outputs are never truncated.
    # It is only a ceiling — models still stop at EOS on their own.
    num_predict: int = field(default_factory=lambda: _env_int("ENIGMA_NUM_PREDICT", 8192))
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

    # Tools: ReAct-style tool use during candidate generation (see tools.py).
    tools_enabled: bool = field(default_factory=lambda: _env("ENIGMA_TOOLS", "1") not in ("0", "false", ""))
    tool_max_steps: int = field(default_factory=lambda: _env_int("ENIGMA_TOOL_MAX_STEPS", 4))
    tool_result_chars: int = field(default_factory=lambda: _env_int("ENIGMA_TOOL_RESULT_CHARS", 4000))
    # 30s killed legitimate work (gdb batch traces, de Bruijn generation,
    # software watchpoints — all timed out in v9/v10/v11b). 60s default.
    tool_timeout_s: float = field(default_factory=lambda: _env_float("ENIGMA_TOOL_TIMEOUT", 60.0))
    # Web search backend, tried in order: Tavily key, then a SearXNG JSON
    # endpoint, else keyless DuckDuckGo (best-effort, may rate-limit).
    tavily_key: str = field(default_factory=lambda: _env("ENIGMA_TAVILY_KEY", ""))
    searx_url: str = field(default_factory=lambda: _env("ENIGMA_SEARX_URL", ""))

    # Memory / self-learning
    recall_top_k: int = field(default_factory=lambda: _env_int("ENIGMA_RECALL_K", 6))
    recall_cases: int = field(default_factory=lambda: _env_int("ENIGMA_RECALL_CASES", 2))
    # Minimum cosine similarity to inject a solved case as a few-shot exemplar.
    # High by design: the eval harness showed loosely-similar cases DEGRADE a
    # strong solver (wrong-exemplar anchoring). Cases should help only on
    # genuinely recurring task families, and stay silent on novel tasks.
    recall_case_floor: float = field(default_factory=lambda: _env_float("ENIGMA_RECALL_CASE_FLOOR", 0.82))
    recall_reflections_k: int = field(default_factory=lambda: _env_int("ENIGMA_RECALL_REFLECTIONS_K", 3))
    evolved_styles_max: int = field(default_factory=lambda: _env_int("ENIGMA_EVOLVED_STYLES_MAX", 4))
    episode_retention_days: int = field(default_factory=lambda: _env_int("ENIGMA_EPISODE_RETENTION_DAYS", 30))
    # Memory is governed by a DISK BUDGET, not fixed counts — it grows freely
    # until the live data on disk reaches this size, then the weakest/oldest
    # rows are evicted (disposable logs first, distilled learning last) and the
    # space reclaimed. This is the primary limit: "expand until disk says stop".
    memory_max_mb: int = field(default_factory=lambda: _env_int("ENIGMA_MEMORY_MAX_MB", 2048))
    # Cold-start grace: distilled learning younger than this is exempt from
    # disk-budget eviction, so new (uses=0) insights aren't culled as junk.
    memory_evict_grace_hours: int = field(default_factory=lambda: _env_int("ENIGMA_MEMORY_GRACE_HOURS", 48))
    # Which memory tiers recall injects into prompts: full | none | cases |
    # insights. Drives the eval harness's memory-on/off ablation.
    memory_mode: str = field(default_factory=lambda: _env("ENIGMA_MEMORY_MODE", "full"))
    # >=0 forces this generation temperature (overriding the bandit arm). The
    # eval harness sets 0 for deterministic, reproducible measurement — otherwise
    # sampling variance (±2-3 tasks at n=12) swamps the memory effect.
    force_temperature: float = field(default_factory=lambda: _env_float("ENIGMA_FORCE_TEMP", -1.0))
    # Optional per-tier hard ceilings, applied on top of the disk budget.
    # 0 = unlimited (the default) — let the disk budget govern the tier.
    reflections_max: int = field(default_factory=lambda: _env_int("ENIGMA_REFLECTIONS_MAX", 0))
    insights_max: int = field(default_factory=lambda: _env_int("ENIGMA_INSIGHTS_MAX", 0))
    cases_max: int = field(default_factory=lambda: _env_int("ENIGMA_CASES_MAX", 0))

    # How much recalled memory reaches the model (scales with num_ctx).
    prompt_exemplar_chars: int = field(default_factory=lambda: _env_int("ENIGMA_PROMPT_EXEMPLAR_CHARS", 8000))
    prompt_case_chars: int = field(default_factory=lambda: _env_int("ENIGMA_PROMPT_CASE_CHARS", 4000))
    prompt_feedback_chars: int = field(default_factory=lambda: _env_int("ENIGMA_PROMPT_FEEDBACK_CHARS", 3000))
    prompt_input_chars: int = field(default_factory=lambda: _env_int("ENIGMA_PROMPT_INPUT_CHARS", 16000))

    # Dreaming: idle-time memory consolidation + self-play (see dream.py)
    dream_enabled: bool = field(default_factory=lambda: _env("ENIGMA_DREAM", "1") not in ("0", "false", ""))
    dream_idle_s: float = field(default_factory=lambda: _env_float("ENIGMA_DREAM_IDLE_S", 60.0))
    # Self-play is contrived coding drills — grounding, not insight. Keep it
    # light so idle time goes to ideation (real discovery), per operator steer.
    dream_max_plays: int = field(default_factory=lambda: _env_int("ENIGMA_DREAM_MAX_PLAYS", 1))
    # Cap _abstract calls per cycle so consolidation cost is flat regardless of
    # how many clusters the (growing) insight set forms.
    dream_max_clusters: int = field(default_factory=lambda: _env_int("ENIGMA_DREAM_MAX_CLUSTERS", 4))
    dream_cluster_min: int = field(default_factory=lambda: _env_int("ENIGMA_DREAM_CLUSTER_MIN", 3))
    dream_cluster_threshold: float = field(default_factory=lambda: _env_float("ENIGMA_DREAM_CLUSTER_THRESHOLD", 0.82))
    # Lexical fallback used when insights have no embeddings (embed model
    # unavailable): token-overlap clustering instead of cosine. Jaccard runs
    # on a much lower scale than cosine, and lexical clusters are sparser, so
    # this path pairs at a lower threshold (min cluster size 2).
    dream_cluster_token_threshold: float = field(default_factory=lambda: _env_float("ENIGMA_DREAM_CLUSTER_TOKEN", 0.25))
    dream_case_dedup_threshold: float = field(default_factory=lambda: _env_float("ENIGMA_DREAM_CASE_DEDUP", 0.95))

    # Ideation: idle-time discovery of novel ideas by combining distant memory
    # concepts, gated on novelty (embedding distance) + value (skeptical score).
    ideate_enabled: bool = field(default_factory=lambda: _env("ENIGMA_IDEATE", "1") not in ("0", "false", ""))
    dream_max_ideas: int = field(default_factory=lambda: _env_int("ENIGMA_DREAM_MAX_IDEAS", 5))
    ideas_max: int = field(default_factory=lambda: _env_int("ENIGMA_IDEAS_MAX", 0))
    # Insight lives at MODERATE conceptual distance: far enough to be non-obvious,
    # close enough that a real bridge exists. Pairing the single most-distant
    # concepts (max novelty) yields incoherent mashups ("sort arrays as if money"),
    # not insight. Draw bridge pairs from this cosine band instead of the extreme.
    idea_bridge_lo: float = field(default_factory=lambda: _env_float("ENIGMA_IDEA_BRIDGE_LO", 0.20))
    idea_bridge_hi: float = field(default_factory=lambda: _env_float("ENIGMA_IDEA_BRIDGE_HI", 0.62))
    # Novelty floor lowered (a coherent, useful insight need not be maximally far
    # from everything); value floor raised and value now dominates ranking.
    idea_min_novelty: float = field(default_factory=lambda: _env_float("ENIGMA_IDEA_MIN_NOVELTY", 0.18))
    idea_min_value: float = field(default_factory=lambda: _env_float("ENIGMA_IDEA_MIN_VALUE", 0.62))
    # Ground kept ideas with a web search (prior-art check). Off by default —
    # search is slow/flaky and needs a backend; on, it attaches real findings.
    idea_ground: bool = field(default_factory=lambda: _env("ENIGMA_IDEA_GROUND", "0") not in ("0", "false", ""))

    # Dream director: an LLM orchestrator that judges which topics each dream
    # cycle should explore, then steers self-play + ideation toward them
    # (instead of purely random sampling). A user-set focus (meta 'dream_focus',
    # via `enigma focus`) is weighted heavily when present.
    dream_direct_enabled: bool = field(default_factory=lambda: _env("ENIGMA_DREAM_DIRECT", "1") not in ("0", "false", ""))
    dream_topics_per_cycle: int = field(default_factory=lambda: _env_int("ENIGMA_DREAM_TOPICS", 3))

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

    def persona(self) -> str:
        """The entity's persona text, or '' if none is configured/readable."""
        if not self.persona_path:
            return ""
        try:
            return Path(self.persona_path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def load_config() -> Config:
    cfg = Config()
    if not cfg.local_models:
        raise SystemExit("ENIGMA_LOCAL_MODELS is empty — set at least one Ollama model name")
    if cfg.candidates_min < 1 or cfg.candidates_max < cfg.candidates_min:
        raise SystemExit("candidate bounds invalid: need 1 <= ENIGMA_CANDIDATES_MIN <= ENIGMA_CANDIDATES")
    cfg.ensure_home()
    return cfg
