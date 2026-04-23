"""
Fox Pydantic output schemas — single source of truth for all LLM-structured responses.
Import these instead of writing ad-hoc dataclasses or regex parsers.
"""

from typing import Any, Literal
from pydantic import BaseModel, Field


# ── Tool names ────────────────────────────────────────────────────────────────

ToolName = Literal[
    "run_bash", "run_python", "read_file",
    "write_file", "grep_search", "list_files",
]


# ── Planning schemas ──────────────────────────────────────────────────────────

class PlanStep(BaseModel):
    tool: ToolName
    description: str = Field(min_length=5, max_length=200)


class Plan(BaseModel):
    intent: str = Field(min_length=5, max_length=300)
    reasoning: str = Field(default="", max_length=1000)
    steps: list[PlanStep] = Field(min_length=1, max_length=6)


# ── Intent / validation schemas ───────────────────────────────────────────────

class Criterion(BaseModel):
    type: str
    args: dict[str, Any] = Field(default_factory=dict)


class Intent(BaseModel):
    summary: str = Field(default="", max_length=300)
    criteria: list[Criterion] = Field(default_factory=list, max_length=5)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "criteria": [{"type": c.type, "args": c.args} for c in self.criteria],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Intent":
        return cls(
            summary=d.get("summary", ""),
            criteria=[
                Criterion(type=c["type"], args=c.get("args", {}))
                for c in d.get("criteria", [])
                if "type" in c
            ],
        )


# ── Step result schema ────────────────────────────────────────────────────────

class StepResult(BaseModel):
    result: str = Field(min_length=1, max_length=500,
                        description="One-line summary of what was found or produced.")
    files_created: list[str] = Field(
        default_factory=list,
        description="Paths of files created or written by this step.",
    )
