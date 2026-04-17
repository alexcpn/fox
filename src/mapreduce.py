"""
Fox MapReduce orchestrator — routes queries to either a single state machine
(simple) or a full map->execute->reduce pipeline (complex).
"""

import json
import os
import re
import uuid
from typing import Optional

from src.states import TaskStateMachine
from src.storage import Storage
from src.commands import CommandRegistry
from src.ollama import build_system_prompt
from src.validator import extract_intent, validate, Intent


# ── Data extraction ───────────────────────────────────────────────────────────

def save_user_input(user_input: str, work_dir: str) -> Optional[str]:
    """Save multi-line input (3+ lines) to work_dir/user_input.txt. Returns path or None."""
    lines = user_input.split("\n")
    if len(lines) < 3:
        return None
    path = os.path.join(work_dir, "user_input.txt")
    with open(path, "w") as f:
        f.write(user_input)
    print(f"  \033[36m📎 Saved input → {path} ({len(lines)} lines)\033[0m")
    return path


# ── Orchestrator ──────────────────────────────────────────────────────────────

class MapReduceOrchestrator:
    def __init__(
        self,
        llm_fn,
        command_registry: CommandRegistry,
        storage: Storage,
        session_id: str,
        work_dir: str,
    ):
        self.llm_fn           = llm_fn
        self.command_registry = command_registry
        self.storage          = storage
        self.session_id       = session_id
        self.work_dir         = work_dir

    def should_decompose(self, user_input: str) -> bool:
        return len(user_input.strip().splitlines()) >= 5

    def execute(
        self,
        user_input: str,
        messages: list[dict],
        data_file: Optional[str] = None,
    ) -> str:
        # Extract success criteria up front (one LLM call).
        intent = extract_intent(self.llm_fn, user_input)
        if intent and intent.criteria:
            print(f"  \033[36m🎯 Intent: {intent.summary}\033[0m")
            for c in intent.criteria:
                print(f"  \033[90m   · {c.type}: {c.args}\033[0m")
        self._current_intent = intent  # stashed for _run_single / _run_mapreduce to persist

        if not self.should_decompose(user_input):
            result = self._run_single(user_input, messages, data_file)
        else:
            result = self._run_mapreduce(user_input, messages, data_file)

        # Validate — cheap file/keyword checks only (Phase 1).
        if intent and intent.criteria:
            ok, failures = validate(intent, result, self.work_dir)
            if ok:
                print(f"  \033[32m✓ intent satisfied\033[0m")
            else:
                print(f"  \033[33m⚠ intent NOT satisfied: {'; '.join(failures)}\033[0m")
                result = self._retry_for_intent(
                    user_input, messages, data_file, intent, failures, result,
                )
        return result

    def _retry_for_intent(
        self,
        user_input: str,
        messages: list[dict],
        data_file: Optional[str],
        intent: Intent,
        failures: list[str],
        prior_result: str,
    ) -> str:
        """One retry. Feed the failure reasons back and re-run as a single task."""
        retry_prompt = (
            f"Your previous response did not fulfill the request.\n"
            f"Unmet criteria: {'; '.join(failures)}\n\n"
            f"Original request: {user_input}\n\n"
            f"Fix the missing parts now. Use run_bash or run_python — do not just describe."
        )
        print(f"  \033[36m↻ retrying with intent feedback\033[0m")
        retry_result = self._run_single(retry_prompt, messages, data_file)

        ok, failures2 = validate(intent, retry_result, self.work_dir)
        if ok:
            return retry_result
        # Still failed — return a result that makes the failure visible.
        print(f"  \033[31m✗ intent still unmet: {'; '.join(failures2)}\033[0m")
        return (
            f"[⚠ intent validation failed: {'; '.join(failures2)}]\n\n"
            f"{retry_result or prior_result}"
        )

    # ── Simple path ───────────────────────────────────────────────────────────

    def _run_single(
        self,
        user_input: str,
        messages: list[dict],
        data_file: Optional[str],
    ) -> str:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        self.storage.create_task(task_id, self.session_id, user_input)
        intent = getattr(self, "_current_intent", None)
        if intent and intent.criteria:
            self.storage.set_task_intent(task_id, json.dumps(intent.to_dict()))

        content = user_input
        if data_file:
            content += f"\n[Data saved to {data_file}]"
        messages.append({"role": "user", "content": content})

        from src.ollama import MAX_TURNS
        sm = TaskStateMachine(task_id=task_id, description=user_input, max_turns=MAX_TURNS)
        result = sm.run(messages, self.llm_fn, self.command_registry, self.storage, self.session_id)

        messages.append({"role": "assistant", "content": result})
        return result

    # ── MapReduce path ────────────────────────────────────────────────────────

    def _run_mapreduce(
        self,
        user_input: str,
        messages: list[dict],
        data_file: Optional[str],
    ) -> str:
        parent_id = f"task-{uuid.uuid4().hex[:8]}"
        self.storage.create_task(parent_id, self.session_id, user_input)
        intent = getattr(self, "_current_intent", None)
        if intent and intent.criteria:
            self.storage.set_task_intent(parent_id, json.dumps(intent.to_dict()))

        # MAP
        print(f"\n  \033[1;36m── MAP ──\033[0m")
        subtask_descriptions = self._map_phase(user_input)
        if not subtask_descriptions:
            return self._run_single(user_input, messages, data_file)

        # EXECUTE each subtask in isolation
        subtask_results: list[str] = []
        prev_results_file = os.path.join(self.work_dir, "previous_results.txt")

        for i, desc in enumerate(subtask_descriptions):
            print(f"\n  \033[1;36m── EXECUTE {i+1}/{len(subtask_descriptions)}: {desc[:80]} ──\033[0m")
            sub_id = f"{parent_id}-sub{i+1}"
            self.storage.create_task(sub_id, self.session_id, desc, parent_id=parent_id)

            # Write compact previous results for this subtask to read
            with open(prev_results_file, "w") as f:
                f.write("\n---\n".join(subtask_results) if subtask_results else "(none yet)")

            sub_messages = self._build_subtask_messages(desc, data_file, prev_results_file)
            sm = TaskStateMachine(task_id=sub_id, description=desc, max_turns=5)
            result = sm.run(sub_messages, self.llm_fn, self.command_registry, self.storage, self.session_id)

            self.storage.update_task_state(sub_id, sm.state.value, result=result)
            subtask_results.append(f"Subtask {i+1} ({desc}):\n{result}")
            print(f"  \033[90m  ✓ subtask {i+1} done\033[0m")

        # REDUCE
        print(f"\n  \033[1;36m── REDUCE ──\033[0m")
        final = self._reduce_phase(user_input, subtask_results)

        self.storage.update_task_state(parent_id, "COMPLETED", result=final)

        # Add to main conversation for continuity
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": final})
        return final

    def _map_phase(self, user_input: str) -> list[str]:
        plan_messages = [
            {
                "role": "system",
                "content": (
                    "You are a task planner. Output ONLY a numbered list of 2-4 tasks. No explanation.\n"
                    "Rules:\n"
                    "- Maximum 4 tasks. Combine related steps.\n"
                    "- First task: read and parse ALL input data from the file.\n"
                    "- Last task: compare/diff/summarise and print results.\n"
                    "- Output ONLY the numbered list."
                ),
            },
            {"role": "user", "content": user_input},
        ]
        response = self.llm_fn(plan_messages, use_tools=False, think=False)
        plan_text = response.get("content", "")
        print(f"\033[36m{plan_text}\033[0m")
        return re.findall(r'^\s*\d+\.\s*(.+)$', plan_text, re.MULTILINE)

    def _build_subtask_messages(
        self,
        description: str,
        data_file: Optional[str],
        prev_results_file: str,
    ) -> list[dict]:
        """Fresh, isolated message list for one subtask."""
        system = build_system_prompt(self.work_dir)
        data_ref = data_file or "(no data file)"
        user_content = (
            f"TASK: {description}\n\n"
            f"FILES YOU MUST USE:\n"
            f"  - Input data: {data_ref}\n"
            f"  - Previous task results: {prev_results_file}\n\n"
            f"RULES:\n"
            f"- Read the input file with run_python. Do NOT invent filenames.\n"
            f"- Do NOT hardcode values. Parse from the file.\n"
            f"- Print your results with print()."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_content},
        ]

    def _reduce_phase(self, user_input: str, subtask_results: list[str]) -> str:
        """TF-IDF ranked synthesis — top results in full, rest as one-liners."""
        from src.relevance import rank_results_for_query

        # Score each subtask result against the original query
        result_docs = [
            {"id": str(i), "text": r}
            for i, r in enumerate(subtask_results)
        ]
        ranked = rank_results_for_query(user_input, result_docs, top_k=2)
        top_ids = {r["id"] for r in ranked}

        # Build reduce payload: top results in full (≤500 chars), rest as summaries
        parts: list[str] = []
        for i, r in enumerate(subtask_results):
            if str(i) in top_ids:
                parts.append(r[:500])
            else:
                first_line = r.splitlines()[0] if r.strip() else "(no output)"
                parts.append(f"[summary] {first_line[:120]}")

        synth_messages = [
            {
                "role": "system",
                "content": (
                    "Synthesize task results into a clear final answer. "
                    "Show actual data values. Be concise. Use tables where appropriate."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original request:\n{user_input}\n\n"
                    f"Task results:\n" + "\n---\n".join(parts) + "\n\n"
                    f"Give the final answer. Show actual values, not placeholders."
                ),
            },
        ]
        response = self.llm_fn(synth_messages, use_tools=False, think=False)
        return response.get("content", "(no synthesis)")
