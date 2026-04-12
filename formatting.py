"""Message formatting utilities for converting between Jami and pi formats."""

from config import ACK_PREFIX


def format_sender(uri, known_senders):
    """Return a short name for a sender URI. Track names in known_senders dict."""
    if uri in known_senders:
        return known_senders[uri]
    # Use last 8 chars as short ID
    short = uri[-8:] if len(uri) > 8 else uri
    known_senders[uri] = short
    return short


def format_conversation_for_pi(messages, our_uri, known_senders):
    """Format recent Jami messages into a conversation transcript for pi.

    Filters out ack/status messages so they don't pollute pi's context.
    """
    lines = []
    for msg in messages:
        sender_uri = msg.get("from", "")
        body = msg.get("body", "").strip()
        msg_type = msg.get("type", "")

        # Only include text messages
        if msg_type != "text/plain" or not body:
            continue

        # Filter out ack/status messages
        if body.startswith(ACK_PREFIX):
            continue

        sender = format_sender(sender_uri, known_senders)
        lines.append(f"[{sender}]: {body}")

    return "\n".join(lines)


def build_prompt(
    new_message, conversation_history, our_uri, known_senders, member_count=2
):
    """Build the prompt to send to pi."""
    sender_uri = new_message.get("from", "")
    body = new_message.get("body", "").strip()
    sender = format_sender(sender_uri, known_senders)

    context_lines = []
    if member_count == 2:
        context_lines.append("(1:1 chat)")
    else:
        context_lines.append(f"(group chat, {member_count} members)")

    if conversation_history:
        history_text = format_conversation_for_pi(
            conversation_history, our_uri, known_senders
        )
        context_lines.append(f"Recent conversation:\n{history_text}")
        context = "\n".join(context_lines)
        return f"{context}\n\nNew message from [{sender}]: {body}"
    else:
        return f"[{sender}]: {body}"
