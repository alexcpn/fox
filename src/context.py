"""
Fox context management — keeps message lists lean for small-context models.

Strategies applied (in order via compress_context):
  1. Progressive system prompt  — strip tool descriptions after turn 0
  2. Sliding window             — evict old turns, leave summary breadcrumb
  3. Tiered tool compression    — keep N most relevant tool results in full,
                                  one-line everything older
"""

import re
from typing import Optional


# ── Smart truncation ──────────────────────────────────────────────────────────

def smart_truncate(tool_name: str, result: str, max_chars: int = 500) -> str:
    """
    Content-aware truncation. Each tool type gets a strategy that preserves
    the most useful parts rather than blindly cutting at `max_chars`.
    """
    # Line-count-based truncation applied regardless of char count
    if tool_name == "read_file":
        lines = result.splitlines()
        if len(lines) <= 20:
            return result
        head = lines[:10]
        tail = lines[-10:]
        omitted = len(lines) - 20
        return "\n".join(head) + f"\n... ({omitted} lines omitted) ...\n" + "\n".join(tail)

    if tool_name == "grep_search":
        lines = [l for l in result.splitlines() if l.strip()]
        if len(lines) <= 5:
            return result
        shown = lines[:5]
        more = len(lines) - 5
        return "\n".join(shown) + f"\n... ({more} more matches)"

    if len(result) <= max_chars:
        return result

    if tool_name == "run_bash":
        # If successful and long, just show first line as confirmation
        if "[exit code:" not in result:
            first_line = result.splitlines()[0] if result.strip() else "(no output)"
            rest_len = len(result) - len(first_line)
            if rest_len > 100:
                return f"Success: {first_line[:200]} ... ({rest_len} more chars)"
        # Non-zero exit or short: keep stderr visible, truncate gently
        return result[:max_chars] + f"\n... (truncated, {len(result) - max_chars} chars omitted)"

    if tool_name == "run_python":
        # Model's own computation — keep more, up to 2KB
        limit = max(max_chars, 2000)
        if len(result) <= limit:
            return result
        return result[:limit] + f"\n... ({len(result) - limit} chars omitted)"

    if tool_name == "list_files":
        lines = result.splitlines()
        if len(lines) <= 20:
            return result
        shown = lines[:20]
        more = len(lines) - 20
        return "\n".join(shown) + f"\n... ({more} more entries)"

    # Default: head + tail
    half = max_chars // 2
    return result[:half] + f"\n... (truncated) ...\n" + result[-half:]


# ── One-line summary ──────────────────────────────────────────────────────────

def one_line_tool_summary(tool_name: str, result: str) -> str:
    """Collapse a tool result to a single bracketed line."""
    first = result.splitlines()[0].strip() if result.strip() else "(no output)"
    if len(first) > 80:
        first = first[:77] + "..."
    return f"[tool: {tool_name} -> {first}]"


# ── Tiered tool result compression ───────────────────────────────────────────

def compress_tool_results(
    messages: list[dict],
    keep_full: int = 2,
    query: Optional[str] = None,
) -> list[dict]:
    """
    Keep the `keep_full` most relevant tool results at full length,
    replace the rest with one-liners.

    If `query` is provided, relevance is determined by TF-IDF.
    Otherwise, falls back to keeping the most recent `keep_full`.
    """
    # Identify all tool-role message indices
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

    if len(tool_indices) <= keep_full:
        return messages  # nothing to compress

    # Determine which indices to keep in full
    if query:
        try:
            from src.relevance import select_relevant_tool_results
            tool_msgs = [messages[i] for i in tool_indices]
            rel_positions = select_relevant_tool_results(query, tool_msgs, keep=keep_full)
            keep_set = {tool_indices[p] for p in rel_positions}
        except Exception:
            # Fallback to most recent
            keep_set = set(tool_indices[-keep_full:])
    else:
        keep_set = set(tool_indices[-keep_full:])

    # Build compressed message list
    result: list[dict] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and i not in keep_set:
            # Determine tool name from preceding assistant message if possible
            tool_name = "tool"
            for j in range(i - 1, -1, -1):
                prev = messages[j]
                if prev.get("role") == "assistant":
                    tcs = prev.get("tool_calls", [])
                    if tcs:
                        tool_name = tcs[-1].get("function", {}).get("name", "tool")
                    break
            compressed: dict = {
                "role": "tool",
                "content": one_line_tool_summary(tool_name, msg.get("content", "")),
            }
            if msg.get("tool_call_id"):
                compressed["tool_call_id"] = msg["tool_call_id"]
            result.append(compressed)
        else:
            result.append(msg)

    return result


