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
    source TEXT NOT NULL DEFAULT 'user',    -- user|dream
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

-- Small key/value store for daemon-published state (e.g. live dream status)
-- the dashboard reads across connections.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Operator-ingested documents (notes, papers, code): chunked + embedded, they
-- join the concept pool so ideation recombines the USER's material, not just
-- the engine's own coding-lesson playbook.
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    text TEXT NOT NULL,
    embedding TEXT,
    created_at REAL NOT NULL
);

-- Semantic memory: cross-task principles distilled during dreaming by
-- clustering episodic insights. `support` = how many insights it abstracts.
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT,
    lesson TEXT NOT NULL,
    embedding TEXT,
    support INTEGER DEFAULT 1,
    uses INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

-- Discoveries: novel ideas generated during dreaming by combining distant
-- memory concepts. `novelty` = embedding distance from prior knowledge,
-- `value` = skeptical usefulness score, `score` = their harmonic mean,
-- `parents` = JSON of the source concept snippets that sparked it.
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL,
    elaboration TEXT,
    embedding TEXT,
    novelty REAL,
    value REAL,
    score REAL,
    parents TEXT,
    uses INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
"""

_MIGRATIONS = (
    "ALTER TABLE insights ADD COLUMN helpful INTEGER DEFAULT 0",
    "ALTER TABLE insights ADD COLUMN harmful INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'user'",
    # Reflections earn outcome credit like insights (were popularity-only).
    "ALTER TABLE reflections ADD COLUMN helpful INTEGER DEFAULT 0",
    "ALTER TABLE reflections ADD COLUMN harmful INTEGER DEFAULT 0",
    # Human feedback on discovered ideas: 0 neutral, 1 liked, -1 dismissed.
    "ALTER TABLE ideas ADD COLUMN rating INTEGER DEFAULT 0",
)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
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
        # (id, lesson, decoded embedding or None, kind) — insights are append-mostly,
        # so cache them to avoid re-decoding 500 embeddings per recall.
        self._insight_cache: list[tuple[int, str, list[float] | None, str]] | None = None

    def close(self) -> None:
        self._db.close()

    # ---- task queue -----------------------------------------------------

    def enqueue(self, task_id: str, spec_json: str, source: str = "user") -> str:
        """Insert the task; on id collision mint a fresh id. Returns the id used."""
        for attempt in range(2):
            try:
                self._db.execute(
                    "INSERT INTO tasks (id, spec, source, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, spec_json, source, time.time()),
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

    def insert_dream_task(self, task_id: str, spec_json: str) -> None:
        """Record a self-play task as already 'running' so the daemon's claim
        loop never picks it up — the dreamer owns and finishes it directly."""
        self._db.execute(
            "INSERT OR REPLACE INTO tasks (id, spec, status, source, created_at, started_at) "
            "VALUES (?, ?, 'running', 'dream', ?, ?)",
            (task_id, spec_json, time.time(), time.time()),
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
        """Recover tasks left 'running' by a killed daemon.

        Only call while holding the daemon pidfile — a second daemon calling
        this would steal a live daemon's in-flight tasks. Dream (self-play)
        tasks are never requeued onto the user queue: an interrupted dream is
        marked 'failed', since re-running it would inject synthetic work.
        """
        self._db.execute(
            "UPDATE tasks SET status='failed', finished_at=? WHERE status='running' AND source='dream'",
            (time.time(),),
        )
        cur = self._db.execute(
            "UPDATE tasks SET status='queued', started_at=NULL WHERE status='running' AND source='user'"
        )
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

    def episode_climb(self) -> list[sqlite3.Row]:
        """Per evaluator kind and iteration index, mean score and sample count over
        locally-run episodes of finished tasks — the iteration-climb aggregation for
        the dashboard. Read-only; the server ships this straight to /api/episodes so
        the client never aggregates."""
        return self._db.execute(
            "SELECT json_extract(t.spec, '$.evaluator.kind') AS kind, e.iteration AS iteration, "
            "AVG(e.score) AS mean_score, COUNT(*) AS n "
            "FROM episodes e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.origin='local' AND t.status IN ('succeeded','exhausted') AND e.score IS NOT NULL "
            "GROUP BY kind, e.iteration ORDER BY kind, e.iteration"
        ).fetchall()

    def prune_episodes(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self._db.execute("DELETE FROM episodes WHERE created_at < ?", (cutoff,))
        self._db.commit()
        return cur.rowcount

    # ---- insights (playbook memory) -----------------------------------------

    def _load_insight_cache(self) -> list[tuple[int, str, list[float] | None, str]]:
        if self._insight_cache is None:
            rows = self._db.execute(
                "SELECT id, lesson, embedding, kind FROM insights ORDER BY id DESC LIMIT 4000"
            ).fetchall()
            self._insight_cache = [
                (r["id"], r["lesson"] or "",
                 json.loads(r["embedding"]) if r["embedding"] else None,
                 r["kind"] or "")
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
            self._insight_cache.insert(0, (row["i"], lesson[:2000], embedding, kind))

    def is_duplicate_insight(self, lesson: str, embedding: list[float] | None) -> bool:
        for _id, existing, emb, _kind in self._load_insight_cache():
            if embedding is not None and emb is not None:
                if _cosine(embedding, emb) > 0.95:
                    return True
            elif _jaccard(_tokens(lesson), _tokens(existing)) > 0.8:
                return True
        return False

    def recent_lessons(self, kind: str, k: int) -> list[str]:
        """The NEWEST lessons of a kind, newest first — recall() ranks by
        similarity, but a campaign's latest strategic lessons lose lexical/cosine
        races to tactical lessons stuffed with objective keywords (v9/v10 both
        repeated failures their own run's lesson addressed)."""
        return [lesson for _, lesson in self.recent_lesson_rows(kind, k)]

    def recent_lesson_rows(self, kind: str, k: int) -> list[tuple[int, str]]:
        """Same as recent_lessons but with ids, so callers can credit/blame the
        exact lessons they injected (closes the ACE loop for agent runs)."""
        rows = self._db.execute(
            "SELECT id, lesson FROM insights WHERE kind=? ORDER BY id DESC LIMIT ?",
            (kind, k)).fetchall()
        return [(r["id"], r["lesson"] or "") for r in rows]

    def golden_lesson_rows(self, kind: str) -> list[tuple[int, str]]:
        """Curated ALWAYS-pinned strategic lessons, marked by a 'GOLDEN:' prefix
        in the lesson text (no schema change). Pinned ahead of the recency-4 in
        every agent head: arvo_23074 attempts 2→3 showed newest-4 pinning gives
        lessons a one-batch shelf life — each autopsy banks ~4 new lessons and
        evicts the previous batch from the prompt."""
        rows = self._db.execute(
            "SELECT id, lesson FROM insights WHERE kind=? AND lesson LIKE 'GOLDEN:%' "
            "ORDER BY id DESC", (kind,)).fetchall()
        return [(r["id"], r["lesson"] or "") for r in rows]

    def recall(self, query: str, query_emb: list[float] | None, top_k: int,
               kind: str | None = None) -> list[tuple[int, str]]:
        """Top insights as (id, lesson). Embedded and non-embedded rows are ranked
        separately (cosine vs token-overlap scales aren't comparable) and merged
        by rank so neither population starves the other. `kind` scopes the race:
        agent lessons should not compete with coding-task lessons for prompt
        slots (264 python_tests vs 50 agent rows at last count)."""
        cache = self._load_insight_cache()
        if kind is not None:
            cache = [row for row in cache if row[3] == kind]
        if not cache:
            return []
        q_tokens = _tokens(query)
        embedded: list[tuple[float, int, str]] = []
        plain: list[tuple[float, int, str]] = []
        for iid, lesson, emb, _kind in cache:
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

    def all_insights(self) -> list[tuple[int, str, list[float] | None, str]]:
        """(id, lesson, embedding, kind) for every cached insight — dream clustering."""
        return list(self._load_insight_cache())

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
        cases = self.recall_cases(kind, query, query_emb, 1)
        return cases[0] if cases else None

    def recall_cases(self, kind: str, query: str, query_emb: list[float] | None, k: int,
                     floor: float = 0.82) -> list[sqlite3.Row]:
        """Top-k solved exemplars of the same evaluator kind, best first. `floor`
        is the min cosine similarity to inject — high, so only near-identical
        (recurring) tasks pull an exemplar; loosely-similar ones bleed and hurt."""
        if k <= 0:
            return []
        rows = self._db.execute(
            "SELECT description, embedding, output, score FROM cases WHERE kind=? ORDER BY id DESC LIMIT 2000",
            (kind,),
        ).fetchall()
        q_tokens = _tokens(query)
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            emb = json.loads(r["embedding"]) if r["embedding"] else None
            if query_emb is not None and emb is not None:
                sim, lo = _cosine(query_emb, emb), floor
            else:
                sim, lo = _jaccard(q_tokens, _tokens(r["description"])), max(0.35, floor - 0.3)
            if sim > lo:
                scored.append((sim, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [r for _, r in scored[:k]]

    def all_cases(self) -> list[sqlite3.Row]:
        """Every case with its embedding — for dream-time dedup/merge."""
        return self._db.execute(
            "SELECT id, kind, description, embedding, score FROM cases ORDER BY id"
        ).fetchall()

    def dashboard_cases(self, limit: int = 400) -> list[sqlite3.Row]:
        """Lean case rows (no embeddings) newest-first for the dashboard's memory-health
        view. Read-only; skips the heavy embedding column all_cases() carries."""
        return self._db.execute(
            "SELECT id, kind, description, score, created_at FROM cases ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def delete_cases(self, ids: list[int]) -> int:
        if not ids:
            return 0
        cur = self._db.execute(
            f"DELETE FROM cases WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        self._db.commit()
        return cur.rowcount

    def sample_dream_seeds(self, kinds: tuple[str, ...], limit: int) -> list[sqlite3.Row]:
        """Recent high-scoring solved cases of open-ended kinds, to seed self-play."""
        if not kinds or limit <= 0:
            return []
        placeholders = ",".join("?" * len(kinds))
        return self._db.execute(
            f"SELECT description, kind, output, score FROM cases "
            f"WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*kinds, limit),
        ).fetchall()

    # ---- reflections (consolidated semantic memory, written by dreaming) ------

    def add_reflection(self, topic: str, lesson: str, embedding: list[float] | None, support: int) -> None:
        self._db.execute(
            "INSERT INTO reflections (topic, lesson, embedding, support, created_at) VALUES (?,?,?,?,?)",
            (topic[:200], lesson[:2000], json.dumps(embedding) if embedding else None, support, time.time()),
        )
        self._db.commit()

    def all_reflections(self, limit: int = 2000) -> list[tuple[int, str, list[float] | None]]:
        rows = self._db.execute(
            "SELECT id, lesson, embedding FROM reflections ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r["id"], r["lesson"] or "", json.loads(r["embedding"]) if r["embedding"] else None) for r in rows]

    def recall_reflections(self, query: str, query_emb: list[float] | None, k: int) -> list[tuple[int, str]]:
        """Top consolidated principles for this query as (id, lesson), best first.
        Ids let the caller credit/blame which principles actually helped."""
        if k <= 0:
            return []
        rows = self.all_reflections()
        q_tokens = _tokens(query)
        scored: list[tuple[float, int, str]] = []
        for rid, lesson, emb in rows:
            if query_emb is not None and emb is not None:
                sim, floor = _cosine(query_emb, emb), 0.25
            else:
                sim, floor = _jaccard(q_tokens, _tokens(lesson)), 0.05
            if sim > floor:
                scored.append((sim, rid, lesson))
        scored.sort(reverse=True)
        top = scored[:k]
        if top:
            ids = [rid for _, rid, _ in top]
            self._db.execute(
                f"UPDATE reflections SET uses = uses + 1 WHERE id IN ({','.join('?' * len(ids))})", ids
            )
            self._db.commit()
        return [(rid, lesson) for _, rid, lesson in top]

    def mark_reflections(self, ids: list[int], helpful: bool) -> None:
        if not ids:
            return
        col = "helpful" if helpful else "harmful"
        self._db.execute(
            f"UPDATE reflections SET {col} = {col} + 1 WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        self._db.commit()

    def prune_reflections(self) -> int:
        """Drop principles that keep hurting when recalled (net-negative), the
        same ACE curation insights get — so platitudes can't become immortal."""
        cur = self._db.execute("DELETE FROM reflections WHERE uses >= 4 AND harmful - helpful >= 3")
        self._db.commit()
        return cur.rowcount

    def list_reflections(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT topic, lesson, support, uses, helpful, harmful, created_at "
            "FROM reflections ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---- eviction (keep memory tiers bounded; called during dreaming) --------

    def cap_reflections(self, cap: int) -> int:
        """Keep the strongest `cap` reflections (by support+uses, then newest).
        cap <= 0 means unlimited (the disk budget governs instead)."""
        if cap <= 0:
            return 0
        cur = self._db.execute(
            "DELETE FROM reflections WHERE id NOT IN "
            "(SELECT id FROM reflections ORDER BY (support + uses) DESC, id DESC LIMIT ?)",
            (cap,),
        )
        self._db.commit()
        return cur.rowcount

    def cap_insights(self, cap: int) -> int:
        """Keep the most-useful `cap` insights (by net-helpful+uses, then newest).
        cap <= 0 means unlimited (the disk budget governs instead)."""
        if cap <= 0:
            return 0
        cur = self._db.execute(
            "DELETE FROM insights WHERE id NOT IN "
            "(SELECT id FROM insights ORDER BY (helpful - harmful + uses) DESC, id DESC LIMIT ?)",
            (cap,),
        )
        self._db.commit()
        if cur.rowcount:
            self._insight_cache = None
        return cur.rowcount

    def cap_cases(self, cap: int) -> int:
        """Keep the best `cap` solved exemplars (by score, then newest).
        cap <= 0 means unlimited (the disk budget governs instead)."""
        if cap <= 0:
            return 0
        cur = self._db.execute(
            "DELETE FROM cases WHERE id NOT IN "
            "(SELECT id FROM cases ORDER BY score DESC, id DESC LIMIT ?)",
            (cap,),
        )
        self._db.commit()
        return cur.rowcount

    def disk_usage(self) -> dict[str, int]:
        """Live data size (pages in use) and the actual file footprint on disk."""
        pc = self._db.execute("PRAGMA page_count").fetchone()[0]
        fc = self._db.execute("PRAGMA freelist_count").fetchone()[0]
        ps = self._db.execute("PRAGMA page_size").fetchone()[0]
        used = max(0, (pc - fc) * ps)
        file_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                file_bytes += Path(str(self._path) + suffix).stat().st_size
            except OSError:
                pass
        return {"used_bytes": used, "file_bytes": file_bytes}

    def enforce_memory_budget(self, max_bytes: int, grace_seconds: float = 0.0) -> dict[str, int]:
        """The primary memory governor: let every tier grow freely until the
        live data reaches `max_bytes`, then evict weakest/oldest first —
        disposable logs before distilled learning, reflections/ideas last — down
        to 90% of budget, and reclaim the freed pages to the OS.

        Distilled-learning tiers get a cold-start grace: rows younger than
        `grace_seconds` are exempt, so a brand-new (uses=0) insight isn't culled
        as 'junk' before it's ever had a chance to be recalled. Liked ideas are
        never evicted. Uses (page_count - freelist_count) * page_size as the
        live-size signal, so deletes register immediately without a per-batch VACUUM."""
        if max_bytes <= 0 or self.disk_usage()["used_bytes"] <= max_bytes:
            return {}
        target = int(max_bytes * 0.9)
        cutoff = time.time() - grace_seconds  # rows younger than this are protected
        # Eviction ladder, least-valuable first. Each rung deletes its oldest /
        # weakest rows; we only descend to the next rung if still over target.
        ladder = (
            ("episodes", "DELETE FROM episodes WHERE id IN "
                         "(SELECT id FROM episodes ORDER BY created_at ASC, id ASC LIMIT ?)"),
            ("dream_tasks", "DELETE FROM tasks WHERE source='dream' AND status != 'running' AND id IN "
                            "(SELECT id FROM tasks WHERE source='dream' AND status != 'running' "
                            "ORDER BY created_at ASC LIMIT ?)"),
            ("old_tasks", "DELETE FROM tasks WHERE status != 'running' AND id IN "
                          "(SELECT id FROM tasks WHERE status != 'running' ORDER BY created_at ASC LIMIT ?)"),
            ("cases", f"DELETE FROM cases WHERE id IN "
                      f"(SELECT id FROM cases WHERE created_at < {cutoff} ORDER BY score ASC, id ASC LIMIT ?)"),
            ("insights", f"DELETE FROM insights WHERE id IN "
                         f"(SELECT id FROM insights WHERE created_at < {cutoff} "
                         f"ORDER BY (helpful - harmful + uses) ASC, id ASC LIMIT ?)"),
            ("ideas", f"DELETE FROM ideas WHERE id IN "
                      f"(SELECT id FROM ideas WHERE created_at < {cutoff} AND rating <= 0 "
                      f"ORDER BY score ASC, id ASC LIMIT ?)"),
            ("reflections", f"DELETE FROM reflections WHERE id IN "
                            f"(SELECT id FROM reflections WHERE created_at < {cutoff} "
                            f"ORDER BY (helpful - harmful + support + uses) ASC, id ASC LIMIT ?)"),
        )
        evicted: dict[str, int] = {}
        batch = 256
        for label, sql in ladder:
            while self.disk_usage()["used_bytes"] > target:
                cur = self._db.execute(sql, (batch,))
                if not cur.rowcount:
                    break
                evicted[label] = evicted.get(label, 0) + cur.rowcount
                self._db.commit()
            if self.disk_usage()["used_bytes"] <= target:
                break
        if evicted:
            self._insight_cache = None
            try:
                self._db.execute("VACUUM")  # return freed pages to the OS
            except sqlite3.OperationalError:
                pass  # locked; freed pages still get reused by future inserts
        return evicted

    # ---- meta (daemon-published state) --------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def add_doc(self, source: str, text: str, embedding: list[float] | None) -> None:
        self._db.execute(
            "INSERT INTO docs (source, text, embedding, created_at) VALUES (?,?,?,?)",
            (source[:300], text[:4000], json.dumps(embedding) if embedding else None, time.time()),
        )
        self._db.commit()

    def all_docs(self, limit: int = 4000) -> list[tuple[str, list[float] | None]]:
        rows = self._db.execute(
            "SELECT text, embedding FROM docs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r["text"] or "", json.loads(r["embedding"]) if r["embedding"] else None) for r in rows]

    def doc_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM docs").fetchone()[0])

    def area_stats(self) -> dict[str, dict[str, int]]:
        """Per-area self-play attempt/solve counts — the difficulty curriculum."""
        raw = self.get_meta("selfplay_areas", "")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    # Recency weight for the competence EMA: recent outcomes dominate so the map
    # tracks CURRENT skill (and its delta), not a slow lifetime average.
    _COMPETENCE_EMA_ALPHA = 0.30

    def record_area_outcome(self, area: str, solved: bool, score: float | None = None) -> None:
        """Record a GROUNDED outcome for an area (verified self-play, bench, or a
        real-task eval score). This is the ONLY input to the competence map — the
        model never rates its own competence, it is measured from what happened."""
        outcome = float(score) if score is not None else (1.0 if solved else 0.0)
        outcome = max(0.0, min(1.0, outcome))
        stats = self.area_stats()
        a = stats.setdefault(area, {"attempts": 0, "solves": 0})
        a["attempts"] += 1
        if solved:
            a["solves"] += 1
        prev = a.get("ema")
        alpha = self._COMPETENCE_EMA_ALPHA
        a["ema"] = outcome if prev is None else alpha * outcome + (1 - alpha) * float(prev)
        a["updated_at"] = time.time()
        self.set_meta("selfplay_areas", json.dumps(stats))

    def competence_map(self) -> dict[str, dict[str, float]]:
        """The self-model: per-area measured competence + uncertainty + a learning
        priority (weak & uncertain areas rank highest — the frontier worth pushing).
        Derived purely from grounded outcomes recorded via record_area_outcome."""
        stats = self.area_stats()
        out: dict[str, dict[str, float]] = {}
        for area, a in stats.items():
            n = int(a.get("attempts", 0) or 0)
            solves = int(a.get("solves", 0) or 0)
            ema = a.get("ema")
            comp = float(ema) if ema is not None else (solves / n if n else 0.5)
            # Beta(solves+1, fails+1) posterior std = how unsure we are.
            alpha, beta = solves + 1, (n - solves) + 1
            var = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
            unc = var ** 0.5
            # Priority: attack weak, uncertain areas; unseen areas are maximal.
            priority = 1.0 if n == 0 else round(0.6 * (1.0 - comp) + 0.4 * min(1.0, unc * 3.4), 4)
            out[area] = {
                "attempts": n, "solves": solves,
                "competence": round(comp, 4), "uncertainty": round(unc, 4),
                "priority": priority, "updated_at": float(a.get("updated_at", 0) or 0),
            }
        return out

    def memory_stats(self) -> dict[str, int]:
        def _n(sql: str) -> int:
            return int(self._db.execute(sql).fetchone()[0])
        return {
            "insights": _n("SELECT COUNT(*) FROM insights"),
            "reflections": _n("SELECT COUNT(*) FROM reflections"),
            "cases": _n("SELECT COUNT(*) FROM cases"),
            "styles": _n("SELECT COUNT(*) FROM styles"),
            "ideas": _n("SELECT COUNT(*) FROM ideas"),
        }

    # ---- ideas (discoveries generated during dreaming) ----------------------

    def add_idea(self, statement: str, elaboration: str, embedding: list[float] | None,
                 novelty: float, value: float, score: float, parents: list[str]) -> None:
        self._db.execute(
            "INSERT INTO ideas (statement, elaboration, embedding, novelty, value, score, parents, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (statement[:1000], (elaboration or "")[:4000], json.dumps(embedding) if embedding else None,
             novelty, value, score, json.dumps(parents)[:2000], time.time()),
        )
        self._db.commit()

    def all_ideas(self, include_dismissed: bool = True) -> list[tuple[int, str, list[float] | None, int]]:
        """(id, statement, embedding, rating) for novelty comparison and dedup."""
        where = "" if include_dismissed else "WHERE rating >= 0"
        rows = self._db.execute(
            f"SELECT id, statement, embedding, rating FROM ideas {where} ORDER BY id DESC LIMIT 4000"
        ).fetchall()
        return [(r["id"], r["statement"] or "", json.loads(r["embedding"]) if r["embedding"] else None, r["rating"])
                for r in rows]

    def list_ideas(self, limit: int = 30) -> list[sqlite3.Row]:
        # Liked ideas float to the top, dismissed sink; then by score.
        return self._db.execute(
            "SELECT id, statement, elaboration, novelty, value, score, parents, rating, created_at "
            "FROM ideas ORDER BY rating DESC, score DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def rate_idea(self, idea_id: int, rating: int) -> bool:
        """Human feedback: 1 liked, -1 dismissed, 0 neutral. Returns True if a row changed."""
        rating = max(-1, min(1, int(rating)))
        cur = self._db.execute("UPDATE ideas SET rating=? WHERE id=?", (rating, idea_id))
        self._db.commit()
        return cur.rowcount > 0

    def liked_idea_embeddings(self) -> list[list[float]]:
        """Embeddings of liked ideas — the operator's taste profile for steering."""
        rows = self._db.execute("SELECT embedding FROM ideas WHERE rating > 0 AND embedding IS NOT NULL").fetchall()
        return [json.loads(r["embedding"]) for r in rows]

    def cap_ideas(self, cap: int) -> int:
        """Keep the best `cap` ideas (liked first, then by score, then newest).
        cap <= 0 means unlimited (the disk budget governs instead)."""
        if cap <= 0:
            return 0
        cur = self._db.execute(
            "DELETE FROM ideas WHERE id NOT IN "
            "(SELECT id FROM ideas ORDER BY rating DESC, score DESC, id DESC LIMIT ?)",
            (cap,),
        )
        self._db.commit()
        return cur.rowcount

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

    def all_styles(self) -> list[sqlite3.Row]:
        """(id, kind, hint) for every evolved style across all kinds — read-only
        aggregation for the dashboard's styles panel (list_styles is per-kind)."""
        return self._db.execute("SELECT id, kind, hint FROM styles ORDER BY kind, id").fetchall()

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
