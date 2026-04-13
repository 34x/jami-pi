"""pi CLI interface — call pi in JSON mode, stream output, track progress."""

import json
import queue
import subprocess
import threading
import time

from config import CANCELLED_MARKER

# Default stall timeout: kill pi if it produces no output for this many seconds.
# This prevents the bot from hanging forever when pi waits for user
# confirmation (e.g. guardrails permissionGate prompts on dangerous
# commands like sudo, rm -rf, etc.) which can't be answered in
# non-interactive --print mode.
DEFAULT_STALL_TIMEOUT = 60


def _tool_label(name, args):
    """Build a readable label for a tool call: 'read bot.py', 'edit foo.sh', etc."""
    path = args.get("path", "")
    if path:
        # Shorten to just the filename (no directory)
        short = path.rsplit("/", 1)[-1]
        offset = args.get("offset")
        if offset and name == "read":
            return f"{name} {short}:{offset}"
        return f"{name} {short}"
    command = args.get("command", "")
    if command:
        # Show first line of command, up to 60 chars
        first_line = command.split("\n")[0]
        short = first_line[:60]
        if len(first_line) > 60:
            short += "…"
        return f"{name} {short}"
    pattern = args.get("pattern", "")
    if pattern:
        return f"{name} {pattern}"
    return name


def call_pi(
    prompt,
    session_file=None,
    extra_args=None,
    on_progress=None,
    cancel=None,
    stall_timeout=DEFAULT_STALL_TIMEOUT,
):
    """Call pi in non-interactive JSON mode and return the assistant's reply text.

    on_progress: optional callback(state) called during streaming.
                 state is a dict with keys: tokens, text, tools, model.
                 tools is a list of (name, status) tuples.
                 Set state["force_update"] = True to bypass throttle.
    cancel: optional threading.Event — set it to terminate pi immediately.
            Returns CANCELLED_MARKER if cancelled.
    stall_timeout: seconds with no output before killing pi (default: 60).
                   Prevents hangs when pi waits for user confirmation
                   (e.g. guardrails permissionGate) that can't be answered
                   in non-interactive mode. Set to 0 to disable.
    """
    cmd = ["pi", "--print", "--mode", "json"]

    if session_file:
        cmd.extend(["--session", session_file])
    else:
        cmd.append("--no-session")

    if extra_args:
        cmd.extend(extra_args)

    cmd.append(prompt)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return "[pi not found — is it installed?]"

    # Reader thread: reads lines from pi stdout and puts them in a queue.
    # This is portable (no fcntl) and works on Linux, macOS, and Windows.
    # When the process is terminated (e.g. on cancel), readline unblocks
    # and raises ValueError/OSError, ending the thread cleanly.
    line_queue = queue.Queue()

    def _reader():
        try:
            for raw_line in proc.stdout:
                line_queue.put(raw_line.decode("utf-8", errors="replace").strip())
        except (ValueError, OSError):
            pass  # stdout closed after terminate()
        line_queue.put(None)  # sentinel: EOF

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    reply = None
    token_count = 0
    text_so_far = ""
    last_progress = 0
    model = ""
    tools = []  # list of (label, status)

    state = {"tokens": 0, "text": "", "tools": [], "model": ""}

    def _process_line(line):
        """Parse a JSON line from pi and update state. Returns True on agent_end."""
        nonlocal reply, token_count, text_so_far, last_progress, model, tools

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False

        etype = event.get("type", "")
        evt = event.get("assistantMessageEvent", {})

        # Capture model from message_start
        if etype == "message_start":
            msg = event.get("message", {})
            if not model and msg.get("model"):
                model = msg["model"]
                state["model"] = model

        # Track streaming text deltas
        if evt.get("type") == "text_delta":
            text_so_far += evt.get("delta", "")
            token_count += 1
            state["tokens"] = token_count
            state["text"] = text_so_far
        elif evt.get("type") == "text_start":
            text_so_far = ""
            token_count = 0
            state["tokens"] = 0
            state["text"] = ""

        # Track tool calls
        elif etype == "tool_execution_start":
            name = event.get("toolName", "?")
            args = event.get("args", {})
            label = _tool_label(name, args)
            tools.append((label, "running"))
            state["tools"] = list(tools)
            state["force_update"] = True
            if on_progress:
                on_progress(state)
        elif etype == "tool_execution_end":
            name = event.get("toolName", "?")
            # Find matching running entry by tool name prefix
            for i in range(len(tools) - 1, -1, -1):
                # label starts with the tool name ("read ...", "edit ...", etc.)
                if tools[i][0].startswith(name) and tools[i][1] == "running":
                    tools[i] = (tools[i][0], "done")
                    break
            state["tools"] = list(tools)
            state["force_update"] = True
            if on_progress:
                on_progress(state)

        # Report progress every ~50 tokens
        if on_progress and token_count > 0 and token_count - last_progress >= 50:
            state["tokens"] = token_count
            state.pop("force_update", None)
            on_progress(state)
            last_progress = token_count

        # Extract final reply from agent_end
        if etype == "agent_end":
            for msg in reversed(event.get("messages", [])):
                if msg.get("role") == "assistant":
                    for part in msg.get("content", []):
                        if part.get("type") == "text":
                            reply = part["text"]
                            break
                    break
            return True

        return False

    # ── Main loop: drain lines from queue, check cancel/stall ──────
    last_output_time = time.monotonic()
    while True:
        # Check for cancellation
        if cancel and cancel.is_set():
            proc.terminate()
            reader_thread.join(timeout=5)
            proc.wait(timeout=5)
            return CANCELLED_MARKER

        # Check for stall (no output for too long)
        if stall_timeout and time.monotonic() - last_output_time > stall_timeout:
            proc.terminate()
            reader_thread.join(timeout=5)
            proc.wait(timeout=5)
            return (
                f"[pi stalled — no output for {stall_timeout}s. "
                f"This may be a permission gate requiring confirmation. "
                f"Check guardrails.json or use --tools to restrict tools.]"
            )

        try:
            line = line_queue.get(timeout=0.2)
        except queue.Empty:
            continue  # no line yet — loop back to check cancel/stall

        # Got output — reset stall timer
        last_output_time = time.monotonic()

        if line is None:
            break  # EOF sentinel

        if not line:
            continue

        if _process_line(line):
            break  # agent_end reached

    # Wait for process and reader to finish
    reader_thread.join(timeout=5)
    proc.wait()
    if proc.returncode != 0 and not reply:
        return f"[pi exited with code {proc.returncode}]"

    # Final progress report
    if on_progress and (not cancel or not cancel.is_set()):
        state["tokens"] = token_count
        state.pop("force_update", None)
        on_progress(state)

    return reply or "[pi returned no text response]"
