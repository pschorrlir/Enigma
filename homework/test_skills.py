#!/usr/bin/env python3
"""Unit checks for enigma/skills.py (plain asserts; run directly)."""
import asyncio
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from enigma.skills import (cyclic, cyclic_find, parse_payload, parse_steps,  # noqa: E402
                           render_template, run_skill, skill_docs,  # noqa: E402
                           run_steps, _RELAY_PATH)  # noqa: E402


def _fake_cexec(canned):
    """canned: list of (match_fn, (code, out)). Returns an async cexec fake."""
    calls = []

    async def cexec(*argv, input_bytes=None, timeout=None):
        calls.append((argv, input_bytes))
        for match, resp in canned:
            if match(argv):
                return resp
        return 1, "unexpected call: %r" % (argv,)

    cexec.calls = calls
    return cexec


def test_find_symbol_nonpie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000004011d6 T win\n0000000000401130 T main\n")),
            (lambda a: a[0] == "readelf", (0, "ELF Header:\n  Type:  EXEC (Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung1 win", cexec)
        assert "0x4011d6" in out and "absolute" in out, out
    asyncio.run(go())


def test_find_symbol_pie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000000011d6 T win\n")),
            (lambda a: a[0] == "readelf", (0, "  Type:  DYN (Position-Independent Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung2 win", cexec)
        assert "0x11d6" in out and "PIE" in out and "offset" in out, out
    asyncio.run(go())


def test_discover_offset():
    pat = cyclic(256)
    rip = struct.unpack("<Q", pat[72:80])[0]

    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (0, "rip            0x%x\n" % rip)),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out, out
    asyncio.run(go())


def test_discover_offset_fallback():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (1, "gdb: command not found")),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out and "gdb: command not found" in out, out
    asyncio.run(go())


def test_deliver_stdin():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "/target/rung1", (0, "flag{test}\n")),
        ])
        out = await run_skill("deliver_stdin", "/target/rung1 A*72 + p64(0x4011d6)", cexec)
        assert "flag{test}" in out, out
        argv, payload = cexec.calls[0]
        assert payload == b"A" * 72 + struct.pack("<Q", 0x4011d6), payload
    asyncio.run(go())


def test_unknown_skill_lists_available():
    async def go():
        out = await run_skill("nope", "", None)
        assert "discover_offset" in out and "deliver_stdin" in out, out
    asyncio.run(go())


def test_toolbox_skill_dispatch():
    """ToolBox.run('skill', ...) reaches the registry via a faked docker exec."""
    import types

    from enigma.tools import ToolBox

    async def go():
        tb = ToolBox.__new__(ToolBox)  # bypass __init__ (needs cfg/http)
        tb._container = "fakecid"
        tb._workdir = "/workspace"
        tb._names = ("shell", "write", "read", "calc", "skill")
        tb.cfg = types.SimpleNamespace(tool_timeout_s=60, tool_result_chars=10000)
        tb.http = None

        async def fake_docker(*args, timeout=None, input_bytes=None):
            assert args[:2] == ("exec", "-i"), args
            if args[3] == "nm":
                return 0, "00000000004011d6 T win\n"
            if args[3] == "readelf":
                return 0, "  Type:  EXEC (Executable file)\n"
            return 1, "unexpected"

        tb._docker = fake_docker
        out = await tb.run("skill", "find_symbol /target/rung1 win")
        assert "0x4011d6" in out, out
        out = await tb.run("skill", "bogus x")
        assert "unknown skill" in out and "deliver_stdin" in out, out

    asyncio.run(go())


