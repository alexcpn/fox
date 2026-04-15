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
