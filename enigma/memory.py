"""SQLite-backed store: task queue, episode log, insight memory, bandit state.

Insights are the self-learning substrate: distilled lessons from finished
tasks, embedded and recalled by cosine similarity (with a token-overlap
fallback when no embedding model is available).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|exhausted|failed
    result TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    arm TEXT,
    score REAL,
    feedback TEXT,
    origin TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    kind TEXT,               -- evaluator kind the lesson came from
    lesson TEXT NOT NULL,
    embedding TEXT,          -- JSON array or NULL
    uses INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bandit (
    context TEXT NOT NULL,   -- evaluator kind
    arm TEXT NOT NULL,       -- json strategy descriptor
    alpha REAL NOT NULL DEFAULT 1.0,
    beta REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (context, arm)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe: CPython's sqlite3 is built serialized
        # (threadsafety=3), and the engine only touches the store from short calls.
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---- task queue -----------------------------------------------------

    def enqueue(self, task_id: str, spec_json: str) -> None:
        self._db.execute(
            "INSERT INTO tasks (id, spec, created_at) VALUES (?, ?, ?)",
            (task_id, spec_json, time.time()),
        )
        self._db.commit()

    def claim_next(self) -> sqlite3.Row | None:
        """Atomically claim the oldest queued task."""
        cur = self._db.execute(
            "UPDATE tasks SET status='running', started_at=? "
            "WHERE id = (SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1) "
            "RETURNING id, spec",
            (time.time(),),
        )
        row = cur.fetchone()
        self._db.commit()
        return row

    def finish(self, task_id: str, status: str, result_json: str) -> None:
        self._db.execute(
            "UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
            (status, result_json, time.time(), task_id),
        )
        self._db.commit()

    def requeue_stale_running(self) -> int:
        """Recover tasks left 'running' by a killed daemon."""
        cur = self._db.execute("UPDATE tasks SET status='queued', started_at=NULL WHERE status='running'")
        self._db.commit()
        return cur.rowcount

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self._db.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- episodes ---------------------------------------------------------

    def log_episode(self, task_id: str, iteration: int, arm: str, score: float, feedback: str, origin: str) -> None:
        self._db.execute(
            "INSERT INTO episodes (task_id, iteration, arm, score, feedback, origin, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, iteration, arm, score, feedback[:2000], origin, time.time()),
        )
        self._db.commit()

    # ---- insights (self-learning memory) -----------------------------------

    def add_insight(self, task_id: str, kind: str, lesson: str, embedding: list[float] | None) -> None:
        self._db.execute(
            "INSERT INTO insights (task_id, kind, lesson, embedding, created_at) VALUES (?,?,?,?,?)",
            (task_id, kind, lesson[:2000], json.dumps(embedding) if embedding else None, time.time()),
        )
        self._db.commit()

    def recall(self, query: str, query_emb: list[float] | None, top_k: int) -> list[str]:
        rows = self._db.execute("SELECT id, lesson, embedding FROM insights ORDER BY id DESC LIMIT 500").fetchall()
        if not rows:
            return []
        scored: list[tuple[float, int, str]] = []
        q_tokens = _tokens(query)
        for r in rows:
            emb = json.loads(r["embedding"]) if r["embedding"] else None
            if query_emb is not None and emb is not None:
                sim = _cosine(query_emb, emb)
            else:
                sim = _jaccard(q_tokens, _tokens(r["lesson"] or ""))
            scored.append((sim, r["id"], r["lesson"]))
        scored.sort(reverse=True)
        picked = [(i, lesson) for sim, i, lesson in scored[:top_k] if sim > 0.05]
        if picked:
            self._db.execute(
                f"UPDATE insights SET uses = uses + 1 WHERE id IN ({','.join('?' * len(picked))})",
                [i for i, _ in picked],
            )
            self._db.commit()
        return [lesson for _, lesson in picked]

    def list_insights(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT kind, lesson, uses, created_at FROM insights ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- bandit state -----------------------------------------------------

    def bandit_arms(self, context: str) -> dict[str, tuple[float, float]]:
        rows = self._db.execute("SELECT arm, alpha, beta FROM bandit WHERE context=?", (context,)).fetchall()
        return {r["arm"]: (r["alpha"], r["beta"]) for r in rows}

    def bandit_update(self, context: str, arm: str, reward: float) -> None:
        """Bernoulli-style update with fractional reward in [0,1]."""
        self._db.execute(
            "INSERT INTO bandit (context, arm, alpha, beta) VALUES (?,?,1,1) ON CONFLICT (context, arm) DO NOTHING",
            (context, arm),
        )
        self._db.execute(
            "UPDATE bandit SET alpha = alpha + ?, beta = beta + ? WHERE context=? AND arm=?",
            (reward, 1.0 - reward, context, arm),
        )
        self._db.commit()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
