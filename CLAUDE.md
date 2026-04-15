# Fox - A Clever and Cunning Agent Loop

Small, agile agent for local LLMs via Ollama. Designed for models with limited context windows — every token counts.

Python 3, minimal dependencies (`requests`, `duckdb`).

## Design Philosophy

Fox is built for small models. Every design decision optimizes for:

- **Minimal context** — aggressive summarization, sliding window on conversation history, tool results compressed before going back to the LLM
- **Tool call reuse** — before executing a tool, check DuckDB for a recent identical call (same name + args). Cache hit = skip execution, return stored result. Saves both time and context tokens.
- **Lean messages** — system prompt is compact. Tool results are truncated and summarized. Prior subtask results are referenced by ID, not inlined.
- **Isolated subtask context** — each subtask state machine gets a fresh, minimal message list (system + task description + tool results only). No cross-contamination of context between subtasks.

## Architecture

Four patterns, kept lean:

### 1. State Machine (`src/states.py`)
Each task runs through explicit states:

```
PENDING -> EXECUTING -> TOOL_CALLING -> WAITING_RESULT -> EVALUATING -> COMPLETED
                                                                 \-> EXECUTING (loop)
                                                           any -> FAILED
```

- `TaskStateMachine.run(llm_fn, command_registry, storage)` is the core agent loop
- Transitions validated and logged to DuckDB
- Context compression: after each EVALUATING step, old tool results in the message list are replaced with one-line summaries (tool name + truncated output). Only the most recent 2 tool results kept in full.

### 2. MapReduce (`src/mapreduce.py`)
Orchestrates simple vs complex queries:

- **Simple**: single `TaskStateMachine`, no decomposition
- **Complex** (5+ input lines): Map -> Execute -> Reduce
- Each subtask gets an **isolated context** — fresh message list with only what it needs
- Reduce phase receives subtask results as compact summaries, not full transcripts
- Subtasks linked to parent via `parent_id` in DuckDB

### 3. Command Pattern (`src/commands.py`)
Each tool is a `ToolCommand` subclass:

- `RunBashCommand`, `RunPythonCommand`, `ReadFileCommand`, `WriteFileCommand`, `GrepSearchCommand`, `ListFilesCommand`
- `CommandRegistry.build(tool_call)` — factory from LLM response
- **Result caching**: before `execute()`, registry checks DuckDB for matching (tool_name, args_hash) from current session. Cache hit skips execution.
- Commands capture timing, exit code, success; `WriteFileCommand` supports `undo()`

### 4. DuckDB Storage (`src/storage.py`)
Persistent at `~/.local/share/fox/history.duckdb`:

| Table | Key columns |
|---|---|
| `sessions` | session_id, model, cwd |
| `tasks` | task_id, parent_id, description, state, result |
| `task_transitions` | task_id, from_state, to_state, reason |
| `tool_calls` | task_id, tool_name, args_hash, args_json, output, success, elapsed |

Dual purpose: execution history + tool call cache. The `args_hash` column enables fast cache lookups.

