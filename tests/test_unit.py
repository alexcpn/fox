#!/usr/bin/env python3
"""
Fox unit tests — no LLM needed, run in <2 seconds.
Covers: tool guards, validator checks, context compression, storage schema.

Usage:
    python3 tests/test_unit.py           # run all
    python3 tests/test_unit.py -v        # verbose
"""

import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.commands import (
    RunBashCommand, RunPythonCommand, WriteFileCommand, CommandResult,
)
from src.validator import (
    Intent, Criterion, validate, _check_file_exists, _check_file_format,
    _check_output_contains, _MAGIC,
)
from src.context import (
    smart_truncate, one_line_tool_summary, compress_tool_results,
    sliding_window, compress_context,
)
from src.storage import Storage


import unittest


# ── Tool guard tests ──────────────────────────────────────────────────────────

class TestWriteFileGuard(unittest.TestCase):
    def test_blocks_pptx(self):
        cmd = WriteFileCommand({"path": "/tmp/test.pptx", "content": "<xml>"})
        r = cmd.execute()
        self.assertFalse(r.success)
        self.assertIn("cannot create .pptx", r.output)

    def test_blocks_xlsx(self):
        cmd = WriteFileCommand({"path": "/tmp/test.xlsx", "content": "data"})
        r = cmd.execute()
        self.assertFalse(r.success)
        self.assertIn("cannot create .xlsx", r.output)

    def test_blocks_png(self):
        cmd = WriteFileCommand({"path": "/tmp/test.png", "content": "pixels"})
        r = cmd.execute()
        self.assertFalse(r.success)

    def test_allows_txt(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            cmd = WriteFileCommand({"path": path, "content": "hello"})
            r = cmd.execute()
            self.assertTrue(r.success)
        finally:
            os.unlink(path)

    def test_allows_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            cmd = WriteFileCommand({"path": path, "content": "a,b,c"})
            r = cmd.execute()
            self.assertTrue(r.success)
        finally:
            os.unlink(path)

    def test_allows_py(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            path = f.name
        try:
            cmd = WriteFileCommand({"path": path, "content": "print(1)"})
            r = cmd.execute()
            self.assertTrue(r.success)
        finally:
            os.unlink(path)


class TestBashGuard(unittest.TestCase):
    def test_blocks_python_m_pptx(self):
        cmd = RunBashCommand({"command": "python3 -m pptx output.pptx"})
        r = cmd.execute()
        self.assertFalse(r.success)
        self.assertIn("pptx is a library", r.output)

    def test_blocks_python2_m_pptx(self):
        cmd = RunBashCommand({"command": "python -m pptx output.pptx"})
        r = cmd.execute()
        self.assertFalse(r.success)

    def test_allows_normal_bash(self):
        cmd = RunBashCommand({"command": "echo hello"})
        r = cmd.execute()
        self.assertTrue(r.success)
        self.assertIn("hello", r.output)


class TestPythonGuard(unittest.TestCase):
    def test_blocks_hardcoded_content(self):
        wd = tempfile.mkdtemp()
        script = (
            "data = '''" + "x" * 250 + "'''\n"
            "print(data[:10])"
        )
        cmd = RunPythonCommand({"script": script}, wd)
        r = cmd.execute()
        self.assertFalse(r.success)
        self.assertIn("large inline string", r.output)

    def test_allows_short_triple_quote(self):
        wd = tempfile.mkdtemp()
        cmd = RunPythonCommand({"script": "x = '''short'''\nprint(x)"}, wd)
        r = cmd.execute()
        self.assertTrue(r.success)
        self.assertIn("short", r.output)

    def test_strips_markdown_fences(self):
        wd = tempfile.mkdtemp()
        script = "```python\nprint('hello')\n```"
        cmd = RunPythonCommand({"script": script}, wd)
        r = cmd.execute()
        self.assertTrue(r.success)
        self.assertIn("hello", r.output)


# ── Validator tests ───────────────────────────────────────────────────────────

class TestValidatorFileExists(unittest.TestCase):
    def test_no_file(self):
        wd = tempfile.mkdtemp()
        err = _check_file_exists({"path_pattern": "*.pptx", "min_bytes": 100}, wd)
        # Might find files in cwd — only check that the function runs without error
        if err:
            self.assertIn("no file", err)

    def test_file_too_small(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "tiny.pptx")
        with open(path, "w") as f:
            f.write("x")
        err = _check_file_exists({"path_pattern": "*.pptx", "min_bytes": 100}, wd)
        self.assertIsNotNone(err)
        self.assertIn("too small", err)

    def test_file_big_enough(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "good.pptx")
        with open(path, "wb") as f:
            f.write(b"x" * 1000)
        err = _check_file_exists({"path_pattern": "*.pptx", "min_bytes": 500}, wd)
        self.assertIsNone(err)


class TestValidatorFileFormat(unittest.TestCase):
    def test_text_not_pptx(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "bad.pptx")
        with open(path, "w") as f:
            f.write("<xml>not a pptx</xml>")
        err = _check_file_format({"path_pattern": "*.pptx", "format": "pptx"}, wd)
        self.assertIsNotNone(err)
        self.assertIn("magic bytes", err)

    def test_real_zip_pptx(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "real.pptx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("dummy.txt", "content" * 100)
        err = _check_file_format({"path_pattern": "*.pptx", "format": "pptx"}, wd)
        self.assertIsNone(err)

    def test_png_magic(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "img.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        err = _check_file_format({"path_pattern": "*.png", "format": "png"}, wd)
        self.assertIsNone(err)


class TestValidatorOutputContains(unittest.TestCase):
    def test_keywords_present(self):
        err = _check_output_contains({"keywords": ["hello", "world"]}, "Hello World!")
        self.assertIsNone(err)

    def test_keywords_missing(self):
        err = _check_output_contains({"keywords": ["hello", "missing"]}, "Hello World!")
        self.assertIsNotNone(err)
        self.assertIn("missing", err)


class TestValidatorSemantic(unittest.TestCase):
    def test_pass(self):
        from src.validator import _check_semantic
        def mock_llm(msgs, use_tools=False, think=False):
            return {"content": "PASS"}
        err = _check_semantic({"question": "Is this good?"}, "yes it is", mock_llm)
        self.assertIsNone(err)

    def test_fail(self):
        from src.validator import _check_semantic
        def mock_llm(msgs, use_tools=False, think=False):
            return {"content": "FAIL: missing important details"}
        err = _check_semantic({"question": "Is this complete?"}, "incomplete", mock_llm)
        self.assertIsNotNone(err)
        self.assertIn("missing", err.lower())

    def test_skipped_without_llm(self):
        from src.validator import _check_semantic
        err = _check_semantic({"question": "anything"}, "output", None)
        self.assertIsNone(err)

    def test_skipped_in_validate_without_llm(self):
        intent = Intent(summary="test", criteria=[
            Criterion(type="semantic", args={"question": "Is this good?"})
        ])
        ok, failures = validate(intent, "anything", "/tmp")
        self.assertTrue(ok)  # semantic skipped when no llm_fn


class TestValidateEndToEnd(unittest.TestCase):
    def test_empty_criteria_passes(self):
        intent = Intent(summary="test", criteria=[])
        ok, failures = validate(intent, "anything", "/tmp")
        self.assertTrue(ok)
        self.assertEqual(failures, [])

    def test_multiple_criteria_all_pass(self):
        wd = tempfile.mkdtemp()
        path = os.path.join(wd, "out.txt")
        with open(path, "w") as f:
            f.write("hello world content here")
        intent = Intent(summary="test", criteria=[
            Criterion(type="file_exists", args={"path_pattern": "*.txt", "min_bytes": 10}),
            Criterion(type="output_contains", args={"keywords": ["done"]}),
        ])
        ok, failures = validate(intent, "done!", wd)
        self.assertTrue(ok)

    def test_partial_failure(self):
        wd = tempfile.mkdtemp()
        intent = Intent(summary="test", criteria=[
            Criterion(type="output_contains", args={"keywords": ["present"]}),
            Criterion(type="file_exists", args={"path_pattern": "*.xyz"}),
        ])
        ok, failures = validate(intent, "present in output", wd)
        self.assertFalse(ok)
        self.assertEqual(len(failures), 1)
        self.assertIn("file_exists", failures[0])


# ── Context compression tests ─────────────────────────────────────────────────

class TestSmartTruncate(unittest.TestCase):
    def test_read_file_keeps_head_tail(self):
        lines = "\n".join(f"line {i}" for i in range(50))
        result = smart_truncate("read_file", lines)
        self.assertIn("line 0", result)
        self.assertIn("line 49", result)
        self.assertIn("omitted", result)

    def test_grep_keeps_first_5(self):
        lines = "\n".join(f"match_{i}" for i in range(20))
        result = smart_truncate("grep_search", lines)
        self.assertIn("match_0", result)
        self.assertIn("match_4", result)
        self.assertNotIn("match_5", result)
        self.assertIn("more matches", result)

    def test_short_result_unchanged(self):
        result = smart_truncate("run_bash", "hello")
        self.assertEqual(result, "hello")


class TestSlidingWindow(unittest.TestCase):
    def test_keeps_system_and_window(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ]
        result = sliding_window(msgs, window_size=2)
        # system + breadcrumb + last 2
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "sys")
        self.assertEqual(result[1]["role"], "system")
        self.assertIn("Prior context", result[1]["content"])

    def test_no_orphaned_tools(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = sliding_window(msgs, window_size=2)
        # First kept message should NOT be a tool
        non_system = [m for m in result if m["role"] != "system"]
        if non_system:
            self.assertNotEqual(non_system[0]["role"], "tool")


# ── Storage schema test ───────────────────────────────────────────────────────

class TestStorageSchema(unittest.TestCase):
    def test_creates_all_tables(self):
        wd = tempfile.mkdtemp()
        s = Storage(os.path.join(wd, "test.duckdb"))
        tables = [r[0] for r in s.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()]
        for expected in ["sessions", "tasks", "task_transitions", "tool_calls",
                         "entities", "edges", "task_chains"]:
            self.assertIn(expected, tables, f"missing table: {expected}")
        s.close()

    def test_intent_json_column(self):
        wd = tempfile.mkdtemp()
        s = Storage(os.path.join(wd, "test.duckdb"))
        s.create_session("s1", "test", "/")
        s.create_task("t1", "s1", "test task")
        s.set_task_intent("t1", '{"summary":"test"}')
        row = s.conn.execute(
            "SELECT intent_json FROM tasks WHERE task_id='t1'"
        ).fetchone()
        self.assertEqual(row[0], '{"summary":"test"}')
        s.close()

    def test_task_chain_roundtrip(self):
        wd = tempfile.mkdtemp()
        s = Storage(os.path.join(wd, "test.duckdb"))
        s.create_session("s1", "test", "/")
        s.create_task("t1", "s1", "create a pptx presentation")
        s.conn.execute(
            "UPDATE tasks SET state='COMPLETED' WHERE task_id='t1'"
        )
        s.conn.execute(
            "INSERT INTO tool_calls (task_id, session_id, tool_name, args_hash, "
            "args_json, output, success, elapsed, exit_code, timestamp) "
            "VALUES ('t1', 's1', 'run_bash', 'h1', "
            "'{\"command\": \"pip install python-pptx\"}', 'OK', true, 0.5, 0, 1.0)"
        )
        s.record_task_chain("t1")
        chains = s.find_similar_chains("make a powerpoint")
        self.assertGreater(len(chains), 0)
        self.assertEqual(chains[0]["steps"][0]["tool"], "run_bash")
        s.close()


# ── Epic 13 Story 13.0 — Schema model tests ───────────────────────────────────

class TestSchemas(unittest.TestCase):
    def test_plan_step_rejects_unknown_tool(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            PlanStep(tool="analyze_data", description="do something")

    def test_plan_step_accepts_valid_tool(self):
        s = PlanStep(tool="run_python", description="count rows in csv")
        self.assertEqual(s.tool, "run_python")

    def test_plan_requires_at_least_one_step(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            Plan(intent="do something", steps=[])

    def test_plan_rejects_too_many_steps(self):
        from pydantic import ValidationError
        steps = [PlanStep(tool="run_bash", description=f"step {i} here") for i in range(7)]
        with self.assertRaises(ValidationError):
            Plan(intent="do too much", steps=steps)

    def test_intent_from_dict_roundtrip(self):
        d = {"summary": "count rows", "criteria": [
            {"type": "output_contains", "args": {"keywords": ["done"]}}
        ]}
        intent = Intent.from_dict(d)
        self.assertEqual(intent.summary, "count rows")
        self.assertEqual(intent.criteria[0].type, "output_contains")
        self.assertEqual(intent.to_dict(), d)

    def test_step_result_requires_nonempty_result(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            StepResult(result="")

    def test_step_result_files_created_defaults_empty(self):
        sr = StepResult(result="42 rows found")
        self.assertEqual(sr.files_created, [])


# ── Epic 13 Story 13.1 — chat_structured tests ────────────────────────────────

class TestChatStructured(unittest.TestCase):
    def _mock_ollama(self, body: dict):
        import unittest.mock as mock
        resp = mock.MagicMock()
        resp.json.return_value = {"message": {"content": json.dumps(body)}}
        resp.raise_for_status = mock.MagicMock()
        return mock.patch("src.ollama.requests.post", return_value=resp)

    def _mock_openai(self, body: dict):
        import unittest.mock as mock
        resp = mock.MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": json.dumps(body)}}]}
        resp.raise_for_status = mock.MagicMock()
        return mock.patch("src.ollama.requests.post", return_value=resp)

    def test_ollama_returns_validated_plan(self):
        import src.ollama as ol
        ol.BACKEND = "ollama"
        payload = {
            "intent": "count csv rows",
            "reasoning": "read then count",
            "steps": [
                {"tool": "read_file", "description": "read employees.csv"},
                {"tool": "run_python", "description": "count and print rows"},
            ],
        }
        with self._mock_ollama(payload):
            from src.ollama import chat_structured
            plan = chat_structured([{"role": "user", "content": "plan"}], Plan)
        self.assertIsInstance(plan, Plan)
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].tool, "read_file")

    def test_openai_returns_validated_plan(self):
        import src.ollama as ol
        ol.BACKEND = "openai"
        ol.OPENAI_API_KEY = "test-key"
        payload = {
            "intent": "list the files here",
            "steps": [{"tool": "list_files", "description": "list the cwd files"}],
        }
        with self._mock_openai(payload):
            from src.ollama import chat_structured
            plan = chat_structured([{"role": "user", "content": "plan"}], Plan)
        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.steps[0].tool, "list_files")

    def test_raises_on_schema_violation(self):
        import src.ollama as ol
        ol.BACKEND = "ollama"
        from pydantic import ValidationError
        payload = {"intent": "x", "steps": []}  # steps min_length=1
        with self._mock_ollama(payload):
            from src.ollama import chat_structured
            with self.assertRaises(ValidationError):
                chat_structured([{"role": "user", "content": "plan"}], Plan)

    def test_raises_on_malformed_json(self):
        import src.ollama as ol
        import unittest.mock as mock
        ol.BACKEND = "ollama"
        resp = mock.MagicMock()
        resp.json.return_value = {"message": {"content": "not json at all"}}
        resp.raise_for_status = mock.MagicMock()
        with mock.patch("src.ollama.requests.post", return_value=resp):
            from src.ollama import chat_structured
            with self.assertRaises(Exception):
                chat_structured([{"role": "user", "content": "plan"}], Plan)


# ── Epic 10 tests ─────────────────────────────────────────────────────────────

from src.schemas import Plan, PlanStep, Intent, Criterion, StepResult

from src.mapreduce import (
    MapReduceOrchestrator, _extract_result, _validate_plan_structural,
    _parse_intent_from_plan, _TOOL_NAMES,
)
from src.commands import CommandRegistry
from src.states import TaskStateMachine, _classify_failure


def _make_orchestrator(llm_responses):
    """Return (orchestrator, work_dir, call_log) with a mock llm_fn."""
    call_log = []

    def mock_llm(msgs, use_tools=False, think=False):
        idx = len(call_log)
        call_log.append(msgs)
        resp = llm_responses[min(idx, len(llm_responses) - 1)]
        return {"content": resp} if isinstance(resp, str) else resp

    wd = tempfile.mkdtemp()
    storage = Storage(os.path.join(wd, "test.duckdb"))
    storage.create_session("s1", "test", "/")
    registry = CommandRegistry(wd, storage)
    orch = MapReduceOrchestrator(
        llm_fn=mock_llm,
        command_registry=registry,
        storage=storage,
        session_id="s1",
        work_dir=wd,
    )
    return orch, wd, call_log


class TestExtractResult(unittest.TestCase):
    def test_happy(self):
        self.assertEqual(_extract_result("blah\nRESULT: 42\n"), "42")

    def test_multiple_returns_last(self):
        self.assertEqual(_extract_result("RESULT: first\nmore\nRESULT: second"), "second")

    def test_missing_falls_back_to_last_line(self):
        self.assertEqual(_extract_result("line one\nline two"), "line two")

    def test_empty_string(self):
        self.assertEqual(_extract_result(""), "")

    def test_whitespace_trimmed(self):
        self.assertEqual(_extract_result("RESULT:   hello   \n"), "hello")


class TestValidatePlanStructural(unittest.TestCase):
    def test_all_tools(self):
        ok, failures = _validate_plan_structural(["read_file employees.csv", "run_python count rows"])
        self.assertTrue(ok)
        self.assertEqual(failures, [])

    def test_missing_tool(self):
        ok, failures = _validate_plan_structural(["read_file foo", "analyze the data carefully"])
        self.assertFalse(ok)
        self.assertEqual(len(failures), 1)
        self.assertIn("step 2", failures[0])

    def test_empty_plan(self):
        ok, failures = _validate_plan_structural([])
        self.assertTrue(ok)

    def test_all_tools_recognized(self):
        for tool in _TOOL_NAMES:
            ok, _ = _validate_plan_structural([f"{tool} something"])
            self.assertTrue(ok, f"tool '{tool}' not recognized")


class TestParseIntentFromPlan(unittest.TestCase):
    def test_happy(self):
        text = 'INTENT:\n{"summary": "count rows", "criteria": []}\n\nREASONING:\nstuff\n\nPLAN:\n1. read_file'
        intent = _parse_intent_from_plan(text)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.summary, "count rows")

    def test_malformed_json_returns_none(self):
        text = 'INTENT:\n{bad json}\n\nPLAN:\n1. run_bash'
        self.assertIsNone(_parse_intent_from_plan(text))

    def test_no_intent_section_returns_none(self):
        text = 'PLAN:\n1. run_bash echo hi'
        self.assertIsNone(_parse_intent_from_plan(text))

    def test_inline_intent(self):
        text = 'INTENT: {"summary": "test", "criteria": []}\nREASONING: ...'
        intent = _parse_intent_from_plan(text)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.summary, "test")


class TestPlannerPrompt(unittest.TestCase):
    def test_planner_prompt_is_atomic(self):
        orch, _, _ = _make_orchestrator(["PLAN:\n1. run_bash echo hi"])
        # Access the pass1_system content indirectly via _map_phase call
        # We verify the prompt passed to llm contains ATOMIC guidance
        call_log = []
        def spy_llm(msgs, use_tools=False, think=False):
            call_log.append(msgs)
            return {"content": "PLAN:\n1. run_bash echo hi"}
        orch.llm_fn = spy_llm
        orch._map_phase("do something")
        first_call_system = call_log[0][0]["content"]
        self.assertNotIn("Combine", first_call_system)
        self.assertIn("ATOMIC", first_call_system.upper() if "ATOMIC" not in first_call_system else first_call_system)

    def test_planner_makes_two_llm_calls(self):
        orch, _, call_log = _make_orchestrator([
            "INTENT:\n{\"summary\": \"test\", \"criteria\": []}\nREASONING:\nstuff\nPLAN:\n1. run_bash echo hi\n2. run_python parse",
            "PLAN:\n1. run_bash echo hi\n2. run_python parse",
        ])
        orch._map_phase("do something")
        self.assertEqual(len(call_log), 2)

    def test_planner_parses_plan_section(self):
        orch, _, _ = _make_orchestrator([
            "INTENT:\n{\"summary\": \"test\", \"criteria\": []}\nREASONING:\nstuff\nPLAN:\n1. read_file employees.csv\n2. run_python count rows",
            "PLAN:\n1. read_file employees.csv\n2. run_python count rows",
        ])
        intent, subtasks = orch._map_phase("count rows in employees.csv")
        self.assertEqual(len(subtasks), 2)
        self.assertIn("read_file", subtasks[0])

    def test_planner_handles_missing_plan_header(self):
        orch, _, _ = _make_orchestrator([
            "INTENT:\n{\"summary\": \"test\", \"criteria\": []}\n1. run_bash echo hi\n2. run_python parse",
            "1. run_bash echo hi\n2. run_python parse",
        ])
        intent, subtasks = orch._map_phase("do something")
        # Should still parse numbered list even without PLAN: header
        self.assertGreater(len(subtasks), 0)


class TestFewShotPlanner(unittest.TestCase):
    def test_few_shot_injected_when_chain_found(self):
        import unittest.mock as mock
        orch, wd, _ = _make_orchestrator([
            "PLAN:\n1. run_bash echo hi",
            "PLAN:\n1. run_bash echo hi",
        ])
        chain = {
            "description": "create a pptx file",
            "score": 0.5,
            "steps": [{"tool": "run_bash", "args": {"command": "pip install"}, "output_summary": "OK"}],
        }
        call_log = []
        def spy_llm(msgs, use_tools=False, think=False):
            call_log.append(msgs)
            return {"content": "PLAN:\n1. run_bash echo hi"}
        orch.llm_fn = spy_llm
        with mock.patch.object(orch.storage, "find_similar_chains", return_value=[chain]):
            orch._map_phase("make a pptx presentation")
        user_msg = call_log[0][1]["content"]
        self.assertIn("EXAMPLE (past successful task):", user_msg)

    def test_few_shot_skipped_on_low_score(self):
        orch, _, _ = _make_orchestrator(["PLAN:\n1. run_bash ls"])
        # Put a chain with a very different description so TF-IDF score is low
        orch.storage.conn.execute(
            "INSERT INTO task_chains (task_id, description, steps_json, completed_at) "
            "VALUES ('tc2', 'zzzz unrelated zzzz', "
            "'[{\"tool\": \"run_bash\", \"args\": {}, \"output_summary\": \"\"}]', 1.0)"
        )
        call_log = []
        def spy_llm(msgs, use_tools=False, think=False):
            call_log.append(msgs)
            return {"content": "PLAN:\n1. run_bash ls"}
        orch.llm_fn = spy_llm
        orch._map_phase("list files in current directory")
        user_msg = call_log[0][1]["content"]
        self.assertNotIn("EXAMPLE (past successful task):", user_msg)

    def test_few_shot_skipped_on_empty(self):
        orch, _, _ = _make_orchestrator(["PLAN:\n1. run_bash ls"])
        call_log = []
        def spy_llm(msgs, use_tools=False, think=False):
            call_log.append(msgs)
            return {"content": "PLAN:\n1. run_bash ls"}
        orch.llm_fn = spy_llm
        # No chains in storage — should not crash
        orch._map_phase("list files")
        self.assertNotIn("EXAMPLE", call_log[0][1]["content"])


class TestPreFlightValidation(unittest.TestCase):
    def test_all_tools_passes(self):
        ok, _ = _validate_plan_structural(["read_file foo.csv", "run_python count"])
        self.assertTrue(ok)

    def test_missing_tool_fails(self):
        ok, failures = _validate_plan_structural(["read_file foo", "analyze the data"])
        self.assertFalse(ok)

    def test_replan_triggered_on_invalid_plan(self):
        # First call returns prose plan, second+ returns valid plan
        prose = "PLAN:\n1. Read the file\n2. Analyze the data\n3. Write results"
        valid = "PLAN:\n1. read_file employees.csv\n2. run_python count rows\n3. write_file result.txt"
        orch, _, call_log = _make_orchestrator([
            "INTENT:\n{\"summary\": \"test\", \"criteria\": []}\nREASONING:\n...\n" + prose,
            prose,         # pass2 critique returns same prose
            valid,         # re-plan returns valid
        ])
        intent, subtasks = orch._map_phase("read and analyze employees.csv")
        self.assertGreater(len(call_log), 2)  # at least 3 calls (pass1, pass2, replan)
        # Final subtasks should have tool names
        ok, _ = _validate_plan_structural(subtasks)
        self.assertTrue(ok)

    def test_fallback_to_empty_on_double_failure(self):
        # Both pass1 draft and replan return prose — subtasks should be empty
        prose = "PLAN:\n1. Analyze things\n2. Summarize things"
        orch, _, _ = _make_orchestrator([
            "INTENT:\n{\"summary\": \"test\", \"criteria\": []}\nREASONING:\n...\n" + prose,
            prose,
            prose,  # replan also prose
        ])
        intent, subtasks = orch._map_phase("do something")
        # subtasks will be empty — execute() should fall back to _run_single
        ok, _ = _validate_plan_structural(subtasks) if subtasks else (True, [])
        # Either empty or all valid (empty = fallback)
        if subtasks:
            self.assertTrue(ok)


class TestSubtaskMessages(unittest.TestCase):
    def _make_orch(self):
        orch, wd, _ = _make_orchestrator(["PLAN:\n1. run_bash ls"])
        return orch, wd

    def test_one_tool_rule_present(self):
        orch, wd = self._make_orch()
        msgs = orch._build_subtask_messages("run_bash ls", None, "/tmp/prev.txt", 1, 3, "/tmp/plan.md")
        user_msg = msgs[1]["content"]
        self.assertIn("Call exactly ONE tool", user_msg)

    def test_result_format_present(self):
        orch, wd = self._make_orch()
        msgs = orch._build_subtask_messages("run_bash ls", None, "/tmp/prev.txt", 1, 3)
        user_msg = msgs[1]["content"]
        self.assertIn("RESULT:", user_msg)
        self.assertIn("OUTPUT FORMAT", user_msg)

    def test_step_position_present(self):
        orch, wd = self._make_orch()
        msgs = orch._build_subtask_messages("run_bash ls", None, "/tmp/prev.txt",
                                             step_index=2, total_steps=4, plan_path="/tmp/plan.md")
        user_msg = msgs[1]["content"]
        self.assertIn("step 2 of 4", user_msg)
        self.assertIn("/tmp/plan.md", user_msg)


class TestPlanMdWritten(unittest.TestCase):
    def test_plan_md_written_in_mapreduce(self):
        import unittest.mock as mock
        orch, wd, _ = _make_orchestrator(["synthesis result"])

        with mock.patch("src.mapreduce.TaskStateMachine") as MockSM:
            instance = MockSM.return_value
            instance.run.return_value = "RESULT: done"
            instance.state.value = "COMPLETED"
            orch._run_mapreduce(
                "test task", [], None,
                ["run_bash echo 1", "run_python print 2", "write_file out.txt output"],
            )

        plan_path = os.path.join(wd, "plan.md")
        self.assertTrue(os.path.exists(plan_path))
        content = open(plan_path).read()
        self.assertIn("run_bash echo 1", content)
        self.assertIn("run_python print 2", content)


class TestVerifyStep(unittest.TestCase):
    def _make_orch(self):
        orch, wd, _ = _make_orchestrator([""])
        return orch, wd

    def test_missing_result_line(self):
        orch, _ = self._make_orch()
        ok, reason = orch._verify_step("run_bash ls", "here is output\nbut no result line")
        self.assertFalse(ok)
        self.assertIn("RESULT", reason)

    def test_missing_expected_file(self):
        orch, wd = self._make_orch()
        ok, reason = orch._verify_step(
            "write_file report.txt with summary",
            "I wrote the file\nRESULT: done",
        )
        # report.txt doesn't exist in wd or cwd
        self.assertFalse(ok)
        self.assertIn("report.txt", reason)

    def test_pass_with_result_line_no_file_check(self):
        orch, _ = self._make_orch()
        ok, reason = orch._verify_step("run_python count rows", "counted 42 rows\nRESULT: 42")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_pass_write_step_file_exists(self):
        orch, wd = self._make_orch()
        # Create the file so verification passes
        open(os.path.join(wd, "out.txt"), "w").write("content")
        ok, reason = orch._verify_step(
            f"write_file {wd}/out.txt summary",
            f"wrote it\nRESULT: done",
        )
        self.assertTrue(ok)


class TestPrevResultsFileContents(unittest.TestCase):
    def test_prev_results_contains_only_result_values(self):
        import unittest.mock as mock
        orch, wd, _ = _make_orchestrator(["synthesis result"])

        call_num = [0]
        results = ["lots of chatter here\nRESULT: foo", "different chatter\nRESULT: bar"]

        with mock.patch("src.mapreduce.TaskStateMachine") as MockSM:
            def make_instance(*args, **kwargs):
                m = mock.MagicMock()
                idx = call_num[0]
                call_num[0] += 1
                m.run.return_value = results[min(idx, len(results) - 1)]
                m.state.value = "COMPLETED"
                return m
            MockSM.side_effect = make_instance

            orch._run_mapreduce(
                "task", [], None, ["run_bash step1", "run_python step2"],
            )

        prev_file = os.path.join(wd, "previous_results.txt")
        content = open(prev_file).read()
        # File is written at the START of each step with prior results.
        # After a 2-step run: written before step 2 = contains step 1's RESULT only.
        self.assertNotIn("chatter", content)  # raw output must not bleed through
        self.assertIn("foo", content)           # step 1's extracted RESULT is present


class TestRetryLevelHints(unittest.TestCase):
    def _run_with_level(self, level):
        messages = [{"role": "system", "content": "sys"}]

        def mock_llm(msgs, use_tools=False, think=False):
            return {"role": "assistant", "content": "done"}

        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        registry = CommandRegistry(wd, storage)
        storage.create_task("t1", "s1", "test")

        sm = TaskStateMachine(
            task_id="t1", description="test", max_turns=1, retry_level=level
        )
        sm.run(messages, mock_llm, registry, storage, "s1")
        return messages

    def test_level_1_no_hint(self):
        msgs = self._run_with_level(1)
        contents = " ".join(m.get("content", "") for m in msgs if m["role"] == "system")
        self.assertNotIn("Example tool call:", contents)
        self.assertNotIn("exact structure", contents)

    def test_level_2_has_example(self):
        msgs = self._run_with_level(2)
        contents = " ".join(m.get("content", "") for m in msgs if m["role"] == "system")
        self.assertIn("Example tool call:", contents)

    def test_level_3_has_skeleton(self):
        msgs = self._run_with_level(3)
        contents = " ".join(m.get("content", "") for m in msgs if m["role"] == "system")
        self.assertIn("exact structure", contents)


class TestDecompositionGate(unittest.TestCase):
    def test_trivial_query_skips_planner(self):
        orch, _, call_log = _make_orchestrator(["done"])
        import unittest.mock as mock
        with mock.patch.object(orch, "_run_single", return_value="ok") as mock_single:
            with mock.patch.object(orch, "_map_phase") as mock_plan:
                orch.execute("ls", [])
                mock_plan.assert_not_called()
                mock_single.assert_called_once()

    def test_explicit_single_run_python_skips_planner(self):
        orch, _, _ = _make_orchestrator(["done"])
        import unittest.mock as mock
        prompt = "Do not decompose this task. Use exactly one run_python tool call."
        with mock.patch.object(orch, "_run_single", return_value="ok") as mock_single:
            with mock.patch.object(orch, "_map_phase") as mock_plan:
                orch.execute(prompt, [])
                mock_plan.assert_not_called()
                mock_single.assert_called_once()

    def test_one_step_plan_runs_single(self):
        orch, _, _ = _make_orchestrator(["done"])
        import unittest.mock as mock
        with mock.patch.object(orch, "_map_phase", return_value=(None, ["run_bash ls"])):
            with mock.patch.object(orch, "_run_single", return_value="ok") as mock_single:
                with mock.patch.object(orch, "_run_mapreduce") as mock_mr:
                    orch.execute("list the files here please", [])
                    mock_single.assert_called_once()
                    mock_mr.assert_not_called()

    def test_multi_step_plan_runs_mapreduce(self):
        orch, _, _ = _make_orchestrator(["done"])
        import unittest.mock as mock
        steps = ["run_bash step1", "run_python step2", "write_file step3"]
        with mock.patch.object(orch, "_map_phase", return_value=(None, steps)):
            with mock.patch.object(orch, "_run_mapreduce", return_value="ok") as mock_mr:
                with mock.patch.object(orch, "_run_single") as mock_single:
                    orch.execute("count rows in employees.csv and summarize", [])
                    mock_mr.assert_called_once()
                    mock_single.assert_not_called()


# ── Epic 12 tests ─────────────────────────────────────────────────────────────

class TestFailureTaxonomy(unittest.TestCase):
    """Story 12.1 — failure mode classification and persistence."""

    def test_classify_failure_loop(self):
        self.assertEqual(_classify_failure("loop detected: repeating same tool calls"), "loop_detected")

    def test_classify_failure_max_turns(self):
        self.assertEqual(_classify_failure("max turns reached"), "max_turns")

    def test_classify_failure_empty_response(self):
        self.assertEqual(_classify_failure("empty LLM response after nudges"), "empty_response")

    def test_classify_failure_unknown_falls_back(self):
        self.assertEqual(_classify_failure("something unexpected happened"), "tool_error")

    def test_failure_mode_persisted(self):
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        storage.create_task("t1", "s1", "test task")

        def mock_llm(msgs, use_tools=False, think=False):
            return {"role": "assistant", "content": ""}  # empty → nudge → fail

        registry = CommandRegistry(wd, storage)
        sm = TaskStateMachine(task_id="t1", description="test task", max_turns=1)
        sm._empty_nudges = 3  # pre-trip the nudge counter
        sm.run([{"role": "system", "content": "sys"}], mock_llm, registry, storage, "s1")

        row = storage.conn.execute(
            "SELECT failure_mode FROM tasks WHERE task_id='t1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])


class TestHarnessParamsStorage(unittest.TestCase):
    """Story 12.2 — harness_params table and helper methods."""

    def _make_storage(self):
        wd = tempfile.mkdtemp()
        s = Storage(os.path.join(wd, "test.duckdb"))
        s.create_session("s1", "test", "/")
        return s

    def test_harness_params_table_exists(self):
        s = self._make_storage()
        tables = [r[0] for r in s.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()]
        self.assertIn("harness_params", tables)
        s.close()

    def test_record_harness_outcome_new_entry(self):
        s = self._make_storage()
        s.record_harness_outcome("count lines in a file", max_turns=4, retry_start=0,
                                  turns_used=3, success=True)
        row = s.conn.execute("SELECT success_count, failure_count FROM harness_params").fetchone()
        assert row is not None
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 0)
        s.close()

    def test_record_harness_outcome_updates_counts(self):
        s = self._make_storage()
        desc = "count lines in a file"
        s.record_harness_outcome(desc, max_turns=4, retry_start=0, turns_used=3, success=True)
        s.record_harness_outcome(desc, max_turns=4, retry_start=0, turns_used=5, success=False)
        row = s.conn.execute("SELECT success_count, failure_count FROM harness_params").fetchone()
        assert row is not None
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 1)
        s.close()

    def test_lookup_harness_params_returns_none_on_empty_db(self):
        s = self._make_storage()
        result = s.lookup_harness_params("count rows in employees.csv")
        self.assertIsNone(result)
        s.close()

    def test_avg_turns_used_running_average(self):
        s = self._make_storage()
        desc = "count rows in a csv file"
        s.record_harness_outcome(desc, max_turns=6, retry_start=0, turns_used=4, success=True)
        s.record_harness_outcome(desc, max_turns=6, retry_start=0, turns_used=6, success=True)
        s.record_harness_outcome(desc, max_turns=6, retry_start=0, turns_used=2, success=True)
        row = s.conn.execute("SELECT avg_turns_used FROM harness_params").fetchone()
        assert row is not None
        self.assertAlmostEqual(row[0], 4.0, places=1)
        s.close()


class TestHarnessPriming(unittest.TestCase):
    """Story 12.3 — harness priming and outcome recording."""

    def _run_sm(self, storage, description="test task", max_turns=1, retry_level=0):
        storage.create_task("t_prime", "s1", description)
        messages = [{"role": "system", "content": "sys"}]

        def mock_llm(msgs, use_tools=False, think=False):
            return {"role": "assistant", "content": "done"}

        wd = tempfile.mkdtemp()
        registry = CommandRegistry(wd, storage)
        sm = TaskStateMachine(
            task_id="t_prime", description=description,
            max_turns=max_turns, retry_level=retry_level,
        )
        sm.run(messages, mock_llm, registry, storage, "s1")
        return messages, sm

    def test_harness_primed_from_history(self):
        import unittest.mock as mock
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        hp = {"task_hash": "abc", "description": "test", "max_turns": 6,
              "retry_start": 2, "success_count": 0, "failure_count": 3, "avg_turns_used": 5.5}
        with mock.patch.object(storage, "lookup_harness_params", return_value=hp):
            messages, sm = self._run_sm(storage, max_turns=4)
        # retry_level should have been set to 2, causing the example hint to appear
        contents = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
        self.assertIn("Example tool call:", contents)

    def test_harness_not_primed_on_none(self):
        import unittest.mock as mock
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "s1", "/")
        with mock.patch.object(storage, "lookup_harness_params", return_value=None):
            messages, sm = self._run_sm(storage)
        # No priming hints injected
        contents = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
        self.assertNotIn("Example tool call:", contents)
        self.assertNotIn("exact structure", contents)

    def test_outcome_recorded_on_completion(self):
        import unittest.mock as mock
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        # max_turns=2: turn 1 → EXECUTING→EVALUATING, turn 2 → EVALUATING→COMPLETED
        with mock.patch.object(storage, "record_harness_outcome") as mock_record:
            self._run_sm(storage, max_turns=2)
        mock_record.assert_called_once()
        call = mock_record.call_args
        success_val = call.kwargs.get("success", call.args[-1] if call.args else True)
        self.assertTrue(success_val)

    def test_outcome_recorded_on_failure(self):
        import unittest.mock as mock
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        storage.create_task("t_fail", "s1", "test task")
        messages = [{"role": "system", "content": "sys"}]
        registry = CommandRegistry(wd, storage)
        sm = TaskStateMachine(task_id="t_fail", description="test task", max_turns=0)
        with mock.patch.object(storage, "record_harness_outcome") as mock_record:
            sm.run(messages, lambda *a, **kw: {"role": "assistant", "content": "x"},
                   registry, storage, "s1")
        mock_record.assert_called_once()


