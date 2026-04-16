"""
Fox chat interface — supports Ollama (local) and OpenAI backends.
"""

import json
import os
import time
import threading
import requests

OLLAMA_URL      = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
MODEL           = os.environ.get("OLLAMA_MODEL",       "gemma4")
BACKEND         = "ollama"   # "ollama" | "openai" — set by resolve_model()
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY",    "")
OPENAI_URL      = "https://api.openai.com/v1/chat/completions"
MAX_TURNS       = int(os.environ.get("MAX_AGENT_TURNS", "30"))
CONTEXT_WINDOW  = int(os.environ.get("CONTEXT_WINDOW",  "8"))
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "500"))


# ── Model resolution ──────────────────────────────────────────────────────────

def list_models() -> list[str]:
    """Return model names available in Ollama, or [] on failure."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


_OPENAI_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]


def _pick_from_list(prompt: str, items: list[str]) -> str:
    """Print a numbered list and return the chosen item."""
    for i, name in enumerate(items, 1):
        print(f"    {i}. {name}")
    print()
    while True:
        try:
            raw = input(f"  {prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  Using first: {items[0]}")
            return items[0]
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]
        elif raw in items:
            return raw
        print(f"  Enter a number 1–{len(items)} or a name.")


def resolve_model(preferred: str) -> str:
    """
    Choose backend and model at startup.
    - If OPENAI_API_KEY is set: ask user — OpenAI or local Ollama?
    - Ollama path: verify model exists, offer list if not found.
    - OpenAI path: show model list, user picks.
    Updates module globals BACKEND and MODEL; returns chosen model name.
    """
    global MODEL, BACKEND

    def base(name: str) -> str:
        return name.split(":")[0].lower()

    # ── Offer OpenAI if key is present ────────────────────────────────────────
    if OPENAI_API_KEY:
        print(f"\n  OpenAI API key found. Which backend?")
        print(f"    1. OpenAI  (fast, cloud)")
        print(f"    2. Ollama  (local, private)")
        print()
        try:
            choice = input("  Choose [1/2, default=1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            choice = "1"

        if choice != "2":
            BACKEND = "openai"
            print(f"\n  OpenAI models:")
            MODEL = _pick_from_list("Choose model (default=gpt-4o-mini)", _OPENAI_MODELS)
            print(f"  → OpenAI / {MODEL}\n")
            return MODEL

    # ── Ollama path ───────────────────────────────────────────────────────────
    BACKEND = "ollama"
    available = list_models()

    if not available:
        print(f"  \033[33m⚠ Could not reach Ollama at {OLLAMA_URL} — proceeding with '{preferred}'\033[0m")
        MODEL = preferred
        return MODEL

    exact = next((m for m in available if m == preferred), None)
    loose = next((m for m in available if base(m) == base(preferred)), None)
    chosen = exact or loose
    if chosen:
        MODEL = chosen
        return MODEL

    # Preferred not found — ask
    print(f"\n  \033[33m⚠ Model '{preferred}' not found in Ollama.\033[0m")
    print(f"  Available models:")
    MODEL = _pick_from_list("Choose a model (number or name)", available)
    print(f"  → Ollama / {MODEL}\n")
    return MODEL


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a bash command and return its stdout and stderr. "
                "Use this for: ls, find, grep, cat, head, tail, wc, du, git, curl, jq, awk, sed, etc. "
                "You have full internet access via curl. Commands run in the user's cwd. "
                "You can install Python packages with pip and run Python scripts with any library: "
                "e.g. 'pip install python-pptx -q && echo OK' to install (echo OK confirms success), "
                "then a separate run_bash call to run the script. "
                "NEVER say you cannot create files — use pip to install what you need. "
                "NOTE: 'pip install -q' produces no output on success — always append '&& echo OK' so you know it worked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run a Python 3 script and return its stdout. Use for data processing, "
                "parsing CSV/TSV/logs, calculations. stdlib only (csv, json, re, collections). "
                "Print your results to stdout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "Python script to execute."},
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Supports an optional line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string",  "description": "Path to the file"},
                    "start_line": {"type": "integer", "description": "First line (1-indexed, optional)"},
                    "end_line":   {"type": "integer", "description": "Last line inclusive (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file, creating it if needed. "
                "TEXT FILES ONLY (.py, .txt, .csv, .md, .json, .html, etc.). "
                "Do NOT use for binary formats (.pptx, .xlsx, .docx, .pdf, .png, etc.) — "
                "those require library-generated binary output; use run_bash with python-pptx/openpyxl instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a regex pattern in files recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern"},
                    "path":    {"type": "string", "description": "Directory or file (default: cwd)"},
                    "include": {"type": "string", "description": "Glob filter, e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":      {"type": "string",  "description": "Directory (default: cwd)"},
                    "recursive": {"type": "boolean", "description": "List recursively (default false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_examples",
            "description": (
                "Search past successful task executions for a given query. "
                "Returns similar completed tasks with their exact tool call sequences. "
                "Use this FIRST when asked to create files (.pptx, .xlsx, .py, etc.) or run "
                "unfamiliar workflows — it shows what tools and arguments worked before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Description of the task you want to accomplish"},
                    "limit": {"type": "integer", "description": "Max examples to return (default: 3)"},
                },
                "required": ["query"],
            },
        },
    },
]


# ── System prompt ─────────────────────────────────────────────────────────────

def _load_mcp_context() -> str:
    mcp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP.md")
    try:
        with open(mcp_path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def build_system_prompt(work_dir: str) -> str:
    return f"""\
