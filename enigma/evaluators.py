"""Pluggable evaluators. Each returns (score in [0,1], feedback text).

Kinds:
  llm_judge    {"criteria": "..."}                — local model scores the output
  python_tests {"tests": "assert solve(2)==4"}    — run candidate code + tests in a subprocess
  json_schema  {"schema": {...}}                  — minimal schema subset validator
  regex        {"pattern": "...", "flags": "is"}  — full match against output
  contains     {"all": [...], "any": [...]}       — substring checks
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from typing import Any

from .llm import OllamaClient, extract_code, extract_json
from .task import TaskSpec


@dataclass(slots=True)
class EvalResult:
    score: float
    feedback: str


class Evaluator:
    def __init__(self, spec: dict[str, Any], ollama: OllamaClient, judge_model: str):
        self.spec = spec
        self.kind = spec.get("kind", "llm_judge")
        self._ollama = ollama
        self._judge_model = judge_model

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
            case other:
                return EvalResult(0.0, f"unknown evaluator kind: {other}")

    # ---- python_tests -------------------------------------------------

    async def _python_tests(self, output: str) -> EvalResult:
        code = extract_code(output)
        tests = self.spec.get("tests", "")
        if not tests:
            return EvalResult(0.0, "python_tests evaluator has no 'tests'")
        # Split tests into individual statements so we can report partial credit.
        test_lines = [t for t in tests.splitlines() if t.strip() and not t.strip().startswith("#")]
        harness = (
            code
            + "\n\n_passed = 0\n_failures = []\n"
            + "".join(
                f"try:\n    {line.strip()}\n    _passed += 1\n"
                f"except Exception as e:\n    _failures.append({line.strip()!r} + ' -> ' + repr(e))\n"
                for line in test_lines
            )
            + "\nimport json as _j\nprint('\\n@@ENIGMA@@' + _j.dumps({'passed': _passed, 'total': "
            + str(len(test_lines))
            + ", 'failures': _failures[:5]}))\n"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                harness,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=float(self.spec.get("timeout", 20)))
        except asyncio.TimeoutError:
            proc.kill()
            return EvalResult(0.0, "candidate code timed out")
        except OSError as e:
            return EvalResult(0.0, f"could not run tests: {e}")
        text = stdout.decode(errors="replace")
        if "@@ENIGMA@@" not in text:
            err = stderr.decode(errors="replace")[-800:]
            return EvalResult(0.0, f"code failed before tests ran: {err or text[-800:]}")
        report = json.loads(text.rsplit("@@ENIGMA@@", 1)[1])
        total = max(report["total"], 1)
        score = report["passed"] / total
        fb = f"{report['passed']}/{total} tests passed"
        if report["failures"]:
            fb += "; failures: " + " | ".join(report["failures"])
        return EvalResult(score, fb)

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
        if re.search(self.spec.get("pattern", ""), output, flags):
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

    # ---- llm_judge -------------------------------------------------------

    async def _llm_judge(self, task: TaskSpec, output: str) -> EvalResult:
        criteria = self.spec.get("criteria", "correctness, completeness, and clarity")
        prompt = (
            f"You are a strict evaluator. Task:\n{task.description}\n\n"
            + (f"Task input:\n{task.input_as_text()}\n\n" if task.input is not None else "")
            + f"Candidate output:\n{output[:6000]}\n\n"
            f"Judge it on: {criteria}.\n"
            'Respond with ONLY JSON: {"score": <0.0-1.0>, "feedback": "<one short paragraph of specific problems, or \'good\'>"}'
        )
        raw = await self._ollama.generate(self._judge_model, prompt, temperature=0.0, format_json=True)
        obj = extract_json(raw)
        if not obj or "score" not in obj:
            return EvalResult(0.5, "judge produced unparseable verdict")
        try:
            score = min(1.0, max(0.0, float(obj["score"])))
        except (TypeError, ValueError):
            return EvalResult(0.5, "judge produced non-numeric score")
        return EvalResult(score, str(obj.get("feedback", "")))


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
