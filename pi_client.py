"""pi CLI interface — call pi in JSON mode, stream output, track progress."""

import fcntl
import json
import os
import subprocess
import time

from config import CANCELLED_MARKER


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
):
    """Call pi in non-interactive JSON mode and return the assistant's reply text.

    on_progress: optional callback(state) called during streaming.
                 state is a dict with keys: tokens, text, tools, model.
                 tools is a list of (name, status) tuples.
                 Set state["force_update"] = True to bypass throttle.
    cancel: optional threading.Event — set it to terminate pi immediately.
            Returns CANCELLED_MARKER if cancelled.
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

    # Make stdout non-blocking so we can check cancel between reads
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    reply = None
    token_count = 0
    text_so_far = ""
    last_progress = 0
    model = ""
    tools = []  # list of (label, status) — label is like "read foo.py", status is "running" or "done"
    buf = ""

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

    # ── Non-blocking read loop ──────────────────────────────────────
    while True:
        # Check for cancellation
        if cancel and cancel.is_set():
            proc.terminate()
            proc.wait(timeout=5)
            return CANCELLED_MARKER

        # Try to read available data
        try:
            data = os.read(fd, 8192)
            if not data:
                break  # EOF
            buf += data.decode("utf-8", errors="replace")
        except BlockingIOError:
            # No data available — brief sleep then retry
            time.sleep(0.1)
            continue
        except OSError:
            break

        # Process complete lines from buffer
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if _process_line(line):
                break
        else:
            continue
        break  # agent_end was reached — exit outer loop

    proc.wait()
    if proc.returncode != 0 and not reply:
        return f"[pi exited with code {proc.returncode}]"

    # Final progress report
    if on_progress and (not cancel or not cancel.is_set()):
        state["tokens"] = token_count
        state.pop("force_update", None)
        on_progress(state)

    return reply or "[pi returned no text response]"
