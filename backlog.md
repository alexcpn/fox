# Fox - Implementation Backlog

## Epic 1: Storage Layer (`src/storage.py`)

### 1.1 DuckDB schema initialization
- Create DB at `~/.local/share/fox/history.duckdb`
- `os.makedirs` for parent dir
- `__init__` connects and calls `_init_schema()`

### 1.2 Sessions table
```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id   VARCHAR PRIMARY KEY,
    started_at   DOUBLE,
    model        VARCHAR,
    cwd          VARCHAR
)
```
- `create_session(session_id, model, cwd)` inserts with `time.time()`

### 1.3 Tasks table
```sql
CREATE TABLE IF NOT EXISTS tasks (
    task_id      VARCHAR PRIMARY KEY,
    session_id   VARCHAR,
    parent_id    VARCHAR,          -- NULL for top-level, parent task_id for subtasks
    description  VARCHAR,
    state        VARCHAR,
    created_at   DOUBLE,
    completed_at DOUBLE,
    result       VARCHAR,
    error        VARCHAR
)
```
- `create_task(task_id, session_id, description, parent_id=None)`
- `update_task_state(task_id, state, result=None, error=None)` — sets `completed_at` for terminal states

### 1.4 Task transitions table
```sql
CREATE SEQUENCE IF NOT EXISTS trans_seq START 1;
CREATE TABLE IF NOT EXISTS task_transitions (
    id           INTEGER PRIMARY KEY DEFAULT nextval('trans_seq'),
    task_id      VARCHAR,
    from_state   VARCHAR,
    to_state     VARCHAR,
    timestamp    DOUBLE,
    reason       VARCHAR
)
```
- `log_transition(task_id, from_state, to_state, reason="")`

### 1.5 Tool calls table (with cache support)
```sql
CREATE SEQUENCE IF NOT EXISTS tc_seq START 1;
CREATE TABLE IF NOT EXISTS tool_calls (
    id           INTEGER PRIMARY KEY DEFAULT nextval('tc_seq'),
    task_id      VARCHAR,
    session_id   VARCHAR,
    tool_name    VARCHAR,
    args_hash    VARCHAR,          -- SHA256 of canonical JSON args
    args_json    VARCHAR,
    output       VARCHAR,
    success      BOOLEAN,
    elapsed      DOUBLE,
    exit_code    INTEGER,
    timestamp    DOUBLE
)
```
- `record_tool_call(task_id, session_id, cmd)` — computes `args_hash`, inserts
- `lookup_cached_tool_call(tool_name, args_hash, max_age=300)` — returns output if fresh cache hit exists. Only caches read-only tools (`read_file`, `grep_search`, `list_files`). Never caches `run_bash`, `run_python`, `write_file`.

### 1.6 Entities table (graph storage)
```sql
CREATE SEQUENCE IF NOT EXISTS ent_seq START 1;
CREATE TABLE IF NOT EXISTS entities (
    entity_id    INTEGER PRIMARY KEY DEFAULT nextval('ent_seq'),
    entity_type  VARCHAR,          -- 'file_path', 'function', 'error_code', 'pattern'
    value        VARCHAR,
    first_seen   DOUBLE
)
```
- Unique constraint on `(entity_type, value)`
- `record_entity(entity_type, value) -> entity_id` — insert or return existing

### 1.7 Edges table (graph storage)
```sql
CREATE SEQUENCE IF NOT EXISTS edge_seq START 1;
CREATE TABLE IF NOT EXISTS edges (
    id           INTEGER PRIMARY KEY DEFAULT nextval('edge_seq'),
    source_type  VARCHAR,          -- 'task', 'tool_call', 'entity', 'session'
    source_id    VARCHAR,
    target_type  VARCHAR,
    target_id    VARCHAR,
    relation     VARCHAR,          -- 'produced', 'mentions', 'informed', 'shares_entity'
    weight       DOUBLE DEFAULT 1.0,
    timestamp    DOUBLE
)
```
- `record_edge(source_type, source_id, target_type, target_id, relation, weight=1.0)`
- `find_related(entity_value) -> list[dict]` — find all tool_calls that mention a given entity
- `detect_cycles(task_id) -> bool` — check if recent tool calls form a cycle (same tool+args repeated)

### 1.8 Graph helper: record_entities_from_tool_call
```python
def record_entities_from_tool_call(self, tool_call_id: str, tool_name: str, args: dict, output: str):
```
- Called after every tool execution
- Extracts entities from both args and output using `relevance.extract_entities()`
- Creates entity records + edges (`tool_call -[mentions]-> entity`)
- For file tools: extracts file path from args as entity
- For grep: extracts matched file paths from output

### 1.9 Startup garbage collection
```python
def gc_incomplete_tasks(self) -> int
```
- Called once at startup, after `create_session()`
- Marks all non-terminal tasks as FAILED: `UPDATE tasks SET state='FAILED', error='session ended', completed_at=? WHERE state NOT IN ('COMPLETED', 'FAILED')`
- Logs a transition for each: `from_state -> FAILED, reason="session ended"`
- Returns count of GC'd tasks
- Prints to terminal if any were cleaned up: `"GC: marked {N} incomplete tasks from prior sessions as FAILED"`
- Completed subtask results remain in `tool_calls` — still queryable via TF-IDF for cross-session recall

### 1.10 Query helpers
- `query(sql) -> list[dict]` — arbitrary read-only SQL
- `get_task_history(task_id) -> list[dict]` — transitions for a task
- `get_related_tool_calls(entity_value) -> list[dict]` — all tool calls mentioning this entity
- `close()` — close connection

---

## Epic 2: Command Pattern (`src/commands.py`)

### 2.1 CommandResult dataclass
```python
@dataclass
class CommandResult:
    output: str
    success: bool
    elapsed: float
    exit_code: Optional[int] = None
```

### 2.2 ToolCommand ABC
```python
class ToolCommand(ABC):
    name: str
    args: dict
    timestamp: float
    result: Optional[CommandResult]

    @abstractmethod
    def execute(self) -> CommandResult

    def undo(self) -> Optional[str]  # default returns None
    def metadata(self) -> dict       # name, args_summary, timestamp, success
    def args_hash(self) -> str       # SHA256 of json.dumps(args, sort_keys=True)
```

### 2.3 RunBashCommand
- `shell=True`, `timeout=120`, `cwd=os.getcwd()`
- Captures stdout, stderr (with `--- stderr ---` separator), exit code
- Truncates to 20KB
- Handles `TimeoutExpired` and generic exceptions

### 2.4 RunPythonCommand
- Accepts `work_dir` in constructor
- Strips markdown code fences from script
- Writes to `{work_dir}/_script.py`, executes with `sys.executable`
- Passes `WORK_DIR` env var
- `timeout=120`, truncate 20KB