You are Fox, a local assistant with tools. Use them. Do not guess.

Working directory: {os.getcwd()}
Scratch directory: {work_dir}

RULES:
- Use tools. Do not guess or describe — execute.
- FILE CREATION: If asked to CREATE, GENERATE, WRITE, or MAKE a file — call run_bash or write_file. Describing what you would write is rejected. Always produce the actual file.
- BINARY FILES (.pptx, .xlsx, .png, .pdf): write_file is TEXT ONLY. Use run_bash with heredoc pattern from Tool Reference. NEVER `python3 -c` for pptx. NEVER `python3 -m pptx`. NEVER `python` (use `python3`).
- When user says "this data" or "the above" — use it directly from context. Do not ask again.
- Pasted data is saved to {work_dir}/user_input.txt — read it with run_python.
- NEVER hardcode data values. Read from files, parse programmatically.
- NEVER ask the user for data that is already in the input.
- Print actual values. Never say "Match" without printing what you compared.

{_load_mcp_context()}"""


# ── Chat ──────────────────────────────────────────────────────────────────────

_SPINNER_FRAMES = [
    "🦊",
    "🦊·",
    "🦊··",
    "🦊···",
    "·🦊··",
    "··🦊·",
    "···🦊",
    "··🦊·",
    "·🦊··",
    "🦊···",
    "🦊··",
    "🦊·",
]


def _spin(stop_event: threading.Event) -> None:
    import sys as _sys
    if not _sys.stdout.isatty():
        stop_event.wait()
        return
    i = 0
    while not stop_event.is_set():
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        print(f"\r  {frame} ", end="", flush=True)
        i += 1
        time.sleep(0.12)


def _normalize_openai_response(msg: dict) -> dict:
    """Convert OpenAI chat response message to internal format.

    - Arguments JSON string → dict
    - Preserves tool call `id` for pairing with tool result messages
    - Keeps content as None when absent (tool-call-only responses)
    """
    raw_tcs = msg.get("tool_calls")
    result: dict = {"role": msg.get("role", "assistant")}

    content = msg.get("content")          # may be None for tool-call-only turns
    result["content"] = content or ""     # empty string for internal use

    if raw_tcs:
        normalized = []
        for tc in raw_tcs:
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            normalized.append({
                "id": tc.get("id", ""),
                "function": {"name": func.get("name", ""), "arguments": args},
            })
        result["tool_calls"] = normalized
    return result


def _prepare_messages_for_openai(messages: list[dict]) -> list[dict]:
    """Sanitize the message list before sending to OpenAI.

    OpenAI requires:
    - assistant tool_call messages: content must be null/absent (not "")
    - tool_calls entries: must have type="function" and arguments as JSON string
    - tool result messages: must have tool_call_id
    """
    out = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")

        if role == "assistant" and m.get("tool_calls"):
            # content must be absent/null, not empty string
            if not m.get("content"):
                m.pop("content", None)
            # Re-serialize tool_calls to OpenAI wire format
            fixed = []
            for tc in m["tool_calls"]:
                tc = dict(tc)
                func = dict(tc.get("function", {}))
                args = func.get("arguments", {})
                if isinstance(args, dict):
                    func["arguments"] = json.dumps(args)  # must be JSON string
                tc["function"] = func
                if "type" not in tc:
                    tc["type"] = "function"
                fixed.append(tc)
            m["tool_calls"] = fixed

        if role == "tool" and "tool_call_id" not in m:
            m["tool_call_id"] = "unknown"  # placeholder if lost via compression

        out.append(m)
    return out


def _chat_openai(messages: list[dict], use_tools: bool) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": MODEL,
        "messages": _prepare_messages_for_openai(messages),
    }
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    resp = requests.post(OPENAI_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return _normalize_openai_response(resp.json()["choices"][0]["message"])


def _chat_ollama(messages: list[dict], use_tools: bool, think: bool) -> dict:
    payload: dict = {
        "model":    MODEL,
        "messages": messages,
        "stream":   False,
        "think":    think,
    }
    if use_tools:
        payload["tools"] = TOOLS

    resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600)
    resp.raise_for_status()
    return resp.json()["message"]


def chat(messages: list[dict], use_tools: bool = True, think: bool = True) -> dict:
    stop = threading.Event()
    spinner = threading.Thread(target=_spin, args=(stop,), daemon=True)
    spinner.start()

    t0 = time.time()
    try:
        if BACKEND == "openai":
            result = _chat_openai(messages, use_tools)
        else:
            result = _chat_ollama(messages, use_tools, think)
    finally:
        stop.set()
        spinner.join()

    elapsed = time.time() - t0
    print(f"\r  \033[90m🦊 [{elapsed:.0f}s]\033[0m  ", end="", flush=True)
    return result
