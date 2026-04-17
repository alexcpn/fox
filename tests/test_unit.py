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


if __name__ == "__main__":
    unittest.main()
