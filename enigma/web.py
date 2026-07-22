"""Local web dashboard: live queue, task submission, results, playbook.

Runs standalone (`enigma web`) against the same SQLite store the daemon
uses — start/stop of the daemon is independent. Stdlib http.server only.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .memory import Store
from .task import TaskSpec

_LOCK = threading.Lock()  # serialize DB access across request threads


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
                        self._json({"daemon": pid, "counts": store.counts()})
                    elif self.path.startswith("/api/tasks"):
                        rows = store.list_tasks(50)
                        out = []
                        for r in rows:
                            spec = json.loads(r["spec"])
                            res = json.loads(r["result"]) if r["result"] else {}
                            out.append({
                                "id": r["id"], "status": r["status"],
                                "description": spec.get("description", "")[:120],
                                "kind": (spec.get("evaluator") or {}).get("kind", "llm_judge"),
                                "score": res.get("score"),
                                "iterations": res.get("iterations"),
                                "elapsed_s": res.get("elapsed_s"),
                                "created_at": r["created_at"],
                            })
                        self._json(out)
                    elif self.path.startswith("/api/task/"):
                        row = store.get_task(self.path.rsplit("/", 1)[1])
                        if row is None:
                            self._json({"error": "not found"}, 404)
                        else:
                            self._json({
                                "id": row["id"], "status": row["status"],
                                "spec": json.loads(row["spec"]),
                                "result": json.loads(row["result"]) if row["result"] else None,
                            })
                    elif self.path == "/api/insights":
                        self._json([
                            {"kind": r["kind"], "lesson": r["lesson"], "uses": r["uses"],
                             "helpful": r["helpful"], "harmful": r["harmful"]}
                            for r in store.list_insights(30)
                        ])
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
<title>Enigma</title>
<style>
:root{--bg:#101418;--panel:#181e24;--line:#2a323b;--text:#d7dee6;--dim:#8a97a4;
--ok:#4cc38a;--run:#e5b567;--bad:#e5726a;--accent:#6aa9e9;font-size:15px}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:1rem/1.5 system-ui,sans-serif;padding:1.2rem}
h1{font-size:1.1rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
h1 b{color:var(--text)}
.grid{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:1rem;margin-top:1rem}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1rem}
.panel h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin-bottom:.7rem}
label{display:block;font-size:.8rem;color:var(--dim);margin:.6rem 0 .2rem}
input,textarea,select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);
border-radius:6px;padding:.45rem .6rem;font:inherit}
textarea{resize:vertical;font-family:ui-monospace,monospace;font-size:.85rem}
button{margin-top:.8rem;background:var(--accent);color:#0b1016;border:0;border-radius:6px;
padding:.5rem 1.1rem;font:inherit;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.1)}
.task{display:flex;gap:.6rem;align-items:baseline;padding:.45rem .3rem;border-bottom:1px solid var(--line);
cursor:pointer;flex-wrap:wrap}
.task:hover{background:#1d242c}
.chip{font-size:.72rem;padding:.05rem .5rem;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.chip.succeeded{color:var(--ok);border-color:var(--ok)}
.chip.running{color:var(--run);border-color:var(--run)}
.chip.failed,.chip.exhausted{color:var(--bad);border-color:var(--bad)}
.desc{flex:1;min-width:12rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--dim);font-size:.78rem}
#detail pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.7rem;
overflow-x:auto;font-size:.82rem;margin:.4rem 0;white-space:pre-wrap}
.lesson{padding:.4rem 0;border-bottom:1px solid var(--line);font-size:.85rem}
.lesson .meta{display:block}
#status{color:var(--dim);font-size:.85rem;margin-top:.3rem}
#status .ok{color:var(--ok)} #status .bad{color:var(--bad)}
.err{color:var(--bad);font-size:.85rem;margin-top:.5rem;min-height:1.2rem}
</style></head><body>
<h1><b>ENIGMA</b> · self-learning task engine</h1>
<div id="status">…</div>
<div class="grid">
<div>
  <div class="panel">
    <h2>Submit task</h2>
    <label>Description</label><textarea id="desc" rows="3" placeholder="What should the engine do?"></textarea>
    <label>Input (optional, text or JSON)</label><textarea id="input" rows="2"></textarea>
    <label>Output kind</label>
    <select id="okind"><option>text</option><option>json</option><option>code</option></select>
    <label>Evaluator</label>
    <select id="ekind" onchange="evfield()">
      <option value="llm_judge">llm_judge — model grades against criteria</option>
      <option value="python_tests">python_tests — run asserts against code</option>
      <option value="json_schema">json_schema — validate structure</option>
      <option value="regex">regex — match a pattern</option>
      <option value="contains">contains — required substrings</option>
    </select>
    <label id="evlabel">Criteria</label><textarea id="evval" rows="3" placeholder="correctness, completeness, clarity"></textarea>
    <button onclick="submitTask()">Submit</button>
    <div class="err" id="suberr"></div>
  </div>
  <div class="panel" style="margin-top:1rem">
    <h2>Playbook (learned insights)</h2>
    <div id="insights" class="meta">none yet</div>
  </div>
</div>
<div>
  <div class="panel">
    <h2>Tasks</h2>
    <div id="tasks" class="meta">loading…</div>
  </div>
  <div class="panel" style="margin-top:1rem">
    <h2>Detail</h2>
    <div id="detail" class="meta">select a task</div>
  </div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const EVFIELDS={llm_judge:["Criteria","correctness, completeness, clarity"],
 python_tests:["Tests (assert lines)","assert solve(2)==4"],
 json_schema:["JSON schema",'{"type":"object","required":["name"]}'],
 regex:["Pattern","^\\\\d{4}-\\\\d{2}-\\\\d{2}$"],contains:["Required substrings (one per line)",""]};
function evfield(){const[l,p]=EVFIELDS[$("ekind").value];$("evlabel").textContent=l;$("evval").placeholder=p}
function esc(s){return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;")}
async function submitTask(){
  $("suberr").textContent="";
  const kind=$("ekind").value, v=$("evval").value.trim();
  const ev={kind};
  if(kind==="llm_judge"&&v)ev.criteria=v;
  if(kind==="python_tests")ev.tests=v;
  if(kind==="regex")ev.pattern=v;
  if(kind==="contains")ev.all=v.split("\\n").filter(x=>x);
  if(kind==="json_schema"){try{ev.schema=JSON.parse(v||"{}")}catch(e){$("suberr").textContent="schema is not valid JSON";return}}
  let input=$("input").value.trim()||null;
  if(input){try{input=JSON.parse(input)}catch(e){/* keep as string */}}
  const body={description:$("desc").value.trim(),input,output:{kind:$("okind").value},evaluator:ev};
  if(!body.description){$("suberr").textContent="description is required";return}
  const r=await fetch("/api/submit",{method:"POST",body:JSON.stringify(body)});
  const j=await r.json();
  if(!r.ok){$("suberr").textContent=j.error||"submit failed";return}
  $("desc").value="";refresh();
}
let selected=null;
async function showTask(id){
  selected=id;
  const t=await(await fetch("/api/task/"+id)).json();
  const res=t.result||{};
  $("detail").innerHTML=
    `<div class="meta">${t.id} · ${esc(t.status)}${res.score!=null?` · score ${res.score}`:""}`+
    `${res.iterations?` · ${res.iterations} iter`:""}${res.cloud_calls?` · ${res.cloud_calls} cloud`:""}`+
    `${res.elapsed_s?` · ${res.elapsed_s}s`:""}</div>`+
    `<pre>${esc(t.spec.description)}</pre>`+
    (res.output?`<div class="meta">output</div><pre>${esc(res.output)}</pre>`:"")+
    (res.feedback?`<div class="meta">evaluator feedback</div><pre>${esc(res.feedback)}</pre>`:"")+
    (res.error?`<div class="meta">error</div><pre>${esc(res.error)}</pre>`:"");
}
async function refresh(){
  try{
    const o=await(await fetch("/api/overview")).json();
    const c=o.counts||{};
    $("status").innerHTML=`daemon: ${o.daemon?`<span class="ok">running (pid ${o.daemon})</span>`:`<span class="bad">stopped</span>`}`+
      ` · queued ${c.queued||0} · running ${c.running||0} · succeeded ${c.succeeded||0}`+
      ` · exhausted ${c.exhausted||0} · failed ${c.failed||0}`;
    const ts=await(await fetch("/api/tasks")).json();
    $("tasks").innerHTML=ts.length?ts.map(t=>
      `<div class="task" onclick="showTask('${t.id}')">`+
      `<span class="chip ${t.status}">${t.status}</span>`+
      `<span class="desc">${esc(t.description)}</span>`+
      `<span class="meta">${t.kind}${t.score!=null?` · ${t.score}`:""}${t.elapsed_s?` · ${t.elapsed_s}s`:""}</span></div>`).join("")
      :"no tasks yet";
    const ins=await(await fetch("/api/insights")).json();
    $("insights").innerHTML=ins.length?ins.map(i=>
      `<div class="lesson">${esc(i.lesson)}<span class="meta">${i.kind} · used ${i.uses}x · +${i.helpful}/−${i.harmful}</span></div>`).join(""):"none yet";
    if(selected)showTask(selected);
  }catch(e){$("status").innerHTML='<span class="bad">dashboard cannot reach the API</span>'}
}
evfield();refresh();setInterval(refresh,2500);
</script></body></html>
"""
