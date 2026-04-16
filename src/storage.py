"""
Fox storage layer — DuckDB-backed persistence for sessions, tasks, tool calls,
entity graph, and startup GC.
"""

import hashlib
import json
import os
import time
from typing import Optional

DB_PATH = os.path.expanduser("~/.local/share/fox/history.duckdb")


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        import duckdb
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            self.conn = duckdb.connect(db_path)
        except Exception:
            print(f"  \033[33m⚠ DB locked ({db_path}), using in-memory storage\033[0m")
            self.conn = duckdb.connect(":memory:")
        self._init_schema()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   VARCHAR PRIMARY KEY,
                started_at   DOUBLE,
                model        VARCHAR,
                cwd          VARCHAR
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id      VARCHAR PRIMARY KEY,
                session_id   VARCHAR,
                parent_id    VARCHAR,
                description  VARCHAR,
                state        VARCHAR,
                created_at   DOUBLE,
                completed_at DOUBLE,
                result       VARCHAR,
                error        VARCHAR
            )
        """)
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS trans_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_transitions (
                id           BIGINT PRIMARY KEY DEFAULT nextval('trans_seq'),
                task_id      VARCHAR,
                from_state   VARCHAR,
                to_state     VARCHAR,
                timestamp    DOUBLE,
                reason       VARCHAR
            )
        """)
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS tc_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_calls (
                id           BIGINT PRIMARY KEY DEFAULT nextval('tc_seq'),
                task_id      VARCHAR,
                session_id   VARCHAR,
                tool_name    VARCHAR,
                args_hash    VARCHAR,
                args_json    VARCHAR,
                output       VARCHAR,
                success      BOOLEAN,
                elapsed      DOUBLE,
                exit_code    INTEGER,
                timestamp    DOUBLE
            )
        """)
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS ent_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id    BIGINT PRIMARY KEY DEFAULT nextval('ent_seq'),
                entity_type  VARCHAR,
                value        VARCHAR,
                first_seen   DOUBLE,
                UNIQUE (entity_type, value)
            )
        """)
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS edge_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                id           BIGINT PRIMARY KEY DEFAULT nextval('edge_seq'),
                source_type  VARCHAR,
                source_id    VARCHAR,
                target_type  VARCHAR,
                target_id    VARCHAR,
                relation     VARCHAR,
                weight       DOUBLE DEFAULT 1.0,
                timestamp    DOUBLE
            )
        """)
        # Successful tool chains — indexed by task for fast playbook lookup
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_chains (
                task_id      VARCHAR PRIMARY KEY,
                description  VARCHAR,
                steps_json   VARCHAR,   -- JSON array of {tool, args, output_summary}
                completed_at DOUBLE
            )
        """)

    # ── Sessions ─────────────────────────────────────────────────────────────

    def create_session(self, session_id: str, model: str, cwd: str):
        self.conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            [session_id, time.time(), model, cwd],
        )

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(
        self,
        task_id: str,
        session_id: str,
        description: str,
        parent_id: Optional[str] = None,
    ):
        self.conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, 'PENDING', ?, NULL, NULL, NULL)",
            [task_id, session_id, parent_id, description, time.time()],
        )

    def update_task_state(
        self,
        task_id: str,
        state: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ):
        if state in ("COMPLETED", "FAILED"):
            self.conn.execute(
                "UPDATE tasks SET state=?, completed_at=?, result=?, error=? WHERE task_id=?",
                [state, time.time(), result, error, task_id],
            )
        else:
            self.conn.execute(
                "UPDATE tasks SET state=? WHERE task_id=?",
                [state, task_id],
            )

    # ── Transitions ───────────────────────────────────────────────────────────

    def log_transition(
        self, task_id: str, from_state: str, to_state: str, reason: str = ""
    ):
        self.conn.execute(
            "INSERT INTO task_transitions (task_id, from_state, to_state, timestamp, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            [task_id, from_state, to_state, time.time(), reason],
        )

    # ── Tool calls ────────────────────────────────────────────────────────────

    @staticmethod
    def _args_hash(args: dict) -> str:
        return hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()
        ).hexdigest()[:16]

    def record_tool_call(self, task_id: str, session_id: str, cmd) -> int:
        """Insert a tool call record. cmd must have .name, .args, .result set."""
        args_hash = self._args_hash(cmd.args)
        self.conn.execute(
            "INSERT INTO tool_calls "
            "(task_id, session_id, tool_name, args_hash, args_json, output, "
            "success, elapsed, exit_code, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                task_id,
                session_id,
                cmd.name,
                args_hash,
                json.dumps(cmd.args),
                cmd.result.output if cmd.result else None,
                cmd.result.success if cmd.result else None,
                cmd.result.elapsed if cmd.result else None,
                cmd.result.exit_code if cmd.result else None,
                time.time(),
            ],
        )
        row = self.conn.execute(
            "SELECT id FROM tool_calls WHERE task_id=? AND tool_name=? "
            "ORDER BY timestamp DESC LIMIT 1",
            [task_id, cmd.name],
        ).fetchone()
        return row[0] if row else -1

    # Only cache idempotent read-only tools
    _CACHEABLE = {"read_file", "grep_search", "list_files"}

    def lookup_cached_tool_call(
        self, tool_name: str, args: dict, max_age: float = 300.0
    ) -> Optional[str]:
        """Return cached output if a recent identical call exists, else None."""
        if tool_name not in self._CACHEABLE:
            return None
        args_hash = self._args_hash(args)
        cutoff = time.time() - max_age
        row = self.conn.execute(
            "SELECT output FROM tool_calls "
            "WHERE tool_name=? AND args_hash=? AND timestamp >= ? AND success=true "
            "ORDER BY timestamp DESC LIMIT 1",
            [tool_name, args_hash, cutoff],
        ).fetchone()
        return row[0] if row else None

    # ── Entity graph ──────────────────────────────────────────────────────────

    def record_entity(self, entity_type: str, value: str) -> int:
        """Upsert entity, return entity_id."""
        self.conn.execute(
            "INSERT INTO entities (entity_type, value, first_seen) VALUES (?, ?, ?) "
            "ON CONFLICT (entity_type, value) DO NOTHING",
            [entity_type, value, time.time()],
        )
        row = self.conn.execute(
            "SELECT entity_id FROM entities WHERE entity_type=? AND value=?",
            [entity_type, value],
        ).fetchone()
        return row[0] if row else -1

    def record_edge(
        self,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
    ):
        self.conn.execute(
            "INSERT INTO edges "
            "(source_type, source_id, target_type, target_id, relation, weight, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [source_type, source_id, target_type, target_id, relation, weight, time.time()],
        )

    def record_entities_from_tool_call(
        self, tool_call_id: int, args: dict, output: str
    ):
        """Extract entities from args+output, store them, create tool_call->entity edges."""
        # Import here to avoid circular import at module load time
        try:
            from src.relevance import extract_entities
        except ImportError:
            return  # relevance not yet available (e.g. during isolated unit tests)

        texts = [output]
        # Add args values as text too
        for v in args.values():
            if isinstance(v, str):
                texts.append(v)

        seen: set[tuple] = set()
        for text in texts:
            for etype, evalue in extract_entities(text):
                if (etype, evalue) in seen:
                    continue
                seen.add((etype, evalue))
                eid = self.record_entity(etype, evalue)
                self.record_edge(
                    "tool_call", str(tool_call_id),
                    "entity", str(eid),
                    "mentions",
                )

    def find_related(self, entity_value: str) -> list[dict]:
        """Find all tool calls that mention a given entity value."""
        rows = self.conn.execute(
            """
            SELECT tc.id, tc.tool_name, tc.args_json, tc.output, tc.timestamp
            FROM tool_calls tc
            JOIN edges e ON e.source_type='tool_call' AND e.source_id=CAST(tc.id AS VARCHAR)
            JOIN entities en ON en.entity_id=CAST(e.target_id AS BIGINT)
            WHERE en.value=?
            ORDER BY tc.timestamp DESC
            LIMIT 20
            """,
            [entity_value],
        ).fetchall()
        cols = ["id", "tool_name", "args_json", "output", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def detect_cycles(self, task_id: str, window: int = 6) -> bool:
        """True if the last `window` tool calls for this task repeat the same (name, args_hash)."""
        rows = self.conn.execute(
            "SELECT tool_name, args_hash FROM tool_calls "
            "WHERE task_id=? ORDER BY timestamp DESC LIMIT ?",
            [task_id, window],
        ).fetchall()
        if len(rows) < 2:
            return False
        # Fast-exit: if the last 2 calls are identical AND both failed, stop now.
        if len(rows) >= 2:
            r0, r1 = rows[0], rows[1]
            if (r0[0], r0[1]) == (r1[0], r1[1]):
                # Check if both failed (success=False in tool_calls)
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE task_id=? AND tool_name=? "
                    "AND args_hash=? AND success=false ORDER BY timestamp DESC LIMIT 2",
                    [task_id, r0[0], r0[1]],
                ).fetchone()
                if row and row[0] >= 2:
                    return True
        if len(rows) < 3:
            return False
        # General case: any (name, args_hash) pair appears 3+ times
        counts: dict = {}
        for row in rows:
            key = (row[0], row[1])
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= 3:
                return True
        return False

    # ── Task chains (playbook) ────────────────────────────────────────────────

    def record_task_chain(self, task_id: str):
        """
        Snapshot the successful tool call sequence for a completed task.
        Called by the state machine when a task reaches COMPLETED.
        Only records chains that have at least one successful tool call.
        """
        rows = self.conn.execute(
            "SELECT t.description, tc.tool_name, tc.args_json, tc.output, tc.success "
            "FROM tasks t JOIN tool_calls tc ON tc.task_id = t.task_id "
            "WHERE t.task_id = ? AND t.state = 'COMPLETED' "
            "ORDER BY tc.timestamp",
            [task_id],
        ).fetchall()

        if not rows:
            return

        description = rows[0][0]
        steps = []
        for _, tool_name, args_json, output, success in rows:
            if not success:
                continue
            # Keep output summary short — just enough for context matching
            output_summary = (output or "")[:200].replace("\n", " ")
            steps.append({
                "tool": tool_name,
                "args": json.loads(args_json) if args_json else {},
                "output_summary": output_summary,
            })

        if not steps:
            return

        self.conn.execute(
            "INSERT INTO task_chains (task_id, description, steps_json, completed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (task_id) DO UPDATE SET steps_json=excluded.steps_json",
            [task_id, description, json.dumps(steps), time.time()],
        )

    def find_similar_chains(self, query: str, limit: int = 3) -> list[dict]:
        """
        Return the `limit` most relevant completed task chains for a query.
        Uses TF-IDF over task descriptions. Returns list of dicts with
        keys: description, steps (list of {tool, args, output_summary}), score.
        """
        rows = self.conn.execute(
            "SELECT task_id, description, steps_json FROM task_chains ORDER BY completed_at DESC LIMIT 200"
        ).fetchall()

        if not rows:
            return []

        try:
            from src.relevance import TFIDFIndex
        except ImportError:
            # Fallback: return most recent
            rows = rows[:limit]
            return [
                {
                    "description": r[1],
                    "steps": json.loads(r[2]),
                    "score": 1.0,
                }
                for r in rows
            ]

        idx = TFIDFIndex()
        for task_id, description, _ in rows:
            idx.add_document(task_id, description)

        scores = dict(idx.score(query))
        ranked = sorted(rows, key=lambda r: scores.get(r[0], 0.0), reverse=True)

        results = []
        for task_id, description, steps_json in ranked[:limit]:
            score = scores.get(task_id, 0.0)
            if score == 0.0 and results:
                break  # stop at zero-score entries once we have some results
            results.append({
                "description": description,
                "steps": json.loads(steps_json),
                "score": round(score, 4),
            })
        return results

    # ── Startup GC ────────────────────────────────────────────────────────────

    def gc_incomplete_tasks(self) -> int:
        """Mark all non-terminal tasks FAILED with reason 'session ended'. Returns count."""
        rows = self.conn.execute(
            "SELECT task_id, state FROM tasks WHERE state NOT IN ('COMPLETED', 'FAILED')"
        ).fetchall()
        if not rows:
            return 0
        now = time.time()
        for task_id, from_state in rows:
            self.conn.execute(
                "UPDATE tasks SET state='FAILED', error='session ended', completed_at=? "
                "WHERE task_id=?",
                [now, task_id],
            )
            self.conn.execute(
                "INSERT INTO task_transitions (task_id, from_state, to_state, timestamp, reason) "
                "VALUES (?, ?, 'FAILED', ?, 'session ended')",
                [task_id, from_state, now],
            )
        return len(rows)

    # ── Query helpers ─────────────────────────────────────────────────────────

    def query(self, sql: str) -> list[dict]:
        result = self.conn.execute(sql)
        cols = [d[0] for d in result.description]
        return [dict(zip(cols, row)) for row in result.fetchall()]

    def get_task_history(self, task_id: str) -> list[dict]:
        return self.query(
            f"SELECT * FROM task_transitions WHERE task_id='{task_id}' ORDER BY timestamp"
        )

    def get_related_tool_calls(self, entity_value: str) -> list[dict]:
        return self.find_related(entity_value)

    def close(self):
        self.conn.close()