# ── Sliding window ────────────────────────────────────────────────────────────

def sliding_window(messages: list[dict], window_size: int = 8) -> list[dict]:
    """
    Keep the system prompt + last `window_size` non-system messages.
    Evicted turns are summarised into a single breadcrumb system message.
    """
    if not messages:
        return messages

    system = messages[0] if messages[0].get("role") == "system" else None
    body = messages[1:] if system else messages

    if len(body) <= window_size:
        return messages

    evicted = body[:-window_size]
    kept = list(body[-window_size:])

    # Never leave orphaned tool messages at the head of the kept window.
    # If a tool message's parent assistant (tool_calls) was evicted, the tool
    # message is now unlinked — OpenAI rejects these. Push them into evicted.
    while kept and kept[0].get("role") == "tool":
        evicted.append(kept.pop(0))

    if not kept:
        return messages  # nothing useful left; keep original

    # Build topic hints from evicted user messages
    user_hints = []
    for m in evicted:
        if m.get("role") == "user":
            text = m.get("content", "")[:60].replace("\n", " ")
            user_hints.append(text)

    hint_str = "; ".join(user_hints) if user_hints else "earlier tool work"
    breadcrumb = {
        "role": "system",
        "content": f"[Prior context: {len(evicted)} messages summarised. Topics: {hint_str}]",
    }

    result = []
    if system:
        result.append(system)
    result.append(breadcrumb)
    result.extend(kept)
    return result


# ── Checkpoint summarization ──────────────────────────────────────────────────

def checkpoint(messages: list[dict], start_idx: int) -> list[dict]:
    """
    Compress messages[start_idx:] into a single progress summary.
    Returns messages[:start_idx] + [summary_message].

    Called by the state machine when looping EVALUATING -> EXECUTING.
    """
    if start_idx >= len(messages):
        return messages

    block = messages[start_idx:]
    tools_called: list[str] = []
    key_findings: list[str] = []

    for msg in block:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "assistant":
            tcs = msg.get("tool_calls", [])
            for tc in tcs:
                name = tc.get("function", {}).get("name", "?")
                tools_called.append(name)

        elif role == "tool" and content:
            first_line = content.splitlines()[0].strip()[:80]
            key_findings.append(first_line)

    tools_str = ", ".join(tools_called) if tools_called else "none"
    findings_str = "; ".join(key_findings[:3]) if key_findings else "no output yet"

    summary = {
        "role": "system",
        "content": (
            f"[Progress: called {tools_str}. "
            f"Key findings: {findings_str}. "
            f"Continue toward the goal.]"
        ),
    }
    return messages[:start_idx] + [summary]


# ── Progressive system prompt ─────────────────────────────────────────────────

# Matches the TOOLS block in the system prompt (tool descriptions are verbose)
_TOOLS_BLOCK_RE = re.compile(
    r'(Available tools|## Tools|TOOLS:).*?(?=\n## |\nRULES:|\Z)',
    re.DOTALL | re.IGNORECASE,
)


def compact_system_prompt(full_prompt: str, turn: int) -> str:
    """
    Turn 0: full prompt.
    Turn >= 1: strip tool descriptions — model already has them in its context.
    """
    if turn == 0:
        return full_prompt
    # Strip tool description block if present
    compacted = _TOOLS_BLOCK_RE.sub("", full_prompt).strip()
    return compacted if compacted else full_prompt


# ── Main entry point ──────────────────────────────────────────────────────────

def compress_context(
    messages: list[dict],
    window_size: int = 8,
    keep_full_tools: int = 2,
    query: Optional[str] = None,
    turn: int = 0,
) -> list[dict]:
    """
    Full compression pipeline:
      1. Progressive system prompt (compact on turn >= 1)
      2. Tiered tool result compression (relevance-aware if query given)
      3. Sliding window

    This is the single entry point called by the state machine at EVALUATING.
    """
    if not messages:
        return messages

    result = list(messages)  # shallow copy — don't mutate caller's list

    # 1. Progressive system prompt
    if turn > 0 and result and result[0].get("role") == "system":
        result[0] = {
            "role": "system",
            "content": compact_system_prompt(result[0].get("content", ""), turn),
        }

    # 2. Tiered tool compression
    result = compress_tool_results(result, keep_full=keep_full_tools, query=query)

    # 3. Sliding window
    result = sliding_window(result, window_size=window_size)

    return result
