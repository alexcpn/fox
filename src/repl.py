"""
Fox REPL — wires all modules together and runs the interactive loop.
"""

import os
import tempfile
import uuid

from src.storage import Storage
from src.commands import CommandRegistry
from src.mapreduce import MapReduceOrchestrator, save_user_input
from src.ollama import chat, build_system_prompt, MODEL, OLLAMA_URL
from src.terminal import read_input


_FOX_BANNER = r"(/\_/\)  🦊 Fox — A Clever and Cunning Agent Loop"


def main():
    work_dir = tempfile.mkdtemp(prefix="fox_work_")
    storage = Storage()

    # GC incomplete tasks from prior sessions
    gc_count = storage.gc_incomplete_tasks()
    if gc_count:
        print(f"  \033[90mGC: marked {gc_count} incomplete task(s) from prior sessions as FAILED\033[0m")

    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    storage.create_session(session_id, MODEL, os.getcwd())

    command_registry = CommandRegistry(work_dir, storage)
    messages = [{"role": "system", "content": build_system_prompt(work_dir)}]

    orchestrator = MapReduceOrchestrator(
        llm_fn=chat,
        command_registry=command_registry,
        storage=storage,
        session_id=session_id,
        work_dir=work_dir,
    )

    print(f"\033[33;1m{_FOX_BANNER}\033[0m")
    print(f"\033[1m  {MODEL} @ {OLLAMA_URL}\033[0m")
    print(f"   cwd:     {os.getcwd()}")
    print(f"   scratch: {work_dir}")
    print(f"   Enter → send  |  Alt+Enter → newline  |  Paste → auto-captured")
    print(f"   'quit' to exit  |  'cd <path>' to change dir  |  'clear' to reset context\n")

    while True:
        try:
            user_input = read_input()
        except (EOFError, KeyboardInterrupt):
            print("Bye!")
            break

        if not user_input:
            continue

        # Built-in commands
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        if user_input.lower() == "clear":
            messages = [{"role": "system", "content": build_system_prompt(work_dir)}]
            print("  Context cleared.")
            continue

        if user_input.startswith("cd "):
            path = os.path.expanduser(user_input[3:].strip())
            try:
                os.chdir(path)
                messages[0] = {"role": "system", "content": build_system_prompt(work_dir)}
                print(f"  → {os.getcwd()}")
            except Exception as e:
                print(f"  Error: {e}")
            continue

        # Save multi-line input to file
        data_file = save_user_input(user_input, work_dir)

        try:
            answer = orchestrator.execute(user_input, messages, data_file)
        except KeyboardInterrupt:
            print("\n  (interrupted)")
            continue
        except Exception as e:
            print(f"\n  \033[31mError: {e}\033[0m\n")
            continue

        print(f"\n\033[1;32m{answer}\033[0m\n")

    storage.close()
