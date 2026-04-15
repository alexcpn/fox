"""
Fox Ollama interface — chat(), TOOLS list, and system prompt builder.
"""

import os
import time
import threading
import requests

OLLAMA_URL   = os.environ.get("OLLAMA_URL",       "http://localhost:11434")
MODEL        = os.environ.get("OLLAMA_MODEL",      "gemma4")
MAX_TURNS    = int(os.environ.get("MAX_AGENT_TURNS", "30"))
CONTEXT_WINDOW  = int(os.environ.get("CONTEXT_WINDOW",  "8"))
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "500"))


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
                "e.g. 'pip install python-pptx -q && python3 -c \"...\"' to create .pptx files, "
                "'pip install pillow -q && python3 script.py' for images, etc. "
                "NEVER say you cannot create files — use pip to install what you need."
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
            "description": "Write content to a file, creating it if needed.",
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
- Internet access via curl in run_bash. Never say you cannot fetch data.
- For data processing use run_python with stdlib only (csv, re, json, collections). No pandas/numpy.
- To use third-party libraries (e.g. python-pptx, pillow, openpyxl), install via run_bash: 'pip install <pkg> -q && python3 -c "..."'. NEVER say you cannot create .pptx, .xlsx, images, or other file formats.
- When user pastes data it is saved to {work_dir}/user_input.txt — read it with run_python.
- NEVER hardcode data values in scripts. ALWAYS read from the file and parse programmatically.
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
    i = 0
    while not stop_event.is_set():
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        print(f"\r  {frame} ", end="", flush=True)
        i += 1
        time.sleep(0.12)


def chat(messages: list[dict], use_tools: bool = True, think: bool = True) -> dict:
    payload: dict = {
        "model":   MODEL,
        "messages": messages,
        "stream":  False,
        "think":   think,
    }
    if use_tools:
        payload["tools"] = TOOLS

    stop = threading.Event()
    spinner = threading.Thread(target=_spin, args=(stop,), daemon=True)
    spinner.start()

    t0 = time.time()
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600)
    finally:
        stop.set()
        spinner.join()

    elapsed = time.time() - t0
    print(f"\r  \033[90m🦊 [{elapsed:.0f}s]\033[0m  ", end="", flush=True)
    resp.raise_for_status()
    return resp.json()["message"]
