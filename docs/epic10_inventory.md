# Epic 10 — Pre-implementation Inventory

## `src/mapreduce.py` anchor lines (at time of inventory)

| Symbol | Line |
|---|---|
| `save_user_input` | 21 |
| `MapReduceOrchestrator.__init__` | 36 |
| `should_decompose` | 50 |
| `execute` | 53 |
| `_retry_for_intent` | 91 |
| `_run_single` | 126 |
| `_run_mapreduce` | 152 |
| `_map_phase` | 217 |
| `_build_subtask_messages` | 237 |
| `_reduce_phase` | 261 |

### Planner system prompt (original)
```
"You are a task planner. Output ONLY a numbered list of 2-4 tasks. No explanation.\n"
"Rules:\n"
"- Maximum 4 tasks. Combine related steps.\n"
"- First task: read and parse ALL input data from the file.\n"
"- Last task: compare/diff/summarise and print results.\n"
"- Output ONLY the numbered list."
```

### Subtask user_content (original)
```
f"TASK: {description}\n\n"
f"FILES YOU MUST USE:\n"
f"  - Input data: {data_ref}\n"
f"  - Previous task results: {prev_results_file}\n\n"
f"RULES:\n"
f"- Read the input file with run_python. Do NOT invent filenames.\n"
f"- Do NOT hardcode values. Parse from the file.\n"
f"- Print your results with print()."
```

## `src/states.py` anchor lines

| Symbol | Line |
|---|---|
| `TaskStateMachine.run` | 96 |
| Playbook injection block | 113–129 |

## `src/storage.py` anchor lines

| Symbol | Line |
|---|---|
| `detect_cycles` | 314 |
| `record_task_chain` | 348 |
| `find_similar_chains` | 388 |

## `src/validator.py` anchor lines

| Symbol | Line |
|---|---|
| `extract_intent` | 91 |
| `_check_semantic` | 186 |
| `validate` | 207 |

## `src/commands.py` — `_COMMAND_MAP` tool names

`run_bash`, `run_python`, `read_file`, `write_file`, `grep_search`, `list_files`, `search_examples`

Note: `search_examples` is in the map but NOT in the TOOLS list exposed to the model for planning purposes. The six planning-relevant tools are: `run_bash`, `run_python`, `read_file`, `write_file`, `grep_search`, `list_files`.
