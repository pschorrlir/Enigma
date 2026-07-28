"""Local web dashboard: live queue, task submission, results, playbook.

Runs standalone (`enigma web`) against the same SQLite store the daemon
uses — start/stop of the daemon is independent. Stdlib http.server only.

The page is a single self-contained inline HTML/CSS/JS string (`_PAGE`): no
external CDNs, fonts, or JS libraries, so it works offline on localhost. Every
DB read goes through `_PerRequestStore` (sqlite is thread-affine; `_LOCK`
serializes access). All GET data endpoints are read-only; only POST /api/submit
mutates (it enqueues a task).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .memory import Store
from .task import TaskSpec

_LOCK = threading.Lock()  # serialize DB access across request threads


def _converse(cfg: Config, message: str, history: list) -> str:
    """Run one direct exchange with the entity. converse() is READ-ONLY over the
    store, so it runs on its own short-lived connection OUTSIDE the global _LOCK —
    otherwise the seconds-long model call would freeze the dashboard's polling."""
    import asyncio

    import httpx

    from .engine import Engine

    async def go() -> str:
        store = Store(cfg.db_path)
        try:
            async with httpx.AsyncClient() as http:
                return await Engine(cfg, store, http).converse(message, history)
        finally:
            store.close()

    return asyncio.run(go())


class _PerRequestStore:
    """ThreadingHTTPServer runs each request on its own thread, and sqlite3
    connections are thread-affine — open a short-lived Store per request."""

    def __init__(self, cfg: Config):
        self._cfg = cfg

    def __enter__(self) -> Store:
        _LOCK.acquire()
        self._store = Store(self._cfg.db_path)
        return self._store

    def __exit__(self, *exc) -> None:
        try:
            self._store.close()
        finally:
            _LOCK.release()


