"""SQLite-backed store: task queue, episode log, playbook memory, bandit state.

Self-learning substrate:
  insights — playbook bullets distilled from finished tasks, with per-bullet
    helpful/harmful counters (ACE-style): recalled bullets get credited or
    blamed by task outcome and pruned when net-negative.
  cases    — Memento-style bank of solved (task, output) exemplars recalled
    as few-shot examples for similar tasks.
  styles   — GEPA-style evolved prompt-style hints, arbitrated by the bandit.

All access must stay on one thread (the daemon event loop or the CLI main
thread) — sqlite3's serialized mode does not make cross-thread statement
sequences on a shared connection safe.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
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
    kind TEXT,
    lesson TEXT NOT NULL,
    embedding TEXT,
    uses INTEGER DEFAULT 0,
    helpful INTEGER DEFAULT 0,
    harmful INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bandit (
    context TEXT NOT NULL,
    arm TEXT NOT NULL,
    alpha REAL NOT NULL DEFAULT 1.0,
    beta REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (context, arm)
);

CREATE TABLE IF NOT EXISTS styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    hint TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    kind TEXT,
    description TEXT NOT NULL,
    embedding TEXT,
    output TEXT NOT NULL,
    score REAL,
    created_at REAL NOT NULL
);
"""