### 2.5 ReadFileCommand
- `os.path.expanduser`, resolve relative to cwd
- Optional `start_line`/`end_line` (1-indexed)
- Line-numbered output: `{lineno:4d} | {line}`
- Truncate 20KB

### 2.6 WriteFileCommand
- `os.makedirs` for parent dir
- Saves `_previous_content` before overwrite (for undo)
- `undo()`: restores previous content, or removes file if it didn't exist before

### 2.7 GrepSearchCommand
- `grep -rn --color=never`, optional `--include` glob
- `timeout=30`, truncate 20KB

### 2.8 ListFilesCommand
- Non-recursive: `ls -lah`
- Recursive: `find` with `-maxdepth 3`, excludes dotfiles
- `timeout=15`, truncate 20KB

### 2.9 CommandRegistry
```python
class CommandRegistry:
    def __init__(self, work_dir: str, storage: Storage)
    def build(self, tool_call: dict) -> ToolCommand
    def execute_with_cache(self, cmd: ToolCommand, task_id: str, session_id: str) -> CommandResult
```
- `build()` maps `tool_call["function"]["name"]` to the right subclass
- `execute_with_cache()`:
  1. If tool is cacheable (`read_file`, `grep_search`, `list_files`): check `storage.lookup_cached_tool_call(cmd.name, cmd.args_hash())`
  2. Cache hit: create `CommandResult` from stored output, set `cmd.result`, skip execution
  3. Cache miss or non-cacheable: call `cmd.execute()`
  4. Always call `storage.record_tool_call()` after

---

## Epic 3: Relevance Engine (`src/relevance.py`)

Zero external dependencies — uses `math`, `collections`, `re` from stdlib.

### 3.1 Tokenizer
```python
def tokenize(text: str) -> list[str]
```
- Lowercase, split on non-alphanumeric
- Remove stopwords (hardcoded small set: the, a, an, is, are, was, were, in, on, at, to, for, of, and, or, it, this, that)
- Returns list of tokens

### 3.2 TF-IDF index
```python
class TFIDFIndex:
    def __init__(self)
    def add_document(self, doc_id: str, text: str)
    def remove_document(self, doc_id: str)
    def score(self, query: str) -> list[tuple[str, float]]  # [(doc_id, score), ...] sorted desc
```
- `add_document`: tokenizes, stores term frequencies per doc, updates document frequencies
- `score`: computes TF-IDF cosine similarity between query and each document
- Lightweight: no matrix math, just dicts. Suitable for corpus of <1000 documents (typical agent session).
- **TF** = term count in doc / total terms in doc
- **IDF** = log(total docs / docs containing term)
- **Cosine similarity** between query TF-IDF vector and each doc TF-IDF vector

### 3.3 Entity extraction
```python
def extract_entities(text: str) -> list[tuple[str, str]]  # [(entity_type, value), ...]
```
- **file_path**: regex `r'(?:/[\w.\-]+)+\.[\w]+'` — matches `/foo/bar.py`, `/tmp/agent_work_xxx/file.txt`
- **function**: regex `r'(?:def|function|func)\s+(\w+)'` — from grep/read results
- **error_code**: regex `r'\[exit code:\s*(\d+)\]'` and `r'Error:\s*(.{1,80})'`
- **pattern**: regex `r'grep.*?["\'](.+?)["\']'` — search patterns used
- Returns deduplicated list

### 3.4 Score and rank tool results
```python
def rank_results_for_query(query: str, results: list[dict], top_k: int = 3) -> list[dict]
```
- `results` is a list of `{"id": str, "text": str}` from DuckDB tool_calls
- Builds a temporary TFIDFIndex, adds all results, scores against query
- Returns top_k results sorted by relevance

### 3.5 Relevance-aware eviction
```python
def select_relevant_tool_results(query: str, tool_messages: list[dict], keep: int = 2) -> list[int]
```
- Given the current query/task and a list of tool result messages, returns the indices of the `keep` most relevant ones (by TF-IDF score)
- Used by context.py to decide which old tool results to keep in full vs. compress
- Falls back to "most recent" if scores are too similar (< 0.05 spread)

---

## Epic 4: Context Management (`src/context.py`)

