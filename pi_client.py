"""pi CLI interface — call pi in JSON mode, stream output, track progress."""

import json
import subprocess


def call_pi(
    prompt, session_file=None, system_prompt=None, extra_args=None, on_progress=None
):
    """Call pi in non-interactive JSON mode and return the assistant's reply text.

    on_progress: optional callback(state) called during streaming.
                 state is a dict with keys: tokens, text, tools, model.
                 tools is a list of (name, status) tuples.
                 Set state["force_update"] = True to bypass throttle.
    """
    cmd = ["pi", "--print", "--mode", "json"]

    if session_file:
        cmd.extend(["--session", session_file])
    else:
        cmd.append("--no-session")

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if extra_args:
        cmd.extend(extra_args)

    cmd.append(prompt)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
    except FileNotFoundError:
        return "[pi not found — is it installed?]"

    reply = None
    token_count = 0
    text_so_far = ""
    last_progress = 0
    model = ""
    tools = []  # list of (name, status) — status is "running" or "done"

    state = {"tokens": 0, "text": "", "tools": [], "model": ""}

    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

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
            tools.append((event.get("toolName", "?"), "running"))
            state["tools"] = list(tools)
            state["force_update"] = True
            if on_progress:
                on_progress(state)
        elif etype == "tool_execution_end":
            name = event.get("toolName", "?")
            for i in range(len(tools) - 1, -1, -1):
                if tools[i][0] == name and tools[i][1] == "running":
                    tools[i] = (name, "done")
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

    proc.wait()
    if proc.returncode != 0 and not reply:
        return f"[pi exited with code {proc.returncode}]"

    # Final progress report
    if on_progress:
        state["tokens"] = token_count
        state.pop("force_update", None)
        on_progress(state)

    return reply or "[pi returned no text response]"