No `conversations` table — conversation context is ephemeral and managed in-memory with sliding window. Only tool calls and task results persist (they're the reusable parts).

## Context Management Strategies

The core problem with small models: context fills up fast during multi-turn tool use. Fox uses 7 strategies to keep context lean.

### Strategy 1: Tiered Tool Result Retention
Not all tool results are equally valuable over time:
- **Just executed** (current turn): full result in messages
- **1-2 turns ago**: truncated to 500 chars (first/last 200 + truncation marker)
- **3+ turns ago**: replaced with one-liner: `"[tool: grep_search -> 14 matches in 3 files]"`
- Full results always in DuckDB — the model can re-query via `query_history` tool if needed

### Strategy 2: Tool Call Caching via DuckDB
Before executing, hash `(tool_name, canonical_args)`. Check DuckDB for a recent hit:
- **Read-only tools** (read_file, grep_search, list_files): return cached result, zero tokens wasted
- **Write tools** (run_bash, run_python, write_file): always execute fresh, but log repeats for loop detection
- Cache keyed on `args_hash` (SHA256 of `json.dumps(args, sort_keys=True)`), configurable `max_age`

### Strategy 3: Progressive System Prompt
System prompt shrinks based on execution phase:
- **First turn**: full prompt with tool descriptions + rules + MCP.md context
- **Subsequent turns in same loop**: strip tool descriptions (model already has them), keep rules + working context only
- **Subtask context**: minimal — just cwd, work_dir, and task description

### Strategy 4: Checkpoint Summarization at EVALUATING
When the state machine hits EVALUATING and loops back to EXECUTING:
1. Extract from recent messages: what tools were called, what was learned, what's still needed
2. Replace the block with a single checkpoint message: `{"role": "system", "content": "Progress: ..."}`
3. Model gets a clean summary instead of replaying full history

### Strategy 5: Smart Truncation (Content-Aware)
Not just character limits — truncation adapts to content type:
- **File reads**: first 10 + last 10 lines, `... ({N} lines omitted)` in between
- **Grep results**: first 5 matches + count: `"14 matches. First 5:\n{matches}\n... (9 more)"`
- **Command output**: if exit 0 and output > 500 chars, `"Success: {first_line}"`
- **Python output**: keep all (it's the model's computation) — but cap at 2KB

### Strategy 6: Sliding Window
- Keep system prompt + last N messages (configurable, default 8)
- Evicted messages compressed into a summary message with topic hints (first 60 chars of each user message)
- Subtask isolation: each subtask SM gets a fresh `[system, task_description]` — no cross-contamination

### Strategy 7: TF-IDF Relevance Scoring (`src/relevance.py`)
Lightweight TF-IDF (stdlib only: `math` + `collections`) scores tool results against the current query:

- **Corpus**: tool call outputs stored in DuckDB
- **Query**: user input or current subtask description
- **Scoring**: TF-IDF cosine similarity between query terms and each tool result

Used in three places:
1. **Reduce phase**: rank subtask results by relevance to original query. Top-K in full, rest as one-liners.
2. **Context compression**: when evicting old tool results, keep the most relevant ones (not just most recent)
3. **Cross-session recall**: find relevant tool results from past sessions

### Execution Graph (`src/storage.py` — graph tables)
Lightweight relation tracking in DuckDB (not a graph database):

**Entities table** — things extracted from tool results:
```sql
entities (entity_id, entity_type, value, first_seen)
-- entity_type: 'file_path', 'function', 'error_code', 'pattern'
```

**Edges table** — relations between objects:
```sql
edges (source_type, source_id, target_type, target_id, relation, weight, timestamp)
-- source/target types: 'task', 'tool_call', 'entity', 'session'
-- relations: 'produced', 'mentions', 'informed', 'shares_entity'
```

Entity extraction is cheap regex:
- File paths: `/foo/bar.py` patterns
- Function names: from grep results (`def foo`, `function foo`)
- Error patterns: `Error: ...`, exit codes

What the graph enables:
- **Smarter context selection**: if the model is working on file X, surface all tool results that mention X — even from 5 turns ago
- **Loop detection**: graph shows `grep→read→grep→read` on same args = cycling. State machine can force FAILED or try a different approach.
- **Connected reduce**: subtask results that share entities get grouped and compared; isolated results get summarized
- **Cross-session continuity**: "last time you worked on this file, here's what you found" — follow entity edges backward

## Module Layout

```
agent.py              # entry point -> src/repl.py:main()
src/
  states.py           # TaskState enum, transitions, TaskStateMachine
  commands.py         # ToolCommand ABC, 6 subclasses, CommandRegistry (with caching)
  mapreduce.py        # MapReduceOrchestrator
  storage.py          # DuckDB schema + helpers + cache lookups + graph tables (entities, edges)
  ollama.py           # chat(), TOOLS list, build_system_prompt()
  context.py          # Sliding window, message compression, smart truncation, checkpoint summarization
  relevance.py        # TF-IDF scoring, entity extraction, relevance-based context selection
  terminal.py         # Raw-mode terminal input (unchanged from original)
  repl.py             # REPL loop, wires everything together
```

## Implementation Order

1. `src/storage.py` — schema + cache lookups + graph tables (entities, edges)
2. `src/commands.py` — tool commands with cache-before-execute
3. `src/relevance.py` — TF-IDF scoring + entity extraction
4. `src/context.py` — sliding window + smart truncation + checkpoint summarization + relevance-based eviction
5. `src/states.py` — state machine with context compression after each turn
6. `src/ollama.py` — extract `chat()`, `TOOLS`, progressive `build_system_prompt()`
7. `src/terminal.py` — copy verbatim from agent.py
8. `src/mapreduce.py` — orchestrator with isolated subtask contexts + TF-IDF reduce
9. `src/repl.py` + update `agent.py` entry point

## Key Interfaces

- `llm_fn(messages, use_tools, think) -> dict` — passed through layers, mockable
- `CommandRegistry.build(tool_call) -> ToolCommand` — factory, checks cache first
- `TaskStateMachine.run(llm_fn, registry, storage) -> str` — drives one task
- `MapReduceOrchestrator.execute(user_input, messages, data_file) -> str` — top-level
- `context.compress_context(messages, window_size, keep_full_tools) -> messages` — combined compression
- `relevance.score_results(query, results) -> list[(result_id, score)]` — TF-IDF ranking
- `relevance.extract_entities(text) -> list[(entity_type, value)]` — regex entity extraction
- `storage.record_entities(tool_call_id, entities)` — persist extracted entities
- `storage.find_related(entity_value) -> list[dict]` — graph traversal for related tool calls

## Configuration

- `OLLAMA_URL` — Ollama endpoint (default: `http://localhost:11434`)
- `OLLAMA_MODEL` — model name (default: `gemma4`)
- `MAX_AGENT_TURNS` — max loop iterations (default: `30`)
- `CONTEXT_WINDOW` — messages to keep in full (default: `8`)
- `TOOL_RESULT_MAX` — max chars per tool result in context (default: `500`)

## Rules

- Tools in `run_python` use stdlib only — no pandas/numpy
- Never hardcode data values — always read from files
- Internet access via curl in `run_bash`
- Every token in context should earn its place