### 4.1 Smart truncation (content-aware)
```python
def smart_truncate(tool_name: str, result: str, max_chars: int = 500) -> str
```
- **read_file**: keep first 10 + last 10 lines, `"... ({N} lines omitted)"` between
- **grep_search**: first 5 matches + `"... ({N} more matches)"`
- **run_bash**: if exit 0 and output > max_chars, `"Success: {first_line}"`; if non-zero, keep full stderr
- **run_python**: keep all up to 2KB (model's own computation)
- **list_files**: first 20 entries + count
- **write_file**: keep as-is (already short)

### 4.2 one_line_tool_summary
```python
def one_line_tool_summary(tool_name: str, result: str) -> str
```
- Returns: `"[tool: {tool_name} -> {first_line_or_80_chars}]"`

### 4.3 Tiered tool result compression
```python
def compress_tool_results(messages: list, keep_full: int = 2, query: str = None) -> list
```
- Walk messages from newest to oldest
- If `query` is provided: use `relevance.select_relevant_tool_results()` to pick which to keep in full
- If no query: keep the `keep_full` most recent tool results at full content
- Replace older tool messages with `one_line_tool_summary`
- Returns new message list (does not mutate input)

### 4.4 sliding_window
```python
def sliding_window(messages: list, window_size: int = 8) -> list
```
- Always keeps `messages[0]` (system prompt)
- If `len(messages) <= window_size + 1`: return as-is
- Otherwise: keep system + summary of evicted turns + last `window_size` messages
- Summary: `{"role": "system", "content": "Prior context: {N} turns. Last discussed: {topic hints}"}`
- Topic hints: first 60 chars of each evicted user message

### 4.5 Checkpoint summarization
```python
def checkpoint(messages: list, start_idx: int) -> list
```
- Takes messages from `start_idx` to end
- Extracts: tools called (names), key results (one-liners), what the model concluded
- Replaces the block with a single `{"role": "system", "content": "Progress: called {tools}, found {key_findings}, next: {pending}"}`
- Called by state machine when looping EVALUATING -> EXECUTING

### 4.6 Progressive system prompt
```python
def compact_system_prompt(full_prompt: str, turn: int) -> str
```
- `turn == 0`: return full prompt (with tool descriptions, rules, MCP.md)
- `turn >= 1`: strip tool description block (model has seen it), keep rules + cwd + work_dir only
- Saves 200-400 tokens per turn after the first

### 4.7 compress_context (main entry point)
```python
def compress_context(messages: list, window_size: int = 8, keep_full_tools: int = 2,
                     query: str = None, turn: int = 0) -> list
```
- Applies in order: progressive system prompt -> sliding window -> tiered tool compression (with relevance if query provided)
- Single entry point called by state machine at EVALUATING

---

## Epic 5: State Machine (`src/states.py`)

### 5.1 TaskState enum
```python
class TaskState(enum.Enum):
    PENDING        = "PENDING"
    EXECUTING      = "EXECUTING"
    TOOL_CALLING   = "TOOL_CALLING"
    WAITING_RESULT = "WAITING_RESULT"
    EVALUATING     = "EVALUATING"
    COMPLETED      = "COMPLETED"
    FAILED         = "FAILED"
```
- Removed PLANNING state (planning is handled by MapReduce orchestrator, not individual task SM)

### 5.2 Transitions table
```python
TRANSITIONS = {
    PENDING:        {EXECUTING},
    EXECUTING:      {TOOL_CALLING, EVALUATING, FAILED},
    TOOL_CALLING:   {WAITING_RESULT, FAILED},
    WAITING_RESULT: {EVALUATING, FAILED},
    EVALUATING:     {EXECUTING, COMPLETED, FAILED},
}
```

### 5.3 Transition dataclass
```python
@dataclass
class Transition:
    from_state: TaskState
    to_state: TaskState
    timestamp: float
    reason: str = ""
```

### 5.4 TaskStateMachine
```python
@dataclass
class TaskStateMachine:
    task_id: str
    description: str
    state: TaskState = PENDING
    history: list[Transition]
    result: Optional[str] = None
    error: Optional[str] = None
    turn_count: int = 0
    max_turns: int = 10

    def transition(self, new_state, reason="")  # validates, logs
    def is_terminal -> bool
    def run(self, messages, llm_fn, command_registry, storage, session_id) -> str
```

### 5.5 `TaskStateMachine.run()` — the core loop
```
transition(PENDING -> EXECUTING)

while not is_terminal and turn_count < max_turns:
    if EXECUTING:
        response = llm_fn(messages, use_tools=True)
        append response to messages
        turn_count += 1
        if response has tool_calls:
            transition -> TOOL_CALLING
        elif response has content:
            transition -> EVALUATING
        else:
            transition -> FAILED("empty response")

    elif TOOL_CALLING:
        for each tool_call:
            cmd = command_registry.build(tool_call)
            transition -> WAITING_RESULT
            result = command_registry.execute_with_cache(cmd, task_id, session_id)
            # Extract entities and record graph edges
            entities = relevance.extract_entities(result.output)
            storage.record_entities_from_tool_call(tc_id, cmd.name, cmd.args, result.output)
            # Smart truncate before adding to messages
            truncated = context.smart_truncate(cmd.name, result.output)
            append {"role": "tool", "content": truncated} to messages
            print tool call + truncated result
        # Check for cycles via graph
        if storage.detect_cycles(task_id):
            transition -> FAILED("loop detected: repeating same tool calls")
        else:
            transition -> EVALUATING

    elif EVALUATING:
        last_response = last assistant message
        if last_response has text content and no tool_calls:
            self.result = content
            transition -> COMPLETED
        else:
            # Compress context with relevance-aware eviction
            messages = context.compress_context(
                messages, query=self.description, turn=turn_count
            )
            transition -> EXECUTING

if turn_count >= max_turns and not is_terminal:
    transition -> FAILED("max turns reached")

return self.result or self.error or "(no response)"
```

- Every `transition()` call also does `storage.log_transition()`
- Print colored status on each transition for observability
- Entity extraction happens at TOOL_CALLING (feeds the graph)
- Context compression with TF-IDF relevance happens at EVALUATING (uses the graph)
- Loop detection via graph edges at TOOL_CALLING (prevents infinite cycling)

---

## Epic 6: Ollama Interface (`src/ollama.py`)

### 6.1 Constants
```python
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma4")
MAX_TURNS = int(os.environ.get("MAX_AGENT_TURNS", "30"))
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "8"))
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "500"))
```

### 6.2 TOOLS list
- Same 6 tool definitions as current agent.py, extracted verbatim

### 6.3 build_system_prompt(work_dir)
- Compact version of current prompt
- Loads MCP.md context
- References work_dir and cwd

### 6.4 chat(messages, use_tools=True, think=True) -> dict
- Same as current: POST to `/api/chat`, returns `response["message"]`
- Prints elapsed time
- `timeout=600`

---

## Epic 7: Terminal I/O (`src/terminal.py`)

### 7.1 Copy verbatim
- `_read_input()`, `_read_raw()`, `_read_piped_input()` from agent.py lines 494-665
- No changes needed — this code works well
- Export `read_input` as the public function name

---

## Epic 8: MapReduce Orchestrator (`src/mapreduce.py`)

### 8.1 MapReduceOrchestrator class
```python
class MapReduceOrchestrator:
    def __init__(self, llm_fn, command_registry, storage, session_id)
    def should_decompose(self, user_input) -> bool   # 5+ lines heuristic
    def execute(self, user_input, messages, data_file=None) -> str
```

### 8.2 Simple path — `_run_single()`
- Create one `TaskStateMachine(max_turns=MAX_TURNS)`
- Append user message to `messages`
- Call `sm.run(messages, ...)`
- Append assistant response to `messages`
- Store task in DuckDB

### 8.3 Map phase — `_map_phase()`
- Fresh message list: compact planner system prompt + user input
- LLM call with `use_tools=False, think=False`
- Parse numbered list with regex
- Returns `list[str]` of subtask descriptions

### 8.4 Execute phase
- For each subtask:
  - Create `TaskStateMachine(max_turns=5)`
  - **Isolated context**: fresh `[system_prompt, task_description]` messages
  - Task description includes: the subtask text, data file path, previous results file path
  - Previous results saved to `{work_dir}/previous_results.txt` (compact summaries, not full output)
  - Run state machine
  - Collect result
  - Store task with `parent_id` in DuckDB

### 8.5 Reduce phase — `_reduce_phase()` (TF-IDF ranked)
- Score each subtask result against original query using `relevance.rank_results_for_query()`
- **Top-K results** (highest TF-IDF score, default K=2): pass in full (up to 500 chars each)
- **Remaining results**: pass as one-line summaries only
- If subtask results share entities (detected via graph edges): group them in the reduce prompt so the model sees connected results together
- Fresh message list: synth system prompt + original query + ranked results
- LLM call with `use_tools=False, think=False`
- Returns final answer string

### 8.6 Data extraction
- `save_user_input(user_input, work_dir)` — saves multi-line input (3+ lines) to `{work_dir}/user_input.txt`
- Returns file path or None

---

## Epic 9: REPL and Entry Point (`src/repl.py`, `agent.py`)

### 9.1 `src/repl.py:main()`
```python
def main():
    work_dir = tempfile.mkdtemp(prefix="fox_work_")
    storage = Storage()

    # GC incomplete tasks from prior sessions before anything else
    gc_count = storage.gc_incomplete_tasks()
    if gc_count:
        print(f"  \033[90mGC: marked {gc_count} incomplete tasks from prior sessions as FAILED\033[0m")

    session_id = f"sess-{uuid4().hex[:8]}"
    storage.create_session(session_id, MODEL, os.getcwd())

    command_registry = CommandRegistry(work_dir, storage)
    messages = [{"role": "system", "content": build_system_prompt(work_dir)}]

    orchestrator = MapReduceOrchestrator(chat, command_registry, storage, session_id)

    # Print banner
    # REPL loop: read_input() -> handle quit/cd/clear -> orchestrator.execute() -> print answer
```

### 9.2 REPL commands
- `quit`/`exit`/`q` — exit
- `cd <path>` — change directory
- `clear` — reset messages to `[system_prompt]`

### 9.3 Update `agent.py`
```python
#!/usr/bin/env python3
"""Fox - A Clever and Cunning Agent Loop"""
from src.repl import main

if __name__ == "__main__":
    main()
```

### 9.4 `src/__init__.py`
- Empty file, makes `src` a package

---

## Verification Checklist

### Storage + Graph
- [ ] `pip install duckdb` succeeds
- [ ] `python -c "from src.storage import Storage; s = Storage(); print(s.conn.execute('SHOW TABLES').fetchall())"` — 6 tables (sessions, tasks, task_transitions, tool_calls, entities, edges)
- [ ] Entity extraction: `extract_entities("reading /tmp/foo.py")` returns `[('file_path', '/tmp/foo.py')]`
- [ ] Graph recording: after a tool call, entities and edges appear in DuckDB
- [ ] Cycle detection: inserting duplicate tool_call edges triggers `detect_cycles() == True`

### Commands + Caching
- [ ] `RunBashCommand({"command": "echo hi"}).execute()` returns `CommandResult(output="hi\n", success=True, ...)`
- [ ] Tool call cache: `read_file` same path twice, second is cache hit (elapsed ~0)
- [ ] Non-cacheable tools (run_bash, run_python, write_file) always execute fresh

### Relevance
- [ ] TF-IDF: index 5 documents, query scores the most relevant one highest
- [ ] `rank_results_for_query("find python files", results)` ranks grep results above ls results
- [ ] Relevance-aware eviction: keeps query-relevant tool results, compresses others

### Context Compression
- [ ] Smart truncation: file read with 100 lines -> first 10 + last 10 + omission marker
- [ ] Sliding window: 20 messages -> compressed to window of 8 + summary
- [ ] Progressive system prompt: turn 0 full, turn 1+ compact (verify token reduction)
- [ ] Checkpoint: 6 messages compressed to 1 progress summary

### State Machine
- [ ] State machine with mock LLM: PENDING -> EXECUTING -> TOOL_CALLING -> WAITING_RESULT -> EVALUATING -> COMPLETED
- [ ] Loop detection: mock LLM repeating same tool call -> FAILED("loop detected")
- [ ] Context compression applied at EVALUATING -> EXECUTING transition

### Startup GC
- [ ] Create a task in EXECUTING state, close session, restart — task is now FAILED with `error="session ended"`
- [ ] Completed tasks are untouched by GC
- [ ] Tool call results from GC'd tasks still queryable via TF-IDF

### End-to-End
- [ ] Simple query through REPL (single state machine path)
- [ ] Complex query (5+ lines) triggers MapReduce with TF-IDF reduce
- [ ] After session: `duckdb ~/.local/share/fox/history.duckdb "SELECT * FROM tool_calls LIMIT 5"` shows records
- [ ] Entities populated: `SELECT * FROM entities LIMIT 10` shows extracted file paths, functions
- [ ] Graph edges: `SELECT * FROM edges LIMIT 10` shows tool_call -> entity relations
- [ ] Restart REPL, previous session data still queryable
- [ ] Cross-session: query finds relevant tool results from a prior session

---

## Epic 10: Small-Model Cleverness — Plan-First Loop

**Motivation**: Small models fail on multi-step tasks because they can't hold a coherent plan in one forward pass. Cleverness must live in the loop, not in a bigger model. Spend initial tokens deriving intent + granular plan, then execute atomic steps. Route everything through the same (small) model.

**Execution guidance for the implementer**:
- Do NOT implement multiple stories at once. One story per PR.
- Each story lists: goal, files to touch, exact edits, tests to write, definition of done.
- Before starting a story, read the listed files in full. Do not skim.
- After each story: run `tests/run_tests.sh`, confirm no prior tests regress.
- Definition of Done is the gate — if any DoD item is unchecked, the story is not done.

---

### Story 10.0 — Dependency inventory (prerequisite, no code change)

**As a** implementer
**I want** to confirm the starting state of the files this Epic touches
**So that** I do not build on assumptions that are already invalid.

**Goal**: read and note the line numbers / current content of these anchors before editing. Paste into the PR description.

**Files to read (no edits)**:
- `src/mapreduce.py` — note line numbers of: `save_user_input`, `should_decompose`, `execute`, `_run_single`, `_run_mapreduce`, `_map_phase`, `_build_subtask_messages`, `_reduce_phase`.
- `src/states.py` — note `TaskStateMachine.run`, the playbook-injection block (currently near line 113–129).
- `src/storage.py` — note `find_similar_chains`, `record_task_chain`, `detect_cycles`.
- `src/validator.py` — note `extract_intent`, `validate`, `_check_semantic`.
- `src/commands.py` — note `_COMMAND_MAP` content (list all tool names).

**Definition of Done**:
- [ ] A short note (`docs/epic10_inventory.md`) records current line numbers, the exact planner prompt string, the exact subtask prompt string, and the list of tool names from `_COMMAND_MAP`.
- [ ] No code files modified.

---

### Story 10.1 — Granularity fix: drop "combine related steps"

**As a** user with a small model
**I want** the planner to produce 3–8 atomic subtasks instead of ≤4 combined steps
**So that** each subtask fits in one forward pass and succeeds.

**Files to touch**:
- `src/mapreduce.py` — `_map_phase()` (planner prompt).
- `src/mapreduce.py` — `_run_mapreduce()` (subtask SM construction).

**Edits**:
1. In `_map_phase()`, replace the planner system prompt with:
   ```
   You are a task planner for a small-model agent. Output ONLY a numbered list of 3–8 ATOMIC tasks. No prose.

   An ATOMIC task:
   - Uses exactly one tool (run_bash, run_python, read_file, write_file, grep_search, list_files).
   - Names the exact file or value it reads.
   - Names the exact file or value it produces.
   - Cannot be split further without losing meaning.

   Rules:
   - Minimum 3 tasks, target 5–6, maximum 8. Never combine steps.
   - First task reads/parses input. Last task prints or writes the final answer.
   - One verb per task. If a task contains "and", split it.
   - Output ONLY the numbered list, nothing else.
   ```
2. In `_run_mapreduce()`, change `TaskStateMachine(task_id=sub_id, description=desc, max_turns=5)` to `max_turns=2`.
3. In `_build_subtask_messages()`, append to `user_content`:
   `\n- Call exactly ONE tool. Print the result. Then stop.`

**Tests to add** (`tests/test_unit.py`):
- `test_planner_prompt_rejects_combine`: assert the string `"Combine"` is NOT in the `_map_phase` system prompt; assert `"ATOMIC"` IS in it.
- `test_subtask_max_turns_is_two`: instantiate `MapReduceOrchestrator`, call `_run_mapreduce` with a stub llm_fn returning a 3-step plan; assert the subtask SM is created with `max_turns=2` (add a spy or inspect via mock).

**Definition of Done**:
- [ ] New planner prompt present verbatim in `_map_phase`.
- [ ] `max_turns=2` in subtask construction.
- [ ] "Call exactly ONE tool" appears in subtask user_content.
- [ ] Both new unit tests pass.
- [ ] `tests/run_tests.sh` passes end-to-end.
- [ ] Manual smoke: run `python3 agent.py`, paste a 2-step task ("read employees.csv and count rows"), observe 3+ atomic subtasks in the MAP output.

---

### Story 10.2 — Structured subtask output: `RESULT:` contract

**As a** downstream step
**I want** each subtask's output to end with a parseable `RESULT:` line
**So that** I can feed it into the next step without asking the model to summarize.

**Files to touch**:
- `src/mapreduce.py` — `_build_subtask_messages()`, `_run_mapreduce()`.

**Edits**:
1. In `_build_subtask_messages()`, append to `user_content`:
   ```
   OUTPUT FORMAT:
   Your final message MUST end with exactly one line:
     RESULT: <value>
   Where <value> is the direct answer to this step (a number, a filename, a short string, or "done").
   Do not add text after the RESULT line.
   ```
2. Add a helper in `mapreduce.py`:
   ```python
   _RESULT_RE = re.compile(r'^RESULT:\s*(.+?)\s*$', re.MULTILINE)

   def _extract_result(text: str) -> str:
       matches = _RESULT_RE.findall(text or "")
       return matches[-1].strip() if matches else (text or "").strip().splitlines()[-1][:200]
   ```
3. In `_run_mapreduce()` where `subtask_results.append(...)` is built, change to include both raw result and extracted RESULT:
   ```python
   extracted = _extract_result(result)
   subtask_results.append(f"Subtask {i+1} ({desc}):\nRESULT: {extracted}\n(full output below)\n{result[:400]}")
   ```
4. When writing `prev_results_file`, write ONLY the `RESULT:` values, one per subtask, so the next subtask sees clean inputs.

**Tests to add**:
- `test_extract_result_happy`: `_extract_result("blah\nRESULT: 42\n")` returns `"42"`.
- `test_extract_result_multiple`: with two RESULT lines, returns the last.
- `test_extract_result_missing`: falls back to the last non-empty line.
- `test_prev_results_file_contains_only_results`: run a stubbed mapreduce with 2 subtasks returning `"chatter\nRESULT: foo"` and `"RESULT: bar"`; assert `previous_results.txt` contains exactly `foo` and `bar`, no chatter.

**Definition of Done**:
- [ ] `OUTPUT FORMAT:` block present in subtask user_content.
- [ ] `_extract_result` function exists with a compiled regex.
- [ ] `previous_results.txt` contains only extracted RESULT values (no raw output, no descriptions).
- [ ] All 4 new tests pass.
- [ ] Manual smoke: run a 3-step MapReduce task, confirm terminal shows `RESULT: ...` at end of each subtask, confirm final answer references those values correctly.

---

### Story 10.3 — Few-shot planner from `task_chains`

**As a** planner
**I want** to see one worked example from a past successful task
**So that** small models imitate a proven structure instead of inventing one.

**Files to touch**:
- `src/mapreduce.py` — `_map_phase()`.
- `src/storage.py` — reuse existing `find_similar_chains` (no edit).

**Edits**:
1. At the top of `_map_phase`, before the LLM call:
   ```python
   example_block = ""
   try:
       chains = self.storage.find_similar_chains(user_input, limit=1)
       if chains and chains[0].get("score", 0) > 0.15:
           chain = chains[0]
           example_lines = [f"Task: {chain['description'][:100]}", "Plan:"]
           for j, step in enumerate(chain["steps"][:6], 1):
               arg_preview = next(iter(step["args"].values()), "")[:60] if step["args"] else ""
               example_lines.append(f"  {j}. {step['tool']}({arg_preview})")
           example_block = "EXAMPLE (past successful task):\n" + "\n".join(example_lines) + "\n\nNOW PLAN FOR THE NEW TASK:\n"
   except Exception as e:
       print(f"  \033[33m⚠ playbook lookup failed: {e}\033[0m")
       example_block = ""
   ```
   Note: do NOT use bare `except: pass`. Print the exception so we can see when it misfires.
2. Prepend `example_block` to the user message:
   ```python
   {"role": "user", "content": example_block + user_input}
   ```

**Tests to add**:
- `test_few_shot_injected_when_chain_found`: mock `storage.find_similar_chains` to return one chain with score 0.5; call `_map_phase`; assert the prompt passed to `llm_fn` contains `"EXAMPLE (past successful task):"`.
- `test_few_shot_skipped_on_low_score`: mock to return score 0.1; assert `"EXAMPLE"` is NOT in the prompt.
- `test_few_shot_skipped_on_empty`: mock to return `[]`; assert no `"EXAMPLE"` in prompt; assert no crash.

**Definition of Done**:
- [ ] Few-shot block injected only when `score > 0.15`.
- [ ] Exception path prints a warning (does not silently swallow).
- [ ] 3 new tests pass.
- [ ] Manual smoke: after running any task once successfully, run a similar task; confirm `EXAMPLE` appears in the planner call (add temporary `print(plan_messages[-1]["content"][:300])` if needed during verification, remove before commit).

---

### Story 10.4 — CoT planner with self-critique

**As a** planner
**I want** to reason about the task before emitting the plan, then critique my own plan
**So that** the plan is structurally sound before execution begins.

**Files to touch**:
- `src/mapreduce.py` — `_map_phase()`.

**Edits**:
1. Rename the existing method to `_map_phase_legacy` (keep for rollback).
2. Add a new `_map_phase` that does two passes:

   **Pass 1 (draft)** — replace the planner prompt with:
   ```
   You are a planner for a small-model agent. Output exactly two sections.

   REASONING:
   <3-5 short lines. Identify: goal, inputs, intermediate values, outputs, the tool for each step.>

   PLAN:
   1. <atomic step — one tool, exact input, exact output>
   2. ...
   (3–8 steps. Each step: one tool from {run_bash, run_python, read_file, write_file, grep_search, list_files}. No "and". No prose.)
   ```

   **Pass 2 (critique)** — feed the draft plan back:
   ```
   Review this plan. For each step, check:
     (a) Does it name exactly one tool from {run_bash, run_python, read_file, write_file, grep_search, list_files}?
     (b) Does it name the exact file or value it reads?
     (c) Does it name the exact file or value it produces?

   If any step fails (a), (b), or (c) — rewrite it.
   If two steps could be split — split them.

   Output ONLY the corrected PLAN: section. No commentary.
   ```

3. Parse the PLAN section after pass 2:
   ```python
   plan_match = re.search(r'PLAN:\s*\n(.*)', critiqued_text, re.DOTALL)
   plan_body = plan_match.group(1) if plan_match else critiqued_text
   return re.findall(r'^\s*\d+\.\s*(.+)$', plan_body, re.MULTILINE)
   ```

4. Keep the `example_block` (from 10.3) prepended to Pass 1's user message.

**Tests to add**:
- `test_cot_planner_two_llm_calls`: mock llm_fn with a call counter; assert `_map_phase` makes exactly 2 calls.
- `test_cot_planner_parses_plan_section`: mock returns `"REASONING: ...\nPLAN:\n1. read_file employees.csv\n2. run_python count rows"`; assert returned list has 2 items.
- `test_cot_planner_handles_missing_plan_header`: mock returns a numbered list with no `PLAN:` header; assert it still parses.

**Definition of Done**:
- [ ] Two-pass planner works end-to-end with a live model on a 3-step task.
- [ ] `_map_phase_legacy` preserved for rollback.
- [ ] 3 new tests pass.
- [ ] Manual smoke: run a complex task; terminal shows both "planner pass 1" and "planner pass 2" debug lines (add temporary prints if needed).

---

### Story 10.5 — Fold intent extraction into the planner

**As a** MapReduce orchestrator
**I want** intent and plan derived in one LLM call
**So that** intent is consistent with the plan and we save a round-trip.

**Files to touch**:
- `src/mapreduce.py` — `execute()`, `_map_phase()`.
- `src/validator.py` — keep `Intent`/`validate`; do NOT remove `extract_intent` (keep as fallback).

**Edits**:
1. Extend Pass 1 planner prompt (on top of 10.4) to include an INTENT section:
   ```
   Output exactly three sections in this order:

   INTENT:
   {"summary": "<one line>", "criteria": [<zero or more criterion objects, see schema below>]}

   REASONING:
   ...

   PLAN:
   ...
   ```
   Append the criterion schema inline (same as in `validator.py:_EXTRACT_SYSTEM`).

2. Add `_parse_intent_from_plan(text: str) -> Optional[Intent]` in `mapreduce.py`:
   - Regex-extract the JSON block after `INTENT:`.
   - `json.loads`, return `Intent.from_dict(...)`.
   - On parse failure return `None`.

3. In `execute()`, replace the standalone `extract_intent(...)` call with:
   ```python
   intent = None
   if self.should_decompose(user_input):
       # intent comes from the planner's INTENT section
       pass  # will be populated inside _run_mapreduce via _map_phase
   if intent is None:
       intent = extract_intent(self.llm_fn, user_input)  # fallback for simple path
   ```
4. `_map_phase` returns `(intent, subtasks)` tuple. Orchestrator stashes intent for the validate step.

**Tests to add**:
- `test_parse_intent_from_plan_happy`: a canned response with valid INTENT JSON → returns an `Intent`.
- `test_parse_intent_from_plan_malformed`: invalid JSON → returns `None` (no crash).
- `test_execute_skips_separate_intent_call_on_mapreduce_path`: mock `extract_intent` with a counter; run mapreduce path; assert counter == 0.
- `test_execute_uses_fallback_intent_on_simple_path`: run simple path; assert counter == 1.

**Definition of Done**:
- [ ] Planner emits INTENT + REASONING + PLAN sections.
- [ ] `_parse_intent_from_plan` handles malformed JSON gracefully.
- [ ] MapReduce path does not call the separate `extract_intent` (saves 1 LLM call).
- [ ] Simple path still calls `extract_intent` as fallback.
- [ ] 4 new tests pass.
- [ ] Manual smoke: run a "create a pptx" task, observe only ONE intent/plan LLM call (not two).

---

### Story 10.6 — Pre-flight plan validation (structural)

**As a** orchestrator
**I want** to reject plans whose steps are prose (no tool mentioned)
**So that** we don't enter execution with a broken plan.

**Files to touch**:
- `src/mapreduce.py` — after `_map_phase`, before subtask execution.

**Edits**:
1. Add module-level constant:
   ```python
   _TOOL_NAMES = {"run_bash", "run_python", "read_file", "write_file", "grep_search", "list_files"}
   ```
2. Add helper:
   ```python
   def _validate_plan_structural(steps: list[str]) -> tuple[bool, list[str]]:
       """Every step must mention at least one tool name. Returns (ok, failure_reasons)."""
       failures = []
       for i, step in enumerate(steps, 1):
           lowered = step.lower()
           if not any(tn in lowered for tn in _TOOL_NAMES):
               failures.append(f"step {i} mentions no tool: {step[:80]}")
       return (len(failures) == 0, failures)
   ```
3. In `_run_mapreduce` right after `_map_phase`:
   ```python
   ok, reasons = _validate_plan_structural(subtask_descriptions)
   if not ok:
       print(f"  \033[33m⚠ plan invalid: {'; '.join(reasons)} — re-planning once\033[0m")
       subtask_descriptions = self._map_phase(user_input + "\n\nPrevious plan was invalid: each step MUST mention one tool name.")[1]
       ok2, _ = _validate_plan_structural(subtask_descriptions)
       if not ok2:
           return self._run_single(user_input, messages, data_file)  # fallback
   ```

**Tests to add**:
- `test_validate_plan_all_tools`: `["read_file foo", "run_python bar"]` → `(True, [])`.
- `test_validate_plan_missing_tool`: `["read_file foo", "analyze the data"]` → `(False, [...])`.
- `test_replan_triggered_on_invalid_plan`: mock llm_fn to return a prose plan first, then a valid plan; assert the valid plan is what executes.
- `test_fallback_to_single_on_double_failure`: mock llm_fn to return prose both times; assert `_run_single` is called.

**Definition of Done**:
- [ ] `_validate_plan_structural` exists and is called exactly once per planning round.
- [ ] One re-plan attempt on failure; then fallback to `_run_single`.
- [ ] 4 new tests pass.

---

### Story 10.7 — Plan persisted as `plan.md`

**As a** subtask
**I want** to see my place in the overall plan
**So that** I make decisions that fit the plan's intent.

**Files to touch**:
- `src/mapreduce.py` — `_run_mapreduce`, `_build_subtask_messages`.

**Edits**:
1. After `_map_phase` returns, write:
   ```python
   plan_path = os.path.join(self.work_dir, "plan.md")
   with open(plan_path, "w") as f:
       f.write(f"# Plan for: {user_input[:200]}\n\n")
       for i, step in enumerate(subtask_descriptions, 1):
           f.write(f"{i}. {step}\n")
   ```
2. Change `_build_subtask_messages` signature to accept `step_index`, `total_steps`, `plan_path`. Prepend to `user_content`:
   ```
   You are step {step_index} of {total_steps}. Full plan is at {plan_path}.
   ```
3. Pass the new args from the execute loop.

**Tests to add**:
- `test_plan_md_written`: after `_run_mapreduce`, assert `plan.md` exists in `work_dir` and contains all step descriptions.
- `test_subtask_messages_reference_plan`: inspect messages for step 2 of 4; assert content contains `"step 2 of 4"` and the plan path.

**Definition of Done**:
- [ ] `plan.md` written at MAP phase completion.
- [ ] Every subtask user message begins with `"You are step N of M. Full plan is at {path}."`.
- [ ] 2 new tests pass.

---

### Story 10.8 — Per-step structural verification

**As a** orchestrator
**I want** to check each subtask's output has a `RESULT:` line and the expected file side-effects before moving on
**So that** failures are caught at the step, not at the end.

**Files to touch**:
- `src/mapreduce.py` — after each subtask in `_run_mapreduce`.

**Edits**:
1. Add helper:
   ```python
   def _verify_step(self, desc: str, result: str) -> tuple[bool, str]:
       """Structural check only — no LLM judge. Returns (ok, reason)."""
       if not _RESULT_RE.search(result or ""):
           return False, "output missing 'RESULT:' line"
       # If step text mentions 'write' or 'create' + a filename, check the file exists.
       if re.search(r'\b(write|create|save|generate)\b', desc, re.I):
           paths = re.findall(r'([\w./-]+\.(?:py|txt|csv|md|json|html|pptx|xlsx|pdf|png))', desc)
           for p in paths:
               candidate = p if os.path.isabs(p) else os.path.join(self.work_dir, p)
               if not os.path.exists(candidate) and not os.path.exists(os.path.join(os.getcwd(), p)):
                   return False, f"expected file {p} not created"
       return True, ""
   ```
2. After each subtask result is collected, call `_verify_step`. On failure, retry the subtask ONCE with the failure reason injected into the description:
   ```
   Previous attempt failed: {reason}. Fix exactly that problem and retry.
   ```

**Tests to add**:
- `test_verify_step_missing_result`: result without `RESULT:` → `(False, ...)`.
- `test_verify_step_missing_file`: step says "write foo.txt", file not present → `(False, ...)`.
- `test_verify_step_pass`: valid result + file exists → `(True, "")`.
- `test_step_retry_on_verify_failure`: mock subtask that fails first, passes second; assert final result is the passing one.

**Definition of Done**:
- [ ] `_verify_step` is structural only (no LLM judge).
- [ ] One retry per subtask on failure.
- [ ] 4 new tests pass.

---

### Story 10.9 — Progressive hint escalation on subtask retry

**As a** retried subtask
**I want** progressively more structure in my retry prompt
**So that** I fail less often on the second or third attempt.

**Files to touch**:
- `src/states.py` — `TaskStateMachine.run()`.
- `src/mapreduce.py` — retry path in `_run_mapreduce`.

**Edits**:
1. Add `retry_level: int = 0` field to `TaskStateMachine`.
2. In the retry path (from 10.8), pass `retry_level` = 1, 2, 3.
3. In `TaskStateMachine.run()`, before the first LLM call, inject a hint based on `retry_level`:
   - **Level 1**: no extra hint (same prompt).
   - **Level 2**: append one concrete example — `"Example tool call: run_python({\"script\": \"with open('x.csv') as f: print(len(f.readlines())); print('RESULT: done')\"})"`.
   - **Level 3**: append a tool call skeleton — `"Use this exact structure, filling in only the arguments: {\"name\": \"<tool>\", \"arguments\": {...}}. Respond with the tool call, nothing else."`.

**Tests to add**:
- `test_retry_level_1_no_hint`: run SM with `retry_level=1`; assert no hint string in messages.
- `test_retry_level_2_has_example`: assert `"Example tool call:"` in messages.
- `test_retry_level_3_has_skeleton`: assert skeleton string in messages.

**Definition of Done**:
- [ ] `retry_level` field on `TaskStateMachine`.
- [ ] Three hint tiers present and selectable.
- [ ] 3 new tests pass.

---

### Story 10.10 — Invert the decomposition gate

**As a** user
**I want** the orchestrator to plan first and decide whether to decompose based on the plan length
**So that** simple tasks skip planning but everything else gets the benefit.

**Files to touch**:
- `src/mapreduce.py` — `execute`, `should_decompose`.

**Edits**:
1. Remove the line-count heuristic; keep a fast-path only for one-liners:
   ```python
   def should_plan(self, user_input: str) -> bool:
       return len(user_input.strip()) > 30  # skip only trivial queries
   ```
2. In `execute`, if `should_plan(user_input)` is False → `_run_single`. Else → call `_map_phase`. If the plan has exactly 1 step → `_run_single`. Else → `_run_mapreduce`.
3. Remove `should_decompose` (mark deprecated, delete call sites).

**Tests to add**:
- `test_trivial_query_skips_planner`: `"what is 2+2"` → `_run_single`, zero planner calls.
- `test_one_step_plan_runs_single`: mock planner returning 1 step → `_run_single` invoked, no mapreduce.
- `test_multi_step_plan_runs_mapreduce`: mock planner returning 4 steps → mapreduce invoked.

**Definition of Done**:
- [ ] `should_decompose` removed or renamed `should_plan`.
- [ ] 3 new tests pass.
- [ ] Manual smoke: 1-liner question (`"list files here"`) runs through `_run_single`; 2-step request (`"count rows in employees.csv and print each column name"`) runs through mapreduce.

---

### Story 10.11 — Recursive decompose-on-demand (advanced, land LAST)

**As a** orchestrator
**I want** to re-plan a failing subtask into smaller pieces instead of giving up
**So that** one stuck step doesn't abort the whole task.

**Files to touch**:
- `src/mapreduce.py` — retry path.

**Edits**:
1. Add `_recurse_split(desc: str, failure_reason: str, depth: int) -> list[str]`:
   - If `depth >= 2`, return `[]` (abort).
   - Call `_map_phase` with `desc + "\n\nPrior attempt failed: " + failure_reason + "\nSplit this into 2–3 smaller atomic steps."`.
   - Validate with `_validate_plan_structural`; return the steps or `[]`.
2. In the subtask retry path, before marking FAILED: call `_recurse_split`. If it returns steps, execute them as nested subtasks (share parent_id, increment depth).
3. Cap total recursion depth at 2 (tracked via an explicit `depth` parameter passed through the call chain).

**Tests to add**:
- `test_recurse_split_depth_cap`: `_recurse_split(desc, reason, depth=2)` returns `[]`.
- `test_recurse_split_happy`: mock `_map_phase` returns 2 steps; `_recurse_split` returns them.
- `test_failed_subtask_triggers_recurse_and_succeeds`: end-to-end test with stubbed failing-then-passing inner steps.

**Definition of Done**:
- [ ] Recursion capped at depth 2.
- [ ] 3 new tests pass.
- [ ] Land only after 10.1–10.10 are green.

---

### Execution order (strict)

Land stories in this sequence. Each must be green before starting the next:

1. **10.0** — inventory only.
2. **10.1** — granularity fix.
3. **10.2** — `RESULT:` contract.
4. **10.3** — few-shot from playbook.
5. **10.4** — CoT + self-critique.
6. **10.5** — fold intent.
7. **10.6** — pre-flight plan validation.
8. **10.7** — `plan.md` artifact.
9. **10.8** — structural per-step verification.
10. **10.9** — hint escalation.
11. **10.10** — invert decomposition gate.
12. **10.11** — recursive decompose-on-demand (optional, advanced).

---

### Cross-cutting Definition of Done (applies to every story)

Every story is only Done when:
- [ ] Code changes compile and import cleanly (`python3 -c "import src.mapreduce, src.states"`).
- [ ] All pre-existing tests still pass (`tests/run_tests.sh` exit 0).
- [ ] New tests cover every new branch (happy path, error path, edge case).
- [ ] No `except Exception: pass` introduced — all new exception handlers log the exception.
- [ ] Manual smoke test documented in the PR description with terminal output showing the new behavior.
- [ ] Files touched are listed in the PR description with line-number anchors.

---

## Epic 11: SOTA additions — retrospective verification + test-time compute

**Motivation**: The 2024–2025 research consensus ("Huang et al., LLMs Cannot Self-Correct Reasoning Yet", ICLR 2024; "Self-Refine", Madaan et al.; Reflexion, Shinn et al.) says: small models fail at *retrospective semantic self-judgment* but succeed at *structural self-critique* and *multi-sample voting*. Epic 11 adds the SOTA moves compatible with small models.

### Story 11.1 — Replace semantic judge with structural judge by default

**As a** developer
**I want** the `semantic` criterion type disabled by default for small models
**So that** we stop relying on unreliable self-judgment.

**Files to touch**: `src/validator.py` (`_check_semantic`), `src/mapreduce.py` (`execute`).

**Edits**:
1. Add env flag: `FOX_SEMANTIC_JUDGE = os.environ.get("FOX_SEMANTIC_JUDGE", "0") == "1"`.
2. In `_check_semantic`, if flag is off → return `None` (skip check). Log once at startup: `"semantic judge: OFF (set FOX_SEMANTIC_JUDGE=1 to enable)"`.
3. Update `_EXTRACT_SYSTEM` prompt in `validator.py` to prefer `output_contains` + `file_exists` + `file_format` over `semantic`; mention `semantic` only as a last resort.

**Definition of Done**:
- [ ] `FOX_SEMANTIC_JUDGE=0` (default) → `_check_semantic` returns None without calling llm_fn.
- [ ] `FOX_SEMANTIC_JUDGE=1` → existing behavior.
- [ ] Startup log prints the current state.
- [ ] New test: `test_semantic_judge_disabled_by_default` — asserts `llm_fn` NOT called.
- [ ] New test: `test_semantic_judge_enabled_when_flagged` — asserts `llm_fn` called with `FOX_SEMANTIC_JUDGE=1`.

---

### Story 11.2 — Plan diversity + structural pick (test-time compute)

**As a** orchestrator
**I want** to generate 3 candidate plans and pick the best by structural criteria
**So that** we exploit cheap test-time compute instead of hoping the first plan is good.

**Files to touch**: `src/mapreduce.py` — `_map_phase`.

**Edits**:
1. After Pass 1 (CoT), run Pass 1 three times with different `temperature` values (0.2, 0.6, 1.0). If the llm_fn signature doesn't support temperature, add a passthrough `options` dict and plumb it through `src/ollama.py` → `_chat_ollama` payload (`"options": {"temperature": ...}`).
2. Score each candidate plan structurally:
   ```python
   def _score_plan(steps: list[str]) -> float:
       if not steps: return 0.0
       score = 0.0
       for s in steps:
           if any(tn in s.lower() for tn in _TOOL_NAMES): score += 1.0
           if "and" in s.lower().split(): score -= 0.3
           if len(s) > 20: score += 0.2
       return score / len(steps)
   ```
3. Pick the highest-scored plan; pass to Pass 2 (critique).

**Tests to add**:
- `test_score_plan_rewards_tool_mentions`.
- `test_score_plan_penalizes_and_conjunction`.
- `test_diversity_picks_best_of_three`: mock llm_fn to return 3 plans with scores `0.5, 0.8, 0.3`; assert the 0.8 plan is passed to the critique.

**Definition of Done**:
- [ ] 3 plans generated at temperatures `{0.2, 0.6, 1.0}`.
- [ ] `_score_plan` has ≥3 unit tests covering its rubric.
- [ ] Best plan by score is sent to critique pass.
- [ ] Smoke: terminal prints `"plan candidates: scores=[0.5, 0.8, 0.3] picked #2"`.
- [ ] Guard for local-only users: env flag `FOX_PLAN_DIVERSITY` default `"1"`; set to `"0"` to disable and save LLM calls.

---

### Story 11.3 — Environment-grounded verifier (files + exit codes)

**As a** validator
**I want** to check for real environment side-effects (file exists, script returns 0, output parses as JSON) instead of asking the LLM
**So that** "pass" means the world actually changed.

**Files to touch**: `src/validator.py` (add new criterion types), `src/mapreduce.py` (optional).

**Edits**:
1. Add criterion types: `command_exit_zero` (run a short check command, assert exit 0), `output_parses_json`, `output_parses_csv`, `line_count_matches`.
2. Implement each check with its own function (follow the shape of `_check_file_exists`).
3. Update `_EXTRACT_SYSTEM` to teach the planner to emit these where appropriate.

**Definition of Done**:
- [ ] 4 new criterion types implemented, each with unit tests for pass + fail paths.
- [ ] Intent extractor prompt knows about them.
- [ ] Manual smoke: "write a python script that outputs valid JSON" → intent extractor emits `output_parses_json` criterion → validator verifies by running the script and parsing.

---

### Cross-cutting DoD for Epic 11

Same as Epic 10, plus:
- [ ] No story in Epic 11 introduces an LLM self-judge on a full answer. Structural checks only.
- [ ] Each new env flag documented in CLAUDE.md under "Configuration".
