"""Configuration, constants, and path helpers for jami-bot-pi."""

import os

# Prefix for bot acknowledgment/status messages — used to filter them from pi context
ACK_PREFIX = "[bot"

# Silent response marker — if pi returns exactly this, the bot stays silent
SILENT_MARKER = "[SILENT]"

# Stop words — single-word messages that cancel a running pi task
STOP_WORDS = {"stop", "abort", "cancel", "kill"}

# Marker returned when pi is cancelled by user
CANCELLED_MARKER = "[CANCELLED]"

# Default session directory
DEFAULT_SESSION_DIR = "/tmp/jami-bot-sessions"

# Default history size
DEFAULT_HISTORY = 20


def is_stop_command(body):
    """Check if a message is a stop command.

    Only matches if the entire message (after stripping) is a single stop word.
    "stop doing that" does NOT match — only exact single-word matches.
    """
    word = body.strip().lower()
    return word in STOP_WORDS and " " not in body.strip()


def load_system_prompt(path=None):
    """Load the system prompt from a markdown file.

    Searches in order: explicit path, ./system-prompt.md, then errors.
    """
    if path:
        with open(path) as f:
            return f.read().strip()

    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system-prompt.md")
    if os.path.exists(local):
        with open(local) as f:
            return f.read().strip()

    raise FileNotFoundError(
        "system-prompt.md not found. Use --system-prompt to specify a path."
    )


def session_path(conv_id, session_dir):
    """Return the pi session file path for a conversation."""
    return os.path.join(session_dir, f"{conv_id}.json")


def is_new_session(session_file):
    """Check if this will be a new pi session (file doesn't exist yet)."""
    return not os.path.exists(session_file)
