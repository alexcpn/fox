"""
Fox state machine — TaskStateMachine drives a single task to completion
through explicit, validated state transitions.
"""

import enum
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.commands import CommandRegistry
    from src.storage import Storage


# ── States ────────────────────────────────────────────────────────────────────

class TaskState(enum.Enum):
    PENDING        = "PENDING"
    EXECUTING      = "EXECUTING"
    TOOL_CALLING   = "TOOL_CALLING"
    WAITING_RESULT = "WAITING_RESULT"
    EVALUATING     = "EVALUATING"
    COMPLETED      = "COMPLETED"
    FAILED         = "FAILED"


TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING:        {TaskState.EXECUTING},
    TaskState.EXECUTING:      {TaskState.TOOL_CALLING, TaskState.EVALUATING, TaskState.FAILED},
    TaskState.TOOL_CALLING:   {TaskState.WAITING_RESULT, TaskState.FAILED},
    TaskState.WAITING_RESULT: {TaskState.EVALUATING, TaskState.FAILED},
    TaskState.EVALUATING:     {TaskState.EXECUTING, TaskState.COMPLETED, TaskState.FAILED},
}

TERMINAL = {TaskState.COMPLETED, TaskState.FAILED}


# ── Transition record ─────────────────────────────────────────────────────────

@dataclass
class Transition:
    from_state: TaskState
    to_state:   TaskState
    timestamp:  float
    reason:     str = ""


# ── State machine ─────────────────────────────────────────────────────────────

@dataclass
class TaskStateMachine:
    task_id:     str
    description: str
    state:       TaskState = TaskState.PENDING
    history:     list[Transition] = field(default_factory=list)
    result:      Optional[str] = None
    error:       Optional[str] = None
    turn_count:  int = 0
    max_turns:   int = 10

    # ── Internal helpers ──────────────────────────────────────────────────────

    def transition(
        self,
        new_state: TaskState,
        reason: str = "",
        storage: Optional["Storage"] = None,
    ):
        allowed = TRANSITIONS.get(self.state, set())
        if new_state not in allowed and new_state not in TERMINAL:
            raise ValueError(
                f"Invalid transition {self.state.value} -> {new_state.value} "
                f"for task {self.task_id}"
            )
        t = Transition(self.state, new_state, time.time(), reason)
        self.history.append(t)
        if storage:
            storage.log_transition(self.task_id, self.state.value, new_state.value, reason)
            storage.update_task_state(self.task_id, new_state.value)
        self.state = new_state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def _fail(self, reason: str, storage: Optional["Storage"] = None):
        self.error = reason
        self.transition(TaskState.FAILED, reason, storage)

    # ── Core loop ─────────────────────────────────────────────────────────────

    def run(
        self,
        messages: list[dict],
        llm_fn,
        command_registry: "CommandRegistry",
        storage: "Storage",
        session_id: str,
    ) -> str:
        """
        Drive this task to COMPLETED or FAILED.
        Returns the final text result (or error message).

        messages: the in-context message list (mutated in place)
        llm_fn:   callable(messages, use_tools, think) -> dict
        """
        from src.context import compress_context, smart_truncate

        self.transition(TaskState.EXECUTING, storage=storage)

        current_response: Optional[dict] = None

        while not self.is_terminal and self.turn_count < self.max_turns:

            # ── EXECUTING: call the LLM ───────────────────────────────────
            if self.state == TaskState.EXECUTING:
                try:
                    current_response = llm_fn(messages, use_tools=True, think=True)
                except Exception as e:
                    self._fail(f"LLM error: {e}", storage)
                    break

                if current_response is not None:
                    messages.append(current_response)
                self.turn_count += 1

                tool_calls = (current_response or {}).get("tool_calls")
                content    = (current_response or {}).get("content", "").strip()

                if tool_calls:
                    self.transition(TaskState.TOOL_CALLING, storage=storage)
                elif content:
                    self.transition(TaskState.EVALUATING, storage=storage)
                else:
                    self._fail("empty LLM response", storage)

            # ── TOOL_CALLING: dispatch commands ───────────────────────────
            elif self.state == TaskState.TOOL_CALLING:
                tool_calls = (current_response or {}).get("tool_calls", [])
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", {})

                    self.transition(TaskState.WAITING_RESULT, storage=storage)

                    try:
                        cmd = command_registry.build(tc)
                        cmd_result = command_registry.execute_with_cache(
                            cmd, self.task_id, session_id
                        )
                    except Exception as e:
                        cmd_result_text = f"Tool error: {e}"
                        _print_tool(name, args, cmd_result_text)
                        messages.append({"role": "tool", "content": cmd_result_text})
                        self.transition(TaskState.EVALUATING, storage=storage)
                        continue

                    truncated = smart_truncate(name, cmd_result.output)
                    _print_tool(name, args, truncated)
                    messages.append({"role": "tool", "content": truncated})

                # Loop detection via graph
                if storage.detect_cycles(self.task_id):
                    self._fail("loop detected: repeating same tool calls", storage)
                    break

                self.transition(TaskState.EVALUATING, storage=storage)

            # ── EVALUATING: final answer or loop back ─────────────────────
            elif self.state == TaskState.EVALUATING:
                resp = current_response or {}
                content = resp.get("content", "").strip()
                has_tool_calls = bool(resp.get("tool_calls"))

                if content and not has_tool_calls:
                    self.result = content
                    self.transition(TaskState.COMPLETED, storage=storage)
                    storage.update_task_state(
                        self.task_id, TaskState.COMPLETED.value, result=content
                    )
                else:
                    # Compress context before next LLM call
                    messages[:] = compress_context(
                        messages,
                        query=self.description,
                        turn=self.turn_count,
                    )
                    self.transition(TaskState.EXECUTING, storage=storage)

        # Max turns exceeded
        if not self.is_terminal:
            self._fail("max turns reached", storage)

        return self.result or self.error or "(no response)"


# ── Display helpers ───────────────────────────────────────────────────────────

def _summarize_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:80]}..." if len(s) > 80 else f"{k}={s}")
    return ", ".join(parts)


def _truncate_display(s: str, n: int = 150) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "..." if len(s) > n else s


def _print_tool(name: str, args: dict, result: str):
    if name == "run_python":
        preview = str(args.get("script", ""))[:80].replace("\n", " ")
        print(f"  \033[90m🐍 run_python({preview}...)\033[0m")
    else:
        print(f"  \033[90m⚙  {name}({_summarize_args(args)})\033[0m")
    print(f"  \033[90m   → {_truncate_display(result)}\033[0m")
