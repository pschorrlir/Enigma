# Skill Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Enigma agent five executable exploitation skills (offset discovery, symbol lookup, cyclic pattern access, payload delivery) as a single `skill` tool, so homework-ladder runs bank wins and skill-assisted solves are measured separately.

**Architecture:** New `enigma/skills.py` registry (name → async host-side coroutine driving `docker exec`). `ToolBox` gains a `skill` tool in container mode plus stdin support in `_docker`. `run_hw.py`/`ladder.py` tag and report skill usage.

**Tech Stack:** Python 3 (host, f-strings OK), asyncio, docker CLI. Tests are plain-assert scripts run directly, matching `homework/test_ladder.py`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-28-skill-tools-design.md`.
- **Port, don't refactor:** `homework/solve_rung1.py` stays untouched (solvability-proof artifact).
- **File-scoped commits only:** the repo has heavy unrelated uncommitted state. NEVER `git add -A` / `git commit -a`; add only the files listed in each commit step.
- A tool must never crash the generation: skill failures return diagnostic text, never raise (existing `ToolBox.run` contract).
- No changes to `enigma/engine.py` — interventions (blocks, pivots, PRM) are untouched.
- Tests follow the existing convention: plain asserts in a `main()`, run via `pipenv run python homework/test_<name>.py` (no pytest).

---

### Task 1: Pure skill helpers in `enigma/skills.py`

**Files:**
- Create: `enigma/skills.py`
- Test: `homework/test_skills.py`

**Interfaces:**
- Consumes: nothing (new module, stdlib only: `re`, `struct`).
- Produces:
  - `cyclic(n: int, subseq: int = 4) -> bytes` — De Bruijn pattern, byte length `n`.
  - `cyclic_find(haystack: bytes, needle: bytes) -> int` — offset or -1.
  - `parse_payload(spec: str) -> bytes` — raises `ValueError` on bad terms.
  - Later tasks rely on these exact names.

- [ ] **Step 1: Write the failing test**

Create `homework/test_skills.py`:

```python
#!/usr/bin/env python3
"""Unit checks for enigma/skills.py (plain asserts; run directly)."""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from enigma.skills import cyclic, cyclic_find, parse_payload  # noqa: E402


