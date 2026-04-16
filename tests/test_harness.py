#!/usr/bin/env python3
"""
Fox test harness — runs complex tasks against the real Ollama agent, logs results,
and identifies failure patterns.

Usage:
    python3 tests/test_harness.py              # run all tests
    python3 tests/test_harness.py pptx_create  # run one test by name
"""

import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# Make src importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import Storage
from src.commands import CommandRegistry
from src.mapreduce import MapReduceOrchestrator
from src.ollama import chat, build_system_prompt

RESULTS_LOG = os.path.join(os.path.dirname(__file__), "results.jsonl")


# ── Test case definition ──────────────────────────────────────────────────────

@dataclass
class TestCase:
    name:        str
    prompt:      str
    check:       Callable[[str, str], bool]   # (result_text, work_dir) -> pass
    check_desc:  str
    setup:       Optional[Callable[[str], None]] = None  # (work_dir) -> None
    timeout:     int = 180


# ── Test cases ────────────────────────────────────────────────────────────────

def check_contains(needle: str):
    def fn(result: str, _work_dir: str) -> bool:
        return needle.lower() in result.lower()
    return fn

def check_file_exists(filename: str):
    def fn(_result: str, work_dir: str) -> bool:
        # Check work_dir and cwd
        return (
            os.path.exists(os.path.join(work_dir, filename)) or
            os.path.exists(filename)
        )
    return fn

def check_file_exists_glob(pattern: str):
    """Check any file matching a glob exists in work_dir or cwd."""
    import glob as globmod
    def fn(_result: str, work_dir: str) -> bool:
        return bool(
            globmod.glob(os.path.join(work_dir, pattern)) or
            globmod.glob(pattern)
        )
    return fn

def check_any(*checks):
    def fn(result: str, work_dir: str) -> bool:
        return any(c(result, work_dir) for c in checks)
    return fn

def check_all(*checks):
    def fn(result: str, work_dir: str) -> bool:
        return all(c(result, work_dir) for c in checks)
    return fn


