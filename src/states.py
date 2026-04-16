"""
Fox state machine — TaskStateMachine drives a single task to completion
through explicit, validated state transitions.
"""

import enum
import re
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


# ── Creation task detection ───────────────────────────────────────────────────

_CREATION_VERBS = re.compile(
    r'\b(create|generate|write|make|build|produce|output|export)\b', re.I
)
_CREATION_TARGETS = re.compile(
    r'\b(pptx?|xlsx?|csv|pdf|image|png|jpg|file|script|report|chart|graph|doc)\b', re.I
)


def _is_creation_task(description: str) -> bool:
    """Return True if the task asks to create/generate a file."""
    return bool(_CREATION_VERBS.search(description) and _CREATION_TARGETS.search(description))


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
    _tools_called: set = field(default_factory=set)  # tracks tool names used this task

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
        if storage:
            storage.update_task_state(self.task_id, "FAILED", error=reason)

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
                    # Model returned nothing — nudge it once before giving up
                    if not hasattr(self, "_empty_nudges"):
                        self._empty_nudges = 0  # type: ignore[attr-defined]
                    self._empty_nudges += 1  # type: ignore[attr-defined]
                    if self._empty_nudges <= 2:
                        messages.append({
                            "role": "user",
                            "content": "Please continue. Write the next tool call or provide your final answer.",
                        })
                        # stay in EXECUTING — will retry LLM call on next iteration
                    else:
                        self._fail("empty LLM response after nudges", storage)

            # ── TOOL_CALLING: dispatch commands ───────────────────────────
            elif self.state == TaskState.TOOL_CALLING:
                tool_calls = (current_response or {}).get("tool_calls", [])
                # Transition once for the whole batch — not per tool call
                self.transition(TaskState.WAITING_RESULT, storage=storage)

                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", {})
                    tool_call_id = tc.get("id", "")  # OpenAI requires this back

                    try:
                        cmd = command_registry.build(tc)
                        cmd_result = command_registry.execute_with_cache(
                            cmd, self.task_id, session_id
                        )
                    except Exception as e:
                        cmd_result_text = f"Tool error: {e}"
                        _print_tool(name, args, cmd_result_text)
                        tool_msg: dict = {"role": "tool", "content": cmd_result_text}
                        if tool_call_id:
                            tool_msg["tool_call_id"] = tool_call_id
                        messages.append(tool_msg)
                        continue  # stay in WAITING_RESULT, process next tool

                    truncated = smart_truncate(name, cmd_result.output)
                    _print_tool(name, args, truncated)
                    tool_msg = {"role": "tool", "content": truncated}
                    if tool_call_id:
                        tool_msg["tool_call_id"] = tool_call_id
                    messages.append(tool_msg)
                    self._tools_called.add(name)

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
                    # Check for hallucinated completion: model described creating a
                    # file but never actually called run_bash or write_file.
                    if _is_creation_task(self.description) and \
                            not (self._tools_called & {"run_bash", "write_file"}) and \
                            not getattr(self, "_creation_nudged", False):
                        self._creation_nudged = True  # type: ignore[attr-defined]
                        print(f"\n  \033[33m⚠ creation task — no file tool called; nudging\033[0m")
                        messages.append({
                            "role": "user",
                            "content": (
                                "You described creating a file but did not call run_bash or write_file. "
                                "You MUST actually create the file:\n"
                                "1. run_bash: pip install <package> -q && echo OK\n"
                                "2. run_bash: python3 -c \"<script that creates the file>\"\n"
                                "Do it now. Do not describe — execute."
                            ),
                        })
                        messages[:] = compress_context(
                            messages, query=self.description, turn=self.turn_count
                        )
                        self.transition(TaskState.EXECUTING, storage=storage)
                    else:
                        self.result = content
                        self.transition(TaskState.COMPLETED, storage=storage)
                        storage.update_task_state(
                            self.task_id, TaskState.COMPLETED.value, result=content
                        )
                        # Persist the successful tool chain as a playbook entry
                        try:
                            storage.record_task_chain(self.task_id)
                        except Exception:
                            pass  # never let chain recording crash the task
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