def main():
    # cyclic round-trip: every 4-byte fragment locates itself
    pat = cyclic(256)
    assert len(pat) == 256
    for off in (0, 4, 72, 128, 252):
        assert cyclic_find(pat, pat[off:off + 4]) == off, off

    # parse_payload: repeats, packed addrs, mixed, whitespace tolerant
    assert parse_payload("A*8") == b"AAAAAAAA"
    assert parse_payload("A*4 + p64(0x4011d6)") == b"AAAA" + struct.pack("<Q", 0x4011d6)
    assert parse_payload("B*2+p32(16)") == b"BB" + struct.pack("<I", 16)
    try:
        parse_payload("nonsense")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("test_skills OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'enigma.skills'`

- [ ] **Step 3: Write minimal implementation**

Create `enigma/skills.py`:

```python
"""Executable skill compilation: curated exploitation procedures exposed as a
single `skill` tool for container-bound agent runs.

The model supplies intent (which procedure, which binary); these host-side
implementations supply the craft. Logic ported from homework/solve_rung1.py —
that file stays untouched as the solvability-proof artifact.
"""
import re
import struct


# ---- De Bruijn cyclic pattern (pwntools-free) --------------------------------
def cyclic(n: int, subseq: int = 4) -> bytes:
    """De Bruijn sequence over a lowercase alphabet, byte length n."""
    k = 26
    alphabet = [chr(ord('a') + i) for i in range(k)]
    a = [0] * (k * subseq)
    seq = []

    def db(t, p):
        if t > subseq:
            if subseq % p == 0:
                seq.extend(a[1:p + 1])
        else:
            a[t] = a[t - p]
            db(t + 1, p)
            for j in range(a[t - p] + 1, k):
                a[t] = j
                db(t + 1, t)

    db(1, 1)
    return "".join(alphabet[i] for i in seq).encode()[:n]


def cyclic_find(haystack: bytes, needle: bytes) -> int:
    """Offset of a byte fragment (e.g. a little-endian register value), or -1."""
    return haystack.find(needle)


def parse_payload(spec: str) -> bytes:
    """Parse a payload spec like 'A*72 + p64(0x4011d6)' into bytes.

    Terms separated by '+': '<char>*<count>' repeats, 'p32(0xADDR)' / 'p64(0xADDR)'
    little-endian packed addresses (hex or decimal). Raises ValueError on anything
    else so the caller can hand the message back to the agent."""
    out = b""
    for term in spec.split("+"):
        term = term.strip()
        if not term:
            continue
        m = re.fullmatch(r"(.)\s*\*\s*(\d+)", term)
        if m:
            out += m.group(1).encode("latin-1") * int(m.group(2))
            continue
        m = re.fullmatch(r"p(32|64)\(\s*(0x[0-9a-fA-F]+|\d+)\s*\)", term)
        if m:
            fmt = "<I" if m.group(1) == "32" else "<Q"
            out += struct.pack(fmt, int(m.group(2), 0))
            continue
        raise ValueError(f"unparseable term {term!r} (want X*N or p64(0xADDR))")
    if not out:
        raise ValueError("empty payload spec")
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/skills.py homework/test_skills.py
git commit -m "feat(skills): cyclic pattern + payload-spec parser helpers"
```

---

### Task 2: Docker-backed skill coroutines + registry

**Files:**
- Modify: `enigma/skills.py` (append)
- Test: `homework/test_skills.py` (extend)

**Interfaces:**
- Consumes: Task 1's `cyclic`, `cyclic_find`, `parse_payload`.
- Produces (Task 3 wires these into ToolBox):
  - `async run_skill(name: str, args: str, cexec) -> str` — dispatcher; unknown names return the available-skill list, never raise.
  - `skill_docs() -> str` — one indented line per skill for tool docs.
  - `cexec` protocol: `async cexec(*argv: str, input_bytes: bytes | None = None, timeout: float | None = None) -> tuple[int, str]` (provided by ToolBox in Task 3; tests fake it).

- [ ] **Step 1: Write the failing tests**

Append to `homework/test_skills.py`, before `if __name__`:

```python
import asyncio  # move to top of file with the other imports

from enigma.skills import run_skill, skill_docs, cyclic  # cyclic already imported


def _fake_cexec(canned):
    """canned: list of (match_fn, (code, out)). Returns an async cexec fake."""
    calls = []

    async def cexec(*argv, input_bytes=None, timeout=None):
        calls.append((argv, input_bytes))
        for match, resp in canned:
            if match(argv):
                return resp
        return 1, "unexpected call: %r" % (argv,)

    cexec.calls = calls
    return cexec


def test_find_symbol_nonpie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000004011d6 T win\n0000000000401130 T main\n")),
            (lambda a: a[0] == "readelf", (0, "ELF Header:\n  Type:  EXEC (Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung1 win", cexec)
        assert "0x4011d6" in out and "absolute" in out, out
    asyncio.run(go())


def test_find_symbol_pie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000000011d6 T win\n")),
            (lambda a: a[0] == "readelf", (0, "  Type:  DYN (Position-Independent Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung2 win", cexec)
        assert "0x11d6" in out and "PIE" in out and "offset" in out, out
    asyncio.run(go())


def test_discover_offset():
    pat = cyclic(256)
    rip = struct.unpack("<Q", pat[72:80])[0]

    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (0, "rip            0x%x\n" % rip)),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out, out
    asyncio.run(go())


def test_discover_offset_fallback():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (1, "gdb: command not found")),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out and "gdb: command not found" in out, out
    asyncio.run(go())


def test_deliver_stdin():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "/target/rung1", (0, "flag{test}\n")),
        ])
        out = await run_skill("deliver_stdin", "/target/rung1 A*72 + p64(0x4011d6)", cexec)
        assert "flag{test}" in out, out
        argv, payload = cexec.calls[0]
        assert payload == b"A" * 72 + struct.pack("<Q", 0x4011d6), payload
    asyncio.run(go())


def test_unknown_skill_lists_available():
    async def go():
        out = await run_skill("nope", "", None)
        assert "discover_offset" in out and "deliver_stdin" in out, out
    asyncio.run(go())
```

And call them from `main()` (append before the final `print`):

```python
    test_find_symbol_nonpie()
    test_find_symbol_pie()
    test_discover_offset()
    test_discover_offset_fallback()
    test_deliver_stdin()
    test_unknown_skill_lists_available()
    assert "discover_offset" in skill_docs() and "deliver_stdin" in skill_docs()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL with `ImportError: cannot import name 'run_skill'`

- [ ] **Step 3: Implement the skill coroutines + registry**

Append to `enigma/skills.py`:

```python
# ---- docker-backed skills -----------------------------------------------------
# Each coroutine: (cexec, args: str) -> str. cexec runs argv inside the bound
# container and returns (exit_code, combined_output). Skills NEVER raise —
# failures come back as diagnostic text the agent can act on.

_FALLBACK_OFFSET = 72  # 64-byte buf + saved rbp at -O0, verified on homework rungs


async def _skill_discover_offset(cexec, args: str) -> str:
    binary = args.split()[0] if args.split() else ""
    if not binary:
        return "usage: skill discover_offset <binary-path>"
    pat = cyclic(256)
    await cexec("bash", "-c", "cat > /tmp/pat.txt", input_bytes=pat)
    code, out = await cexec(
        "gdb", "-q", "-batch",
        "-ex", "run < /tmp/pat.txt",
        "-ex", "info registers rip",
        "-ex", "x/1gx $rsp",
        binary, timeout=120)
    m = re.search(r"rip\s+(0x[0-9a-f]+)", out)
    if m:
        rip = int(m.group(1), 16)
        off = cyclic_find(pat, struct.pack("<Q", rip)[:4])
        if off >= 0:
            return (f"offset to saved return address: {off} "
                    f"(crash rip=0x{rip:x} located in cyclic pattern)")
    # Non-canonical ret: $rip still points at ret; the return slot is at $rsp.
    m = re.search(r"0x[0-9a-f]+:\s+(0x[0-9a-f]+)", out)
    if m:
        slot = int(m.group(1), 16)
        off = cyclic_find(pat, struct.pack("<Q", slot))
        if off >= 0:
            return (f"offset to saved return address: {off} "
                    f"(non-canonical ret; return slot @rsp=0x{slot:x})")
    return ("offset discovery INCONCLUSIVE — gdb output:\n" + out[-800:] +
            f"\nfallback for -O0 layout (64-byte buf + saved rbp): {_FALLBACK_OFFSET}")


async def _skill_find_symbol(cexec, args: str) -> str:
    parts = args.split()
    if len(parts) < 2:
        return "usage: skill find_symbol <binary> <symbol>"
    binary, name = parts[0], parts[1]
    code, out = await cexec("nm", binary)
    m = re.search(rf"^([0-9a-f]+) [Tt] {re.escape(name)}$", out, re.M)
    if not m:
        return (f"symbol '{name}' not found as a text symbol in nm output "
                f"(exit {code}):\n{out[:800]}")
    addr = int(m.group(1), 16)
    _, hdr = await cexec("readelf", "-h", binary)
    type_line = next((l for l in hdr.splitlines() if "Type:" in l), "")
    if "DYN" in type_line:
        return (f"{name} is at file offset 0x{addr:x} — PIE binary: "
                f"runtime address = leaked_base + 0x{addr:x}")
    return f"{name} = 0x{addr:x} (absolute address — non-PIE binary)"


async def _skill_cyclic(cexec, args: str) -> str:
    try:
        n = int(args.split()[0])
    except (IndexError, ValueError):
        return "usage: skill cyclic <length>  (prints a De Bruijn pattern)"
    return cyclic(min(n, 4096)).decode("latin-1")


async def _skill_cyclic_find(cexec, args: str) -> str:
    tok = args.split()[0] if args.split() else ""
    tok = tok[2:] if tok.startswith("0x") else tok
    try:
        needle = bytes.fromhex(tok)
    except ValueError:
        return "usage: skill cyclic_find <hexbytes>  e.g. cyclic_find 0x62616164"
    off = cyclic(4096).find(needle)
    if off < 0:
        return f"{tok} not found in the standard cyclic pattern"
    return f"offset of 0x{tok} in the cyclic pattern: {off}"


async def _skill_deliver_stdin(cexec, args: str) -> str:
    binary, _, spec = args.strip().partition(" ")
    if not binary or not spec.strip():
        return ("usage: skill deliver_stdin <binary> <spec>  "
                "e.g. skill deliver_stdin /target/rung1 A*72 + p64(0x4011d6)")
    try:
        payload = parse_payload(spec)
    except ValueError as e:
        return f"bad payload spec: {e}"
    code, out = await cexec(binary, input_bytes=payload, timeout=30)
    return f"[sent {len(payload)} bytes on stdin, exit {code}]\n{out.rstrip()}"


SKILLS = {
    "discover_offset": ("discover_offset <binary> — crash under gdb on a cyclic "
                        "pattern; returns the exact offset to the saved return address",
                        _skill_discover_offset),
    "find_symbol": ("find_symbol <binary> <name> — nm + PIE check; returns the "
                    "function's absolute address (non-PIE) or file offset (PIE)",
                    _skill_find_symbol),
    "cyclic": ("cyclic <n> — print a De Bruijn pattern of length n (payload padding "
               "whose every 4-byte fragment locates its own offset)",
               _skill_cyclic),
    "cyclic_find": ("cyclic_find <hexbytes> — offset of a byte fragment (e.g. a "
                    "register value) in the standard cyclic pattern",
                    _skill_cyclic_find),
    "deliver_stdin": ("deliver_stdin <binary> <spec> — run the target with a payload "
                      "on stdin; spec e.g. 'A*72 + p64(0x4011d6)'; returns its output",
                      _skill_deliver_stdin),
}


def skill_docs() -> str:
    return "\n".join(f"          {doc}" for doc, _ in SKILLS.values())


async def run_skill(name: str, args: str, cexec) -> str:
    entry = SKILLS.get(name)
    if entry is None:
        return ("unknown skill '%s'; available:\n" % name) + skill_docs()
    return await entry[1](cexec, args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/skills.py homework/test_skills.py
git commit -m "feat(skills): docker-backed skill coroutines + registry"
```

---

### Task 3: Wire the `skill` tool into ToolBox

**Files:**
- Modify: `enigma/tools.py` — `_docker` (~line 174), `_names` in `bind_container` (~line 98), `docs()` (~line 110), `run()` (~line 146); add `_cexec` and `_skill` methods.
- Test: `homework/test_skills.py` (extend)

**Interfaces:**
- Consumes: Task 2's `run_skill(name, args, cexec) -> str` and `skill_docs() -> str`.
- Produces:
  - `ToolBox._docker(*args, timeout=None, input_bytes=None) -> tuple[int, str]` — new optional `input_bytes`; default behavior unchanged.
  - `ToolBox._cexec(*argv, input_bytes=None, timeout=None) -> tuple[int, str]` — the cexec protocol Task 2's skills consume.
  - Container-mode tool set is now `("shell", "write", "read", "calc", "skill")`.

- [ ] **Step 1: Write the failing test**

Append to `homework/test_skills.py`, before `if __name__`:

```python
def test_toolbox_skill_dispatch():
    """ToolBox.run('skill', ...) reaches the registry via a faked docker exec."""
    import types

    from enigma.tools import ToolBox

    async def go():
        tb = ToolBox.__new__(ToolBox)  # bypass __init__ (needs cfg/http)
        tb._container = "fakecid"
        tb._workdir = "/workspace"
        tb._names = ("shell", "write", "read", "calc", "skill")
        tb.cfg = types.SimpleNamespace(tool_timeout_s=60, tool_result_chars=10000)
        tb.http = None

        async def fake_docker(*args, timeout=None, input_bytes=None):
            assert args[:2] == ("exec", "-i"), args
            if args[3] == "nm":
                return 0, "00000000004011d6 T win\n"
            if args[3] == "readelf":
                return 0, "  Type:  EXEC (Executable file)\n"
            return 1, "unexpected"

        tb._docker = fake_docker
        out = await tb.run("skill", "find_symbol /target/rung1 win")
        assert "0x4011d6" in out, out
        out = await tb.run("skill", "bogus x")
        assert "unknown skill" in out and "deliver_stdin" in out, out

    asyncio.run(go())
```

And add `test_toolbox_skill_dispatch()` to `main()` before the final `print`.

Note: `_clip` reads only `self.cfg.tool_result_chars`, which the `SimpleNamespace` provides, so the `__new__` bypass is safe.

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL — `tb.run("skill", ...)` returns `unknown tool 'skill'`

- [ ] **Step 3: Implement the ToolBox wiring**

In `enigma/tools.py`:

a) Top of file, add the import (tools.py uses relative imports — `from .config import Config`):

```python
from .skills import run_skill, skill_docs
```

b) `bind_container` (~line 98) — add `"skill"`:

```python
        self._names = ("shell", "write", "read", "calc", "skill")
```

c) `_docker` — add stdin support:

```python
    async def _docker(self, *args: str, timeout: float | None = None,
                      input_bytes: bytes | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None
                  else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge; agents want combined output
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input_bytes),
                                            timeout=timeout or self.cfg.tool_timeout_s)
```

(rest of `_docker` unchanged)

d) Add `_cexec` and `_skill` methods (next to `_shell`):

```python
    async def _cexec(self, *argv: str, input_bytes: bytes | None = None,
                     timeout: float | None = None) -> tuple[int, str]:
        """Exec argv inside the bound container with optional stdin — the
        callable handed to skill coroutines (see enigma/skills.py)."""
        return await self._docker("exec", "-i", self._container, *argv,
                                  input_bytes=input_bytes, timeout=timeout)

    async def _skill(self, arg: str) -> str:
        if self._container is None:
            return "no container bound"
        name, _, rest = arg.strip().partition(" ")
        return self._clip(await run_skill(name.strip(), rest.strip(), self._cexec))
```

e) `run()` — dispatch before the `unknown tool` fallback:

```python
            if name == "skill":
                return await self._skill(arg)
```

f) `docs()` container block — add the skill entry after the `calc` line:

```python
                "  skill — run a curated exploitation procedure (executes host-side, "
                "reliable): TOOL skill: <name> <args>. Available skills:\n"
                + skill_docs() + "\n"
                "          e.g. TOOL skill: discover_offset /target/rung1\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/tools.py homework/test_skills.py
git commit -m "feat(tools): skill tool + docker stdin support in ToolBox"
```

---

### Task 4: Live-container integration test

**Files:**
- Create: `homework/test_skills_live.py`

**Interfaces:**
- Consumes: Task 3's wired `ToolBox`; the `enigma-homework:latest` docker image
  (already built — `homework/build.sh`); `homework/flags.json` key `rung1`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the integration test**

Create `homework/test_skills_live.py`:

```python
#!/usr/bin/env python3
"""Live integration check: skills against a real enigma-homework container.

Requires docker + the enigma-homework:latest image (homework/build.sh).
Plain asserts; run directly: pipenv run python homework/test_skills_live.py
"""
import asyncio
import dataclasses
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from run_hw import _load_enigma_env  # noqa: E402

NAME = "enigma-hw-skilltest"


def sh(*args, **kw):
    return subprocess.run(list(args), capture_output=True, **kw)


async def main():
    _load_enigma_env()
    import httpx
    from enigma.config import load_config
    from enigma.tools import ToolBox

    expected = json.load(open(os.path.join(HERE, "flags.json")))["rung1"]

    sh("docker", "rm", "-f", NAME)
    r = sh("docker", "run", "-d", "--name", NAME,
           "enigma-homework:latest", "sleep", "infinity")
    assert r.returncode == 0, r.stderr.decode()
    cid = r.stdout.decode().strip()
    try:
        r = sh("docker", "cp", os.path.join(HERE, "flags", "rung1.txt"),
               "%s:/flag.txt" % NAME)
        assert r.returncode == 0, r.stderr.decode()

        cfg = load_config()
        async with httpx.AsyncClient() as http:
            tb = ToolBox(cfg, http)
            tb.bind_container(cid, workdir="/workspace")

            out = await tb.run("skill", "discover_offset /target/rung1")
            assert "offset to saved return address: 72" in out, out

            out = await tb.run("skill", "find_symbol /target/rung1 win")
            assert "0x" in out and "absolute" in out, out
            win = out.split("win = ")[1].split(" ")[0]

            out = await tb.run("skill", "deliver_stdin /target/rung1 A*72 + p64(%s)" % win)
            assert expected in out, out

            # docs advertise the skills
            docs = tb.docs()
            assert "discover_offset" in docs and "deliver_stdin" in docs, docs
    finally:
        sh("docker", "rm", "-f", NAME)
    print("test_skills_live OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `pipenv run python homework/test_skills_live.py`
Expected: PASS — prints `test_skills_live OK` (first run takes ~30s: gdb under docker). If the image is missing, build it first: `bash homework/build.sh`.

- [ ] **Step 3: Commit**

```bash
git add homework/test_skills_live.py
git commit -m "test(skills): live-container integration check"
```

---

### Task 5: Tag and report skill usage

**Files:**
- Modify: `homework/run_hw.py` (~lines 183-185, final printout)
- Modify: `homework/ladder.py` — `_transcript_stats` (lines 32-56), `_print_matrix` (lines 59-68), row dict (lines 94-101), fallback stats (line 91-93)
- Test: `homework/test_ladder.py` (extend)

**Interfaces:**
- Consumes: transcript records (`{"action": "tool", "tool": "skill", ...}`).
- Produces: `skill_steps: int` and `solved_with_skill: bool` in `run_rung`'s result dict; `skill_steps` in `_transcript_stats` output and ladder rows.

- [ ] **Step 1: Write the failing test**

In `homework/test_ladder.py`, add a skill record to the synthetic transcript (change the `recs` list):

```python
    recs = [
        {"step": 1, "action": "tool", "tool": "shell", "arg": "ls", "result": "ok"},
        {"step": 2, "action": "tool", "tool": "shell", "arg": "gdb x",
         "result": "[harness strategy pivot] proposes:\nstop that\n\nrest"},
        {"step": 3, "action": "tool", "tool": "read", "arg": "/a",
         "result": "[blocked by harness] NOT executed"},
        {"step": 4, "action": "tool", "tool": "skill",
         "arg": "discover_offset /target/rung1", "result": "offset to saved return address: 72"},
        {"step": 5, "action": "done", "summary": "flag written"},
    ]
```

And update the assertions:

```python
    assert stats["steps"] == 4, stats          # tool steps only
    assert stats["pivots"] == 1, stats
    assert stats["blocked"] == 1, stats
    assert stats["skill_steps"] == 1, stats
    assert stats["solved"] is True, stats      # action == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_ladder.py`
Expected: FAIL with `KeyError: 'skill_steps'`

- [ ] **Step 3: Implement tagging**

a) `homework/ladder.py` `_transcript_stats` — count skill calls:

```python
def _transcript_stats(path: str) -> dict:
    """Intervention/outcome stats for one attempt's transcript JSONL."""
    steps = pivots = blocked = skill_steps = 0
    solved = False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("action") == "tool":
                    steps += 1
                    skill_steps += r.get("tool") == "skill"
                    res = str(r.get("result", ""))
                    pivots += "harness strategy pivot" in res
                    blocked += "blocked by harness" in res
                elif r.get("action") == "done":
                    solved = True
    except OSError:
        pass
    return {"steps": steps, "pivots": pivots, "blocked": blocked,
            "skill_steps": skill_steps, "solved": solved}
```

b) `_print_matrix` — add the column and the assisted/unaided split:

