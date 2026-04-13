"""Configuration, constants, and path helpers for jami-pi."""

import os

# Prefix for bot acknowledgment/status messages — used to filter them from pi context
ACK_PREFIX = "[bot:"

# Silent response marker — if pi returns exactly this, the bot stays silent
SILENT_MARKER = "__SILENT__"

# Stop words — single-word messages that cancel a running pi task
STOP_WORDS = {"stop", "abort", "cancel", "kill"}

# Marker returned when pi is cancelled by user
CANCELLED_MARKER = "[CANCELLED]"

# Default session directory
DEFAULT_SESSION_DIR = "/tmp/jami-pi-sessions"

# Default history size
DEFAULT_HISTORY = 20

# Trigger modes
TRIGGER_ALL = "all"  # respond to every message (1:1 default)
TRIGGER_MENTION = "mention"  # respond only if bot name mentioned or reply to bot
TRIGGER_SMART = "smart"  # mention/reply check first, then LLM decides
TRIGGER_MODES = {TRIGGER_ALL, TRIGGER_MENTION, TRIGGER_SMART}


def is_stop_command(body):
    """Check if a message is a stop command.

    Only matches if the entire message (after stripping) is a single stop word.
    "stop doing that" does NOT match — only exact single-word matches.
    """
    word = body.strip().lower()
    return word in STOP_WORDS and " " not in body.strip()


def should_respond(body, trigger, bot_names, parent_id="", our_message_ids=None):
    """Decide whether the bot should respond to a message.

    Args:
        body: message text
        trigger: TRIGGER_ALL, TRIGGER_MENTION, or TRIGGER_SMART
        bot_names: list of lowercase strings to match (alias, uri fragment, etc.)
        parent_id: message's reply-to parent ID (empty if not a reply)
        our_message_ids: set of message IDs sent by the bot (for reply detection)

    Returns:
        True if the bot should process the message,
        "smart" if trigger=smart and mention detected (needs LLM check),
        False if the bot should ignore the message entirely.
    """
    if trigger == TRIGGER_ALL:
        return True

    # Mention check: does the message text contain any of our names?
    body_lower = body.lower()
    mentioned = any(name in body_lower for name in bot_names if name)

    # Reply check: is this a reply to one of the bot's messages?
    replying_to_bot = False
    if parent_id and our_message_ids is not None:
        replying_to_bot = parent_id in our_message_ids

    if trigger == TRIGGER_MENTION:
        return mentioned or replying_to_bot

    if trigger == TRIGGER_SMART:
        if mentioned or replying_to_bot:
            return "smart"  # TODO: add LLM relevance check before proceeding
        return False

    return False


def session_path(conv_id, session_dir):
    """Return the pi session file path for a conversation."""
    return os.path.join(session_dir, f"{conv_id}.json")


def is_new_session(session_file):
    """Check if this will be a new pi session (file doesn't exist yet)."""
    return not os.path.exists(session_file)
