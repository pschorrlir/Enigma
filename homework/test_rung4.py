#!/usr/bin/env python3
"""Rung 4 checks (plain asserts; run directly).

This task's portion: handler.sh protocol edge cases against a live container
with the server running. Task 2 extends this file with the full solve chain.

Run: pipenv run python homework/test_rung4.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "enigma-hw-rung4-test"


def sh(*args, input_bytes=None, timeout=120):
    return subprocess.run(list(args), input=input_bytes,
                          capture_output=True, timeout=timeout)


def setup_container():
    sh("docker", "rm", "-f", NAME)
    r = sh("docker", "run", "-d", "--name", NAME,
           "enigma-homework:latest", "sleep", "infinity")
    assert r.returncode == 0, r.stderr.decode()
    flag = json.load(open(os.path.join(HERE, "flags.json")))["rung4"]
    r = sh("docker", "exec", "-d", "-e", "FLAG=" + flag, NAME,
           "/opt/homework/server/start_server.sh")
    assert r.returncode == 0, r.stderr.decode()
    # probe the port (bash /dev/tcp), fail fast if the service never came up
    r = sh("docker", "exec", NAME, "bash", "-c",
           "for i in 1 2 3 4 5 6 7 8 9 10; do "
           "(echo > /dev/tcp/127.0.0.1/8000) 2>/dev/null && exit 0; sleep 1; "
           "done; exit 1")
    assert r.returncode == 0, "server never listened on 8000: " + r.stdout.decode()
    return flag


def handler_case(data: bytes) -> str:
    """Pipe raw bytes at the handler directly (no socat) and return output."""
    r = sh("docker", "exec", "-i", NAME,
           "/opt/homework/server/handler.sh", input_bytes=data, timeout=15)
    return r.stdout.decode(errors="replace")


def test_handler_edges():
    out = handler_case(b"abc")                      # short header
    assert "ERROR: short header" in out, out
    out = handler_case(b"zzzzzzzz" + b"A" * 10)     # bad hex
    assert "ERROR: invalid hex header" in out, out
    out = handler_case(b"00000000")                 # zero size
    assert "ERROR" in out, out
    out = handler_case(b"ffffffff" + b"A" * 10)     # oversize
    assert "ERROR: invalid size" in out, out


def main():
    flag = setup_container()
    try:
        test_handler_edges()

        # full solve chain via the proof script
        r = subprocess.run([sys.executable,
                            os.path.join(HERE, "solve_rung4.py"), NAME],
                           capture_output=True, timeout=300)
        out = r.stdout.decode(errors="replace")
        assert r.returncode == 0 and "PASS" in out, out + r.stderr.decode(errors="replace")
    finally:
        sh("docker", "rm", "-f", NAME)
    print("test_rung4 OK")


if __name__ == "__main__":
    main()