```python
def _print_matrix(rows: list) -> None:
    print("\n=== LADDER MATRIX ===")
    print("%-6s %-8s %-10s %-6s %-7s %-8s %-6s %s" %
          ("rung", "attempt", "status", "steps", "pivots", "blocked", "skill",
           "transcript"))
    for r in rows:
        print("%-6d %-8d %-10s %-6s %-7s %-8s %-6s %s" %
              (r["rung"], r["attempt"], r["status"], r["steps"],
               r["pivots"], r["blocked"], r["skill_steps"],
               os.path.basename(r["transcript"])))
    solved = [r for r in rows if r["solved"]]
    assisted = sum(1 for r in solved if r.get("skill_steps"))
    print("solves: %d/%d attempts (%d skill-assisted, %d unaided)" %
          (len(solved), len(rows), assisted, len(solved) - assisted))
```

c) `_drive` rows — carry `skill_steps` through, both in the fallback dict and the row:

```python
            stats = _transcript_stats(tx) if tx else {
                "steps": res.get("steps", 0), "pivots": 0, "blocked": 0,
                "skill_steps": 0,
                "solved": res.get("status") in ("solved", "done")}
            rows.append({"rung": rung, "attempt": attempt,
                         "status": res.get("status"),
                         "solved": res.get("status") in ("solved", "done")
                                   or stats["solved"],
                         "steps": stats["steps"], "pivots": stats["pivots"],
                         "blocked": stats["blocked"],
                         "skill_steps": stats["skill_steps"], "transcript": tx,
                         "wall_s": int((datetime.datetime.now() - before)
                                       .total_seconds())})
```