_MIGRATIONS = (
    "ALTER TABLE insights ADD COLUMN helpful INTEGER DEFAULT 0",
    "ALTER TABLE insights ADD COLUMN harmful INTEGER DEFAULT 0",
)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=2000")
        self._db.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._db.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._db.commit()
        # (id, lesson, decoded embedding or None) — insights are append-mostly,
        # so cache them to avoid re-decoding 500 embeddings per recall.
        self._insight_cache: list[tuple[int, str, list[float] | None]] | None = None

    def close(self) -> None:
        self._db.close()

    # ---- task queue -----------------------------------------------------

    def enqueue(self, task_id: str, spec_json: str) -> str:
        """Insert the task; on id collision mint a fresh id. Returns the id used."""
        for attempt in range(2):
            try:
                self._db.execute(
                    "INSERT INTO tasks (id, spec, created_at) VALUES (?, ?, ?)",
                    (task_id, spec_json, time.time()),
                )
                self._db.commit()
                return task_id
            except sqlite3.IntegrityError:
                if attempt:
                    raise
                task_id = uuid.uuid4().hex[:12]
                spec = json.loads(spec_json)
                spec["id"] = task_id
                spec_json = json.dumps(spec)
        return task_id

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
        """Recover tasks left 'running' by a killed daemon.

        Only call while holding the daemon pidfile — a second daemon calling
        this would steal a live daemon's in-flight tasks.
        """
        cur = self._db.execute("UPDATE tasks SET status='queued', started_at=NULL WHERE status='running'")
        self._db.commit()
        return cur.rowcount

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def list_succeeded_specs(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT spec, result FROM tasks WHERE status='succeeded' ORDER BY created_at").fetchall()

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

    def episode_history(self, kind: str, limit: int = 2000) -> list[sqlite3.Row]:
        """(iteration, score, final task status) for locally-run episodes of this
        evaluator kind — the calibration data for cascade deferral."""
        return self._db.execute(
            "SELECT e.iteration, e.score, t.status FROM episodes e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.origin='local' AND t.status IN ('succeeded','exhausted') "
            "AND json_extract(t.spec, '$.evaluator.kind') = ? "
            "ORDER BY e.id DESC LIMIT ?",
            (kind, limit),
        ).fetchall()

    def prune_episodes(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self._db.execute("DELETE FROM episodes WHERE created_at < ?", (cutoff,))
        self._db.commit()
        return cur.rowcount

    # ---- insights (playbook memory) -----------------------------------------

    def _load_insight_cache(self) -> list[tuple[int, str, list[float] | None]]:
        if self._insight_cache is None:
            rows = self._db.execute(
                "SELECT id, lesson, embedding FROM insights ORDER BY id DESC LIMIT 500"
            ).fetchall()
            self._insight_cache = [
                (r["id"], r["lesson"] or "", json.loads(r["embedding"]) if r["embedding"] else None)
                for r in rows
            ]
        return self._insight_cache

    def add_insight(self, task_id: str, kind: str, lesson: str, embedding: list[float] | None) -> None:
        self._db.execute(
            "INSERT INTO insights (task_id, kind, lesson, embedding, created_at) VALUES (?,?,?,?,?)",
            (task_id, kind, lesson[:2000], json.dumps(embedding) if embedding else None, time.time()),
        )
        self._db.commit()
        if self._insight_cache is not None:
            row = self._db.execute("SELECT last_insert_rowid() AS i").fetchone()
            self._insight_cache.insert(0, (row["i"], lesson[:2000], embedding))

    def is_duplicate_insight(self, lesson: str, embedding: list[float] | None) -> bool:
        for _id, existing, emb in self._load_insight_cache():
            if embedding is not None and emb is not None:
                if _cosine(embedding, emb) > 0.95:
                    return True
            elif _jaccard(_tokens(lesson), _tokens(existing)) > 0.8:
                return True
        return False

    def recall(self, query: str, query_emb: list[float] | None, top_k: int) -> list[tuple[int, str]]:
        """Top insights as (id, lesson). Embedded and non-embedded rows are ranked
        separately (cosine vs token-overlap scales aren't comparable) and merged
        by rank so neither population starves the other."""
        cache = self._load_insight_cache()
        if not cache:
            return []
        q_tokens = _tokens(query)
        embedded: list[tuple[float, int, str]] = []
        plain: list[tuple[float, int, str]] = []
        for iid, lesson, emb in cache:
            if query_emb is not None and emb is not None:
                embedded.append((_cosine(query_emb, emb), iid, lesson))
            else:
                plain.append((_jaccard(q_tokens, _tokens(lesson)), iid, lesson))
        embedded.sort(reverse=True)
        plain.sort(reverse=True)
        merged: list[tuple[int, str]] = []
        e = p = 0
        while len(merged) < top_k and (e < len(embedded) or p < len(plain)):
            if e < len(embedded) and embedded[e][0] > 0.3:
                merged.append((embedded[e][1], embedded[e][2]))
                e += 1
            elif p < len(plain) and plain[p][0] > 0.05:
                merged.append((plain[p][1], plain[p][2]))
                p += 1
            else:
                break
        if merged:
            ids = [i for i, _ in merged]
            self._db.execute(
                f"UPDATE insights SET uses = uses + 1 WHERE id IN ({','.join('?' * len(ids))})", ids
            )
            self._db.commit()
        return merged

    def mark_insights(self, ids: list[int], helpful: bool) -> None:
        if not ids:
            return
        col = "helpful" if helpful else "harmful"
        self._db.execute(
            f"UPDATE insights SET {col} = {col} + 1 WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        self._db.commit()

    def prune_insights(self) -> int:
        """Drop playbook bullets that keep hurting (ACE curation, delete-only)."""
        cur = self._db.execute("DELETE FROM insights WHERE uses >= 4 AND harmful - helpful >= 3")
        self._db.commit()
        if cur.rowcount:
            self._insight_cache = None
        return cur.rowcount

    def list_insights(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT kind, lesson, uses, helpful, harmful, created_at FROM insights ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---- case bank (Memento) ------------------------------------------------

    def add_case(self, task_id: str, kind: str, description: str, embedding: list[float] | None,
                 output: str, score: float) -> None:
        self._db.execute(
            "INSERT INTO cases (task_id, kind, description, embedding, output, score, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, kind, description[:2000], json.dumps(embedding) if embedding else None,
             output[:8000], score, time.time()),
        )
        self._db.commit()

    def recall_case(self, kind: str, query: str, query_emb: list[float] | None) -> sqlite3.Row | None:
        """Best matching solved exemplar of the same evaluator kind, or None."""
        rows = self._db.execute(
            "SELECT description, embedding, output, score FROM cases WHERE kind=? ORDER BY id DESC LIMIT 200",
            (kind,),
        ).fetchall()
        best, best_sim = None, 0.0
        q_tokens = _tokens(query)
        for r in rows:
            emb = json.loads(r["embedding"]) if r["embedding"] else None
            if query_emb is not None and emb is not None:
                sim = _cosine(query_emb, emb)
                floor = 0.55
            else:
                sim = _jaccard(q_tokens, _tokens(r["description"]))
                floor = 0.25
            if sim > max(best_sim, floor):
                best, best_sim = r, sim
        return best

    # ---- evolved styles (GEPA) ------------------------------------------------

    def add_style(self, kind: str, hint: str, cap: int) -> None:
        self._db.execute(
            "INSERT INTO styles (kind, hint, created_at) VALUES (?,?,?)", (kind, hint[:600], time.time())
        )
        # Keep only the newest `cap` evolved styles per kind; the bandit's Beta
        # posteriors on older arms are the quality filter before this cap bites.
        self._db.execute(
            "DELETE FROM styles WHERE kind=? AND id NOT IN "
            "(SELECT id FROM styles WHERE kind=? ORDER BY id DESC LIMIT ?)",
            (kind, kind, cap),
        )
        self._db.commit()

    def list_styles(self, kind: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT id, hint FROM styles WHERE kind=? ORDER BY id", (kind,)).fetchall()

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
