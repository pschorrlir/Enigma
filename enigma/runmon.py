"""ExploitGym run monitor — live web dashboard for Enigma agent runs.

Watches `enigma_transcript.jsonl` files under an out-root (default
~/exploitgym/out) and renders a live, auto-refreshing timeline: steps, tool
calls, harness interventions (blocks / nudges / pivots), working-memory
consolidations, server contact, and final scores.

Stdlib only (http.server + inline HTML/JS, no CDNs) — same philosophy as
web.py, but read-only and focused on run observation instead of the daemon.

    enigma runmon                 # http://127.0.0.1:8766
    enigma runmon --port 8778 --out-root ~/exploitgym/out
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_OUT_ROOT = os.path.expanduser("~/exploitgym/out")
RUNNING_GRACE_S = 180  # transcript touched within this => "running"

_FLAG_RULES = (
    ("pivot", "harness strategy pivot"),
    ("blocked", "blocked by harness"),
    ("nudge", "harness checkpoint"),
    ("skel", "circuit-breaker] This call's SKELETON"),
    ("writeguard", "without EVER executing it"),
)


def _record_view(r: dict) -> dict:
    """Compact, classified view of one transcript record for the timeline."""
    res = str(r.get("result", ""))
    flags = [name for name, marker in _FLAG_RULES if marker in res]
    return {
        "n": r.get("step"),
        "a": r.get("action"),
        "tool": r.get("tool", ""),
        "thought": str(r.get("thought", ""))[:300],
        "arg": str(r.get("arg", ""))[:600],
        "res": res[:900],
        "flags": flags,
        "wm": str(r.get("working_memory", ""))[:2000] if r.get("action") == "consolidate" else "",
        "summary": str(r.get("summary", ""))[:300] if r.get("action") == "done" else "",
    }


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_stats(records: list[dict]) -> dict:
    tools = [r for r in records if r.get("action") == "tool"]
    results = [str(r.get("result", "")) for r in tools]
    args = [str(r.get("arg", "")) for r in tools]
    server_steps = [r["step"] for r in tools
                    if "create_server" in str(r.get("arg", ""))
                    and "Invalid token" not in str(r.get("result", ""))]
    return {
        "steps": tools[-1]["step"] if tools else 0,
        "tool_calls": len(tools),
        "blocked": sum(1 for x in results if "blocked by harness" in x),
        "pivots": sum(1 for x in results if "harness strategy pivot" in x),
        "nudges": sum(1 for x in results if "harness checkpoint" in x),
        "server_creates": len(server_steps),
        "first_server_step": server_steps[0] if server_steps else None,
        "dynamic": sum(1 for r in tools if r.get("tool") in ("shell", "python")),
        "wm_updates": sum(1 for r in records if r.get("action") == "consolidate"),
        "done": next((r for r in records if r.get("action") == "done"), None) is not None,
    }


def _scan_runs(out_root: Path) -> list[dict]:
    """All runs under out_root, newest transcript first."""
    runs = []
    for t in out_root.glob("*/**/enigma_transcript.jsonl"):
        try:
            st = t.stat()
        except OSError:
            continue
        run_dir = t.parent
        rel = str(t.relative_to(out_root))
        result = _load_json(run_dir / "result.json")
        enig_result = _load_json(run_dir / "enigma_result.json")
        cfg = _load_json(run_dir / "config.json") or {}
        score = None
        if result:
            checks = result.get("checks") or []
            score = sum(c.get("score", 0.0) for c in checks)
        age = time.time() - st.st_mtime
        if result is not None:
            status = "finished"
        elif age < RUNNING_GRACE_S:
            status = "running"
        else:
            status = "stalled"
        runs.append({
            "id": str(run_dir.relative_to(out_root)),
            "transcript": rel,
            "task": cfg.get("task_id", ""),
            "model": (cfg.get("agent_extra_kwargs") or {}).get("enigma_model", ""),
            "status": status,
            "score": score,
            "agent_status": (enig_result or {}).get("status"),
            "mtime": st.st_mtime,
            "age_s": int(age),
        })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _read_run(out_root: Path, rel: str, from_idx: int) -> dict | None:
    """Parse one transcript; returns stats + records from record-index `from_idx`.
    Transcripts are append-only, so a parsed-record offset is a stable cursor.
    Path is constrained to out_root."""
    t = (out_root / rel).resolve()
    if not str(t).startswith(str(out_root.resolve())) or not t.exists():
        return None
    all_records: list[dict] = []
    try:
        with open(t, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    all_records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return None
    latest_wm = next((str(r.get("working_memory")) for r in reversed(all_records)
                      if r.get("action") == "consolidate" and r.get("working_memory")), "")
    return {
        "stats": _run_stats(all_records),
        "records": [_record_view(r) for r in all_records[from_idx:]],
        "next_from": len(all_records),
        "latest_wm": latest_wm,
    }


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Enigma run monitor</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#c9d1d9;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--info:#58a6ff;--pivot:#bc8cff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:baseline}
header h1{font-size:15px;margin:0}
header .sub{color:var(--dim);font-size:12px}
#dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:4px}
#layout{display:flex;height:calc(100vh - 45px)}
#runs{width:300px;min-width:300px;overflow-y:auto;border-right:1px solid var(--border);padding:8px}
.run{padding:8px 10px;margin-bottom:6px;background:var(--panel);border:1px solid var(--border);border-radius:6px;cursor:pointer}
.run:hover{border-color:var(--info)}
.run.sel{border-color:var(--info);box-shadow:0 0 0 1px var(--info)}
.run .name{font-weight:600;font-size:12px;word-break:break-all}
.run .meta{color:var(--dim);font-size:11px;margin-top:3px}
.badge{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10px;font-weight:600;margin-right:4px}
.b-running{background:#1f6feb33;color:var(--info)}
.b-finished{background:#23863633;color:var(--ok)}
.b-stalled{background:#9e6a0333;color:var(--warn)}
.b-solved{background:#238636;color:#fff}
.b-fail{background:#da363333;color:var(--bad)}
#detail{flex:1;overflow-y:auto;padding:10px 16px}
#stats{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:6px 12px;font-size:12px}
.stat b{font-size:15px;display:block}
.stat.hot b{color:var(--pivot)}
#wm{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:12px;white-space:pre-wrap;font-size:12px;color:var(--dim)}
#wm h3{margin:0 0 6px;font-size:12px;color:var(--fg)}
.step{border-left:2px solid var(--border);padding:4px 10px;margin-bottom:8px}
.step.blocked{border-left-color:var(--bad)}
.step.pivot{border-left-color:var(--pivot)}
.step.nudge,.step.skel,.step.writeguard{border-left-color:var(--warn)}
.step.consolidate{border-left-color:var(--info)}
.step.done{border-left-color:var(--ok)}
.step .hd{font-size:11px;color:var(--dim);margin-bottom:2px}
.step .hd b{color:var(--fg)}
.step .thought{color:var(--dim);font-size:12px;margin:2px 0}
.step pre{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:6px;margin:4px 0;white-space:pre-wrap;word-break:break-word;font-size:11px;max-height:180px;overflow-y:auto}
.flag{font-size:10px;font-weight:700;padding:0 5px;border-radius:3px;margin-left:4px}
.f-blocked{background:#da3633;color:#fff}
.f-pivot{background:#8957e5;color:#fff}
.f-nudge,.f-skel,.f-writeguard{background:#9e6a03;color:#fff}
h2{font-size:13px;margin:14px 0 8px;color:var(--dim)}
</style></head>
<body>
<header><span id="dot"></span><h1>Enigma run monitor</h1>
<span class="sub" id="rootinfo"></span><span class="sub" id="tick"></span></header>
<div id="layout"><div id="runs"></div><div id="detail"><p class="sub">select a run</p></div></div>
<script>
let selected = localStorage.getItem('runmon.sel') || null;
let nextFrom = 0, pinned = true, inFlight = false;
const $ = s => document.querySelector(s);

async function j(u){ const r = await fetch(u); if(!r.ok) throw new Error('http '+r.status); return r.json(); }

function esc(s){ const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }

function setDot(ok){ $('#dot').style.background = ok ? 'var(--ok)' : 'var(--bad)'; }

function buildDetail(){
  $('#detail').innerHTML =
    '<div id="stats"></div>' +
    '<div id="wm" style="display:none"><h3>latest working memory</h3><span id="wmbody"></span></div>' +
    '<h2>timeline</h2><div id="steps"></div>';
}

async function refreshRuns(){
  let data;
  try { data = await j('/api/runs'); } catch(e){ setDot(false); return; }
  setDot(true);
  $('#rootinfo').textContent = data.out_root;
  const box = $('#runs'); box.innerHTML = '';
  for(const r of data.runs){
    const div = document.createElement('div');
    div.className = 'run' + (r.id===selected?' sel':'');
    const score = r.score===null?'':(r.score>0?'<span class="badge b-solved">SOLVED '+r.score+'</span>':'<span class="badge b-fail">0.0</span>');
    div.innerHTML = '<div class="name">'+esc(r.id)+'</div>' +
      '<div class="meta"><span class="badge b-'+r.status+'">'+r.status+'</span>'+score+
      (r.agent_status?esc(r.agent_status):'')+'</div>' +
      '<div class="meta">'+esc(r.model)+' · '+r.age_s+'s ago</div>';
    div.onclick = ()=>{ selected=r.id; localStorage.setItem('runmon.sel',r.id); nextFrom=0; buildDetail(); loadRun(true); refreshRuns(); };
    box.appendChild(div);
  }
  if(!selected && data.runs.length){ selected=data.runs[0].id; nextFrom=0; buildDetail(); loadRun(true); }
}

function statBox(label, val, hot){
  return '<div class="stat'+(hot?' hot':'')+'"><b>'+val+'</b>'+label+'</div>';
}

function renderStep(rec){
  const div = document.createElement('div');
  const flags = (rec.flags||[]).map(f=>'<span class="flag f-'+f+'">'+f.toUpperCase()+'</span>').join('');
  if(rec.a==='consolidate'){
    div.className='step consolidate';
    div.innerHTML='<div class="hd"><b>step '+rec.n+'</b> · working-memory consolidation</div>'+
      (rec.wm?'<pre>'+esc(rec.wm)+'</pre>':'');
    return div;
  }
  if(rec.a==='done'){
    div.className='step done';
    div.innerHTML='<div class="hd"><b>step '+rec.n+'</b> · DONE</div><pre>'+esc(rec.summary||'')+'</pre>';
    return div;
  }
  div.className='step '+(rec.flags||[]).join(' ');
  div.innerHTML='<div class="hd"><b>step '+rec.n+'</b> · TOOL '+esc(rec.tool)+flags+'</div>'+
    (rec.thought?'<div class="thought">'+esc(rec.thought)+'</div>':'')+
    '<pre>TOOL '+esc(rec.tool)+': '+esc(rec.arg)+'</pre>'+
    (rec.res?'<pre>'+esc(rec.res)+'</pre>':'');
  return div;
}

async function loadRun(reset){
  if(!selected || inFlight) return;
  inFlight = true;
  try {
    const data = await j('/api/run?path='+encodeURIComponent(selected)+'&from='+nextFrom);
    if(!data || data.error){ setDot(false); return; }
    setDot(true);
    // transcript was truncated (a new run reused the dir) → resync from scratch
    if(data.next_from < nextFrom){ nextFrom = 0; buildDetail(); return; }
    if(!$('#steps')) buildDetail();
    if(reset){ $('#steps').innerHTML=''; }
    const s = data.stats;
    $('#stats').innerHTML =
      statBox('steps', s.steps)+
      statBox('server creates', s.server_creates, s.server_creates>1)+
      statBox('first server @', s.first_server_step===null?'—':s.first_server_step)+
      statBox('pivots', s.pivots, s.pivots>0)+
      statBox('blocked', s.blocked, s.blocked>0)+
      statBox('nudges', s.nudges)+
      statBox('dynamic', s.dynamic)+
      statBox('wm updates', s.wm_updates);
    if(data.latest_wm){ $('#wm').style.display=''; $('#wmbody').textContent = data.latest_wm; }
    for(const rec of data.records) $('#steps').appendChild(renderStep(rec));
    nextFrom = data.next_from;
    if(pinned) $('#detail').scrollTop = $('#detail').scrollHeight;
  } catch(e){
    setDot(false);  // transient fetch/parse error — next poll retries
  } finally {
    inFlight = false;
  }
}

setInterval(refreshRuns, 5000);
setInterval(()=>{ loadRun(false); $('#tick').textContent = new Date().toLocaleTimeString(); }, 3000);
$('#detail').addEventListener('scroll', e=>{
  const el = e.target;
  pinned = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
});
refreshRuns();
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    out_root: Path = Path(DEFAULT_OUT_ROOT)

    def log_message(self, fmt, *a):  # quiet
        pass

    def _send(self, body: str | bytes, ctype: str = "application/json", code: int = 200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(_PAGE, "text/html; charset=utf-8")
            return
        if u.path == "/api/runs":
            self._send(json.dumps({
                "out_root": str(self.out_root),
                "runs": _scan_runs(self.out_root),
            }))
            return
        if u.path == "/api/run":
            q = parse_qs(u.query)
            rel = (q.get("path") or [""])[0]
            from_line = int((q.get("from") or ["0"])[0] or 0)
            # rel is a run dir; transcript lives at <dir>/enigma_transcript.jsonl
            data = _read_run(self.out_root, str(Path(rel) / "enigma_transcript.jsonl"), from_line)
            if data is None:
                self._send(json.dumps({"error": "not found"}), code=404)
                return
            self._send(json.dumps(data))
            return
        self._send(json.dumps({"error": "unknown"}), code=404)


def serve(out_root: str, host: str, port: int) -> None:
    root = Path(os.path.expanduser(out_root)).resolve()
    if not root.is_dir():
        raise SystemExit(f"out-root not found: {root}")
    _Handler.out_root = root
    srv = ThreadingHTTPServer((host, port), _Handler)
    print(f"run monitor on http://{host}:{port}  (watching {root})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="enigma runmon", description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--out-root", default=DEFAULT_OUT_ROOT)
    args = p.parse_args(argv)
    serve(args.out_root, args.host, args.port)


if __name__ == "__main__":
    main()