def test_parse_steps_shorthand():
    steps, hex8, err = parse_steps(r"main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")
    assert err is None, err
    assert steps == [("expect", r"main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is False


def test_parse_steps_explicit_with_hex8():
    steps, hex8, err = parse_steps(
        r"expect:Choice: send:1 expect:main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9) hex8")
    assert err is None, err
    assert steps == [("expect", "Choice:"), ("send", "1"),
                     ("expect", r"main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is True


def test_parse_steps_errors():
    # missing template
    steps, _, err = parse_steps(r"main:\s*(0x[0-9a-f]+)")
    assert steps is None and err
    # no send step
    steps, _, err = parse_steps("expect:foo expect:bar")
    assert steps is None and "send" in err
    # step limit (9 steps)
    steps, _, err = parse_steps(" ".join(["send:x"] * 9))
    assert steps is None and "8" in err
    # expect regex with a space
    steps, _, err = parse_steps("expect:hello world send:x")
    assert steps is None and err
    # bad regex
    steps, _, err = parse_steps("expect:(unclosed send:x")
    assert steps is None and err


def test_render_template_leaks():
    leaks = {"leak": 0x5555555542c2, "leak1": 0x5555555542c2, "leak2": 0x1000}
    out = render_template("A*72 + p64({leak}-0xb9)", leaks)
    assert out == b"A" * 72 + struct.pack("<Q", 0x5555555542c2 - 0xb9)
    out = render_template("p64({leak2}+16)", leaks)
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak2+16})", leaks)  # offset inside braces
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak})", leaks)
    assert out == struct.pack("<Q", 0x5555555542c2)
    # literal text terms (menu answers) pass through as bytes
    assert render_template("1", {}) == b"1"
    assert render_template("A*2 + yes", {}) == b"AAyes"
    try:
        render_template("p64({leak3})", leaks)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "leak3" in str(e)


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.written = b""

    def write(self, data):
        self.written += data
        self.proc._on_write(data)

    async def drain(self):
        pass

    def close(self):
        pass


class FakeProc:
    """Scripted interactive process: preloaded stdout, then per-send responses.

    EOF semantics: with no responses, EOF immediately after the banner; with
    responses, EOF right after the last one (so drains never hang). The
    StreamReader is built lazily on first stdout access — Python 3.13 requires
    a running loop, and tests construct FakeProc outside asyncio.run."""

    def __init__(self, banner: bytes, responses: list):
        self._banner = banner
        self._responses = list(responses)
        self._reader = None
        self.stdin = _FakeStdin(self)

    @property
    def stdout(self):
        if self._reader is None:
            import asyncio as _aio
            self._reader = _aio.StreamReader()
            self._reader.feed_data(self._banner)
            if not self._responses:
                self._reader.feed_eof()
        return self._reader

    def _on_write(self, data):
        if self._responses:
            self.stdout.feed_data(self._responses.pop(0))
        if not self._responses:
            self.stdout.feed_eof()

    async def wait(self):
        return 0

    def kill(self):
        pass


def _fake_spawn(proc):
    async def spawn(*argv):
        spawn.argv = argv
        return proc
    return spawn


def test_run_steps_leak_and_deliver():
    main_addr = 0x5555555542C2
    proc = FakeProc(b"rung2: main: 0x%x\n" % main_addr, [b"flag{test}\n"])

    async def go():
        return await run_steps(
            _fake_spawn(proc), ("/target/rung2",),
            [("expect", r"main:\s*(0x[0-9a-f]+)"), ("send", "A*72 + p64({leak}-0xb9)")])

    out = asyncio.run(go())
    assert "flag{test}" in out, out
    assert proc.stdin.written == b"A" * 72 + struct.pack("<Q", main_addr - 0xB9), \
        proc.stdin.written


def test_run_steps_expect_failure_reports_read():
    proc = FakeProc(b"nothing useful here\n", [])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:(0x\S+)"), ("send", "A*1")])

    out = asyncio.run(go())
    assert "expect FAILED" in out and "nothing useful here" in out, out


def test_run_steps_hex8_prefix():
    proc = FakeProc(b"main: 0x1000\n", [b"ok\n"])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:\s*(0x[0-9a-f]+)"),
                                ("send", "A*72 + p64({leak}-0xb9)")], hex8=True)

    asyncio.run(go())
    payload = b"A" * 72 + struct.pack("<Q", 0x1000 - 0xB9)
    assert proc.stdin.written == ("%08x" % len(payload)).encode() + payload


def test_run_steps_multi_expect_leak_vars():
    proc = FakeProc(b"base: 0x2000\nmain: 0x2c2\n", [b"flag{multi}\n"])

    async def go():
        return await run_steps(
            _fake_spawn(proc), ("/t",),
            [("expect", r"base:\s*(0x[0-9a-f]+)"), ("expect", r"main:\s*(0x[0-9a-f]+)"),
             ("send", "p64({leak1}) + p64({leak2}) + p64({leak})")])

    out = asyncio.run(go())
    assert "flag{multi}" in out, out
    want = struct.pack("<Q", 0x2000) + struct.pack("<Q", 0x2C2) + struct.pack("<Q", 0x2C2)
    assert proc.stdin.written == want, proc.stdin.written


