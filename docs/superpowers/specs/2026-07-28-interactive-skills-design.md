# Interactive exploit skills — pwn_stdin / pwn_tcp design

Date: 2026-07-28
Status: approved (design), pending implementation plan

## Context

Ladder autopsy 2026-07-28: rung 2 (ret2win under PIE) is unsolvable with the
current skill set. `deliver_stdin` is one-shot — it spawns a fresh process, so
a runtime address leaked in an earlier process is garbage under ASLR. The
agent's PIE *analysis* was fully correct (offsets, symbol arithmetic); only
the delivery primitive is missing. Rung 2 attempt 1's final WM proves the
agent never understood why delivery kept failing.

Fix: skills that keep one process/session open — read the leak, compute, and
deliver within it. Same constraint exists in ExploitGym (`exp.pie` tasks,
server banner → payload over one socket).

Decisions locked during brainstorm:

- **Flow:** ordered step list (`expect:` / `send:`), with the two-phase
  shorthand preserved. Not a full DSL.
- **Transports:** stdin (docker exec) AND tcp (in-container relay) now.
- TCP goes through an **in-container relay** because ExploitGym servers live
  on `cybergym-internal`, unreachable from the host.

## Architecture

One host-side **step engine** in `enigma/skills.py` + two **transports**.

Transport interface (what the engine needs):

- `read_until(pattern: str) -> tuple[str, re.Match | None]` — read until the
  regex matches, timeout, or EOF.
- `send(data: bytes) -> None`
- `read_rest() -> str` — drain until EOF/timeout.

`StdinTransport` wraps `docker exec -i <cid> <binary>`. `TcpTransport` wraps
`docker exec -i <cid> python3 /tmp/tcp_relay.py <host> <port>`; the relay
(~20 lines, python 3.5-safe, no f-strings) bridges its stdin/stdout to the
socket and is injected once per container via `docker cp`.

ToolBox plumbing:

- New `ToolBox._docker_spawn(*argv) -> asyncio.subprocess.Process` (PIPE
  stdin/stdout, `start_new_session=True`).
- `run_skill(name, args, cexec, spawn)` — new `spawn` parameter; the five
  existing skills ignore it. `ToolBox._skill` passes `self._docker_spawn`.

## Skills

Registry gains two entries:

- `pwn_stdin <binary> <steps...>`
- `pwn_tcp <host> <port> <steps...> [hex8]`

### Step grammar

- `expect:<regex>` — read until match. A capture group binds `{leak}`
  (and `{leak1}`, `{leak2}`, … in expect order when several expects capture).
- `send:<template>` — render and send. Template runs until the next step
  token, so it may contain spaces.
- Step tokens are `expect:`, `send:`, and the bare keyword `hex8`; a trailing
  bare template (shorthand for `send:`) runs to the next step token or end of
  args.
- Shorthand: `pwn_stdin <binary> <regex> <template>` ≡
  `expect:<regex> send:<template>`.
- `hex8` (tcp only, trailing keyword): wrap the final rendered payload in
  ExploitGym's 8-byte-lowercase-hex size prefix before sending.
- Limits: max 8 steps; capture groups bind numbered `{leak}` vars only — no
  named groups, no conditionals, no loops.

### Template arithmetic

`parse_payload` gains `{leak}`, `{leakN}`, `{leak+0xN}`, `{leak-0xN}` terms
(hex or decimal N), substituted with the captured integers before the
existing `X*N` / `p32/p64` parsing runs.

### Arg parsing

Space-separated tokens. `expect:`/`send:` tokens start steps; each regex must
be a single non-space token (agent writes `main:(0x\S+)`, never a literal
space). Anything unparsable returns usage text with a working example.

### Worked examples (for tool docs)

```
TOOL skill: pwn_stdin /target/rung2 main:(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)
TOOL skill: pwn_stdin /target/t expect:Choice: send:1 expect:main:(0x\S+) send:A*72 + p64({leak}-0xb9)
TOOL skill: pwn_tcp 172.18.0.5 8000 expect:welcome send:1 expect:main:(0x\S+) A*72 + p64({leak}-0xb9) hex8
```

## Error handling

Never raise (the tool-must-not-crash contract). Diagnostic text on: spawn
failure, expect timeout (15s, 64KB cap) or no-match (return what was actually
read — the agent needs it to fix its regex), bad template, relay injection
failure (docker cp output), connection refused, EOF before expect matched.

## Testing

- Unit (faked spawn): engine drives a scripted fake process — regex capture,
  multi-expect `{leak1}`/`{leak2}` binding, `{leak-0xb9}` arithmetic, hex8
  wrapping, step-limit, timeout/no-match diagnostics, usage errors.
- Live stdin (extend `homework/test_skills_live.py`): `find_symbol` for
  main/win offsets, then `pwn_stdin` with the real banner regex; assert the
  planted flag from `homework/flags.json`.
- Live tcp: a small python leak-server inside the homework container spoken
  to through the relay; assert the same flow end-to-end including hex8.
- Gate: one `pipenv run python homework/run_hw.py --rung 2` run — pass when
  the agent solves with `pwn_stdin` in the transcript (`skill_steps > 0`).

## Non-goals

- Expect/send DSL (conditionals, loops, named captures).
- UDP/TLS transports.
- The capstone homework rung (socat server replica) — homework design, not
  this spec.
- Changes to existing skills' behavior (their `run_skill` call just gains an
  ignored parameter).