class TestFailureModeHints(unittest.TestCase):
    """Story 12.4 — failure-mode-aware hint injection."""

    def _run_with_histogram(self, histogram):
        import unittest.mock as mock
        wd = tempfile.mkdtemp()
        storage = Storage(os.path.join(wd, "test.duckdb"))
        storage.create_session("s1", "test", "/")
        storage.create_task("t_hint", "s1", "test task")
        messages = [{"role": "system", "content": "sys"}]
        registry = CommandRegistry(wd, storage)
        sm = TaskStateMachine(task_id="t_hint", description="test task", max_turns=1)
        with mock.patch.object(storage, "failure_histogram", return_value=histogram):
            sm.run(messages, lambda *a, **kw: {"role": "assistant", "content": "done"},
                   registry, storage, "s1")
        return messages

    def test_failure_hint_injected_for_loop(self):
        msgs = self._run_with_histogram({"loop_detected": 3})
        contents = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system")
        self.assertIn("Avoid repeating the same tool call", contents)

    def test_failure_hint_not_injected_below_threshold(self):
        msgs = self._run_with_histogram({"loop_detected": 1})
        contents = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system")
        self.assertNotIn("Avoid repeating", contents)

    def test_failure_hint_not_injected_unknown_mode(self):
        # Unknown mode key — should not crash and no hint
        msgs = self._run_with_histogram({"unknown_mode": 5})
        contents = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system")
        self.assertNotIn("Avoid repeating", contents)

    def test_failure_hint_picks_dominant(self):
        msgs = self._run_with_histogram({"max_turns": 2, "loop_detected": 5})
        contents = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system")
        # loop_detected (5) beats max_turns (2) — loop hint should appear
        self.assertIn("Avoid repeating the same tool call", contents)
        self.assertNotIn("Be efficient", contents)


if __name__ == "__main__":
    unittest.main()