d) `homework/run_hw.py` — compute tags from the streamed transcript and add them to the result + printout. After the `learn_from_agent_run` try/except block (~line 174), inside `run_rung` before the final print:

```python
        skill_steps = 0
        try:
            with open(transcript_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r_ = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if r_.get("action") == "tool" and r_.get("tool") == "skill":
                        skill_steps += 1
        except OSError:
            pass
        result["skill_steps"] = skill_steps
        result["solved_with_skill"] = (result.get("status") in ("solved", "done")
                                       and skill_steps > 0)
```

And extend the final print:

```python
    print("[run_hw] rung%d status=%s steps=%s skill_steps=%s solved_with_skill=%s "
          "transcript=%s expected=%s"
          % (rung, result.get("status"), result.get("steps"),
             result.get("skill_steps"), result.get("solved_with_skill"),
             transcript_path, expected))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run python homework/test_ladder.py && pipenv run python homework/test_skills.py`
Expected: both print OK

- [ ] **Step 5: Commit**

```bash
git add homework/run_hw.py homework/ladder.py homework/test_ladder.py
git commit -m "feat(homework): tag + report skill-assisted solves"
```

---

### Task 6: Integration gate + AGENTS.md update

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run a real rung-1 attempt with skills enabled**

Run (background, ~30 min):

