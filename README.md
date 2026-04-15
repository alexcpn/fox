# Fox 🦊

A clever and cunning agent loop for local LLMs via Ollama. Built for small models — every token counts.

## Quick start

```bash
pip install duckdb requests
python3 agent.py
```

Requires [Ollama](https://ollama.com) running locally with at least one model pulled:

```bash
ollama pull gemma4
```

## What it does

Fox is an interactive terminal agent. You give it a task; it uses tools to complete it. It works well with small local models (8B–27B) because it actively manages context rather than letting it bloat.

```
🦊 Fox — gemma4 @ http://localhost:11434
   cwd:     /your/working/directory
   scratch: /tmp/fox_work_xxxxx

❯ what python files are in this directory?
  ⚙  grep_search(pattern=\.py$, path=.)
     → agent.py, src/commands.py, src/context.py ...
```

## Tools

| Tool | Description |
|---|---|
| `run_bash` | Execute shell commands. Full internet access via curl. |
| `run_python` | Run Python 3 scripts. stdlib only — csv, json, re, collections. |
| `read_file` | Read files with optional line ranges. |
| `write_file` | Write or create files. |
| `grep_search` | Recursive regex search across files. |
| `list_files` | List directory contents. |

## Architecture

Four patterns keep Fox fast and lean:

### State machine
Each task drives through explicit states: `PENDING → EXECUTING → TOOL_CALLING → WAITING_RESULT → EVALUATING → COMPLETED`. Transitions are validated and logged. Loop detection (same tool call repeated ≥3 times) forces `FAILED` before the model spins forever.

### MapReduce
Simple queries (< 5 lines) go straight to a single state machine. Complex queries get decomposed:
- **Map** — LLM breaks the query into 2–4 subtasks
- **Execute** — each subtask runs in an isolated context (no cross-contamination)
- **Reduce** — results are TF-IDF ranked and synthesised into a final answer

### Command pattern
Each tool is a `ToolCommand` subclass with `execute()`, `undo()` (where meaningful), and a result cache. Read-only tools (`read_file`, `grep_search`, `list_files`) are cached by content hash — repeated calls within a session return instantly.

### DuckDB storage
Persistent history at `~/.local/share/fox/history.duckdb`. Stores every session, task, state transition, and tool call. Enables:
- **Tool call cache** — skip re-executing identical read-only calls
- **Entity graph** — file paths, functions, error codes extracted from tool outputs and linked as a relation graph for smarter context selection
- **Cross-session recall** — TF-IDF search over past tool outputs
- **Startup GC** — incomplete tasks from crashed sessions are marked FAILED cleanly

### Context management
Seven strategies to keep the message list lean for small context windows:

1. **Tiered tool retention** — last 2 tool results in full, older ones compressed to one-liners
2. **Tool call caching** — don't re-execute, don't re-add to context
3. **Progressive system prompt** — strip tool descriptions after turn 0 (model already has them)
4. **Checkpoint summarisation** — when looping, compress progress into a single summary message
5. **Smart truncation** — file reads keep first/last 10 lines; grep keeps first 5 matches; bash shows first line on success
6. **Sliding window** — keep system prompt + last 8 messages; evict older turns with a breadcrumb
7. **TF-IDF relevance** — when evicting, keep the tool results most relevant to the current query, not just the most recent

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4` | Model to use |
| `MAX_AGENT_TURNS` | `30` | Max tool-use iterations per query |
| `CONTEXT_WINDOW` | `8` | Messages to keep in full |
| `TOOL_RESULT_MAX` | `500` | Max chars per tool result in context |

```bash
OLLAMA_MODEL=llama3.2 python3 agent.py
```

## Terminal shortcuts

| Key | Action |
|---|---|
| `Enter` | Submit |
| `Alt+Enter` | Insert newline |
| Paste | Auto-detected, saved to file |
| `Ctrl+C` | Cancel current input |
| `Ctrl+D` | Exit |
| `Ctrl+U` | Clear current line |

## REPL commands

| Command | Action |
|---|---|
| `quit` / `exit` / `q` | Exit Fox |
| `cd <path>` | Change working directory |
| `clear` | Reset conversation context |

## Module layout

```
agent.py          entry point
src/
  storage.py      DuckDB schema, cache, entity graph, startup GC
  commands.py     ToolCommand subclasses + CommandRegistry
  relevance.py    TF-IDF index + entity extraction (stdlib only)
  context.py      Context compression pipeline
  states.py       TaskState enum + TaskStateMachine
  ollama.py       chat(), TOOLS list, system prompt
  terminal.py     Raw-mode terminal input
  mapreduce.py    MapReduceOrchestrator
  repl.py         REPL loop
```

## Inspecting history

```bash
duckdb ~/.local/share/fox/history.duckdb
```

```sql
-- Recent tool calls
SELECT tool_name, success, elapsed, output[:100] FROM tool_calls ORDER BY timestamp DESC LIMIT 10;

-- Task state transitions
SELECT task_id, from_state, to_state, reason FROM task_transitions ORDER BY timestamp DESC LIMIT 20;

-- Entities extracted from tool outputs
SELECT entity_type, value FROM entities ORDER BY first_seen DESC LIMIT 20;

-- Failed tasks
SELECT description, error FROM tasks WHERE state = 'FAILED' ORDER BY created_at DESC LIMIT 10;
```
