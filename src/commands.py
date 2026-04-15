"""
Fox command pattern — one ToolCommand subclass per tool, CommandRegistry with
cache-before-execute via Storage.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage import Storage


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    output: str
    success: bool
    elapsed: float
    exit_code: Optional[int] = None


# ── Base ──────────────────────────────────────────────────────────────────────

class ToolCommand(ABC):
    name: str = ""

    def __init__(self, args: dict):
        self.args: dict = args
        self.timestamp: float = 0.0
        self.result: Optional[CommandResult] = None

    @abstractmethod
    def execute(self) -> CommandResult: ...

    def undo(self) -> Optional[str]:
        return None

    def args_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.args, sort_keys=True).encode()
        ).hexdigest()[:16]

    @property
    def metadata(self) -> dict:
        return {
            "name": self.name,
            "args_summary": self._summarize_args(),
            "timestamp": self.timestamp,
            "success": self.result.success if self.result else None,
        }

    def _summarize_args(self) -> str:
        parts = []
        for k, v in self.args.items():
            s = str(v)
            parts.append(f"{k}={s[:80]}..." if len(s) > 80 else f"{k}={s}")
        return ", ".join(parts)

    @staticmethod
    def _truncate(text: str, limit: int = 20_000) -> str:
        return text[:limit] if len(text) > limit else text


# ── Subclasses ────────────────────────────────────────────────────────────────

class RunBashCommand(ToolCommand):
    name = "run_bash"

    def execute(self) -> CommandResult:
        t0 = time.time()
        try:
            r = subprocess.run(
                self.args["command"], shell=True,
                capture_output=True, text=True, timeout=120, cwd=os.getcwd(),
            )
            out = r.stdout or ""
            if r.stderr:
                out += ("\n--- stderr ---\n" + r.stderr) if out else r.stderr
            if r.returncode != 0:
                out += f"\n[exit code: {r.returncode}]"
            self.result = CommandResult(
                self._truncate(out) or "(no output)",
                r.returncode == 0, time.time() - t0, r.returncode,
            )
        except subprocess.TimeoutExpired:
            self.result = CommandResult("(command timed out)", False, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result


class RunPythonCommand(ToolCommand):
    name = "run_python"

    def __init__(self, args: dict, work_dir: str):
        super().__init__(args)
        self.work_dir = work_dir

    def execute(self) -> CommandResult:
        t0 = time.time()
        script = self.args["script"]
        # Strip markdown code fences that LLMs often include
        script = re.sub(r'^```(?:python)?\s*\n?', '', script)
        script = re.sub(r'\n?```\s*$', '', script)
        script_path = os.path.join(self.work_dir, "_script.py")
        try:
            with open(script_path, "w") as f:
                f.write(script)
            r = subprocess.run(
                [sys.executable, script_path],
                capture_output=True, text=True, timeout=120, cwd=os.getcwd(),
                env={**os.environ, "WORK_DIR": self.work_dir},
            )
            out = r.stdout or ""
            if r.stderr:
                out += ("\n--- stderr ---\n" + r.stderr) if out else r.stderr
            if r.returncode != 0:
                out += f"\n[exit code: {r.returncode}]"
            self.result = CommandResult(
                self._truncate(out) or "(no output)",
                r.returncode == 0, time.time() - t0, r.returncode,
            )
        except subprocess.TimeoutExpired:
            self.result = CommandResult("(command timed out)", False, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result


class ReadFileCommand(ToolCommand):
    name = "read_file"

    def execute(self) -> CommandResult:
        t0 = time.time()
        try:
            path = os.path.expanduser(self.args["path"])
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            with open(path) as f:
                lines = f.readlines()
            start = self.args.get("start_line", 1) - 1
            end = self.args.get("end_line", len(lines))
            selected = lines[max(0, start):end]
            numbered = [
                f"{i + max(0, start) + 1:4d} | {line}"
                for i, line in enumerate(selected)
            ]
            output = self._truncate("".join(numbered)) or "(empty file)"
            self.result = CommandResult(output, True, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result


class WriteFileCommand(ToolCommand):
    name = "write_file"

    def __init__(self, args: dict):
        super().__init__(args)
        self._previous_content: Optional[str] = None

    def execute(self) -> CommandResult:
        t0 = time.time()
        try:
            path = os.path.expanduser(self.args["path"])
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            if os.path.exists(path):
                with open(path) as f:
                    self._previous_content = f.read()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(self.args["content"])
            output = f"Wrote {len(self.args['content'])} bytes to {path}"
            self.result = CommandResult(output, True, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result

    def undo(self) -> Optional[str]:
        path = os.path.expanduser(self.args["path"])
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        try:
            if self._previous_content is not None:
                with open(path, "w") as f:
                    f.write(self._previous_content)
                return f"Restored {path}"
            elif os.path.exists(path):
                os.remove(path)
                return f"Removed {path}"
        except Exception as e:
            return f"Undo failed: {e}"
        return None


class GrepSearchCommand(ToolCommand):
    name = "grep_search"

    def execute(self) -> CommandResult:
        t0 = time.time()
        try:
            cmd = ["grep", "-rn", "--color=never"]
            if self.args.get("include"):
                cmd += [f"--include={self.args['include']}"]
            cmd.append(self.args["pattern"])
            cmd.append(self.args.get("path", "."))
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=os.getcwd()
            )
            self.result = CommandResult(
                self._truncate(r.stdout) or "(no matches)",
                True, time.time() - t0, r.returncode,
            )
        except subprocess.TimeoutExpired:
            self.result = CommandResult("(command timed out)", False, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result


class ListFilesCommand(ToolCommand):
    name = "list_files"

    def execute(self) -> CommandResult:
        t0 = time.time()
        try:
            path = self.args.get("path", ".")
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            if self.args.get("recursive"):
                r = subprocess.run(
                    ["find", path, "-maxdepth", "3", "-not", "-path", "*/.*"],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                r = subprocess.run(
                    ["ls", "-lah", path],
                    capture_output=True, text=True, timeout=15,
                )
            self.result = CommandResult(
                self._truncate(r.stdout) or "(empty)",
                True, time.time() - t0, r.returncode,
            )
        except subprocess.TimeoutExpired:
            self.result = CommandResult("(command timed out)", False, time.time() - t0)
        except Exception as e:
            self.result = CommandResult(f"Error: {e}", False, time.time() - t0)
        return self.result


# ── Registry ──────────────────────────────────────────────────────────────────

_COMMAND_MAP: dict[str, type] = {
    "run_bash":    RunBashCommand,
    "run_python":  RunPythonCommand,
    "read_file":   ReadFileCommand,
    "write_file":  WriteFileCommand,
    "grep_search": GrepSearchCommand,
    "list_files":  ListFilesCommand,
}


class CommandRegistry:
    def __init__(self, work_dir: str, storage: "Storage"):
        self.work_dir = work_dir
        self.storage = storage

    def build(self, tool_call: dict) -> ToolCommand:
        name = tool_call["function"]["name"]
        args = tool_call["function"].get("arguments", {})
        cls = _COMMAND_MAP.get(name)
        if cls is None:
            raise ValueError(f"Unknown tool: {name}")
        if cls is RunPythonCommand:
            return cls(args, self.work_dir)
        return cls(args)

    def execute_with_cache(
        self, cmd: ToolCommand, task_id: str, session_id: str
    ) -> "CommandResult":
        """Check cache, execute if miss, record result."""
        cmd.timestamp = time.time()

        # Cache check for read-only tools
        cached_output = self.storage.lookup_cached_tool_call(cmd.name, cmd.args)
        if cached_output is not None:
            cmd.result = CommandResult(cached_output, True, 0.0)
            # Still record the cache hit so the graph stays consistent
            self.storage.record_tool_call(task_id, session_id, cmd)
            return cmd.result

        # Execute
        cmd.execute()

        # Persist
        tc_id = self.storage.record_tool_call(task_id, session_id, cmd)

        # Extract entities for the graph (best-effort)
        if cmd.result and tc_id >= 0:
            self.storage.record_entities_from_tool_call(tc_id, cmd.args, cmd.result.output)

        return cmd.result or CommandResult("(no result)", False, 0.0)
