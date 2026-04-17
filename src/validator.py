"""
Fox intent validator — extracts success criteria from a user request,
validates the final output against them. Catches silent COMPLETEDs where the
LLM claims done but the file is missing / empty / wrong format.
"""

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Magic bytes for format validation ─────────────────────────────────────────

_MAGIC: dict[str, bytes] = {
    "pptx": b"PK\x03\x04",   # Office formats are zip containers
    "xlsx": b"PK\x03\x04",
    "docx": b"PK\x03\x04",
    "zip":  b"PK\x03\x04",
    "pdf":  b"%PDF",
    "png":  b"\x89PNG",
    "jpg":  b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff",
    "gif":  b"GIF8",
}


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Criterion:
    type: str
    args: dict = field(default_factory=dict)


@dataclass
class Intent:
    summary: str = ""
    criteria: list[Criterion] = field(default_factory=list)

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


# ── Extraction ────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """You extract success criteria from a user request.
Output ONLY a single JSON object — no prose, no markdown fences.

Schema:
{
  "summary": "one-line goal",
  "criteria": [
    {"type": "file_exists",     "args": {"path_pattern": "*.pptx", "min_bytes": 500}},
    {"type": "file_format",     "args": {"path_pattern": "*.pptx", "format": "pptx"}},
    {"type": "output_contains", "args": {"keywords": ["foo"]}}
  ]
}

Criterion types:
- file_exists: a file matching path_pattern (glob) must exist. min_bytes is an optional floor.
- file_format: file must have correct magic bytes for format (pptx/xlsx/docx/pdf/png/jpg/zip).
- output_contains: the final text response must include these keywords (case-insensitive).

Rules:
- Include criteria ONLY for things explicitly requested. Do not invent.
- For file creation: always pair file_exists (min_bytes 500 for binary, 10 for text) with file_format.
- Maximum 3 criteria. Use globs like "*.pptx" unless the user named a file.
- For questions / explanations with no file output, return criteria: [].
"""


def extract_intent(llm_fn, user_input: str) -> Optional[Intent]:
    """Single LLM call. Returns None on parse failure (skip validation)."""
    try:
        response = llm_fn(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": user_input},
            ],
            use_tools=False, think=False,
        )
        content = (response.get("content") or "").strip()
        # Strip markdown fences the model often adds despite instructions
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        # Grab the first JSON object if there's trailing prose
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        return Intent.from_dict(data)
    except Exception:
        return None


# ── Validation checks ─────────────────────────────────────────────────────────

def _resolve_paths(pattern: str, work_dir: str) -> list[str]:
    """Resolve a glob against work_dir, then cwd. Absolute patterns pass through."""
    if os.path.isabs(pattern):
        return glob.glob(pattern)
    hits = glob.glob(os.path.join(work_dir, pattern))
    if not hits:
        hits = glob.glob(os.path.join(os.getcwd(), pattern))
    return hits


def _check_file_exists(args: dict, work_dir: str) -> Optional[str]:
    pattern = args.get("path_pattern", "")
    if not pattern:
        return None
    min_bytes = int(args.get("min_bytes", 0))
    matches = _resolve_paths(pattern, work_dir)
    if not matches:
        return f"no file matching {pattern!r}"
    big_enough = [m for m in matches if os.path.getsize(m) >= min_bytes]
    if not big_enough:
        sizes = ", ".join(f"{os.path.basename(m)}={os.path.getsize(m)}B" for m in matches)
        return f"{pattern!r} exists but too small (need ≥{min_bytes}B): {sizes}"
    return None


def _check_file_format(args: dict, work_dir: str) -> Optional[str]:
    pattern = args.get("path_pattern", "")
    fmt = args.get("format", "").lower()
    magic = _MAGIC.get(fmt)
    if not pattern or not magic:
        return None
    matches = _resolve_paths(pattern, work_dir)
    if not matches:
        return f"no file to format-check: {pattern!r}"
    for m in matches:
        try:
            with open(m, "rb") as f:
                if f.read(len(magic)) == magic:
                    return None
        except Exception:
            continue
    files = ", ".join(os.path.basename(m) for m in matches)
    return f"{files} does not match {fmt} magic bytes (likely text written to a binary path)"


def _check_output_contains(args: dict, output: str) -> Optional[str]:
    keywords = args.get("keywords", []) or []
    out_lower = (output or "").lower()
    missing = [kw for kw in keywords if kw.lower() not in out_lower]
    if missing:
        return f"response missing: {missing}"
    return None


_CHECKS = {
    "file_exists":     lambda args, wd, out: _check_file_exists(args, wd),
    "file_format":     lambda args, wd, out: _check_file_format(args, wd),
    "output_contains": lambda args, wd, out: _check_output_contains(args, out),
}


def validate(intent: Intent, final_output: str, work_dir: str) -> tuple[bool, list[str]]:
    """Run each criterion. Returns (all_passed, list_of_failure_reasons)."""
    if not intent.criteria:
        return True, []
    failures: list[str] = []
    for c in intent.criteria:
        check = _CHECKS.get(c.type)
        if check is None:
            continue  # unknown type — forward-compat
        err = check(c.args, work_dir, final_output)
        if err:
            failures.append(f"[{c.type}] {err}")
    return len(failures) == 0, failures