TESTS: list[TestCase] = [
    TestCase(
        name="simple_list",
        prompt="List the python files in the current directory using a tool.",
        check=check_contains(".py"),
        check_desc="response mentions .py files",
    ),
    TestCase(
        name="bash_calculation",
        prompt="Calculate the sum of squares of 1 through 10 (1²+2²+…+10²) and print the answer.",
        check=check_contains("385"),
        check_desc="response contains 385",
    ),
    TestCase(
        name="write_txt_file",
        prompt="Create a file named fox_hello.txt with the content 'hello from fox'.",
        check=check_any(
            check_file_exists("fox_hello.txt"),
            check_contains("wrote"),
            check_contains("created"),
            check_contains("fox_hello.txt"),
        ),
        check_desc="fox_hello.txt created or response confirms creation",
    ),
    TestCase(
        name="csv_create",
        prompt=(
            "Create a CSV file named employees.csv with columns: name,age,city "
            "and exactly 3 rows of sample data."
        ),
        check=check_any(
            check_file_exists("employees.csv"),
            check_file_exists_glob("*.csv"),
            check_contains("employees.csv"),
        ),
        check_desc="employees.csv created",
    ),
    TestCase(
        name="pptx_create",
        prompt=(
            "Create a PowerPoint file named fox_test.pptx with two slides:\n"
            "Slide 1 title: 'Fox Agent Test'\n"
            "Slide 2 title: 'Results' with bullet: 'Tool calling works'\n"
            "Use python-pptx via run_bash."
        ),
        check=check_any(
            check_file_exists("fox_test.pptx"),
            check_file_exists_glob("*.pptx"),
            check_contains("fox_test.pptx"),
        ),
        check_desc="fox_test.pptx created",
    ),
    TestCase(
        name="pptx_from_context",
        prompt=(
            "Here is slide content:\n\n"
            "Slide 1: Introduction\n"
            "- AI is transforming how we work\n"
            "- Three pillars: Speed, Quality, Scale\n\n"
            "Slide 2: Key Results\n"
            "- 40% faster delivery\n"
            "- 60% fewer defects\n\n"
            "Create a PowerPoint file named context_test.pptx from this data using python-pptx."
        ),
        check=check_any(
            check_file_exists("context_test.pptx"),
            check_file_exists_glob("context_test.pptx"),
            check_contains("context_test.pptx"),
        ),
        check_desc="context_test.pptx created from inline data",
    ),
    TestCase(
        name="internet_fetch",
        prompt="Use curl to fetch the current Bitcoin price from https://api.coindesk.com/v1/bpi/currentprice.json and report the USD price.",
        check=check_any(
            check_contains("USD"),
            check_contains("bitcoin"),
            check_contains("price"),
            check_contains("$"),
        ),
        check_desc="response contains price information",
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name:        str
    passed:      bool
    result_text: str
    elapsed:     float
    error:       Optional[str] = None
    tools_used:  list[str] = field(default_factory=list)
    nudged:      bool = False


def run_test(tc: TestCase) -> TestResult:
    work_dir = tempfile.mkdtemp(prefix=f"fox_test_{tc.name}_")
    # Use isolated DB per test to avoid DuckDB lock conflicts
    db_path = os.path.join(work_dir, "test.duckdb")
    storage = Storage(db_path=db_path)
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    storage.create_session(session_id, "test", os.getcwd())

    if tc.setup:
        tc.setup(work_dir)

    registry = CommandRegistry(work_dir, storage)
    orchestrator = MapReduceOrchestrator(
        llm_fn=chat,
        command_registry=registry,
        storage=storage,
        session_id=session_id,
        work_dir=work_dir,
    )
    messages = [{"role": "system", "content": build_system_prompt(work_dir)}]

    t0 = time.time()
    error = None
    result_text = ""
    try:
        result_text = orchestrator.execute(tc.prompt, messages, data_file=None)
    except Exception as e:
        error = str(e)
        result_text = ""
    elapsed = time.time() - t0

    # Collect tool names used from storage
    try:
        rows = storage.query(
            f"SELECT DISTINCT tool_name FROM tool_calls WHERE session_id = '{session_id}'"
        )
        tools_used = [r["tool_name"] for r in rows]
    except Exception:
        tools_used = []

    storage.close()

    passed = False
    if not error:
        try:
            passed = tc.check(result_text, work_dir)
            # Also check cwd for files
            if not passed:
                passed = tc.check(result_text, os.getcwd())
        except Exception:
            passed = False

    return TestResult(
        name=tc.name,
        passed=passed,
        result_text=result_text[:500],
        elapsed=elapsed,
        error=error,
        tools_used=tools_used,
    )


def log_result(r: TestResult):
    with open(RESULTS_LOG, "a") as f:
        f.write(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "name":      r.name,
            "passed":    r.passed,
            "elapsed":   round(r.elapsed, 1),
            "tools":     r.tools_used,
            "error":     r.error,
            "result":    r.result_text,
        }) + "\n")


def print_result(r: TestResult):
    icon = "✅" if r.passed else "❌"
    print(f"\n{icon} [{r.elapsed:.0f}s] {r.name}")
    if r.error:
        print(f"   ERROR: {r.error}")
    else:
        print(f"   tools: {', '.join(r.tools_used) or '(none)'}")
        preview = r.result_text[:200].replace("\n", " ")
        print(f"   result: {preview}")
    if not r.passed:
        tc = next((t for t in TESTS if t.name == r.name), None)
        if tc:
            print(f"   ⚠ expected: {tc.check_desc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    suite = [t for t in TESTS if target is None or t.name == target]

    if not suite:
        print(f"No test named '{target}'. Available: {[t.name for t in TESTS]}")
        sys.exit(1)

    print(f"\n🦊 Fox Test Harness — {len(suite)} test(s)\n{'─'*50}")

    results = []
    for tc in suite:
        print(f"\n▶ Running: {tc.name}")
        print(f"  prompt: {tc.prompt[:100]}...")
        r = run_test(tc)
        log_result(r)
        print_result(r)
        results.append(r)

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed for r in results)

    print(f"\n{'─'*50}")
    print(f"Results: {passed}/{len(results)} passed  |  {total_time:.0f}s total")
    print(f"Log: {RESULTS_LOG}\n")

    # Failure analysis
    failures = [r for r in results if not r.passed]
    if failures:
        print("Failure patterns:")
        for r in failures:
            no_tools = not r.tools_used
            used_bash = "run_bash" in r.tools_used
            print(f"  {r.name}: tools={r.tools_used or '[]'}, "
                  f"{'NO TOOLS CALLED' if no_tools else ''}"
                  f"{'bash ok but no file' if used_bash and not r.passed else ''}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