```bash
cd ~/Enigma && pipenv run python homework/run_hw.py --rung 1 --steps 120 --timeout 1800
```

Expected: agent solves (status `solved`/`done`), printout shows `skill_steps > 0` and `solved_with_skill=True`. Check the transcript (`homework/out/rung1_<ts>.jsonl`) to confirm `TOOL skill:` calls appear. If the agent solves WITHOUT skills that's also a pass for the gate only if `solved_with_skill` correctly reads `False` — the tagging must be truthful either way.

- [ ] **Step 2: Update AGENTS.md**

Add a short section documenting the new capability, in the existing style:

```markdown
## Skill tools (2026-07-28)

- `TOOL skill: <name> <args>` in container mode — host-side executable
  procedures (`enigma/skills.py` registry): discover_offset, find_symbol,
  cyclic, cyclic_find, deliver_stdin. Ported from homework/solve_rung1.py
  (which stays as the proof artifact).
- ToolBox._docker gained `input_bytes` (stdin) — skills deliver payloads and
  patterns through it.
- Skill usage is measured: `skill_steps` / `solved_with_skill` in run_hw
  results and the ladder matrix (assisted vs unaided split).
- Design: docs/superpowers/specs/2026-07-28-skill-tools-design.md
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: skill tools section in AGENTS.md"
```
