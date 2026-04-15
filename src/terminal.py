"""
Fox terminal I/O — raw-mode input with Alt+Enter for newlines and paste detection.
Copied verbatim from the original agent.py (lines 494-665).

Public interface: read_input() -> str
"""

import os
import select
import sys
import termios
import tty


def read_input() -> str:
    """
    Read user input from terminal.
    - Enter → submit
    - Alt+Enter → newline
    - Pasted multi-line text → auto-detected and captured
    """
    fd = sys.stdin.fileno()

    if not os.isatty(fd):
        return _read_piped_input()

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return _read_raw(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_piped_input() -> str:
    lines = []
    first = sys.stdin.readline()
    if not first:
        raise EOFError
    lines.append(first.rstrip("\n"))
    while select.select([sys.stdin], [], [], 0.05)[0]:
        line = sys.stdin.readline()
        if not line:
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines).strip()


def _read_raw(fd: int) -> str:
    """Read input in raw terminal mode with Alt+Enter for newlines."""
    lines = [""]
    cursor_line = 0
    prompt = "\033[1;34m❯ \033[0m"

    sys.stdout.write(f"\r\033[K{prompt}")
    sys.stdout.flush()

    while True:
        ch = os.read(fd, 1)
        if not ch:
            raise EOFError

        b = ch[0]

        # Enter (CR) — submit or paste continuation
        if b == 13:
            if select.select([fd], [], [], 0.02)[0]:
                lines.append("")
                cursor_line += 1
                sys.stdout.write(f"\r\n\033[K\033[90m· \033[0m")
                sys.stdout.flush()
                continue
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            text = "\n".join(lines).strip()
            line_count = len([l for l in lines if l.strip()])
            if line_count > 1:
                first_line = lines[0][:80]
                sys.stdout.write(f"\033[90m  ({line_count} lines: \"{first_line}...\")\033[0m\r\n")
                sys.stdout.flush()
            return text

        # LF in paste (some terminals send LF instead of CR)
        elif b == 10:
            if select.select([fd], [], [], 0.02)[0]:
                lines.append("")
                cursor_line += 1
                sys.stdout.write(f"\r\n\033[K\033[90m· \033[0m")
                sys.stdout.flush()
                continue
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            text = "\n".join(lines).strip()
            line_count = len([l for l in lines if l.strip()])
            if line_count > 1:
                first_line = lines[0][:80]
                sys.stdout.write(f"\033[90m  ({line_count} lines: \"{first_line}...\")\033[0m\r\n")
                sys.stdout.flush()
            return text

        # ESC — Alt+Enter or escape sequences
        elif b == 27:
            if select.select([fd], [], [], 0.05)[0]:
                next_ch = os.read(fd, 1)
                if next_ch and next_ch[0] == 13:
                    lines.append("")
                    cursor_line += 1
                    sys.stdout.write(f"\r\n\033[K\033[90m· \033[0m")
                    sys.stdout.flush()
                    continue
                elif next_ch and next_ch[0] == 91:
                    if select.select([fd], [], [], 0.05)[0]:
                        os.read(fd, 1)
                    continue
            continue

        # Ctrl+C
        elif b == 3:
            sys.stdout.write("^C\r\n")
            sys.stdout.flush()
            raise KeyboardInterrupt

        # Ctrl+D
        elif b == 4:
            if all(l == "" for l in lines):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                raise EOFError
            continue

        # Backspace
        elif b in (127, 8):
            if lines[cursor_line]:
                lines[cursor_line] = lines[cursor_line][:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            elif cursor_line > 0:
                lines.pop(cursor_line)
                cursor_line -= 1
                sys.stdout.write(f"\r\033[A\033[K\033[90m· \033[0m{lines[cursor_line]}")
                sys.stdout.flush()
            continue

        # Ctrl+U — clear line
        elif b == 21:
            lines[cursor_line] = ""
            p = prompt if cursor_line == 0 else "\033[90m· \033[0m"
            sys.stdout.write(f"\r\033[K{p}")
            sys.stdout.flush()
            continue

        # Printable ASCII
        elif 32 <= b < 127:
            lines[cursor_line] += chr(b)
            sys.stdout.write(chr(b))
            sys.stdout.flush()

        # UTF-8
        elif b >= 128:
            remaining = 0
            if b >> 5 == 0b110:
                remaining = 1
            elif b >> 4 == 0b1110:
                remaining = 2
            elif b >> 3 == 0b11110:
                remaining = 3
            buf = ch
            for _ in range(remaining):
                buf += os.read(fd, 1)
            char = buf.decode("utf-8", errors="replace")
            lines[cursor_line] += char
            sys.stdout.write(char)
            sys.stdout.flush()

        # Tab
        elif b == 9:
            lines[cursor_line] += "    "
            sys.stdout.write("    ")
            sys.stdout.flush()
