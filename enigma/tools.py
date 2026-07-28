"""Tools the engine can call while solving, via a ReAct text protocol.

During candidate generation the model may emit, on its own line:

    TOOL <name>: <input>

The engine runs the tool, appends `TOOL RESULT [...]: <output>` to the
transcript, and lets the model continue until it writes `FINAL: <answer>`.
This works with any local model — no native tool-calling support needed.

Tools:
  python  — run Python in a sandboxed subprocess, returns stdout/stderr
  fetch   — HTTP GET a URL, returns readable text
  search  — web search (Tavily / SearXNG / keyless DuckDuckGo), top results
  calc    — safe arithmetic / math expression evaluation
"""

from __future__ import annotations

import ast
import asyncio
import logging
import math
import operator
import os
import re
import shlex
import sys
import tempfile

import httpx

from .config import Config
from .evaluators import _kill_tree

log = logging.getLogger("enigma.tools")

_TOOL_RE = re.compile(r"^[ \t>*-]*TOOL\s+(\w+)\s*:[ \t]*(.*)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
# Block cloud-metadata endpoints; everything else (incl. localhost) is allowed
# for a personal engine that may legitimately hit local services.
_BLOCKED_HOSTS = ("169.254.169.254", "metadata.google.internal")


class ToolCall:
    __slots__ = ("name", "arg", "start")

    def __init__(self, name: str, arg: str, start: int):
        self.name = name
        self.arg = arg
        self.start = start  # offset of the TOOL line in the source text


def parse_tool_call(text: str) -> ToolCall | None:
    """First `TOOL name: arg` in the text; arg runs to end (may be multiline)."""
    m = _TOOL_RE.search(text)
    if not m:
        return None
    name = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    # Stop the argument at anything that marks the END of this call's input: a
    # fabricated result, a commit, OR the model's NEXT tool call / DONE. Without
    # the latter two, a model that writes `TOOL write: …` followed by its next
    # planned step leaks that step into the file content (silently corrupting an
    # exact-byte payload). One message = one tool call; the rest is not the arg.
    arg = re.split(
        # Also cut at the harness's OWN result dialect — the model parrots it
        # back as fabricated results mid-generation (v11c step 115 wrote
        # "wrote 264 bytes to ..." INTO the payload file).
        r"\n\s*(?:FINAL:|DONE:|RESULT:|TO+L\s+RESULT|TOOL\s+\w+\s*:|\[step\b|\[exit\b"
        r"|\[blocked\b|\[circuit-breaker\b|\[harness\b|wrote\s+\d+\s+bytes\b|```)",
        arg, maxsplit=1,
    )[0].strip()
    # Strip a surrounding code fence if the model wrapped the input.
    fence = re.match(r"^```[a-zA-Z0-9_+-]*\n(.*)\n```$", arg, re.DOTALL)
    if fence:
        arg = fence.group(1)
    return ToolCall(name, arg, m.start())


class ToolBox:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http
        self._names = ("python", "fetch", "search", "calc")
        # When bound to a container (exploitation / sandbox agent mode) the tool
        # set switches to acting INSIDE that box: a real shell + file writer.
        self._container: str | None = None
        self._workdir = "/workspace"

    def bind_container(self, container_id: str, workdir: str = "/workspace") -> None:
        """Point the shell/write tools at a live Docker container. Turns the
        toolbox from 'compute on the host' into 'act inside the sandbox' — the
        substrate for the exploitation agent loop. The host-side `python` tool is
        NOT offered in this mode: agents kept feeding it container paths it can't
        see (~13 wasted steps across runs); shell `python3` covers the need."""
        self._container = container_id
        self._workdir = workdir or "/workspace"
        self._names = ("shell", "write", "read", "calc")

    @property
    def in_container(self) -> bool:
        return self._container is not None

    @property
    def enabled(self) -> bool:
        return self.cfg.tools_enabled

    def docs(self) -> str:
        if self._container is not None:
            return (
                "TOOLS — you are working INSIDE a Linux sandbox container. Act by "
                "calling tools; each result is real and appended for you to react to.\n"
                "To call a tool, write on its own line exactly:\n"
                "    TOOL <name>: <input>\n"
                "Then STOP — do not write the result yourself; the real one is appended.\n"
                "Available tools:\n"
                f"  shell — run a bash command in the container (cwd {self._workdir}). "
                "Persistent filesystem across calls. e.g. TOOL shell: ls -la; file ./target\n"
                "         Analysis/exploitation tooling is available IN the container — use it via "
                "shell: gdb (batch: gdb -batch -ex run -ex bt --args ./t <in>), objdump -d, readelf, "
                "nm, strings, checksec, xxd, and python3 with pwntools (from pwn import *) for "
                "building/sending payloads. Prefer these over guessing. "
                "WARNING: container python3 may be 3.5; f-strings and type hints are SyntaxError. "
                "Use .format() or % formatting.\n"
                "  read  — print a file's contents. e.g. TOOL read: /workspace/target.c\n"
                "  write — write a file. FIRST line = path, rest = contents. e.g.\n"
                "          TOOL write: /workspace/exploit.py\n          import sys\n          ...\n"
                "  calc  — evaluate a math expression ON THE HOST.\n"
                "When the objective is complete, write on its own line: DONE: <one-line summary>."
            )
        return (
            "TOOLS — you may call these to get data or compute before answering.\n"
            "To call a tool, write on its own line exactly:\n"
            "    TOOL <name>: <input>\n"
            "Then end your message right after that line — do NOT write the result "
            "yourself. The real result is appended and you continue.\n"
            "Available tools:\n"
            "  python — run Python code; print() what you need. e.g. TOOL python: print(2**0.5)\n"
            "  fetch  — GET a URL, returns page text. e.g. TOOL fetch: https://example.com\n"
            "  search — web search, returns top results. e.g. TOOL search: Titan ocean depth\n"
            "  calc   — evaluate a math expression. e.g. TOOL calc: 100e6/(1.38e-23*90)\n"
            "Call tools only when they genuinely help. When finished, write:\n"
            "    FINAL: <your complete final answer>"
        )

    async def run(self, name: str, arg: str) -> str:
        try:
            if name == "shell":
                return await self._shell(arg)
            if name == "write":
                return await self._write(arg)
            if name == "read":
                return await self._read(arg)
            if name == "python":
                return await self._python(arg)
            if name == "fetch":
                return await self._fetch(arg)
            if name == "search":
                return await self._search(arg)
            if name == "calc":
                return self._calc(arg)
            return f"unknown tool '{name}'; available: {', '.join(self._names)}"
        except Exception as e:  # a tool must never crash the generation
            log.warning("tool %s failed: %s", name, e)
            return f"tool error: {e}"

    # ---- container shell / files (sandbox agent mode) --------------------

    def _in_box(self, path: str) -> str:
        """Resolve a possibly-relative path against the container workdir."""
        path = path.strip()
        return path if path.startswith("/") else f"{self._workdir.rstrip('/')}/{path}"

    async def _docker(self, *args: str, timeout: float | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge; agents want combined output
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout or self.cfg.tool_timeout_s)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            return 124, f"(timed out after {timeout or self.cfg.tool_timeout_s:.0f}s)"
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        return proc.returncode, out.decode(errors="replace")

    async def _shell(self, cmd: str) -> str:
        if self._container is None:
            return "no container bound"
        if not cmd.strip():
            return "no command"
        wrapped = f"cd {shlex.quote(self._workdir)} 2>/dev/null; {cmd}"
        code, out = await self._docker("exec", self._container, "bash", "-lc", wrapped)
        out = out.rstrip()
        tag = "" if code == 0 else f"[exit {code}]\n"
        result = self._clip(tag + (out or "(no output)"))
        # Post-execution diagnostics for dialects that each burned dozens of steps:
        notes = []
        if "gdb" in cmd and "-batch" not in cmd and "(gdb)" in out:
            # ~45 steps across v9/v10/v11 were bare GPL banners from interactive
            # gdb — the session dies when the call ends, nothing ran.
            notes.append("you ran INTERACTIVE gdb — nothing executed and the session is "
                         "non-persistent. Use gdb -batch -ex 'cmd' -ex 'cmd' --args <bin> instead.")
        if ">/dev/null" in cmd.replace(" ", "") or "> /dev/null" in cmd:
            # v10c hid its own generator errors 3× with > /dev/null 2>&1.
            notes.append("you discarded output to /dev/null — if this failed, the error is "
                         "invisible to you. Drop the redirect (or use 2>&1 | tail) so you can debug.")
        if notes:
            result += "\n\n[harness note] " + " ".join(notes)
        return result

    async def _read(self, path: str) -> str:
        if self._container is None:
            return "no container bound"
        code, out = await self._docker("exec", self._container, "cat", self._in_box(path))
        return self._clip(out.rstrip() if code == 0 else f"[cannot read {path}]\n{out.rstrip()}")

    async def sandbox_orientation(self) -> str | None:
        """A REAL first look at a bound container: workdir listing plus the
        contents of any task-material files (README, description, etc.), so the
        agent's first decision is grounded in observed paths instead of guessed
        ones. Returns None when not container-bound."""
        if self._container is None:
            return None
        wd = self._workdir.rstrip("/")
        cmd = (
            f"cd {shlex.quote(wd)} 2>/dev/null || exit 0; "
            "echo '== pwd =='; pwd; echo '== ls -la =='; ls -la; "
            "for f in README README.md readme.txt description.txt task.txt run.sh; do "
            "if [ -f \"$f\" ]; then echo \"== $f ==\"; head -c 2500 \"$f\"; echo; fi; done; "
            "echo '== tooling (ONLY these exist — do not call anything else) =='; "
            "for t in curl wget nc socat gdb python3 python perl xxd objdump checksec; do "
            "p=$(command -v $t 2>/dev/null) && echo \"$t: $p\" || echo \"$t: MISSING\"; done; "
            "python3 --version 2>&1 | sed 's/^/python3 version: /'; "
            "echo '== / (top level) =='; ls -la / | head -30"
        )
        code, out = await self._docker("exec", self._container, "bash", "-lc", cmd)
        out = out.strip()
        if not out:
            return None
        return self._clip(out)

    async def _write(self, arg: str) -> str:
        if self._container is None:
            return "no container bound"
        head, _, content = arg.partition("\n")
        path = self._in_box(head)
        if not head.strip():
            return "write needs a file path on the first line"
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write(content)
            tmp.close()
            await self._docker("exec", self._container, "mkdir", "-p", os.path.dirname(path) or "/")
            code, out = await self._docker("cp", tmp.name, f"{self._container}:{path}")
        finally:
            os.unlink(tmp.name)
        return f"wrote {len(content)} bytes to {path}" if code == 0 else f"write failed: {out.strip()}"

    def _clip(self, text: str) -> str:
        limit = self.cfg.tool_result_chars
        return text if len(text) <= limit else text[:limit] + f"\n…[truncated at {limit} chars]"

    # ---- python ----------------------------------------------------------

    async def _python(self, code: str) -> str:
        if not code.strip():
            return "no code provided"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", code,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            return f"could not start python: {e}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.tool_timeout_s)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            return f"python timed out after {self.cfg.tool_timeout_s:.0f}s"
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if err and not out:
            return self._clip(f"[stderr]\n{err}")
        if err:
            return self._clip(f"{out}\n[stderr]\n{err}")
        return self._clip(out or "(no output; remember to print() results)")

    # ---- fetch -----------------------------------------------------------

    async def _fetch(self, url: str) -> str:
        url = url.strip().split()[0] if url.strip() else ""
        if not url.startswith(("http://", "https://")):
            return "fetch needs an http(s):// URL"
        if any(h in url for h in _BLOCKED_HOSTS):
            return "fetch blocked: metadata/link-local address"
        try:
            r = await self.http.get(url, timeout=self.cfg.tool_timeout_s,
                                    follow_redirects=True, headers={"User-Agent": "enigma/0.2"})
        except httpx.HTTPError as e:
            return f"fetch failed: {e}"
        ctype = r.headers.get("content-type", "")
        body = r.text
        if "html" in ctype or body.lstrip()[:1] == "<":
            body = _html_to_text(body)
        return self._clip(f"[{r.status_code} {ctype}]\n{body.strip()}")

    # ---- search ----------------------------------------------------------

    async def _search(self, query: str) -> str:
        query = query.strip()
        if not query:
            return "empty query"
        if self.cfg.tavily_key:
            return self._clip(await self._search_tavily(query))
        if self.cfg.searx_url:
            return self._clip(await self._search_searx(query))
        return self._clip(await self._search_ddg(query))

    async def _search_tavily(self, query: str) -> str:
        r = await self.http.post(
            "https://api.tavily.com/search",
            json={"api_key": self.cfg.tavily_key, "query": query, "max_results": 5},
            timeout=self.cfg.tool_timeout_s,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return "no results"
        return "\n".join(f"- {x.get('title','')} — {x.get('url','')}\n  {x.get('content','')[:300]}" for x in results)

    async def _search_searx(self, query: str) -> str:
        r = await self.http.get(
            self.cfg.searx_url.rstrip("/") + "/search",
            params={"q": query, "format": "json"},
            timeout=self.cfg.tool_timeout_s,
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:5]
        if not results:
            return "no results"
        return "\n".join(f"- {x.get('title','')} — {x.get('url','')}\n  {x.get('content','')[:300]}" for x in results)

    async def _search_ddg(self, query: str) -> str:
        try:
            r = await self.http.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                timeout=self.cfg.tool_timeout_s,
                headers={"User-Agent": "Mozilla/5.0 (enigma)"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            return f"search failed (no Tavily/SearXNG backend configured): {e}"
        hits = re.findall(
            r'result__a[^>]*href="(.*?)".*?>(.*?)</a>.*?result__snippet[^>]*>(.*?)</a>',
            r.text, re.DOTALL,
        )
        if not hits:
            return "no results parsed (DuckDuckGo may be rate-limiting; set ENIGMA_TAVILY_KEY or ENIGMA_SEARX_URL)"
        out = []
        for href, title, snippet in hits[:5]:
            out.append(f"- {_html_to_text(title)} — {href}\n  {_html_to_text(snippet)[:300]}")
        return "\n".join(out)

    # ---- calc ------------------------------------------------------------

    def _calc(self, expr: str) -> str:
        try:
            return str(_safe_eval(expr.strip()))
        except Exception as e:
            return f"calc error: {e}"


# ---- helpers -------------------------------------------------------------

def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#x27;", "'").replace("&quot;", '"').replace("&nbsp;", " "))
    return re.sub(r"[ \t]*\n\s*\n\s*", "\n\n", re.sub(r"[ \t]+", " ", html)).strip()


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_NAMES = {k: getattr(math, k) for k in ("pi", "e", "tau", "sqrt", "log", "log10", "log2",
          "exp", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "floor", "ceil",
          "factorial", "hypot", "degrees", "radians")}
_NAMES.update({"abs": abs, "round": round, "min": min, "max": max})


def _safe_eval(expr: str):
    """Evaluate an arithmetic/math expression with no names/attrs/calls beyond
    a whitelist — never touches builtins or the filesystem."""
    node = ast.parse(expr, mode="eval").body

    def ev(n):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float, complex)):
                return n.value
            raise ValueError("only numeric constants allowed")
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            return _BINOPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _NAMES and not callable(_NAMES[n.id]):
            return _NAMES[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _NAMES:
            fn = _NAMES[n.func.id]
            if not callable(fn):
                raise ValueError(f"{n.func.id} is not callable")
            return fn(*[ev(a) for a in n.args])
        raise ValueError("unsupported expression")

    return ev(node)
