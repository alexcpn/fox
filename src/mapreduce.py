"""
Fox MapReduce orchestrator — routes queries to either a single state machine
(simple) or a full map->execute->reduce pipeline (complex).

Epic 10: plan-first loop for small models. Always plan, then route on plan length.
"""

import json
import os
import re
import time as _time
import uuid
from typing import Optional

from src.states import TaskStateMachine
from src.storage import Storage
from src.commands import CommandRegistry
from src.ollama import build_system_prompt, chat_structured
from src.schemas import Plan, PlanStep, Intent, StepResult
from src.validator import extract_intent, validate


# ── Constants ─────────────────────────────────────────────────────────────────

# Tools available for planning (subset of _COMMAND_MAP — excludes search_examples)
_TOOL_NAMES = {"run_bash", "run_python", "read_file", "write_file", "grep_search", "list_files"}

# Structured output enabled by default; set FOX_STRUCTURED_OUTPUT=0 to fall back to CoT regex
_STRUCTURED_OUTPUT = os.environ.get("FOX_STRUCTURED_OUTPUT", "1") == "1"

# Compiled regex for RESULT: line extraction
_RESULT_RE = re.compile(r'^RESULT:\s*(.+?)\s*$', re.MULTILINE)


# ── Module-level helpers ──────────────────────────────────────────────────────

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


def _extract_result(text: str) -> str:
    """Extract the last RESULT: line value, or fall back to last non-empty line."""
    matches = _RESULT_RE.findall(text or "")
    if matches:
        return matches[-1].strip()
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def _validate_plan_structural(steps: list) -> tuple:
    """Every step must mention at least one tool name. Returns (ok, failure_reasons)."""
    failures = []
    for i, step in enumerate(steps, 1):
        lowered = step.lower()
        if not any(tn in lowered for tn in _TOOL_NAMES):
            failures.append(f"step {i} mentions no tool: {step[:80]}")
    return (len(failures) == 0, failures)


