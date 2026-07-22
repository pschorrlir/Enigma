"""Pluggable evaluators. Each returns (score in [0,1], feedback text).

Kinds:
  llm_judge    {"criteria": "..."}                — rubric-decomposed local judging
  python_tests {"tests": "assert solve(2)==4"}    — run candidate code + tests in a subprocess
  json_schema  {"schema": {...}}                  — minimal schema subset validator
  regex        {"pattern": "...", "flags": "is"}  — search against output
  contains     {"all": [...], "any": [...]}       — substring checks
  prm          {"aggregate": "min|mean|prod|last"} — step-level scoring via the
               Qwen2.5-Math-PRM sidecar (sidecar/prm_server.py); best for
               multi-step reasoning/derivation tasks
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from typing import Any

from .llm import AnthropicClient, LLMError, OllamaClient, extract_code, extract_json
from .task import TaskSpec

_REGEX_HAYSTACK_LIMIT = 200_000


@dataclass(slots=True)
class EvalResult:
    score: float
    feedback: str


class Evaluator:
    """One instance per task run — caches the generated rubric across candidates."""

    def __init__(
        self,
        spec: dict[str, Any],
        ollama: OllamaClient | None,
        judge_model: str,
        cloud: AnthropicClient | None = None,
        prm_url: str = "http://127.0.0.1:8799",
    ):
        self.spec = spec
        self.kind = spec.get("kind", "llm_judge")
        self._ollama = ollama
        self._judge_model = judge_model
        self._cloud = cloud
        self._prm_url = str(spec.get("url", prm_url)).rstrip("/")
        self._rubric: list[dict[str, Any]] | None = None
        self._rubric_failed = False

    async def evaluate(self, task: TaskSpec, output: str) -> EvalResult:
        match self.kind:
            case "python_tests":
                return await self._python_tests(output)
            case "json_schema":
                return self._json_schema(output)
            case "regex":
                return self._regex(output)
            case "contains":
                return self._contains(output)
            case "llm_judge":
                return await self._llm_judge(task, output)
            case "prm":
                return await self._prm(task, output)
            case other:
                return EvalResult(0.0, f"unknown evaluator kind: {other}")

    # ---- python_tests -------------------------------------------------

    async def _python_tests(self, output: str) -> EvalResult:
        code = extract_code(output)
        tests = self.spec.get("tests", "")
        if not tests:
            return EvalResult(0.0, "python_tests evaluator has no 'tests'")
        try:
            stmts = _split_statements(tests)
        except SyntaxError as e:
            return EvalResult(0.0, f"tests are not valid python: {e}")
        if not stmts:
            return EvalResult(0.0, "tests contain no statements")
        try:
            timeout = float(self.spec.get("timeout", 20) or 20)
        except (TypeError, ValueError):
            timeout = 20.0

        harness = (
            code
            + "\n\n_passed, _failures = 0, []\n"
            + f"_stmts = {stmts!r}\n"
            + "for _s in _stmts:\n"
            + "    try:\n"
            + "        exec(compile(_s, '<test>', 'exec'), globals())\n"
            + "        _passed += 1\n"
            + "    except BaseException as _e:\n"
            + "        _failures.append(_s.replace(chr(10), ' ')[:100] + ' -> ' + repr(_e)[:150])\n"
            + "import json as _j\n"
            + "print('\\n@@ENIGMA@@' + _j.dumps({'passed': _passed, 'total': len(_stmts), 'failures': _failures[:5]}))\n"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                harness,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: we can kill grandchildren
            )
        except OSError as e:
            return EvalResult(0.0, f"could not run tests: {e}")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            return EvalResult(0.0, "candidate code timed out")
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        text = stdout.decode(errors="replace")
        if "@@ENIGMA@@" not in text:
            err = stderr.decode(errors="replace")[-800:]
            return EvalResult(0.0, f"code failed before tests ran: {err or text[-800:]}")
        # Candidate code may print after the marker (atexit, threads) — take one line.
        report_line = text.rsplit("@@ENIGMA@@", 1)[1].splitlines()[0] if text.rsplit("@@ENIGMA@@", 1)[1] else ""
        try:
            report = json.loads(report_line)
            passed, total = int(report["passed"]), max(int(report["total"]), 1)
            failures = list(report.get("failures", []))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return EvalResult(0.0, "test report was corrupted by candidate output")
        fb = f"{passed}/{total} tests passed"
        if failures:
            fb += "; failures: " + " | ".join(str(f) for f in failures)
        return EvalResult(passed / total, fb)

    # ---- json_schema ---------------------------------------------------

    def _json_schema(self, output: str) -> EvalResult:
        obj = extract_json(output)
        if obj is None:
            try:
                obj = json.loads(extract_code(output))
            except json.JSONDecodeError:
                return EvalResult(0.0, "output is not valid JSON")
        errors: list[str] = []
        _validate(obj, self.spec.get("schema", {}), "$", errors)
        if errors:
            return EvalResult(max(0.0, 1.0 - 0.25 * len(errors)), "schema violations: " + "; ".join(errors[:6]))
        return EvalResult(1.0, "valid against schema")

    # ---- regex / contains ----------------------------------------------

    def _regex(self, output: str) -> EvalResult:
        flags = 0
        for ch in self.spec.get("flags", ""):
            flags |= {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}.get(ch, 0)
        try:
            pattern = re.compile(self.spec.get("pattern", ""), flags)
        except re.error as e:
            return EvalResult(0.0, f"invalid regex pattern: {e}")
        if pattern.search(output[:_REGEX_HAYSTACK_LIMIT]):
            return EvalResult(1.0, "pattern matched")
        return EvalResult(0.0, f"output did not match /{self.spec.get('pattern', '')}/")

    def _contains(self, output: str) -> EvalResult:
        must = self.spec.get("all", [])
        any_of = self.spec.get("any", [])
        missing = [s for s in must if s not in output]
        any_ok = (not any_of) or any(s in output for s in any_of)
        checks = len(must) + (1 if any_of else 0)
        passed = (len(must) - len(missing)) + (1 if (any_of and any_ok) else 0)
        score = passed / checks if checks else 1.0
        fb = "all substrings present" if score == 1.0 else f"missing: {missing[:5]}" + ("" if any_ok else "; no 'any' matched")
        return EvalResult(score, fb)

    # ---- llm_judge: rubric-decomposed (Rubrics-as-Rewards) ----------------

    async def _llm_judge(self, task: TaskSpec, output: str) -> EvalResult:
        rubric = await self._get_rubric(task)
        if rubric:
            return await self._judge_by_rubric(task, output, rubric)
        return await self._judge_holistic(task, output)

    async def _get_rubric(self, task: TaskSpec) -> list[dict[str, Any]] | None:
        """Build a weighted binary checklist once per task; cloud model if available."""
        if self._rubric is not None or self._rubric_failed:
            return self._rubric
        criteria = self.spec.get("criteria", "correctness, completeness, and clarity")
        prompt = (
            f"Task:\n{task.description[:2000]}\n\n"
            f"Quality criteria: {criteria}\n\n"
            "Decompose these into 4-7 binary checks a grader can answer yes/no about a "
            "candidate output. Respond with ONLY JSON: "
            '{"rubric": [{"check": "<specific yes/no question>", "weight": <1-3>}, ...]}'
        )
        try:
            if self._cloud is not None and self._cloud.enabled:
                raw = await self._cloud.generate(prompt, max_tokens=1024)
            else:
                raw = (await self._ollama.generate(self._judge_model, prompt, temperature=0.0, format_json=True)).text
        except LLMError:
            self._rubric_failed = True
            return None
        obj = extract_json(raw)
        items = obj.get("rubric") if obj else None
        if not isinstance(items, list):
            self._rubric_failed = True
            return None
        rubric = [
            {"check": str(i["check"])[:300], "weight": max(1.0, min(3.0, float(i.get("weight", 1))))}
            for i in items
            if isinstance(i, dict) and i.get("check")
        ][:7]
        if len(rubric) < 2:
            self._rubric_failed = True
            return None
        self._rubric = rubric
        return rubric

    async def _judge_by_rubric(self, task: TaskSpec, output: str, rubric: list[dict[str, Any]]) -> EvalResult:
        checklist = "\n".join(f"{i+1}. {item['check']}" for i, item in enumerate(rubric))
        prompt = (
            f"Task:\n{task.description[:2000]}\n\n"
            + (f"Task input:\n{task.input_as_text()[:2000]}\n\n" if task.input is not None else "")
            + f"Candidate output:\n{output[:6000]}\n\n"
            f"Answer each check strictly for this candidate:\n{checklist}\n\n"
            'Respond with ONLY JSON: {"checks": [true/false per item, in order], '
            '"feedback": "<one short paragraph naming the failed checks, or \'good\'>"}'
        )
        raw = (await self._ollama.generate(self._judge_model, prompt, temperature=0.0, format_json=True)).text
        obj = extract_json(raw)
        checks = obj.get("checks") if obj else None
        if not isinstance(checks, list) or len(checks) != len(rubric):
            return await self._judge_holistic(task, output)
        total = sum(item["weight"] for item in rubric)
        got = sum(item["weight"] for item, ok in zip(rubric, checks) if bool(ok))
        return EvalResult(got / total, str(obj.get("feedback", ""))[:1500])

    async def _judge_holistic(self, task: TaskSpec, output: str) -> EvalResult:
        criteria = self.spec.get("criteria", "correctness, completeness, and clarity")
        prompt = (
            f"You are a strict evaluator. Task:\n{task.description}\n\n"
            + (f"Task input:\n{task.input_as_text()}\n\n" if task.input is not None else "")
            + f"Candidate output:\n{output[:6000]}\n\n"
            f"Judge it on: {criteria}.\n"
            'Respond with ONLY JSON: {"score": <0.0-1.0>, "feedback": "<one short paragraph of specific problems, or \'good\'>"}'
        )
        raw = (await self._ollama.generate(self._judge_model, prompt, temperature=0.0, format_json=True)).text
        obj = extract_json(raw)
        if not obj or "score" not in obj:
            return EvalResult(0.5, "judge produced unparseable verdict")
        try:
            score = min(1.0, max(0.0, float(obj["score"])))
        except (TypeError, ValueError):
            return EvalResult(0.5, "judge produced non-numeric score")
        return EvalResult(score, str(obj.get("feedback", "")))


    # ---- prm: step-level process reward via sidecar -------------------------

    async def _prm(self, task: TaskSpec, output: str) -> EvalResult:
        steps = _split_steps(output)
        if not steps:
            return EvalResult(0.0, "output has no scoreable steps")
        query = task.description + (("\n\n" + task.input_as_text()[:2000]) if task.input is not None else "")
        try:
            r = await self._ollama.http.post(
                f"{self._prm_url}/score",
                json={"query": query, "steps": steps},
                timeout=90.0,
            )
        except Exception as e:
            return EvalResult(0.0, f"prm sidecar unreachable at {self._prm_url}: {e}")
        if r.status_code != 200:
            detail = r.text[:200]
            return EvalResult(0.0, f"prm sidecar error {r.status_code}: {detail}")
        data = r.json()
        scores = data.get("step_scores", [])
        if not scores:
            return EvalResult(0.0, "prm returned no step scores")
        aggregate = self.spec.get("aggregate", "min")
        score = float(data.get(aggregate, min(scores)))
        weakest = min(range(len(scores)), key=lambda i: scores[i])
        fb = f"step scores (agg={aggregate}): " + ", ".join(f"{s:.2f}" for s in scores)
        if scores[weakest] < 0.8:
            fb += f" | weakest step #{weakest + 1}: \"{steps[weakest][:200]}\""
        return EvalResult(min(1.0, max(0.0, score)), fb)


def _split_steps(output: str) -> list[str]:
    """Split a solution into reasoning steps: paragraph blocks first, then
    numbered/bulleted lines, then sentences as a last resort."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", output) if b.strip()]
    if len(blocks) >= 2:
        return blocks[:32]
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    numbered = [l for l in lines if re.match(r"^(\d+[.)]|[-*•])\s+", l)]
    if len(numbered) >= 2:
        return numbered[:32]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", output) if len(s.strip()) > 10]
    return sentences[:32]


def _split_statements(tests: str) -> list[str]:
    """Split test source into top-level statements (multi-line safe)."""
    tree = ast.parse(tests)
    return [ast.unparse(node) for node in tree.body]


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the child's whole process group and reap it."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    try:
        await proc.wait()
    except Exception:
        pass


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    """Minimal JSON-schema subset: type, properties, required, items, enum."""
    t = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool, "null": type(None)}
    if t and t in type_map and not isinstance(value, type_map[t]):
        errors.append(f"{path}: expected {t}")
        return
    if t == "number" and isinstance(value, bool):
        errors.append(f"{path}: expected number")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required key")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", errors)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]", errors)
