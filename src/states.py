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

        # Auto-inject top-1 playbook if a similar task succeeded before
        try:
            chains = storage.find_similar_chains(self.description, limit=1)
            if chains and chains[0].get("score", 0) > 0.15:
                chain = chains[0]
                steps = " → ".join(
                    f"{s['tool']}({list(s['args'].values())[0][:40] if s['args'] else ''})"
                    for s in chain["steps"][:5]
                )
                hint = (
                    f"[Playbook: a similar task \"{chain['description'][:60]}\" "
                    f"succeeded with: {steps}. Follow this pattern.]"
                )
                messages.append({"role": "system", "content": hint})
                print(f"  \033[36m📋 playbook injected (score={chain['score']})\033[0m")
        except Exception:
            pass  # never block task execution for playbook lookup

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
                    # Post-task intent validation happens at the orchestrator level;
                    # here we just mark COMPLETED and persist.
                    self.result = content
                    self.transition(TaskState.COMPLETED, storage=storage)
                    storage.update_task_state(
                        self.task_id, TaskState.COMPLETED.value, result=content
                    )
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