def _parse_intent_from_plan(text: str) -> Optional[Intent]:
    """Extract INTENT JSON block from planner output. Returns Intent or None on failure."""
    try:
        # Try multiline JSON block after INTENT:\n
        m = re.search(r'INTENT:\s*\n(\{.*?\})', text, re.DOTALL)
        if not m:
            # Try inline JSON on same line as INTENT:
            m = re.search(r'INTENT:\s*(\{.*?\})', text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        return Intent.from_dict(data)
    except Exception:
        return None


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

    def should_plan(self, user_input: str) -> bool:
        """Skip planning for trivial prompts and explicit single-tool requests."""
        text = user_input.strip()
        lowered = text.lower()
        single_tool_hints = (
            "do not decompose" in lowered or
            "use exactly one run_python" in lowered or
            "use a single run_python" in lowered
        )
        if single_tool_hints:
            return False
        return len(text) > 30

    # Deprecated alias kept for any external callers
    def should_decompose(self, user_input: str) -> bool:
        return self.should_plan(user_input)

    def execute(
        self,
        user_input: str,
        messages: list,
        data_file: Optional[str] = None,
    ) -> str:
        task_started = _time.time()

        # Skip planning for trivial one-liners
        if not self.should_plan(user_input):
            result = self._run_single(user_input, messages, data_file)
            return result

        # Always plan first — two-pass CoT planner returns (intent, subtasks)
        intent, subtask_descriptions = self._map_phase(user_input, data_file)

        # Fallback intent extraction when planner couldn't parse INTENT section
        if intent is None:
            intent = extract_intent(self.llm_fn, user_input)

        if intent and intent.criteria:
            print(f"  \033[36m🎯 Intent: {intent.summary}\033[0m")
            for c in intent.criteria:
                print(f"  \033[90m   · {c.type}: {c.args}\033[0m")
        self._current_intent = intent

        # Route based on plan length
        if not subtask_descriptions or len(subtask_descriptions) <= 1:
            result = self._run_single(user_input, messages, data_file)
        else:
            result = self._run_mapreduce(user_input, messages, data_file, subtask_descriptions)

        # Validate against intent criteria
        if intent and intent.criteria:
            ok, failures = validate(
                intent, result, self.work_dir,
                llm_fn=self.llm_fn, started_at=task_started,
            )
            if ok:
                print(f"  \033[32m✓ intent satisfied\033[0m")
            else:
                print(f"  \033[33m⚠ intent NOT satisfied: {'; '.join(failures)}\033[0m")
                result = self._retry_for_intent(
                    user_input, messages, data_file, intent, failures, result, task_started,
                )
        return result

    def _retry_for_intent(
        self,
        user_input: str,
        messages: list,
        data_file: Optional[str],
        intent: Intent,
        failures: list,
        prior_result: str,
        started_at: float = 0,
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

        ok, failures2 = validate(
            intent, retry_result, self.work_dir,
            llm_fn=self.llm_fn, started_at=started_at,
        )
        if ok:
            return retry_result
        print(f"  \033[31m✗ intent still unmet: {'; '.join(failures2)}\033[0m")
        return (
            f"[⚠ intent validation failed: {'; '.join(failures2)}]\n\n"
            f"{retry_result or prior_result}"
        )

    # ── Simple path ───────────────────────────────────────────────────────────

    def _run_single(
        self,
        user_input: str,
        messages: list,
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
        messages: list,
        data_file: Optional[str],
        subtask_descriptions: list,
    ) -> str:
        parent_id = f"task-{uuid.uuid4().hex[:8]}"
        self.storage.create_task(parent_id, self.session_id, user_input)
        intent = getattr(self, "_current_intent", None)
        if intent and intent.criteria:
            self.storage.set_task_intent(parent_id, json.dumps(intent.to_dict()))

        total = len(subtask_descriptions)

        # Persist plan as work-dir artifact so each subtask can reference it
        plan_path = os.path.join(self.work_dir, "plan.md")
        with open(plan_path, "w") as f:
            f.write(f"# Plan for: {user_input[:200]}\n\n")
            for i, step in enumerate(subtask_descriptions, 1):
                f.write(f"{i}. {step}\n")

        print(f"\n  \033[1;36m── MAP ({total} steps) ──\033[0m")

        # EXECUTE each subtask in isolation
        subtask_results: list = []
        result_values: list = []  # only extracted RESULT: values for previous_results.txt
        subtask_failures = 0
        prev_results_file = os.path.join(self.work_dir, "previous_results.txt")

        for i, desc in enumerate(subtask_descriptions):
            print(f"\n  \033[1;36m── EXECUTE {i+1}/{total}: {desc[:80]} ──\033[0m")
            sub_id = f"{parent_id}-sub{i+1}"
            self.storage.create_task(sub_id, self.session_id, desc, parent_id=parent_id)

            # Write only clean RESULT values — not raw output — as context for next step
            with open(prev_results_file, "w") as f:
                f.write("\n".join(result_values) if result_values else "(none yet)")

            sub_messages = self._build_subtask_messages(
                desc, data_file, prev_results_file,
                step_index=i + 1, total_steps=total, plan_path=plan_path,
            )
            sm = TaskStateMachine(task_id=sub_id, description=desc, max_turns=4)
            result = sm.run(
                sub_messages, self.llm_fn, self.command_registry, self.storage, self.session_id
            )
            self.storage.update_task_state(sub_id, sm.state.value, result=result)

            # Parse StepResult JSON when structured output is on
            files_created: Optional[list] = None
            if _STRUCTURED_OUTPUT:
                try:
                    m = re.search(r'\{.*\}', result, re.DOTALL)
                    if m:
                        step_result = StepResult.model_validate_json(m.group(0))
                        extracted = step_result.result
                        files_created = step_result.files_created
                    else:
                        extracted = _extract_result(result)
                except Exception:
                    extracted = _extract_result(result)
            else:
                extracted = _extract_result(result)

            # Per-step structural verification; one retry on failure
            ok, reason = self._verify_step(desc, result, files_created=files_created)
            if not ok:
                print(f"  \033[33m  ⚠ step {i+1} verify failed: {reason} — retrying\033[0m")
                retry_id = f"{sub_id}-retry"
                retry_desc = (
                    f"Previous attempt failed: {reason}. Fix exactly that problem and retry.\n\n"
                    f"Original task: {desc}"
                )
                self.storage.create_task(retry_id, self.session_id, retry_desc, parent_id=parent_id)
                retry_messages = self._build_subtask_messages(
                    retry_desc, data_file, prev_results_file,
                    step_index=i + 1, total_steps=total, plan_path=plan_path,
                )
                retry_sm = TaskStateMachine(
                    task_id=retry_id, description=retry_desc, max_turns=4, retry_level=1,
                )
                result = retry_sm.run(
                    retry_messages, self.llm_fn, self.command_registry, self.storage, self.session_id
                )
                # Re-extract after retry
                if _STRUCTURED_OUTPUT:
                    try:
                        m2 = re.search(r'\{.*\}', result, re.DOTALL)
                        if m2:
                            sr2 = StepResult.model_validate_json(m2.group(0))
                            extracted = sr2.result
                        else:
                            extracted = _extract_result(result)
                    except Exception:
                        extracted = _extract_result(result)
                else:
                    extracted = _extract_result(result)

            # `extracted` already set above — don't re-assign
            result_values.append(extracted)
            subtask_results.append(
                f"Subtask {i+1} ({desc}):\nRESULT: {extracted}\n(full output below)\n{result[:400]}"
            )

            if sm.state.value == "FAILED":
                subtask_failures += 1
                print(f"  \033[31m  ✗ subtask {i+1} FAILED\033[0m")
            else:
                print(f"  \033[90m  ✓ subtask {i+1} done\033[0m")

        # Abort if majority failed
        if subtask_failures > total / 2:
            msg = f"majority of subtasks failed ({subtask_failures}/{total})"
            print(f"  \033[31m✗ {msg} — skipping reduce\033[0m")
            self.storage.update_task_state(parent_id, "FAILED", error=msg)
            messages.append({"role": "user", "content": user_input})
            messages.append({"role": "assistant", "content": f"[FAILED: {msg}]"})
            return f"[FAILED: {msg}]"

        # REDUCE
        print(f"\n  \033[1;36m── REDUCE ──\033[0m")
        final = self._reduce_phase(user_input, subtask_results)

        self.storage.update_task_state(parent_id, "COMPLETED", result=final)
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": final})
        return final

    # ── Planner ───────────────────────────────────────────────────────────────

    def _map_phase_legacy(self, user_input: str) -> list:
        """Original one-shot planner — kept for rollback. Returns list[str]."""
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
        return re.findall(r'^\s*\d+\.\s*(.+)$', plan_text, re.MULTILINE)

    def _map_phase(self, user_input: str, data_file: Optional[str] = None) -> tuple:
        """
        Planner: structured output path (default) + CoT fallback.
        Returns (Optional[Intent], list[str] of subtask descriptions).

        Structured path: single chat_structured(Plan) call — grammar-constrained,
        every step guaranteed to have a valid tool name.

        CoT fallback (FOX_STRUCTURED_OUTPUT=0 or on exception): two-pass text planner
        with self-critique and pre-flight structural validation.
        """
        tool_list = ", ".join(sorted(_TOOL_NAMES))
        data_ref = data_file or os.path.join(self.work_dir, "user_input.txt")

        # Few-shot from playbook (story 10.3)
        example_block = ""
        try:
            chains = self.storage.find_similar_chains(user_input, limit=1)
            if chains and chains[0].get("score", 0) > 0.15:
                chain = chains[0]
                example_lines = [f"Task: {chain['description'][:100]}", "Plan:"]
                for j, step in enumerate(chain["steps"][:6], 1):
                    arg_preview = (
                        next(iter(step["args"].values()), "")[:60]
                        if step["args"] else ""
                    )
                    example_lines.append(f"  {j}. {step['tool']}({arg_preview})")
                example_block = (
                    "EXAMPLE (past successful task):\n"
                    + "\n".join(example_lines)
                    + "\n\nNOW PLAN FOR THE NEW TASK:\n"
                )
        except Exception as e:
            print(f"  \033[33m⚠ playbook lookup failed: {e}\033[0m")

        # ── Structured path (default) ─────────────────────────────────────────
        if _STRUCTURED_OUTPUT:
            system = (
                "You are a task planner. Output a JSON plan matching the schema.\n\n"
                "Rules:\n"
                "- steps: 2–5 items. Each step calls exactly ONE tool.\n"
                "- intent: one sentence summarising the user's goal.\n"
                "- reasoning: think through the approach first.\n"
                "- First step: read or parse input data.\n"
                "- Last step: print or write the final result.\n"
                f"- IMPORTANT: The ONLY input file available is: {data_ref}\n"
                f"  Do NOT invent filenames. Use: {data_ref}\n"
            )
            user_msg = example_block + user_input
            try:
                print(f"  \033[90m🗺  structured planner...\033[0m")
                plan: Plan = chat_structured(
                    [{"role": "system", "content": system},
                     {"role": "user",   "content": user_msg}],
                    Plan,
                )
                intent = Intent.from_dict({"summary": plan.intent, "criteria": []})
                subtasks = [f"{s.tool} {s.description}" for s in plan.steps]
                print(f"  \033[36m📋 {len(subtasks)}-step plan: {plan.intent[:80]}\033[0m")
                return (intent, subtasks)
            except Exception as e:
                print(f"  \033[33m⚠ structured plan failed ({e}), falling back to CoT\033[0m")
                # Fall through to CoT path

        # ── Pass 1: draft ─────────────────────────────────────────────────────
        pass1_system = (
            "You are a planner for a small-model agent. Output exactly three sections.\n\n"
            "INTENT:\n"
            "{\"summary\": \"<one line goal>\", \"criteria\": [<zero or more criterion objects>]}\n\n"
            "Criterion schema (use only what the user explicitly asked for; for trivial tasks use []):\n"
            "  {\"type\": \"file_exists\",     \"args\": {\"path_pattern\": \"*.pptx\", \"min_bytes\": 500}}\n"
            "  {\"type\": \"file_format\",     \"args\": {\"path_pattern\": \"*.pptx\", \"format\": \"pptx\"}}\n"
            "  {\"type\": \"output_contains\", \"args\": {\"keywords\": [\"word1\"]}}\n\n"
            "REASONING:\n"
            "<3-5 short lines. Identify: goal, inputs, intermediate values, outputs, the tool for each step.>\n\n"
            "PLAN:\n"
            "1. <atomic step — one tool, exact input, exact output>\n"
            "2. ...\n"
            f"(3–8 steps. Each step uses ONE tool from {{{tool_list}}}. No 'and'. No prose.)\n\n"
            f"IMPORTANT: The ONLY input file available is: {data_ref}\n"
            f"Do NOT invent filenames. Every step that reads data must use: {data_ref}"
        )
        pass1_messages = [
            {"role": "system", "content": pass1_system},
            {"role": "user",   "content": example_block + user_input},
        ]
        print(f"  \033[90m🗺  planner pass 1...\033[0m")
        response1 = self.llm_fn(pass1_messages, use_tools=False, think=False)
        draft_text = response1.get("content", "")
        print(f"\033[36m{draft_text[:400]}\033[0m")

        # ── Pass 2: self-critique ─────────────────────────────────────────────
        pass2_messages = [
            {
                "role": "system",
                "content": (
                    "Review this plan. For each step check:\n"
                    f"  (a) Does it name exactly one tool from {{{tool_list}}}?\n"
                    "  (b) Does it name the exact file or value it reads?\n"
                    "  (c) Does it name the exact file or value it produces?\n\n"
                    "If any step fails (a), (b), or (c) — rewrite it.\n"
                    "If a step contains 'and' — split it into two steps.\n\n"
                    "Output ONLY the corrected PLAN: section. No commentary."
                ),
            },
            {"role": "user", "content": draft_text},
        ]
        print(f"  \033[90m🗺  planner pass 2 (critique)...\033[0m")
        response2 = self.llm_fn(pass2_messages, use_tools=False, think=False)
        critiqued_text = response2.get("content", "")

        # Parse PLAN section from critiqued output; fall back to draft
        plan_match = re.search(r'PLAN:\s*\n(.*)', critiqued_text, re.DOTALL)
        plan_body = plan_match.group(1) if plan_match else critiqued_text
        subtasks = re.findall(r'^\s*\d+\.\s*(.+)$', plan_body, re.MULTILINE)

        if not subtasks:
            plan_match2 = re.search(r'PLAN:\s*\n(.*)', draft_text, re.DOTALL)
            plan_body2 = plan_match2.group(1) if plan_match2 else draft_text
            subtasks = re.findall(r'^\s*\d+\.\s*(.+)$', plan_body2, re.MULTILINE)

        # Parse INTENT from draft text
        intent = _parse_intent_from_plan(draft_text)

        # ── Pre-flight structural validation (story 10.6) ─────────────────────
        ok, reasons = _validate_plan_structural(subtasks)
        if not ok:
            print(f"  \033[33m⚠ plan invalid: {'; '.join(reasons)} — re-planning once\033[0m")
            replan_messages = [
                {"role": "system", "content": pass1_system},
                {
                    "role": "user",
                    "content": (
                        user_input
                        + f"\n\nPrevious plan was invalid: {'; '.join(reasons)}. "
                        + f"Each step MUST mention one tool name from {{{tool_list}}}."
                    ),
                },
            ]
            r3 = self.llm_fn(replan_messages, use_tools=False, think=False)
            replan_text = r3.get("content", "")
            plan_match3 = re.search(r'PLAN:\s*\n(.*)', replan_text, re.DOTALL)
            plan_body3 = plan_match3.group(1) if plan_match3 else replan_text
            subtasks = re.findall(r'^\s*\d+\.\s*(.+)$', plan_body3, re.MULTILINE)
            ok2, _ = _validate_plan_structural(subtasks)
            if not ok2:
                # Both attempts invalid — return empty so execute() falls back to _run_single
                subtasks = []

        return (intent, subtasks)

    def _build_subtask_messages(
        self,
        description: str,
        data_file: Optional[str],
        prev_results_file: str,
        step_index: int = 1,
        total_steps: int = 1,
        plan_path: Optional[str] = None,
    ) -> list:
        """Fresh, isolated message list for one subtask."""
        system = build_system_prompt(self.work_dir)
        data_ref = data_file or "(no data file)"
        plan_ref = f"Full plan is at {plan_path}." if plan_path else ""
        if _STRUCTURED_OUTPUT:
            output_format = (
                "OUTPUT FORMAT — your FINAL message must be a JSON object only:\n"
                '{"result": "<one-line answer>", "files_created": ["path/to/file"]}\n'
                "Use empty list for files_created if no files were written.\n"
                "Do NOT wrap in markdown fences. Output ONLY the JSON as your last message."
            )
        else:
            output_format = (
                "OUTPUT FORMAT:\n"
                "Your final message MUST end with exactly one line:\n"
                "  RESULT: <value>\n"
                "Where <value> is the direct answer to this step.\n"
                "Do not add text after the RESULT line."
            )
        user_content = (
            f"You are step {step_index} of {total_steps}. {plan_ref}\n\n"
            f"TASK: {description}\n\n"
            f"FILES YOU MUST USE:\n"
            f"  - Input data: {data_ref}\n"
            f"  - Previous step results: {prev_results_file}\n\n"
            f"RULES:\n"
            f"- Read the input file with run_python. Do NOT invent filenames.\n"
            f"- Do NOT hardcode values. Parse from the file.\n"
            f"- Call exactly ONE tool. Print the result. Then stop.\n"
            f"- Print your results with print().\n\n"
            f"{output_format}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_content},
        ]

    def _reduce_phase(self, user_input: str, subtask_results: list) -> str:
        """TF-IDF ranked synthesis — top results in full, rest as one-liners."""
        from src.relevance import rank_results_for_query

        result_docs = [
            {"id": str(i), "text": r}
            for i, r in enumerate(subtask_results)
        ]
        ranked = rank_results_for_query(user_input, result_docs, top_k=2)
        top_ids = {r["id"] for r in ranked}

        parts: list = []
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

    def _verify_step(
        self,
        desc: str,
        result: str,
        files_created: Optional[list] = None,
    ) -> tuple:
        """
        Structural check only — no LLM judge.
        Returns (ok, reason).

        When files_created is provided (from StepResult JSON), check those exact
        paths instead of regex-guessing filenames from the description.
        """
        # Structured path: explicit file list from StepResult
        if files_created is not None:
            for path in files_created:
                if not os.path.exists(path) and not os.path.exists(
                    os.path.join(self.work_dir, path)
                ):
                    return False, f"expected file {path} not found"
            return True, ""

        # Legacy path: check RESULT: line then regex on description
        if not _RESULT_RE.search(result or ""):
            return False, "output missing 'RESULT:' line"
        if re.search(r'(write_file|write|create|save|generate)', desc, re.I):
            paths = re.findall(
                r'([\w./-]+\.(?:py|txt|csv|md|json|html|pptx|xlsx|pdf|png))', desc
            )
            for p in paths:
                candidate = p if os.path.isabs(p) else os.path.join(self.work_dir, p)
                if not os.path.exists(candidate) and not os.path.exists(
                    os.path.join(os.getcwd(), p)
                ):
                    return False, f"expected file {p} not created"
        return True, ""
