# Skill tools — executable skill compilation for the Enigma agent

Date: 2026-07-28
Status: approved (design), pending implementation plan

## Context

Ladder result 2026-07-28: zero solves on the homework ladder (rung 1 ret2win
included) despite clean agent behavior. AGENTS.md conclusion: "harness is
solved; the gap is craft, and imitation (skill compilation / solve-script
exemplars / LoRA on solve traces) is the lever." This spec covers the chosen
form: **executable skill tools** — curated exploitation procedures exposed as
callable tools so the model supplies intent while proven code supplies craft.

Decisions locked during brainstorm:

- **Form:** atomic primitive skills (not macro solve-per-bug-class tools).
- **Execution:** host-side Python driving `docker exec` (refactored from the
  proven `homework/solve_rung*.py` logic); no in-container interpreters.
- **Scope:** all container-bound runs — homework (`run_hw.py`) and the
  ExploitGym bridge both get them via `ToolBox.bind_container`.
- **Measurement:** skill usage is tagged; assisted vs unaided solves reported
  separately (grounded-competence north star).
- **Invocation:** single `skill` tool backed by a registry; parser untouched.

## Architecture

New module `enigma/skills.py`:

- `SKILLS: dict[str, Skill]` registry. A `Skill` is `(name, doc, coroutine)`.
- Each skill coroutine signature: `(docker_exec, args: str) -> str`, where
  `docker_exec` is the toolbox's existing container exec helper
  (`ToolBox._docker`), so skills share its timeout and output merging.
- `skill_docs() -> str` renders the registry into the tool-docs block.

`enigma/tools.py`:

- Container-mode tool set gains `"skill"`: `self._names = ("shell", "write", "read", "calc", "skill")`.
- `run()` dispatches `name == "skill"` → parse first token as skill name,
  remainder as args → look up registry → await the coroutine with
  `self._docker`. Unknown name returns the available-skill list (mirrors the
  existing `unknown tool` path).
- `docs()` container block gains a `skill` entry: one line per skill plus one
  worked example (`TOOL skill: discover_offset /target/rung1`).

Port, don't refactor: the logic is **copied** out of
`homework/solve_rung1.py` into `enigma/skills.py`; `solve_rung1.py` stays
untouched as the solvability-proof artifact. Duplication accepted
deliberately.

## Skill inventory (v1)

Five atomic primitives:

- `discover_offset <binary>` — De Bruijn cyclic pattern (256 bytes) written to
  `/tmp/pat.txt`, `gdb -q -batch` crash, offset recovered from `$rip`
  (canonical case) or the qword at `$rsp` (non-canonical ret). Ported from
  `solve_rung1.py:56-88`. Returns the integer offset, or the gdb output plus
  the documented `-O0` fallback (72) when inconclusive.
- `find_symbol <binary> <name>` — `nm` lookup. Returns the absolute address
  (static/non-PIE) and, when the binary is PIE, the symbol's file offset so
  the agent can do its own leak arithmetic (rung 2).
- `cyclic <n>` and `cyclic_find <hexbytes>` — direct access to the pattern
  generator/finder so the agent can hand-craft payloads without pwntools.
- `deliver_stdin <binary> <payload_spec>` — run the target with a payload on
  stdin, return its output. `payload_spec` mini-DSL: literal char repeats and
  packed addresses, e.g. `A*72 + p64(0x4011d6)`. Parsed host-side into bytes.

Explicitly excluded (YAGNI): leak parsing, ret2libc chains, heap helpers.
Added later only when rung 2/3 or ExploitGym runs demand them.

## Prompt surface and tagging

- Tool docs gain the `skill` section (names + one-line docs + worked example).
  No changes to stop sequences, parser, or any intervention.
- Skill calls are inherently distinguishable in the transcript
  (`tool == "skill"`). `homework/run_hw.py` adds `skill_steps` (count of tool
  records with name `skill`) and `solved_with_skill` (status solved AND
  `skill_steps > 0`) to the result dict and the final printout;
  `homework/ladder.py` reports assisted vs unaided solves separately.

## Error handling

- Unknown skill name → return the list of available skills; never raise.
- gdb/nm failures or inconclusive offset → return the failing tool's output
  verbatim plus the documented fallback; never an exception (a tool must
  never crash the generation — existing `ToolBox.run` contract).
- All skills inherit the existing docker-exec timeout; no new failure modes
  in `agent_run`.

## Testing

- Unit tests (alongside `homework/test_ladder.py`): boot the homework image,
  assert `discover_offset` returns 72 on `/target/rung1`, `find_symbol`
  returns win's known address, `cyclic`/`cyclic_find` round-trip, and
  `deliver_stdin` with the correct spec makes rung1 print the flag.
- Integration gate: one `pipenv run python homework/run_hw.py --rung 1` run —
  pass when the agent solves with `skill_steps > 0` and the transcript shows
  the tags.
- Regression: existing interventions (blocks, pivots, PRM rerank) untouched;
  `skill` is one more tool in the set.

## Non-goals

- LoRA/DPO training on skill traces (later stage, fed by these runs).
- Auto-generated rung variants, additional rungs (already in homework/PLAN.md
  expansion list).
- Dream consolidation repoint (separate known breakage: gemma4:26b deleted).