def test_pwn_tcp_injects_relay():
    proc = FakeProc(b"main: 0x1000\n", [b"flag{tcp}\n"])
    calls = []

    async def cexec(*argv, input_bytes=None, timeout=None):
        calls.append((argv, input_bytes))
        if argv[0] == "test":
            return 1, ""  # relay missing
        return 0, ""

    async def go():
        from enigma.skills import run_skill
        spawn = _fake_spawn(proc)
        out = await run_skill(
            "pwn_tcp",
            r"10.0.0.2 8000 main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9) hex8",
            cexec, spawn)
        assert spawn.argv == ("python3", _RELAY_PATH, "10.0.0.2", "8000"), \
            spawn.argv
        return out

    out = asyncio.run(go())
    assert "flag{tcp}" in out, out
    assert calls[0][0] == ("test", "-f", _RELAY_PATH), calls
    assert calls[1][0][:2] == ("bash", "-c") and calls[1][1], calls
    payload = b"A" * 72 + struct.pack("<Q", 0x1000 - 0xB9)
    assert proc.stdin.written == ("%08x" % len(payload)).encode() + payload
    from enigma.skills import _RELAY_SRC
    assert 'f"' not in _RELAY_SRC and "os.set_blocking" not in _RELAY_SRC  # py3.5


def test_run_steps_dead_target_write_reports():
    """Target dies mid-session: stdin.write raises; run_steps must return
    diagnostic text (never raise) with the transcript so far."""

    class _DeadStdin(_FakeStdin):
        def write(self, data):
            raise ConnectionResetError(104, "Connection reset by peer")

    proc = FakeProc(b"main: 0x1000\n", [b"never used\n"])
    proc.stdin = _DeadStdin(proc)

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:\s*(0x[0-9a-f]+)"),
                                ("send", "A*72 + p64({leak})")])

    out = asyncio.run(go())
    assert "send FAILED" in out and "ConnectionResetError" in out, out
    assert "main: 0x1000" in out, out  # transcript so far is included


def test_run_steps_nonnumeric_leak_reports():
    """Capture group matching non-numeric text must be a diagnostic, not a
    ValueError out of the coroutine."""
    proc = FakeProc(b"main: ZZZ\n", [])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"main:\s*(\S+)"), ("send", "A*1")])

    out = asyncio.run(go())
    assert "non-numeric leak" in out and "captured 'ZZZ'" in out, out
    assert r"(\\S+)" in out, out  # the offending regex is shown (%r-escaped)


def test_run_steps_no_send_guard():
    proc = FakeProc(b"banner\n", [])

    async def go():
        return await run_steps(_fake_spawn(proc), ("/t",),
                               [("expect", r"banner:")])

    out = asyncio.run(go())
    assert "no send step" in out, out


def test_pwn_stdin_rejects_hex8():
    async def go():
        from enigma.skills import run_skill
        return await run_skill("pwn_stdin", "/t main:(0x1) A*1 hex8", None, None)
    out = asyncio.run(go())
    assert "hex8" in out and "pwn_tcp" in out, out


def main():
    # cyclic round-trip: every 4-byte fragment locates itself
    pat = cyclic(256)
    assert len(pat) == 256
    for off in (0, 4, 72, 128, 252):
        assert cyclic_find(pat, pat[off:off + 4]) == off, off

    # parse_payload: repeats, packed addrs, mixed, whitespace tolerant
    assert parse_payload("A*8") == b"AAAAAAAA"
    assert parse_payload("A*4 + p64(0x4011d6)") == b"AAAA" + struct.pack("<Q", 0x4011d6)
    assert parse_payload("B*2+p32(16)") == b"BB" + struct.pack("<I", 16)
    try:
        parse_payload("nonsense")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    test_find_symbol_nonpie()
    test_find_symbol_pie()
    test_discover_offset()
    test_discover_offset_fallback()
    test_deliver_stdin()
    test_unknown_skill_lists_available()
    test_toolbox_skill_dispatch()
    assert "discover_offset" in skill_docs() and "deliver_stdin" in skill_docs()
    test_parse_steps_shorthand()
    test_parse_steps_explicit_with_hex8()
    test_parse_steps_errors()
    test_render_template_leaks()
    test_run_steps_leak_and_deliver()
    test_run_steps_expect_failure_reports_read()
    test_run_steps_hex8_prefix()
    test_run_steps_multi_expect_leak_vars()
    test_pwn_tcp_injects_relay()
    test_run_steps_dead_target_write_reports()
    test_run_steps_nonnumeric_leak_reports()
    test_run_steps_no_send_guard()
    test_pwn_stdin_rejects_hex8()
    print("test_skills OK")


if __name__ == "__main__":
    main()
