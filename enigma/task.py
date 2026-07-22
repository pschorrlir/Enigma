"""Task model. Input and output are deliberately flexible.

A task is JSON:
{
  "description": "what to do",
  "input": <any JSON value or string>,          # optional
  "output": {"kind": "text" | "json" | "code"}, # optional, default text
  "evaluator": {                                 # optional, default llm_judge
    "kind": "llm_judge" | "python_tests" | "json_schema" | "regex" | "contains",
    ... kind-specific fields ...
  },
  "max_iterations": int,                         # optional overrides
  "target_score": float
}
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

VALID_OUTPUT_KINDS = ("text", "json", "code")


@dataclass(slots=True)
class TaskSpec:
    description: str
    input: Any = None
    output_kind: str = "text"
    evaluator: dict[str, Any] = field(default_factory=lambda: {"kind": "llm_judge"})
    max_iterations: int | None = None
    target_score: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_json(cls, data: dict[str, Any] | str) -> "TaskSpec":
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("task must be a JSON object")
        desc = data.get("description")
        if not desc or not isinstance(desc, str):
            raise ValueError("task requires a string 'description'")
        out = data.get("output") or {}
        kind = out.get("kind", "text") if isinstance(out, dict) else str(out)
        if kind not in VALID_OUTPUT_KINDS:
            raise ValueError(f"output.kind must be one of {VALID_OUTPUT_KINDS}")
        evaluator = data.get("evaluator") or {"kind": "llm_judge"}
        return cls(
            description=desc,
            input=data.get("input"),
            output_kind=kind,
            evaluator=evaluator,
            max_iterations=data.get("max_iterations"),
            target_score=data.get("target_score"),
            id=data.get("id", uuid.uuid4().hex[:12]),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "description": self.description,
                "input": self.input,
                "output": {"kind": self.output_kind},
                "evaluator": self.evaluator,
                "max_iterations": self.max_iterations,
                "target_score": self.target_score,
            }
        )

    def input_as_text(self) -> str:
        if self.input is None:
            return ""
        if isinstance(self.input, str):
            return self.input
        return json.dumps(self.input, indent=2)


@dataclass(slots=True)
class Candidate:
    content: str
    score: float = 0.0
    feedback: str = ""
    origin: str = "local"  # local | cloud


@dataclass(slots=True)
class TaskResult:
    task_id: str
    status: str  # succeeded | exhausted | failed
    best: Candidate | None
    iterations: int
    cloud_calls: int
    elapsed_s: float