def _parse_topics(raw: str) -> list:
    """Decode the director's dream_topics meta (a JSON list of short strings)."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(t) for t in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _caps(cfg: Config) -> dict[str, int]:
    return {"reflections": cfg.reflections_max, "insights": cfg.insights_max,
            "cases": cfg.cases_max, "ideas": cfg.ideas_max}


def serve(cfg: Config, host: str, port: int) -> None:

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

        def _html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # always serve the current dashboard
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path == "/" or self.path.startswith("/index"):
                    self._html(_PAGE.encode())
                    return
                with _PerRequestStore(cfg) as store:
                    if self.path == "/api/overview":
                        pid = _daemon_pid(cfg)
                        disk = store.disk_usage()
                        self._json({
                            "daemon": pid,
                            "counts": store.counts(),
                            "memory": store.memory_stats(),
                            "memory_used_mb": round(disk["used_bytes"] / 1e6, 1),
                            "memory_file_mb": round(disk["file_bytes"] / 1e6, 1),
                            "memory_max_mb": cfg.memory_max_mb,
                            "models": list(cfg.local_models),
                            "utility_model": cfg.utility_model or (cfg.local_models[0] if cfg.local_models else ""),
                            "tools": cfg.tools_enabled,
                            "dream_enabled": cfg.dream_enabled,
                            "dream_idle_s": cfg.dream_idle_s,
                            "dream_state": store.get_meta("dream_state", "idle") if pid else "off",
                            "last_dream": store.get_meta("last_dream", ""),
                            "last_dream_at": store.get_meta("last_dream_at", ""),
                            "dream_topics": _parse_topics(store.get_meta("dream_topics", "")),
                            "dream_focus": store.get_meta("dream_focus", ""),
                            "persona": cfg.persona(),
                            "concurrency": cfg.concurrency,
                            "target_score": cfg.target_score,
                            "cloud_model": cfg.cloud_model,
                            "cloud_enabled": bool(cfg.anthropic_api_key),
                        })
                    elif self.path == "/api/config":
                        self._json({
                            "local_models": list(cfg.local_models),
                            "utility_model": cfg.utility_model or (cfg.local_models[0] if cfg.local_models else ""),
                            "tools_enabled": cfg.tools_enabled,
                            "dream_enabled": cfg.dream_enabled,
                            "dream_idle_s": cfg.dream_idle_s,
                            "target_score": cfg.target_score,
                            "max_iterations": cfg.max_iterations,
                            "concurrency": cfg.concurrency,
                            "caps": _caps(cfg),
                            "reflections_max": cfg.reflections_max,
                            "insights_max": cfg.insights_max,
                            "cases_max": cfg.cases_max,
                            "cloud_model": cfg.cloud_model,
                            "cloud_enabled": bool(cfg.anthropic_api_key),
                        })
                    elif self.path.startswith("/api/tasks"):
                        out = []
                        for r in store.list_tasks(50):
                            spec = json.loads(r["spec"])
                            res = json.loads(r["result"]) if r["result"] else {}
                            err = res.get("error")
                            out.append({
                                "id": r["id"], "status": r["status"], "source": r["source"],
                                "description": spec.get("description", "")[:180],
                                "kind": (spec.get("evaluator") or {}).get("kind", "llm_judge"),
                                "score": res.get("score"),
                                "iterations": res.get("iterations"),
                                "cloud_calls": res.get("cloud_calls"),
                                "elapsed_s": res.get("elapsed_s"),
                                "error": (err[:200] if isinstance(err, str) else None),
                                "target_score": spec.get("target_score"),
                                "created_at": r["created_at"],
                                "started_at": r["started_at"],
                                "finished_at": r["finished_at"],
                            })
                        self._json(out)
                    elif self.path.startswith("/api/task/"):
                        row = store.get_task(self.path.rsplit("/", 1)[1])
                        if row is None:
                            self._json({"error": "not found"}, 404)
                        else:
                            self._json({
                                "id": row["id"], "status": row["status"], "source": row["source"],
                                "spec": json.loads(row["spec"]),
                                "result": json.loads(row["result"]) if row["result"] else None,
                                "created_at": row["created_at"], "started_at": row["started_at"],
                                "finished_at": row["finished_at"],
                            })
                    elif self.path == "/api/insights":
                        self._json([
                            {"kind": r["kind"], "lesson": r["lesson"], "uses": r["uses"],
                             "helpful": r["helpful"], "harmful": r["harmful"], "created_at": r["created_at"]}
                            for r in store.list_insights(30)
                        ])
                    elif self.path == "/api/reflections":
                        self._json([
                            {"topic": r["topic"], "lesson": r["lesson"], "support": r["support"],
                             "uses": r["uses"], "created_at": r["created_at"]}
                            for r in store.list_reflections(30)
                        ])
                    elif self.path == "/api/ideas":
                        out = []
                        for r in store.list_ideas(40):
                            try:
                                parents = json.loads(r["parents"]) if r["parents"] else []
                            except (ValueError, TypeError):
                                parents = []
                            out.append({
                                "id": r["id"], "rating": r["rating"],
                                "statement": r["statement"], "elaboration": r["elaboration"],
                                "novelty": r["novelty"], "value": r["value"], "score": r["score"],
                                "parents": parents if isinstance(parents, list) else [],
                                "created_at": r["created_at"],
                            })
                        self._json(out)
                    elif self.path == "/api/cases":
                        self._json([
                            {"id": r["id"], "kind": r["kind"] or "unknown",
                             "description": (r["description"] or "")[:200],
                             "score": r["score"], "created_at": r["created_at"]}
                            for r in store.dashboard_cases(400)
                        ])
                    elif self.path == "/api/styles":
                        grouped: dict[str, list] = {}
                        for r in store.all_styles():
                            grouped.setdefault(r["kind"] or "unknown", []).append(
                                {"id": r["id"], "hint": r["hint"]})
                        self._json(grouped)
                    elif self.path == "/api/episodes":
                        agg: dict[str, list] = {}
                        for r in store.episode_climb():
                            agg.setdefault(r["kind"] or "unknown", []).append(
                                {"iteration": r["iteration"], "mean_score": r["mean_score"], "n": r["n"]})
                        self._json(agg)
                    elif self.path == "/api/competence":
                        cm = store.competence_map()
                        areas = [{"area": a, **m} for a, m in cm.items()]
                        seen = [x for x in areas if x["attempts"] > 0]
                        mean = round(sum(x["competence"] for x in seen) / len(seen), 4) if seen else None
                        self._json({
                            "areas": areas,
                            "measured": len(seen),
                            "mean": mean,
                            "topics": _parse_topics(store.get_meta("dream_topics", "")),
                        })
                    else:
                        self._json({"error": "not found"}, 404)
            except Exception as e:  # keep the dashboard alive on any handler bug
                try:
                    self._json({"error": str(e)}, 500)
                except Exception:
                    pass

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/api/submit":
                    task = TaskSpec.from_json(raw.decode())
                    with _PerRequestStore(cfg) as store:
                        task_id = store.enqueue(task.id, task.to_json())
                    self._json({"id": task_id})
                elif self.path == "/api/focus":
                    body = json.loads(raw.decode() or "{}")
                    focus = str(body.get("focus", "")).strip()
                    with _PerRequestStore(cfg) as store:
                        store.set_meta("dream_focus", focus)
                    self._json({"ok": True, "focus": focus})
                elif self.path == "/api/idea/rate":
                    body = json.loads(raw.decode() or "{}")
                    if body.get("id") is None:
                        raise ValueError("idea id is required")
                    idea_id = int(body["id"])
                    rating = max(-1, min(1, int(body.get("rating", 0))))
                    with _PerRequestStore(cfg) as store:
                        store.rate_idea(idea_id, rating)
                    self._json({"ok": True, "rating": rating})
                elif self.path == "/api/ask":
                    body = json.loads(raw.decode() or "{}")
                    message = str(body.get("message", "")).strip()
                    if not message:
                        raise ValueError("message is required")
                    raw_hist = body.get("history") or []
                    history = [
                        {"role": str(h.get("role", "user")), "content": str(h.get("content", ""))[:4000]}
                        for h in raw_hist if isinstance(h, dict) and h.get("content")
                    ][-8:]
                    # Runs OUTSIDE _PerRequestStore: converse is read-only and slow,
                    # so it must not hold the global lock (would stall polling).
                    answer = _converse(cfg, message, history)
                    self._json({"answer": answer})
                else:
                    self._json({"error": "not found"}, 404)
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"enigma dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _daemon_pid(cfg: Config) -> int | None:
    import os

    try:
        pid = int(cfg.pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enigma — vitals</title>
<style>
:root{
  --plane:#0a0e13; --surface:#10161f; --surface-2:#161d28; --line:#222c3a; --line-soft:#1a2230;
  --ink:#e7edf5; --ink-dim:#93a3b7; --ink-mute:#5d6b7e;
  --live:#37d29f; --work:#f0a94c; --dream:#a98bef; --offline:#e5595a;
  --good:#35c26b; --warn:#e6b23e; --serious:#ec835a; --critical:#e5595a;
  --user:#46c6e6; --dreamseries:#a98bef; --seq:#3987e5; --seq-soft:#1c5cab;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box;margin:0}
html,body{overflow-x:hidden}
body{background:var(--plane);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1440px;margin:0 auto;padding:24px}
@media(max-width:700px){.wrap{padding:16px}}
.mono{font-family:var(--mono)}
.tnum{font-variant-numeric:tabular-nums}
.eyebrow{font-family:var(--mono);font-size:.68rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.18em;color:var(--ink-mute)}
.ptitle{font-family:var(--mono);font-size:.78rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.12em;color:var(--ink-dim)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.panel-hd{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}

/* ---- HERO / VITALS RIBBON ---- */
.hero{position:relative;border:1px solid var(--line);border-radius:12px;overflow:hidden;
  background:linear-gradient(180deg,#0c121b,#0a0e13);box-shadow:inset 0 1px 0 rgba(255,255,255,.02);
  transition:opacity .4s,filter .4s}
.hero.stale{opacity:.6;filter:saturate(.4)}
#ribbon{display:block;width:100%;height:120px}
@media(max-width:700px){#ribbon{height:72px}}
.hero-body{display:flex;flex-wrap:wrap;gap:16px 24px;align-items:flex-end;justify-content:space-between;
  padding:16px 20px 18px;border-top:1px solid var(--line-soft)}
.hero-left{min-width:min(360px,100%)}
.state-word{font-family:var(--mono);font-size:2.1rem;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;line-height:1.05}
.state-sub{font-family:var(--mono);font-size:.74rem;color:var(--ink-dim);margin-top:4px}
.dream-line{color:var(--dream);font-size:.86rem;margin-top:8px;max-width:60ch;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.topics{font-family:var(--mono);font-size:.74rem;color:var(--dream);margin-top:8px;
  max-width:64ch;line-height:1.4}
.topics .tk{opacity:.85} .topics .lb{color:var(--ink-mute)}
.focus{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px}
.focus-cur{font-family:var(--mono);font-size:.74rem;color:var(--ink-dim)}
.focus-cur b{color:var(--dream);font-weight:600}
.focus-in{width:auto;flex:1 1 160px;min-width:120px;max-width:280px;font-family:var(--mono);
  font-size:.74rem;padding:6px 10px}
.focus-in:focus{border-color:var(--dream)}
.focus-btn{padding:6px 12px;font-size:.72rem}
.focus-btn:hover{border-color:var(--dream);color:var(--dream)}
.hero-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px}
.queue{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
.qchip{font-family:var(--mono);font-size:.72rem;font-variant-numeric:tabular-nums;
  padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--ink-dim);white-space:nowrap}
.qchip b{font-weight:600;color:var(--ink)}
.pip{position:absolute;top:12px;right:14px;display:flex;align-items:center;gap:7px;
  font-family:var(--mono);font-size:.68rem;letter-spacing:.02em;color:var(--ink-mute);z-index:2}
.pip .dot{width:8px;height:8px;border-radius:50%;background:var(--live);
  box-shadow:0 0 8px rgba(55,210,159,.6)}
.pip.fail .dot{background:var(--offline);box-shadow:0 0 8px rgba(229,89,90,.6)}
.pip.fail{color:var(--offline)}
.btn{font-family:var(--mono);font-size:.76rem;font-weight:600;letter-spacing:.02em;cursor:pointer;
  border-radius:8px;padding:9px 15px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink)}
.btn:hover{border-color:var(--ink-mute)}
.btn.primary{border-color:var(--live);color:var(--live);background:transparent}
.btn.primary:hover{background:rgba(55,210,159,.08)}
.btn.ghost{background:transparent}
.btn:disabled,.btn[aria-disabled=true]{opacity:.4;cursor:not-allowed;border-style:dashed}
.hero-actions{display:flex;gap:8px}
.dbadge{font-family:var(--mono);font-size:.7rem;padding:3px 10px;border-radius:999px;
  border:1px solid var(--dream);color:var(--dream);display:inline-flex;align-items:center;gap:6px}
.dbadge.active .d{width:7px;height:7px;border-radius:50%;background:var(--dream);animation:pulse 1.3s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media(prefers-reduced-motion:reduce){.dbadge.active .d{animation:none}}

/* ---- SIGNAL STRIP ---- */
.tiles{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px}
.tile{flex:1 1 180px;min-width:150px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.tile .val{font-family:var(--mono);font-size:1.7rem;font-weight:600;line-height:1.1;margin:6px 0 4px}
.tile .sub{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim);font-variant-numeric:tabular-nums}
@media(max-width:700px){.tile{flex:1 1 44%}}

/* ---- MAIN GRID ---- */
.main{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-top:16px}
.trio{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-top:16px}
@media(max-width:1099px){.main{grid-template-columns:1fr}.trio{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.trio{grid-template-columns:1fr}}
.stack{display:flex;flex-direction:column;gap:16px}

/* chips / filters */
.chips{display:flex;flex-wrap:wrap;gap:6px}
.fchip{font-family:var(--mono);font-size:.72rem;padding:4px 11px;border-radius:999px;cursor:pointer;
  border:1px solid var(--line);background:transparent;color:var(--ink-dim)}
.fchip:hover{border-color:var(--ink-mute)}
.fchip.on{color:var(--ink);border-color:var(--ink-dim);background:var(--surface-2)}
.legend{display:flex;gap:14px;font-family:var(--mono);font-size:.7rem;color:var(--ink-dim);align-items:center}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend .sw{width:9px;height:9px;border-radius:50%}
.verdict{font-family:var(--mono);font-size:.74rem;font-weight:600;letter-spacing:.02em}
.trend-good{color:var(--good)} .trend-hold{color:var(--ink-dim)} .trend-bad{color:var(--critical)}
.trend-warm{color:var(--ink-mute)}

svg.chart{display:block;width:100%;height:auto}
.axtick{font-family:var(--mono);font-size:10px;fill:var(--ink-mute)}
.empty{color:var(--ink-dim);font-size:.86rem;padding:14px 4px}
.chart-wrap{position:relative}
.tt{position:absolute;pointer-events:none;background:var(--surface-2);border:1px solid var(--line);
  border-radius:8px;padding:6px 9px;font-family:var(--mono);font-size:.7rem;color:var(--ink);
  white-space:nowrap;transform:translate(-50%,-115%);opacity:0;transition:opacity .1s;z-index:5}

/* discoveries */
.disco{margin-top:16px;border:1px solid var(--line);border-radius:12px;padding:16px 18px;
  background:linear-gradient(180deg,rgba(169,139,239,.07),var(--surface));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.disco-hd{display:flex;align-items:baseline;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.disco-hd .wm{font-family:var(--mono);font-size:.94rem;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dream)}
.disco-hd .ds{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim)}
.idea-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:1099px){.idea-grid{grid-template-columns:1fr}}
.idea{border:1px solid var(--line);border-left:3px solid var(--dream);border-radius:10px;
  padding:12px 14px;background:var(--surface);cursor:pointer;transition:border-color .15s}
.idea:hover{border-color:var(--ink-mute);border-left-color:var(--dream)}
.idea .stmt{color:var(--ink);font-size:.95rem;line-height:1.45}
.scorebar{height:6px;background:var(--surface-2);border-radius:4px;overflow:hidden;margin:9px 0 0}
.scorebar span{display:block;height:100%;background:var(--dream);border-radius:4px}
.idea .nums{display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap}
.ichip{font-family:var(--mono);font-size:.68rem;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
  color:var(--ink-dim);font-variant-numeric:tabular-nums}
.ichip.dreamt{color:var(--dream);border-color:rgba(169,139,239,.5)}
.idea .exp{font-family:var(--mono);font-size:.64rem;color:var(--ink-mute)}
.idea .elab{color:var(--ink-dim);font-size:.84rem;line-height:1.5;margin-top:9px;
  max-height:0;overflow:hidden;transition:max-height .25s;padding-top:0}
.idea.open .elab{max-height:400px}
.idea .spark{font-family:var(--mono);font-size:.68rem;color:var(--ink-mute);margin-top:9px}
.idea.liked{border-left-color:var(--good);background:linear-gradient(180deg,rgba(53,194,107,.07),var(--surface))}
.idea.dismissed{opacity:.5}
.idea.dismissed .stmt{text-decoration:line-through;text-decoration-color:var(--ink-mute)}
.rate{display:inline-flex;gap:5px}
.th{cursor:pointer;background:transparent;border:1px solid var(--line);border-radius:999px;
  padding:2px 8px;font-size:.74rem;line-height:1.1;filter:grayscale(.7);opacity:.65;transition:all .12s}
.th:hover{border-color:var(--ink-mute);filter:none;opacity:1}
.th.up.on{border-color:var(--good);background:rgba(53,194,107,.14);filter:none;opacity:1}
.th.down.on{border-color:var(--critical);background:rgba(229,89,90,.14);filter:none;opacity:1}
.disco-hint{font-family:var(--mono);font-size:.66rem;color:var(--ink-mute);margin-top:12px}
.meter .fill.ideafill{background:var(--dream)}

/* self-model / competence */
.selfmodel{margin-top:16px}
.sm-sum{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.sm-sum b{color:var(--ink);font-weight:600}
.sm-row{display:grid;grid-template-columns:minmax(120px,168px) 1fr 44px;gap:12px;align-items:center;
  padding:7px 0;border-top:1px solid var(--line-soft)}
.sm-row:first-child{border-top:0}
.sm-name{font-size:.86rem;color:var(--ink);display:flex;align-items:center;gap:7px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sm-fr{width:6px;height:6px;border-radius:50%;background:var(--dream);flex:none;
  box-shadow:0 0 6px rgba(169,139,239,.5)}
.sm-bararea{display:flex;flex-direction:column;gap:3px;min-width:0}
.sm-track{height:9px;background:var(--surface-2);border-radius:4px;overflow:hidden}
.sm-fill{height:100%;border-radius:4px}
.sm-cap{font-family:var(--mono);font-size:.64rem;color:var(--ink-mute);font-variant-numeric:tabular-nums}
.sm-comp{font-family:var(--mono);font-size:.82rem;font-weight:600;text-align:right;
  font-variant-numeric:tabular-nums;color:var(--ink-dim)}
.sm-unexplored{font-family:var(--mono);font-size:.68rem;color:var(--ink-mute);margin-top:11px}
@media(max-width:700px){.sm-row{grid-template-columns:minmax(96px,1fr) 2fr 40px}}

/* memory meters */
.meter{margin:10px 0}
.meter .mlab{display:flex;justify-content:space-between;align-items:baseline;font-family:var(--mono);
  font-size:.72rem;color:var(--ink-dim);font-variant-numeric:tabular-nums;margin-bottom:5px}
.meter .track{height:9px;background:var(--surface-2);border-radius:4px;overflow:hidden}
.meter .fill{height:100%;border-radius:4px;background:var(--seq)}
.near{color:var(--warn)}.over{color:var(--serious)}
.segbar{display:flex;height:16px;border-radius:4px;overflow:hidden;background:var(--surface-2);margin:8px 0}
.segbar i{display:block;height:100%;border-right:2px solid var(--surface)}
.kindchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.caserow{font-size:.8rem;color:var(--ink-dim);padding:5px 0;border-top:1px solid var(--line-soft);
  display:flex;gap:8px;align-items:baseline}
.caserow .cd{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.caserow .cs{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink-mute)}

/* tasks table */
.tsearch{font-family:var(--mono);font-size:.74rem;background:var(--surface-2);color:var(--ink);
  border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:150px}
.ttable{width:100%;overflow-x:auto}
.trow{display:grid;grid-template-columns:78px 58px 1fr 96px 52px 42px 60px 56px;gap:10px;align-items:center;
  padding:8px 4px;border-top:1px solid var(--line-soft);cursor:pointer;position:relative}
.trow:hover{background:var(--surface-2)}
.trow .desc{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
.trow .m{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim);font-variant-numeric:tabular-nums}
.trow.run{border-left:2px solid var(--work)}
@media(prefers-reduced-motion:no-preference){
  .trow.run{animation:runpulse 1.8s ease-in-out infinite}
  .flash{animation:flash 1.2s ease-out}
}
@keyframes runpulse{0%,100%{border-left-color:var(--work)}50%{border-left-color:rgba(240,169,76,.3)}}
@keyframes flash{0%{background:rgba(70,198,230,.18)}100%{background:transparent}}
.srule{width:3px;align-self:stretch;border-radius:2px;position:absolute;left:0;top:6px;bottom:6px}
.chip{font-family:var(--mono);font-size:.68rem;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
  color:var(--ink-dim);white-space:nowrap;text-align:center;justify-self:start}
.chip.succeeded{color:var(--good);border-color:rgba(53,194,107,.5)}
.chip.running{color:var(--work);border-color:rgba(240,169,76,.5)}
.chip.failed,.chip.exhausted{color:var(--serious);border-color:rgba(236,131,90,.5)}
.chip.queued{color:var(--ink-dim)}
.chip.dream{color:var(--dream);border-color:rgba(169,139,239,.5)}
.chip.user{color:var(--user);border-color:rgba(70,198,230,.4)}
@media(max-width:700px){
  .trow{grid-template-columns:70px 1fr 52px 56px;grid-auto-flow:row}
  .trow .hidesm{display:none}
}

/* reflections / insights / styles */
.lrow{padding:10px 0;border-top:1px solid var(--line-soft)}
.lrow:first-child{border-top:0}
.lrow .lesson{color:var(--ink);font-size:.9rem}
.lrow .meta{font-family:var(--mono);font-size:.7rem;color:var(--ink-mute);margin-top:4px;
  font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.lrow.dreammade{border-left:2px solid var(--dream);padding-left:10px}
.divbar{display:inline-flex;width:64px;height:8px;background:var(--surface-2);border-radius:4px;overflow:hidden;
  align-items:stretch}
.divbar .h{background:var(--good)} .divbar .x{background:var(--critical)}
.tag{font-family:var(--mono);font-size:.64rem;padding:1px 6px;border-radius:999px;border:1px solid var(--critical);
  color:var(--critical)}
.dotc{width:7px;height:7px;border-radius:50%;background:var(--critical);display:inline-block}
.stylegrp{padding:10px 0;border-top:1px solid var(--line-soft)}
.stylegrp:first-child{border-top:0}
.styleitem{display:flex;gap:8px;align-items:baseline;padding:4px 0}
.styleitem .idc{font-family:var(--mono);font-size:.64rem;color:var(--ink-mute);border:1px solid var(--line);
  border-radius:999px;padding:1px 6px;flex:none}
.styleitem .h{color:var(--ink-dim);font-size:.85rem}

/* slide-overs */
.backdrop{position:fixed;inset:0;background:rgba(4,7,11,.6);opacity:0;pointer-events:none;
  transition:opacity .25s;z-index:40}
.backdrop.open{opacity:1;pointer-events:auto}
.sheet{position:fixed;top:0;right:0;height:100%;width:min(560px,100%);background:var(--surface);
  border-left:1px solid var(--line);transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);
  z-index:50;display:flex;flex-direction:column;box-shadow:-16px 0 40px rgba(0,0,0,.4)}
.sheet.open{transform:none}
@media(max-width:700px){.sheet{width:100%}}
.sheet-hd{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 18px;
  border-bottom:1px solid var(--line)}
.sheet-body{padding:18px;overflow-y:auto;flex:1}
.x{cursor:pointer;color:var(--ink-dim);border:1px solid var(--line);border-radius:8px;background:var(--surface-2);
  width:30px;height:30px;font-size:1rem;line-height:1}
.x:hover{color:var(--ink)}
label{display:block;font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--ink-dim);margin:14px 0 5px}
input,textarea,select{width:100%;background:var(--surface-2);color:var(--ink);border:1px solid var(--line);
  border-radius:8px;padding:9px 11px;font-family:var(--sans);font-size:.9rem}
textarea{resize:vertical;font-family:var(--mono);font-size:.82rem}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--seq)}
.adv{border-top:1px solid var(--line-soft);margin-top:16px;padding-top:6px}
.adv summary{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim);cursor:pointer;padding:8px 0}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.err{color:var(--critical);font-size:.82rem;margin-top:12px;min-height:1.1rem}
.metric-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 14px}
.metric{font-family:var(--mono);font-size:.72rem;color:var(--ink-dim);border:1px solid var(--line);
  border-radius:8px;padding:5px 9px;font-variant-numeric:tabular-nums}
.metric b{color:var(--ink);font-weight:600}
pre{background:var(--plane);border:1px solid var(--line);border-radius:8px;padding:11px;overflow-x:auto;
  font-family:var(--mono);font-size:.78rem;white-space:pre-wrap;word-break:break-word;color:var(--ink-dim);margin:6px 0}
pre.errblk{border-color:var(--critical);color:var(--critical)}
.sublab{font-family:var(--mono);font-size:.68rem;text-transform:uppercase;letter-spacing:.1em;
  color:var(--ink-mute);margin:14px 0 4px}
.foot{font-size:.76rem;color:var(--ink-mute);margin-top:16px;font-style:italic}
.copy{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}

/* chat / ask */
#ask .sheet-body{display:flex;flex-direction:column;overflow:hidden;padding:0}
.ask-log{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.ask-empty{color:var(--ink-mute);font-family:var(--mono);font-size:.82rem;font-style:italic;
  text-align:center;margin:auto 8px;line-height:1.6}
.msg{max-width:82%;padding:9px 12px;border-radius:12px;font-size:.9rem;line-height:1.5;
  white-space:pre-wrap;word-break:break-word}
.msg .who{font-family:var(--mono);font-size:.6rem;text-transform:uppercase;letter-spacing:.12em;
  color:var(--ink-mute);margin-bottom:4px}
.msg.user{align-self:flex-end;background:var(--surface-2);border:1px solid var(--line);color:var(--ink);
  border-bottom-right-radius:4px}
.msg.entity{align-self:flex-start;color:var(--ink);border:1px solid var(--line);border-left:3px solid var(--dream);
  background:linear-gradient(180deg,rgba(169,139,239,.08),var(--surface));border-bottom-left-radius:4px}
.msg.entity .who{color:var(--dream)}
.msg.err{align-self:flex-start;background:transparent;border:1px solid var(--critical);color:var(--critical);
  font-family:var(--mono);font-size:.78rem;white-space:pre-wrap}
.ask-think{align-self:flex-start;display:flex;align-items:center;gap:9px;font-family:var(--mono);
  font-size:.74rem;color:var(--dream)}
.ask-think .dots{display:inline-flex;gap:4px}
.ask-think .dots i{width:6px;height:6px;border-radius:50%;background:var(--dream);opacity:.4}
@media(prefers-reduced-motion:no-preference){
  .ask-think .dots i{animation:thinkblink 1.1s ease-in-out infinite}
  .ask-think .dots i:nth-child(2){animation-delay:.18s}
  .ask-think .dots i:nth-child(3){animation-delay:.36s}}
@keyframes thinkblink{0%,100%{opacity:.25}50%{opacity:1}}
.ask-inputrow{display:flex;gap:8px;align-items:flex-end;padding:12px 16px;border-top:1px solid var(--line);
  background:var(--surface)}
.ask-inputrow textarea{flex:1;resize:none;font-family:var(--sans);font-size:.9rem}
.ask-inputrow .btn{flex:none;height:38px}
.ask-inputrow textarea:disabled,.ask-inputrow .btn:disabled{opacity:.5;cursor:not-allowed}

/* toast */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(20px);opacity:0;
  background:var(--surface-2);border:1px solid var(--live);color:var(--ink);font-family:var(--mono);
  font-size:.78rem;padding:10px 16px;border-radius:10px;z-index:60;transition:opacity .25s,transform .25s;
  pointer-events:none}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.skel{background:linear-gradient(90deg,var(--surface-2),var(--line),var(--surface-2));
  background-size:200% 100%;border-radius:6px;color:transparent}
@media(prefers-reduced-motion:no-preference){.skel{animation:sk 1.4s linear infinite}}
@keyframes sk{0%{background-position:200% 0}100%{background-position:-200% 0}}
.stale-tag{font-family:var(--mono);font-size:.62rem;color:var(--warn);border:1px solid var(--warn);
  border-radius:999px;padding:0 6px;margin-left:6px}
</style></head><body>
<div class="wrap">

  <!-- A · HERO -->
  <header class="hero" id="hero" aria-live="polite">
    <div class="pip" id="pip"><span class="dot"></span><span id="piptext">connecting…</span></div>
    <canvas id="ribbon"></canvas>
    <div class="hero-body">
      <div class="hero-left">
        <div class="state-word" id="stateWord" style="color:var(--ink-mute)">CONNECTING</div>
        <div class="state-sub" id="stateSub">reading vitals…</div>
        <div class="dream-line" id="lastDream"></div>
        <div class="topics" id="dreamTopics"></div>
        <div class="focus" id="focusCtrl">
          <span class="focus-cur" id="focusCur">🎯 focus: —</span>
          <input class="focus-in" id="focusInput" placeholder="steer the next dream…" aria-label="dream focus">
          <button class="btn ghost focus-btn" id="focusSet">Set</button>
          <button class="btn ghost focus-btn" id="focusClear" title="Clear focus">Clear</button>
        </div>
      </div>
      <div class="hero-right">
        <div class="hero-actions">
          <button class="btn" id="dreamNow" aria-disabled="true"
            title="Requires a daemon dream-request hook (not built)">Dream now</button>
          <button class="btn" id="askBtn" title="Talk to the entity directly">💬 Ask</button>
          <button class="btn primary" id="newTaskBtn">+ New task</button>
        </div>
        <div class="queue" id="queue"></div>
      </div>
    </div>
  </header>

  <!-- B · SIGNAL STRIP -->
  <div class="tiles" id="tiles">
    <div class="tile"><div class="eyebrow">Success rate</div><div class="val skel">00%</div><div class="sub skel">—</div></div>
    <div class="tile"><div class="eyebrow">Learning</div><div class="val skel">—</div><div class="sub skel">—</div></div>
    <div class="tile"><div class="eyebrow">Throughput</div><div class="val skel">—</div><div class="sub skel">—</div></div>
    <div class="tile"><div class="eyebrow">Memory</div><div class="val skel">—</div><div class="sub skel">—</div></div>
    <div class="tile"><div class="eyebrow">Dream</div><div class="val skel">—</div><div class="sub skel">—</div></div>
  </div>

  <!-- DISCOVERIES · the engine's headline output -->
  <section class="disco" id="disco">
    <div class="disco-hd">
      <span class="wm">Discoveries</span>
      <span class="ds">insights from dreaming — coherent connections across memory, gated on value over novelty</span>
      <span style="flex:1"></span>
      <span class="ds" id="ideaCount"></span>
    </div>
    <div class="idea-grid" id="ideas"><div class="empty">Loading…</div></div>
    <div class="disco-hint" id="ideaHint"></div>
  </section>

  <!-- SELF-MODEL · grounded competence map -->
  <section class="panel selfmodel" id="selfmodel">
    <div class="panel-hd" style="flex-wrap:wrap">
      <div><div class="eyebrow">Self-model</div>
        <div class="ptitle" style="margin-top:2px">Competence frontier — measured, not self-rated</div></div>
      <span class="sm-sum" id="smSum"></span>
    </div>
    <div id="smRows"><div class="empty">Loading…</div></div>
  </section>

  <!-- C | D -->
  <div class="main">
    <div class="stack">
      <section class="panel">
        <div class="panel-hd">
          <div><div class="eyebrow">Learning</div><div class="ptitle" style="margin-top:2px">Score over time</div></div>
          <div class="verdict" id="c1verdict">—</div>
        </div>
        <div class="chips" style="margin-bottom:8px">
          <button class="fchip on" data-src="all">all</button>
          <button class="fchip" data-src="user">user</button>
          <button class="fchip" data-src="dream">dream</button>
          <span style="flex:1"></span>
          <span class="legend"><span><span class="sw" style="background:var(--user)"></span>user</span>
            <span><span class="sw" style="background:var(--dreamseries)"></span>dream</span>
            <span><span class="sw" style="width:14px;height:2px;border-radius:2px;background:var(--ink-dim)"></span>trend</span></span>
        </div>
        <div class="chart-wrap" id="c1wrap"><div class="empty">Loading…</div></div>
      </section>
      <section class="panel">
        <div class="panel-hd">
          <div><div class="eyebrow">Learning</div><div class="ptitle" style="margin-top:2px">Iteration climb — does iterating help?</div></div>
        </div>
        <div class="chips" id="climbKinds" style="margin-bottom:8px"></div>
        <div class="chart-wrap" id="c2wrap"><div class="empty">Loading…</div></div>
      </section>
    </div>
    <section class="panel">
      <div class="eyebrow">Memory health</div>
      <div class="ptitle" style="margin:2px 0 10px">Tiers vs caps</div>
      <div id="meters"><div class="empty">Loading…</div></div>
      <div class="ptitle" style="margin:18px 0 4px">Case bank by kind</div>
      <div id="caseBank"><div class="empty">Loading…</div></div>
    </section>
  </div>

  <!-- E · TASKS -->
  <section class="panel" style="margin-top:16px">
    <div class="panel-hd" style="flex-wrap:wrap">
      <div class="ptitle">Tasks</div>
      <div class="chips" style="align-items:center">
        <button class="fchip on" data-tsrc="all">all</button>
        <button class="fchip" data-tsrc="user">user</button>
        <button class="fchip" data-tsrc="dream">dream</button>
        <span style="width:1px;height:16px;background:var(--line);margin:0 4px"></span>
        <button class="fchip on" data-tstat="all">all</button>
        <button class="fchip" data-tstat="running">running</button>
        <button class="fchip" data-tstat="succeeded">succeeded</button>
        <button class="fchip" data-tstat="needswork">needs-work</button>
        <input class="tsearch" id="tsearch" placeholder="search description / kind / id">
      </div>
    </div>
    <div class="ttable" id="tasks"><div class="empty">Loading…</div></div>
  </section>

  <!-- G | H | I -->
  <div class="trio">
    <section class="panel">
      <div class="eyebrow">Principles</div><div class="ptitle" style="margin:2px 0 6px">Reflections</div>
      <div id="reflections"><div class="empty">Loading…</div></div>
    </section>
    <section class="panel">
      <div class="eyebrow">Learned</div><div class="ptitle" style="margin:2px 0 6px">Playbook · insights</div>
      <div id="insights"><div class="empty">Loading…</div></div>
    </section>
    <section class="panel">
      <div class="eyebrow">Bandit</div><div class="ptitle" style="margin:2px 0 6px">Evolved styles</div>
      <div id="styles"><div class="empty">Loading…</div></div>
    </section>
  </div>

  <p class="foot">Enigma · self-learning task engine — monitoring dashboard, read-only except New task.</p>
  <p class="foot" id="personaLine" style="margin-top:4px;color:var(--ink-mute)"></p>
</div>

<!-- F · DETAIL -->
<div class="backdrop" id="backdrop"></div>
<aside class="sheet" id="detail" aria-hidden="true">
  <div class="sheet-hd"><div class="ptitle" id="detailTitle">Task</div><button class="x" data-close>✕</button></div>
  <div class="sheet-body" id="detailBody"></div>
</aside>

<!-- J · SUBMIT -->
<aside class="sheet" id="submit" aria-hidden="true">
  <div class="sheet-hd"><div class="ptitle">New task</div><button class="x" data-close>✕</button></div>
  <div class="sheet-body">
    <label>Description</label>
    <textarea id="desc" rows="3" placeholder="What should the engine do?"></textarea>
    <label>Input <span style="text-transform:none;letter-spacing:0;color:var(--ink-mute)">(optional · text or JSON)</span></label>
    <textarea id="input" rows="2"></textarea>
    <div class="row2">
      <div><label>Output kind</label>
        <select id="okind"><option>text</option><option>json</option><option>code</option></select></div>
      <div><label>Evaluator</label>
        <select id="ekind">
          <option value="llm_judge">llm_judge</option>
          <option value="python_tests">python_tests</option>
          <option value="json_schema">json_schema</option>
          <option value="regex">regex</option>
          <option value="contains">contains</option>
          <option value="prm">prm</option>
        </select></div>
    </div>
    <label id="evlabel">Criteria</label>
    <textarea id="evval" rows="3" placeholder="correctness, completeness, clarity"></textarea>
    <details class="adv"><summary>Advanced</summary>
      <div class="row2">
        <div><label>Target score</label><input id="tscore" type="number" step="0.01" min="0" max="1" placeholder="default"></div>
        <div><label>Max iterations</label><input id="maxit" type="number" step="1" min="1" placeholder="default"></div>
      </div>
    </details>
    <button class="btn primary" id="submitBtn" style="margin-top:16px;width:100%">Queue task</button>
    <div class="err" id="suberr"></div>
  </div>
</aside>

<!-- K · ASK / CHAT -->
<aside class="sheet" id="ask" aria-hidden="true">
  <div class="sheet-hd"><div class="ptitle">Ask Enigma</div><button class="x" data-close>✕</button></div>
  <div class="sheet-body">
    <div class="ask-log" id="askLog"></div>
    <div class="ask-inputrow">
      <textarea id="askInput" rows="2" placeholder="Ask the entity anything…" aria-label="message to the entity"></textarea>
      <button class="btn primary" id="askSend">Send</button>
    </div>
  </div>
</aside>

<div class="toast" id="toast"></div>

<script>
"use strict";
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const RM=matchMedia("(prefers-reduced-motion:reduce)").matches;
function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function num(x,d){return (typeof x==="number"&&isFinite(x))?x.toFixed(d):"—";}
function rel(ts){
  if(!ts) return "";
  const s=Math.max(0,(Date.now()/1000)-Number(ts));
  if(s<45) return Math.round(s)+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago";
  if(s<86400) return Math.round(s/3600)+"h ago";
  return Math.round(s/86400)+"d ago";
}
function elapsed(t){
  if(typeof t.elapsed_s==="number") return t.elapsed_s.toFixed(0)+"s";
  if(t.status==="running"&&t.started_at) return Math.max(0,Math.round(Date.now()/1000-t.started_at))+"s";
  return "—";
}

/* ---------- shared state ---------- */
const S={overview:null,config:null,tasks:[],cases:[],ideas:[],styles:{},episodes:{},insights:[],reflections:[],
  competence:null,chat:[],
  srcFilter:"all",tStat:"all",tSearch:"",climbKind:null,lastPoll:0,fail:false,prevStatus:{},asking:false};

/* ---------- fetch scheduler (tiers, no stacking) ---------- */
async function getJSON(u){const r=await fetch(u);if(!r.ok){let m="http "+r.status;try{m=(await r.json()).error||m}catch(e){}throw new Error(m);}return r.json();}
let busyFast=false,busySlow=false,busyCold=false;
async function tickFast(){
  if(busyFast||document.hidden) return; busyFast=true;
  try{
    const [ov,ts]=await Promise.all([getJSON("/api/overview"),getJSON("/api/tasks")]);
    S.overview=ov; S.tasks=ts; S.lastPoll=Date.now(); pollOK();
    renderHero(); renderTiles(); renderScore(); renderTasks();
  }catch(e){pollFail();}
  finally{busyFast=false;}
}
async function tickSlow(){
  if(busySlow||document.hidden) return; busySlow=true;
  try{
    const [cs,id,cm]=await Promise.all([getJSON("/api/cases"),getJSON("/api/ideas"),getJSON("/api/competence")]);
    S.cases=cs; S.ideas=id; S.competence=cm; renderMemory(); renderTiles(); renderIdeas(); renderSelfModel();
  }catch(e){}
  finally{busySlow=false;}
}
async function tickCold(){
  if(busyCold||document.hidden) return; busyCold=true;
  try{
    const [cfg,ep,st,ins,rf]=await Promise.all([
      getJSON("/api/config"),getJSON("/api/episodes"),getJSON("/api/styles"),
      getJSON("/api/insights"),getJSON("/api/reflections")]);
    S.config=cfg; S.episodes=ep; S.styles=st; S.insights=ins; S.reflections=rf;
    renderClimb(); renderStyles(); renderInsights(); renderReflections(); renderScore(); renderMemory();
  }catch(e){}
  finally{busyCold=false;}
}
function pollOK(){S.fail=false;$("#hero").classList.remove("stale");$("#pip").classList.remove("fail");}
function pollFail(){S.fail=true;$("#hero").classList.add("stale");const p=$("#pip");p.classList.add("fail");$("#piptext").textContent="reconnecting…";}
setInterval(()=>{ // pip counts up between polls
  if(S.fail) return;
  const s=Math.round((Date.now()-S.lastPoll)/1000);
  $("#piptext").textContent=S.lastPoll?("updated "+s+"s ago"):"connecting…";
},1000);

/* ---------- A · HERO ---------- */
function deriveState(o){
  if(!o||o.daemon==null) return "offline";
  if(o.dream_state==="dreaming") return "dreaming";
  if((o.counts&&o.counts.running)>0) return "working";
  return "idle";
}
const STCOLOR={offline:"--offline",idle:"--live",working:"--work",dreaming:"--dream"};
const STWORD={offline:"OFFLINE",idle:"IDLE",working:"WORKING",dreaming:"DREAMING"};
function renderHero(){
  const o=S.overview||{}, c=o.counts||{}, st=deriveState(o);
  const col="var("+STCOLOR[st]+")";
  const sw=$("#stateWord"); sw.textContent=STWORD[st]; sw.style.color=col;
  let sub = o.daemon!=null ? ("daemon pid "+o.daemon+" · up") : "daemon down";
  if(o.dream_enabled){
    sub += st==="dreaming" ? '  ·  <span class="dbadge active"><span class="d"></span>DREAMING</span>'
      : '  ·  <span class="dbadge">dreams armed · idle '+(o.dream_idle_s||0)+'s</span>';
  }
  if(o.tools) sub+='  ·  tools';
  if(o.cloud_enabled&&o.cloud_model) sub+="  ·  "+esc(o.cloud_model);
  $("#stateSub").innerHTML=sub;
  const q=(k,cls)=>'<span class="qchip">'+k+' <b class="'+(cls||"")+'">'+(c[k.toLowerCase()]||c[k]||0)+'</b></span>';
  $("#queue").innerHTML=
    '<span class="qchip">queued <b>'+(c.queued||0)+'</b></span>'+
    '<span class="qchip">running <b style="color:var(--work)">'+(c.running||0)+'</b></span>'+
    '<span class="qchip">done <b style="color:var(--good)">'+(c.succeeded||0)+'</b></span>'+
    '<span class="qchip">exhausted <b style="color:var(--serious)">'+(c.exhausted||0)+'</b></span>'+
    '<span class="qchip">failed <b style="color:var(--critical)">'+(c.failed||0)+'</b></span>';
  const ld=$("#lastDream");
  if(o.last_dream){
    const when=o.last_dream_at?(", "+rel(o.last_dream_at)):"";
    ld.textContent="🌙 last dream"+when+" — "+o.last_dream;
  } else ld.textContent="";
  const topics=o.dream_topics||[];
  $("#dreamTopics").innerHTML=topics.length
    ? '<span class="lb">exploring:</span> '+topics.map(t=>'<span class="tk">'+esc(t)+'</span>').join(' · ')
    : '';
  const fc=o.dream_focus||"";
  $("#focusCur").innerHTML='🎯 focus: '+(fc?'<b>'+esc(fc)+'</b>':'—');
  const fi=$("#focusInput");
  if(document.activeElement!==fi) fi.value=fc; // don't clobber while the operator is typing
  const pel=$("#personaLine"), pz=(o.persona||"").trim();
  if(pel){
    if(pz){const first=(pz.match(/^[^.!?]*[.!?]/)||[pz])[0].trim(); pel.textContent="“"+first+"”";}
    else pel.textContent="";
  }
}
async function setFocus(val){
  try{
    const r=await fetch("/api/focus",{method:"POST",body:JSON.stringify({focus:val})});
    const j=await r.json();
    if(!r.ok){toast(j.error||"Couldn't set focus.");return;}
    toast(j.focus?("Focus set — "+j.focus):"Focus cleared");
    tickFast();
  }catch(e){toast("Couldn't reach the engine.");}
}

/* ---- Vitals ribbon (canvas, state-driven) ---- */
const cv=$("#ribbon"), cx=cv.getContext("2d");
let dpr=1,W=0,H=0,buf=null,curAmp=0,tgtAmp=0,curRGB=[93,107,126],raf=0,t0=performance.now();
function hexRGB(v){v=v.replace("#","");return [parseInt(v.slice(0,2),16),parseInt(v.slice(2,4),16),parseInt(v.slice(4,6),16)];}
const RGB={offline:hexRGB("e5595a"),idle:hexRGB("37d29f"),working:hexRGB("f0a94c"),dreaming:hexRGB("a98bef")};
function sizeRibbon(){
  dpr=Math.min(2,window.devicePixelRatio||1);
  W=cv.clientWidth; H=cv.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr; cx.setTransform(dpr,0,0,dpr,0,0);
  const n=Math.max(2,Math.floor(W));
  buf=new Float32Array(n);
}
function sampleFor(st,ts){ // ts seconds; returns -1..1 shape (pre-amplitude)
  if(st==="offline") return 0;
  if(st==="dreaming") return Math.sin(ts*0.9)*0.55 + Math.sin(ts*0.37)*0.18;
  if(st==="idle"){ // calm heartbeat blip every ~2s
    const p=(ts%2)/2, d=p<0.5?(p-0.06):999;
    const blip=Math.exp(-Math.pow((p-0.06)/0.02,2))*0.9 - Math.exp(-Math.pow((p-0.11)/0.02,2))*0.35;
    return blip + Math.sin(ts*6)*0.015;
  }
  // working: sharper faster spikes
  return Math.sin(ts*7.5)*0.5 + Math.sin(ts*13.3)*0.28 + Math.sin(ts*3.1)*0.16;
}
function drawGuides(){
  cx.clearRect(0,0,W,H);
  cx.strokeStyle="rgba(34,44,58,.7)"; cx.lineWidth=1;
  [0.25,0.5,0.75].forEach(f=>{const y=H*f;cx.beginPath();cx.moveTo(0,y+0.5);cx.lineTo(W,y+0.5);cx.stroke();});
}
function drawTrace(){
  const [r,g,b]=curRGB.map(Math.round);
  cx.strokeStyle="rgb("+r+","+g+","+b+")"; cx.lineWidth=2; cx.lineJoin="round"; cx.lineCap="round";
  cx.beginPath();
  const mid=H*0.5;
  for(let i=0;i<buf.length;i++){const x=i, y=mid - buf[i]*(H*0.5-6);
    if(i===0) cx.moveTo(x,y); else cx.lineTo(x,y);}
  cx.stroke();
}
function frame(now){
  const st=deriveState(S.overview);
  const o=S.overview||{}, conc=(o.concurrency||S.config&&S.config.concurrency)||1;
  tgtAmp = st==="offline"?0 : st==="idle"?0.55 : st==="dreaming"?0.7
    : Math.min(1, 0.5 + 0.5*Math.min(1,(o.counts&&o.counts.running||1)/conc));
  const tRGB=RGB[st];
  for(let k=0;k<3;k++) curRGB[k]+=(tRGB[k]-curRGB[k])*0.06;
  curAmp+=(tgtAmp-curAmp)*0.06;
  const ts=(now-t0)/1000;
  // scroll buffer left, push newest sample
  for(let i=0;i<buf.length-1;i++) buf[i]=buf[i+1];
  buf[buf.length-1]=sampleFor(st,ts)*curAmp;
  drawGuides(); drawTrace();
  if(S.fail){cx.fillStyle="rgba(10,14,19,.4)";cx.fillRect(0,0,W,H);}
  raf=requestAnimationFrame(frame);
}
function staticRibbon(){ // reduced-motion: one representative cycle, no loop
  sizeRibbon(); drawGuides();
  const st=deriveState(S.overview); curRGB=RGB[st].slice();
  const amp=st==="offline"?0:0.6;
  for(let i=0;i<buf.length;i++){buf[i]=sampleFor(st,(i/buf.length)*4)*amp;}
  drawTrace();
}
function startRibbon(){
  sizeRibbon();
  if(RM){staticRibbon();return;}
  cancelAnimationFrame(raf); raf=requestAnimationFrame(frame);
}
window.addEventListener("resize",()=>{sizeRibbon(); if(RM) staticRibbon();});
document.addEventListener("visibilitychange",()=>{
  if(document.hidden){cancelAnimationFrame(raf);}
  else if(!RM){raf=requestAnimationFrame(frame);}
});

/* ---------- B · SIGNAL STRIP ---------- */
function finishedScores(src){ // {t, scores in finish order} for source filter
  return S.tasks.filter(t=>t.finished_at&&typeof t.score==="number"&&(src==="all"||t.source===src))
    .slice().sort((a,b)=>a.finished_at-b.finished_at);
}
function trendVerdict(){
  const pts=finishedScores("all").map(t=>t.score);
  if(pts.length<6) return {word:"→ WARMING UP",cls:"trend-warm",sub:pts.length+" finished · need 6"};
  const k=Math.floor(pts.length/3), recent=pts.slice(-k), prior=pts.slice(0,pts.length-k);
  const mean=a=>a.reduce((x,y)=>x+y,0)/a.length;
  const d=mean(recent)-mean(prior);
  if(d>0.02) return {word:"↑ IMPROVING",cls:"trend-good",sub:"+"+d.toFixed(2)+" avg, last "+k};
  if(d<-0.02) return {word:"↓ SLIPPING",cls:"trend-bad",sub:d.toFixed(2)+" avg, last "+k};
  return {word:"→ HOLDING",cls:"trend-hold",sub:d.toFixed(2)+" avg, last "+k};
}
function renderTiles(){
  const o=S.overview||{}, c=o.counts||{}, m=o.memory||{};
  const done=c.succeeded||0, bad=(c.exhausted||0)+(c.failed||0), denom=done+bad;
  const rate=denom?Math.round(done/denom*100):0;
  const seg=(v,col)=>denom?'<i style="flex:'+v+';background:'+col+'"></i>':'';
  const rateBar='<div class="segbar" style="height:6px;margin-top:8px">'+
    seg(done,"var(--good)")+seg(c.exhausted||0,"var(--serious)")+seg(c.failed||0,"var(--critical)")+'</div>';
  const tv=trendVerdict();

  // throughput: finishes in last hour + 12x5min sparkline
  const now=Date.now()/1000, fin=S.tasks.filter(t=>t.finished_at);
  const lastHr=fin.filter(t=>now-t.finished_at<=3600).length;
  const buckets=new Array(12).fill(0);
  fin.forEach(t=>{const age=now-t.finished_at; if(age<=3600){const b=11-Math.floor(age/300); if(b>=0&&b<12)buckets[b]++;}});
  const mx=Math.max(1,...buckets);
  const spark='<svg class="chart" viewBox="0 0 96 26" style="margin-top:8px;width:96px" preserveAspectRatio="none" aria-hidden="true">'+
    buckets.map((v,i)=>{const h=v/mx*22,x=i*8; return '<rect x="'+x+'" y="'+(24-h)+'" width="6" height="'+Math.max(1,h)+'" rx="1.5" fill="var(--seq)"/>';}).join("")+'</svg>';

  // memory pressure: disk budget used
  const mUsed=o.memory_used_mb||0, mMax=o.memory_max_mb||0;
  const pct=mMax?Math.round(100*mUsed/mMax):0;
  const pcol=pct>=90?"var(--serious)":pct>=70?"var(--warn)":"var(--good)";
  const pnote=mUsed+" / "+mMax+" MB on disk";

  // dream tile
  const dstate=o.dream_state==="dreaming"?'<span style="color:var(--dream)">● dreaming</span>':(o.dream_state==="off"?"off":"idle");
  const dsince=o.last_dream_at?rel(o.last_dream_at)+" since last":(o.dream_enabled?"no dream yet":"disabled");

  $("#tiles").innerHTML=
    tile("Success rate",rate+"%",done+" done · "+bad+" needs-work",rateBar)+
    tile("Learning",'<span class="'+tv.cls+'">'+tv.word+'</span>',tv.sub,"")+
    tile("Throughput",lastHr+' <span style="font-size:.9rem;color:var(--ink-dim)">/hr</span>',"finishes, last hour",spark)+
    tile("Memory",'<span style="color:'+pcol+'">'+pct+'%</span>',pnote,"")+
    tile("Dream",dstate,dsince,"");
}
function tile(eb,val,sub,extra){
  return '<div class="tile"><div class="eyebrow">'+eb+'</div><div class="val">'+val+'</div>'+
    '<div class="sub">'+sub+'</div>'+(extra||"")+'</div>';
}

/* ---------- C1 · score over time ---------- */
function renderScore(){
  const wrap=$("#c1wrap");
  const tv=trendVerdict(); const vd=$("#c1verdict"); vd.className="verdict "+tv.cls; vd.textContent=tv.word;
  const target=(S.config&&S.config.target_score)!=null?S.config.target_score:(S.overview&&S.overview.target_score)||0.95;
  const show=S.srcFilter;
  const all=finishedScores("all");
  if(!all.length){wrap.innerHTML='<div class="empty">No finished tasks yet. Submit one, or let it dream.</div>';return;}
  const pts=all.filter(t=>show==="all"||t.source===show);
  const Wc=640,Hc=240,PL=34,PR=12,PT=12,PB=22;
  const x=i=>PL+(pts.length<=1?0.5:(i/(pts.length-1)))*(Wc-PL-PR);
  const y=v=>PT+(1-v)*(Hc-PT-PB);
  let g="";
  [0,.25,.5,.75,1].forEach(f=>{const yy=y(f);g+='<line x1="'+PL+'" y1="'+yy+'" x2="'+(Wc-PR)+'" y2="'+yy+'" stroke="var(--line)" stroke-width="1"/>'+
    '<text class="axtick" x="'+(PL-6)+'" y="'+(yy+3)+'" text-anchor="end">'+f.toFixed(2)+'</text>';});
  g+='<line x1="'+PL+'" y1="'+y(target)+'" x2="'+(Wc-PR)+'" y2="'+y(target)+'" stroke="var(--ink-mute)" stroke-width="1" stroke-dasharray="4 4"/>'+
    '<text class="axtick" x="'+(Wc-PR)+'" y="'+(y(target)-4)+'" text-anchor="end" style="fill:var(--ink-dim)">target '+target+'</text>';
  // moving average trend over shown points
  if(pts.length>=3){
    const win=Math.max(3,Math.floor(pts.length/6)); let path="";
    for(let i=0;i<pts.length;i++){let s=0,n=0;for(let j=Math.max(0,i-win);j<=Math.min(pts.length-1,i+win);j++){s+=pts[j].score;n++;}
      path+=(i?"L":"M")+x(i).toFixed(1)+" "+y(s/n).toFixed(1)+" ";}
    g+='<path d="'+path+'" fill="none" stroke="var(--ink-dim)" stroke-width="2"/>';
  }
  const CO={user:"var(--user)",dream:"var(--dreamseries)"};
  let dots="", meta=[];
  pts.forEach((t,i)=>{const cxp=x(i),cyp=y(t.score),col=CO[t.source]||"var(--ink-dim)";
    dots+='<circle cx="'+cxp.toFixed(1)+'" cy="'+cyp.toFixed(1)+'" r="4" fill="'+col+'" stroke="var(--surface)" stroke-width="2"/>';
    meta.push({x:cxp,y:cyp,t});});
  wrap.innerHTML='<svg class="chart" id="c1svg" viewBox="0 0 '+Wc+' '+Hc+'" role="img">'+g+dots+
    '<line id="c1cross" x1="0" y1="'+PT+'" x2="0" y2="'+(Hc-PB)+'" stroke="var(--ink-mute)" stroke-width="1" opacity="0"/></svg>'+
    '<div class="tt" id="c1tt"></div>';
  const svg=$("#c1svg"),tt=$("#c1tt"),cross=$("#c1cross");
  svg.addEventListener("mousemove",e=>{
    const r=svg.getBoundingClientRect(),mx=(e.clientX-r.left)/r.width*Wc;
    let best=null,bd=1e9; meta.forEach(m=>{const d=Math.abs(m.x-mx);if(d<bd){bd=d;best=m;}});
    if(!best||bd>40){tt.style.opacity=0;cross.setAttribute("opacity","0");return;}
    cross.setAttribute("x1",best.x);cross.setAttribute("x2",best.x);cross.setAttribute("opacity",".5");
    tt.style.opacity=1; tt.style.left=(best.x/Wc*100)+"%"; tt.style.top=(best.y/Hc*100)+"%";
    tt.innerHTML=esc(best.t.id)+' · '+esc(best.t.kind)+' · '+best.t.score.toFixed(2)+' · '+best.t.source+' · '+rel(best.t.finished_at);
  });
  svg.addEventListener("mouseleave",()=>{tt.style.opacity=0;cross.setAttribute("opacity","0");});
}

/* ---------- C2 · iteration climb ---------- */
function renderClimb(){
  const kinds=Object.keys(S.episodes||{}).filter(k=>(S.episodes[k]||[]).length);
  const box=$("#climbKinds");
  if(!kinds.length){box.innerHTML="";$("#c2wrap").innerHTML='<div class="empty">Not enough local episodes yet.</div>';return;}
  if(!S.climbKind||!kinds.includes(S.climbKind)) S.climbKind=kinds[0];
  box.innerHTML=kinds.map(k=>'<button class="fchip'+(k===S.climbKind?" on":"")+'" data-kind="'+k+'">'+k+'</button>').join("");
  box.querySelectorAll("[data-kind]").forEach(b=>b.onclick=()=>{S.climbKind=b.dataset.kind;renderClimb();});
  const rows=(S.episodes[S.climbKind]||[]).slice().sort((a,b)=>a.iteration-b.iteration);
  const wrap=$("#c2wrap");
  if(rows.length<2){wrap.innerHTML='<div class="empty">Not enough local episodes for '+esc(S.climbKind)+' yet.</div>';return;}
  const Wc=640,Hc=150,PL=34,PR=12,PT=12,PB=22;
  const maxIt=Math.max(...rows.map(r=>r.iteration));
  const x=it=>PL+(maxIt?it/maxIt:0)*(Wc-PL-PR);
  const y=v=>PT+(1-v)*(Hc-PT-PB);
  let g="";
  [0,.5,1].forEach(f=>{const yy=y(f);g+='<line x1="'+PL+'" y1="'+yy+'" x2="'+(Wc-PR)+'" y2="'+yy+'" stroke="var(--line)" stroke-width="1"/>'+
    '<text class="axtick" x="'+(PL-6)+'" y="'+(yy+3)+'" text-anchor="end">'+f.toFixed(1)+'</text>';});
  rows.forEach(r=>{g+='<text class="axtick" x="'+x(r.iteration)+'" y="'+(Hc-6)+'" text-anchor="middle">'+r.iteration+'</text>';});
  let path="",area="",dots="",meta=[];
  rows.forEach((r,i)=>{const xp=x(r.iteration),yp=y(r.mean_score);path+=(i?"L":"M")+xp.toFixed(1)+" "+yp.toFixed(1)+" ";
    area+=(i?"L":"M"+xp.toFixed(1)+" "+y(0)+" L")+xp.toFixed(1)+" "+yp.toFixed(1)+" ";});
  area+="L"+x(rows[rows.length-1].iteration)+" "+y(0)+" Z";
  g+='<path d="'+area+'" fill="var(--seq)" opacity=".1"/>'+'<path d="'+path+'" fill="none" stroke="var(--seq)" stroke-width="2"/>';
  rows.forEach(r=>{const xp=x(r.iteration),yp=y(r.mean_score),rad=Math.min(7,3+Math.sqrt(r.n));
    dots+='<circle cx="'+xp.toFixed(1)+'" cy="'+yp.toFixed(1)+'" r="'+rad.toFixed(1)+'" fill="var(--seq)" stroke="var(--surface)" stroke-width="2"/>';
    meta.push({x:xp,y:yp,r});});
  wrap.innerHTML='<svg class="chart" id="c2svg" viewBox="0 0 '+Wc+' '+Hc+'" role="img">'+g+dots+'</svg><div class="tt" id="c2tt"></div>';
  const svg=$("#c2svg"),tt=$("#c2tt");
  svg.addEventListener("mousemove",e=>{const rc=svg.getBoundingClientRect(),mx=(e.clientX-rc.left)/rc.width*Wc,my=(e.clientY-rc.top)/rc.height*Hc;
    let best=null,bd=1e9;meta.forEach(m=>{const d=Math.hypot(m.x-mx,m.y-my);if(d<bd){bd=d;best=m;}});
    if(!best||bd>30){tt.style.opacity=0;return;}
    tt.style.opacity=1;tt.style.left=(best.x/Wc*100)+"%";tt.style.top=(best.y/Hc*100)+"%";
    tt.innerHTML='iter '+best.r.iteration+' · mean '+best.r.mean_score.toFixed(2)+' · n='+best.r.n;});
  svg.addEventListener("mouseleave",()=>{tt.style.opacity=0;});
}

/* ---------- D · memory health ---------- */
function renderMemory(){
  const o=S.overview||{}, m=o.memory||{};
  const used=o.memory_used_mb||0, max=o.memory_max_mb||0;
  const pct=max?Math.min(100,used/max*100):0;
  const fillc=pct>=90?"var(--serious)":pct>=70?"var(--warn)":"var(--seq)";
  const noteCls=pct>=90?"over":pct>=70?"near":"";
  const note=pct>=70?' <span class="'+noteCls+'">⚠ near budget</span>':'';
  const tchip=(k,v)=>'<span class="qchip">'+k+' <b>'+(v||0)+'</b></span>';
  $("#meters").innerHTML=
    '<div class="meter"><div class="mlab"><span>DISK BUDGET'+note+'</span><span>'+used+' / '+max+' MB</span></div>'+
      '<div class="track"><div class="fill" style="width:'+pct.toFixed(0)+'%;background:'+fillc+'"></div></div></div>'+
    '<div class="kindchips" style="margin-top:10px">'+
      tchip("reflections",m.reflections)+tchip("insights",m.insights)+tchip("cases",m.cases)+
      tchip("ideas",m.ideas)+tchip("styles",m.styles)+'</div>'+
    '<div style="font-family:var(--mono);font-size:.66rem;color:var(--ink-mute);margin-top:8px">'+
      'memory grows until disk, then evicts weakest-first'+
      (o.memory_file_mb?' · '+o.memory_file_mb+' MB on disk':'')+'</div>';

  const box=$("#caseBank");
  if(!S.cases.length){box.innerHTML='<div class="empty">No cases banked yet — the engine hasn\\'t solved enough to remember.</div>';return;}
  const byk={}; S.cases.forEach(c=>{byk[c.kind]=(byk[c.kind]||0)+1;});
  const kinds=Object.entries(byk).sort((a,b)=>b[1]-a[1]);
  const total=S.cases.length;
  const KC=["var(--user)","var(--dreamseries)","var(--seq)","var(--warn)","var(--good)","var(--serious)"];
  const seg=kinds.map(([k,n],i)=>'<i style="flex:'+n+';background:'+KC[i%KC.length]+'" title="'+esc(k)+' '+n+'"></i>').join("");
  const chips=kinds.map(([k,n],i)=>'<span class="qchip"><span class="sw" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+KC[i%KC.length]+';margin-right:5px"></span>'+esc(k)+' <b>'+n+'</b></span>').join("");
  const newest=S.cases.slice(0,6).map(c=>'<div class="caserow"><span class="cd">'+esc(c.description)+'</span><span class="cs">'+num(c.score,2)+'</span></div>').join("");
  box.innerHTML='<div class="segbar">'+seg+'</div><div class="kindchips">'+chips+'</div>'+
    '<div style="font-family:var(--mono);font-size:.68rem;color:var(--ink-mute);margin:8px 0 2px">'+kinds.length+' kinds across '+total+' cases · newest:</div>'+newest;
}

/* ---------- DISCOVERIES · ideas ---------- */
function renderIdeas(){
  const box=$("#ideas"), cnt=$("#ideaCount"), hint=$("#ideaHint");
  if(!S.ideas.length){cnt.textContent="";hint.textContent="";box.innerHTML='<div class="empty">No ideas yet — the engine discovers them while dreaming.</div>';return;}
  cnt.textContent=S.ideas.length+" ranked liked-first";
  hint.innerHTML='👍 teaches the director what to explore · 👎 steers it away — ratings shape future dreams';
  box.innerHTML=S.ideas.map((it,i)=>{
    const sc=+it.score||0, nv=+it.novelty||0, vl=+it.value||0, rt=+it.rating||0;
    const par=(it.parents||[]).map(esc).join(" + ");
    const cls=rt===1?" liked":rt===-1?" dismissed":"";
    return '<div class="idea'+cls+'" data-id="'+it.id+'">'+
      '<div class="stmt">'+esc(it.statement)+'</div>'+
      '<div class="scorebar"><span style="width:'+(sc*100).toFixed(0)+'%"></span></div>'+
      '<div class="nums"><span class="ichip dreamt">score '+sc.toFixed(2)+'</span>'+
        '<span class="ichip">novelty '+nv.toFixed(2)+'</span>'+
        '<span class="ichip">value '+vl.toFixed(2)+'</span>'+
        '<span class="rate" data-id="'+it.id+'">'+
          '<button class="th up'+(rt===1?" on":"")+'" data-r="1" aria-label="Like this idea" title="👍 teaches the director what to explore">👍</button>'+
          '<button class="th down'+(rt===-1?" on":"")+'" data-r="-1" aria-label="Dismiss this idea" title="👎 steers the director away">👎</button>'+
        '</span>'+
        '<span style="flex:1"></span>'+
        (it.elaboration?'<span class="exp">details ▾</span>':'')+'</div>'+
      (it.elaboration?'<div class="elab">'+esc(it.elaboration)+'</div>':'')+
      (par?'<div class="spark">sparked by: '+par+'</div>':'')+'</div>';
  }).join("");
  box.querySelectorAll(".idea").forEach(c=>c.onclick=e=>{
    if(e.target.closest(".rate")) return; // thumbs handle their own clicks
    const open=c.classList.toggle("open");
    const ex=c.querySelector(".exp"); if(ex) ex.textContent=open?"details ▴":"details ▾";
  });
  box.querySelectorAll(".th").forEach(b=>b.onclick=e=>{
    e.stopPropagation();
    const id=+b.closest(".rate").dataset.id, want=+b.dataset.r;
    const idea=S.ideas.find(x=>x.id===id), cur=idea?(+idea.rating||0):0;
    rateIdea(id, cur===want?0:want);
  });
}
async function rateIdea(id,rating){
  try{
    const r=await fetch("/api/idea/rate",{method:"POST",body:JSON.stringify({id:id,rating:rating})});
    const j=await r.json();
    if(!r.ok){toast(j.error||"Couldn't rate this idea.");return;}
    const idea=S.ideas.find(x=>x.id===id); if(idea) idea.rating=j.rating; // optimistic
    renderIdeas(); tickSlow(); // re-sort from the server (liked floats up)
  }catch(e){toast("Couldn't reach the engine.");}
}

/* ---------- SELF-MODEL · competence map ---------- */
function compColor(c){return c<0.5?"var(--serious)":c<0.8?"var(--warn)":"var(--good)";}
function renderSelfModel(){
  const cm=S.competence; if(!cm) return; // no data yet — leave last content
  const sum=$("#smSum"), box=$("#smRows");
  const areas=cm.areas||[];
  const measured=areas.filter(a=>(a.attempts||0)>0);
  if(!measured.length){
    sum.textContent="";
    box.innerHTML='<div class="empty">No measured competence yet — run <span class="mono">enigma bench --record</span> or self-play to fill in the self-model.</div>';
    return;
  }
  sum.innerHTML=(cm.mean!=null?'mean <b>'+cm.mean.toFixed(2)+'</b> · ':'')+'<b>'+cm.measured+'</b> area'+(cm.measured===1?'':'s');
  measured.sort((a,b)=>(a.competence||0)-(b.competence||0)); // weakest first — the frontier
  let rows=measured.map(a=>{
    const c=+a.competence||0, unc=+a.uncertainty||0, n=a.attempts||0;
    const frontier=(+a.priority||0)>=0.5;
    return '<div class="sm-row">'+
      '<div class="sm-name" title="'+esc(a.area)+'">'+(frontier?'<span class="sm-fr" title="frontier"></span>':'')+esc(a.area)+'</div>'+
      '<div class="sm-bararea">'+
        '<div class="sm-track"><div class="sm-fill" style="width:'+(c*100).toFixed(0)+'%;background:'+compColor(c)+'"></div></div>'+
        '<div class="sm-cap">n='+n+' ±'+unc.toFixed(2)+(frontier?' · frontier':'')+'</div>'+
      '</div>'+
      '<div class="sm-comp" style="color:'+compColor(c)+'">'+c.toFixed(2)+'</div></div>';
  }).join("");
  const unexplored=areas.filter(a=>(a.attempts||0)===0).map(a=>a.area);
  if(unexplored.length) rows+='<div class="sm-unexplored">unexplored: '+unexplored.map(esc).join(", ")+'</div>';
  box.innerHTML=rows;
}

/* ---------- E · tasks ---------- */
function matchStat(t){
  if(S.tStat==="all")return true;
  if(S.tStat==="needswork")return t.status==="exhausted"||t.status==="failed";
  return t.status===S.tStat;
}
function renderTasks(){
  const box=$("#tasks");
  const q=S.tSearch.toLowerCase();
  const rows=S.tasks.filter(t=>(S.srcFilter==="all"||t.source===S.srcFilter)&&matchStat(t)&&
    (!q||(t.description||"").toLowerCase().includes(q)||(t.kind||"").toLowerCase().includes(q)||t.id.toLowerCase().includes(q)));
  if(!rows.length){
    const idle=(S.overview&&S.overview.dream_idle_s)||60;
    box.innerHTML='<div class="empty">'+(S.tasks.length?"No tasks match this filter.":"Queue is empty. The engine will start dreaming after "+idle+"s idle.")+'</div>';
    return;
  }
  const target=(S.config&&S.config.target_score)||0.95;
  box.innerHTML=rows.map(t=>{
    const changed=S.prevStatus[t.id]&&S.prevStatus[t.id]!==t.status;
    const srule=typeof t.score==="number"?('<span class="srule" style="background:'+(t.score>=target?"var(--good)":t.score>=target*0.6?"var(--warn)":"var(--critical)")+'"></span>'):"";
    return '<div class="trow'+(t.status==="running"?" run":"")+(changed&&!RM?" flash":"")+'" data-id="'+esc(t.id)+'">'+srule+
      '<span class="chip '+t.status+'">'+t.status+'</span>'+
      '<span class="chip '+(t.source==="dream"?"dream":"user")+'">'+t.source+'</span>'+
      '<span class="desc">'+esc(t.description)+'</span>'+
      '<span class="m hidesm">'+esc(t.kind)+'</span>'+
      '<span class="m">'+num(t.score,2)+'</span>'+
      '<span class="m hidesm">'+(t.iterations!=null?t.iterations+"it":"—")+'</span>'+
      '<span class="m hidesm">'+elapsed(t)+'</span>'+
      '<span class="m">'+rel(t.finished_at||t.started_at||t.created_at)+'</span></div>';
  }).join("");
  box.querySelectorAll(".trow").forEach(r=>r.onclick=()=>openDetail(r.dataset.id));
  const ns={}; S.tasks.forEach(t=>ns[t.id]=t.status); S.prevStatus=ns;
}

/* ---------- G · reflections ---------- */
function renderReflections(){
  const box=$("#reflections");
  if(!S.reflections.length){box.innerHTML='<div class="empty">No principles yet. They form when dreaming consolidates the playbook.</div>';return;}
  const maxSup=Math.max(...S.reflections.map(r=>r.support||0));
  box.innerHTML=S.reflections.map(r=>{
    const strong=(r.support||0)>=Math.max(3,maxSup*0.7);
    return '<div class="lrow'+(strong?" dreammade":"")+'"><div class="lesson">'+esc(r.lesson)+'</div>'+
      '<div class="meta">'+(r.topic?esc(r.topic)+' · ':'')+'abstracts '+(r.support||0)+' insights · used '+(r.uses||0)+'×</div></div>';
  }).join("");
}

/* ---------- H · insights ---------- */
function renderInsights(){
  const box=$("#insights");
  if(!S.insights.length){box.innerHTML='<div class="empty">No insights distilled yet.</div>';return;}
  const rows=S.insights.slice().sort((a,b)=>((b.helpful-b.harmful)-(a.helpful-a.harmful)));
  box.innerHTML=rows.map(i=>{
    const net=(i.helpful||0)-(i.harmful||0), tot=Math.max(1,(i.helpful||0)+(i.harmful||0));
    const hp=(i.helpful||0)/tot*100, xp=(i.harmful||0)/tot*100;
    const bar='<span class="divbar"><span class="h" style="flex:'+hp+'"></span><span class="x" style="flex:'+xp+'"></span></span>';
    const neg=net<0?' <span class="dotc"></span><span class="tag">pruning candidate</span>':'';
    return '<div class="lrow"><div class="lesson">'+esc(i.lesson)+'</div>'+
      '<div class="meta">'+bar+esc(i.kind||"")+' · used '+(i.uses||0)+'× · net '+(net>=0?"+":"")+net+neg+'</div></div>';
  }).join("");
}

/* ---------- I · styles ---------- */
function renderStyles(){
  const box=$("#styles");
  const kinds=Object.keys(S.styles||{});
  if(!kinds.length){box.innerHTML='<div class="empty">No evolved styles yet — the engine is still using its built-in strategies.</div>';return;}
  box.innerHTML=kinds.map(k=>'<div class="stylegrp"><div class="eyebrow" style="margin-bottom:6px">'+esc(k)+'</div>'+
    S.styles[k].map(s=>'<div class="styleitem"><span class="idc">#'+s.id+'</span><span class="h">'+esc(s.hint)+'</span></div>').join("")+'</div>').join("");
}

/* ---------- F · detail slide-over ---------- */
function openSheet(id){const bd=$("#backdrop"),sh=$("#"+id);bd.classList.add("open");sh.classList.add("open");sh.setAttribute("aria-hidden","false");}
function closeSheets(){$("#backdrop").classList.remove("open");$$(".sheet").forEach(s=>{s.classList.remove("open");s.setAttribute("aria-hidden","true");});
  if(location.hash.startsWith("#task/")) history.replaceState(null,"","#");}
async function openDetail(id){
  openSheet("detail"); location.hash="task/"+id;
  const body=$("#detailBody"); body.innerHTML='<div class="empty">Loading…</div>';
  let t; try{t=await getJSON("/api/task/"+encodeURIComponent(id));}catch(e){body.innerHTML='<div class="err">Couldn\\'t load task — '+esc(e.message)+'</div>';return;}
  if(t.error){body.innerHTML='<div class="empty">Task not found.</div>';return;}
  const sp=t.spec||{}, res=t.result||{}, ev=sp.evaluator||{};
  $("#detailTitle").innerHTML='<span class="chip '+t.status+'">'+t.status+'</span> <span class="chip '+(t.source==="dream"?"dream":"user")+'">'+t.source+'</span>';
  const M=[];
  if(res.score!=null)M.push('score <b>'+num(res.score,2)+'</b>');
  if(res.iterations!=null)M.push('iters <b>'+res.iterations+'</b>');
  if(res.cloud_calls!=null)M.push('cloud <b>'+res.cloud_calls+'</b>');
  if(res.elapsed_s!=null)M.push('elapsed <b>'+num(res.elapsed_s,0)+'s</b>');
  if(res.origin)M.push('origin <b>'+esc(res.origin)+'</b>');
  const evField=ev.criteria||ev.tests||ev.pattern||(ev.all?ev.all.join(", "):null)||(ev.schema?JSON.stringify(ev.schema):null)||ev.aggregate;
  let html='<div style="font-family:var(--mono);font-size:.72rem;color:var(--ink-mute);margin-bottom:8px">'+
    '<span class="copy" data-copy="'+esc(t.id)+'">'+esc(t.id)+' ⧉</span></div>'+
    '<div class="metric-row">'+M.map(x=>'<span class="metric">'+x+'</span>').join("")+'</div>'+
    '<div class="sublab">Description</div><div style="font-size:.9rem">'+esc(sp.description)+'</div>';
  if(sp.input!=null&&sp.input!=="") html+='<div class="sublab">Input</div><pre>'+esc(typeof sp.input==="string"?sp.input:JSON.stringify(sp.input,null,2))+'</pre>';
  html+='<div class="sublab">Evaluator</div><div class="metric-row"><span class="metric">kind <b>'+esc(ev.kind||"llm_judge")+'</b></span>'+
    '<span class="metric">target <b>'+num(sp.target_score!=null?sp.target_score:(S.config&&S.config.target_score),2)+'</b></span>'+
    '<span class="metric">max iters <b>'+(sp.max_iterations!=null?sp.max_iterations:(S.config&&S.config.max_iterations||"—"))+'</b></span></div>';
  if(evField)html+='<pre>'+esc(evField)+'</pre>';
  if(res.output!=null)html+='<div class="sublab">Output</div><pre>'+esc(res.output)+'</pre>';
  if(res.feedback)html+='<div class="sublab">Evaluator feedback</div><div style="font-size:.86rem;color:var(--ink-dim)">'+esc(res.feedback)+'</div>';
  if(res.error)html+='<div class="sublab">Error</div><pre class="errblk">'+esc(res.error)+'</pre>';
  html+='<div class="sublab">Actions</div><div class="chips">'+
    '<button class="btn ghost" id="cloneBtn">Clone to new task</button>'+
    '<button class="btn ghost" data-copy="'+esc(t.id)+'">Copy id</button>'+
    '<button class="btn" aria-disabled="true" title="No safe store method backs cancel/retry (future).">Cancel</button>'+
    '<button class="btn" aria-disabled="true" title="No safe store method backs cancel/retry (future).">Retry</button></div>'+
    '<p class="foot">Per-iteration tool calls and tokens aren\\'t recorded yet.</p>';
  body.innerHTML=html;
  body.querySelectorAll("[data-copy]").forEach(b=>b.onclick=()=>{copy(b.dataset.copy);toast("Copied "+b.dataset.copy);});
  const cb=$("#cloneBtn"); if(cb) cb.onclick=()=>cloneToForm(sp);
}
function copy(txt){try{navigator.clipboard.writeText(txt);}catch(e){}}

/* ---------- J · submit ---------- */
const EVFIELDS={llm_judge:["Criteria","correctness, completeness, clarity"],
 python_tests:["Tests (assert lines)","assert solve(2)==4"],
 json_schema:["JSON schema",'{"type":"object","required":["name"]}'],
 regex:["Pattern","^\\\\d{4}-\\\\d{2}-\\\\d{2}$"],
 contains:["Required substrings (one per line)",""],
 prm:["Aggregate (min / mean / prod / last)","min"]};
function evfield(){const[l,p]=EVFIELDS[$("#ekind").value];$("#evlabel").textContent=l;$("#evval").placeholder=p;}
function cloneToForm(sp){
  const ev=sp.evaluator||{kind:"llm_judge"};
  $("#desc").value=sp.description||"";
  $("#input").value=sp.input==null?"":(typeof sp.input==="string"?sp.input:JSON.stringify(sp.input));
  $("#okind").value=(sp.output&&sp.output.kind)||"text";
  $("#ekind").value=ev.kind||"llm_judge"; evfield();
  $("#evval").value=ev.criteria||ev.tests||ev.pattern||(ev.all?ev.all.join("\\n"):"")||(ev.schema?JSON.stringify(ev.schema,null,2):"")||ev.aggregate||"";
  $("#tscore").value=sp.target_score!=null?sp.target_score:"";
  $("#maxit").value=sp.max_iterations!=null?sp.max_iterations:"";
  closeSheets(); openSheet("submit");
}
async function submitTask(){
  const err=$("#suberr"); err.textContent="";
  const kind=$("#ekind").value, v=$("#evval").value.trim(), ev={kind};
  if(kind==="llm_judge"&&v)ev.criteria=v;
  if(kind==="python_tests")ev.tests=v;
  if(kind==="regex")ev.pattern=v;
  if(kind==="contains")ev.all=v.split("\\n").map(x=>x.trim()).filter(Boolean);
  if(kind==="prm"&&v)ev.aggregate=v;
  if(kind==="json_schema"){try{ev.schema=JSON.parse(v||"{}");}catch(e){err.textContent="Schema isn't valid JSON — check the braces.";return;}}
  let input=$("#input").value.trim()||null;
  if(input){try{input=JSON.parse(input);}catch(e){/* keep as plain string */}}
  const body={description:$("#desc").value.trim(),input,output:{kind:$("#okind").value},evaluator:ev};
  if(!body.description){err.textContent="Description is required — tell the engine what to do.";return;}
  const ts=$("#tscore").value.trim(), mi=$("#maxit").value.trim();
  if(ts){const n=parseFloat(ts); if(isNaN(n)||n<0||n>1){err.textContent="Target score must be between 0 and 1.";return;} body.target_score=n;}
  if(mi){const n=parseInt(mi,10); if(isNaN(n)||n<1){err.textContent="Max iterations must be a positive whole number.";return;} body.max_iterations=n;}
  let r,j;
  try{r=await fetch("/api/submit",{method:"POST",body:JSON.stringify(body)});j=await r.json();}
  catch(e){err.textContent="Couldn't reach the engine — is it running?";return;}
  if(!r.ok){err.textContent=j.error||"Submit failed.";return;}
  closeSheets(); $("#desc").value=""; $("#input").value=""; $("#evval").value="";
  toast("Task queued — "+j.id); S.highlight=j.id; tickFast();
}
let highlightTimer=0;

/* ---------- K · ask / chat ---------- */
function renderChat(){
  const log=$("#askLog");
  let html="";
  if(!S.chat.length && !S.asking){
    html='<div class="ask-empty">Ask me what I\\'m working on, what I\\'m bad at, or anything else.</div>';
  } else {
    html=S.chat.map(m=>{
      if(m.role==="error") return '<div class="msg err">'+esc(m.content)+'</div>';
      const who=m.role==="user"?"you":"enigma";
      const cls=m.role==="user"?"user":"entity";
      return '<div class="msg '+cls+'"><div class="who">'+who+'</div>'+esc(m.content)+'</div>';
    }).join("");
    if(S.asking) html+='<div class="ask-think"><span>thinking</span>'+
      '<span class="dots"><i></i><i></i><i></i></span></div>';
  }
  log.innerHTML=html;
  log.scrollTop=log.scrollHeight; // pin to newest
}
async function askSend(){
  if(S.asking) return;
  const input=$("#askInput"), msg=input.value.trim();
  if(!msg) return;
  S.chat.push({role:"user",content:msg});
  input.value="";
  S.asking=true; setAskBusy(true); renderChat();
  // last ~8 turns as history (server also clamps to 8)
  const history=S.chat.filter(m=>m.role==="user"||m.role==="assistant").slice(-8)
    .map(m=>({role:m.role,content:m.content}));
  try{
    const r=await fetch("/api/ask",{method:"POST",body:JSON.stringify({message:msg,history:history})});
    let j={}; try{j=await r.json();}catch(e){}
    if(!r.ok) S.chat.push({role:"error",content:j.error||("Couldn't get a reply (http "+r.status+").")});
    else S.chat.push({role:"assistant",content:j.answer||""});
  }catch(e){
    S.chat.push({role:"error",content:"Couldn't reach the engine — is it running?"});
  }finally{
    S.asking=false; setAskBusy(false); renderChat();
    input.focus();
  }
}
function setAskBusy(b){
  $("#askSend").disabled=b; $("#askInput").disabled=b;
  $("#askSend").textContent=b?"…":"Send";
}
function openAsk(){
  openSheet("ask"); renderChat();
  const i=$("#askInput"); setTimeout(()=>{i.focus();},60);
}

/* ---------- toast ---------- */
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");clearTimeout(highlightTimer);
  highlightTimer=setTimeout(()=>t.classList.remove("show"),2600);}

/* ---------- wiring ---------- */
$("#focusSet").onclick=()=>setFocus($("#focusInput").value.trim());
$("#focusClear").onclick=()=>{$("#focusInput").value="";setFocus("");};
$("#focusInput").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();setFocus(e.target.value.trim());}});
$("#newTaskBtn").onclick=()=>{$("#suberr").textContent="";openSheet("submit");};
$("#askBtn").onclick=openAsk;
$("#askSend").onclick=askSend;
$("#askInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();askSend();}});
$("#submitBtn").onclick=submitTask;
$("#ekind").onchange=evfield;
$("#backdrop").onclick=closeSheets;
$$("[data-close]").forEach(b=>b.onclick=closeSheets);
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeSheets();});
$$("[data-src]").forEach(b=>b.onclick=()=>{S.srcFilter=b.dataset.src;$$("[data-src]").forEach(x=>x.classList.toggle("on",x===b));renderScore();renderTasks();});
$$("[data-tsrc]").forEach(b=>b.onclick=()=>{S.srcFilter=b.dataset.tsrc;$$("[data-tsrc]").forEach(x=>x.classList.toggle("on",x===b));$$("[data-src]").forEach(x=>x.classList.toggle("on",x.dataset.src===S.srcFilter));renderScore();renderTasks();});
$$("[data-tstat]").forEach(b=>b.onclick=()=>{S.tStat=b.dataset.tstat;$$("[data-tstat]").forEach(x=>x.classList.toggle("on",x===b));renderTasks();});
$("#tsearch").oninput=e=>{S.tSearch=e.target.value;renderTasks();};

/* boot */
evfield(); startRibbon();
tickFast(); tickSlow(); tickCold();
setInterval(tickFast,2500); setInterval(tickSlow,10000); setInterval(tickCold,30000);
if(location.hash.startsWith("#task/")) openDetail(location.hash.slice(6));
</script>
</body></html>
"""
