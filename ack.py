"""Ack/status message management — build and edit bot status messages in Jami."""

import time

from config import ACK_PREFIX


def build_ack_body(bot_id, status, model="", tokens=0, tools=None):
    """Build the body for a bot status/ack message.

    Args:
        bot_id: Short bot identifier (first 4 chars of Jami URI)
        status: "in progress" or "done"
        model: Model name (if known)
        tokens: Token count so far
        tools: List of (name, status) tuples for tool calls
    """
    lines = [f"[bot:{bot_id}]", f"status: {status}"]
    if model:
        lines.append(f"model: {model}")
    if tokens:
        lines.append(f"tokens: {tokens}")
    if tools:
        for name, tool_status in tools:
            lines.append(f"tool: {name} ({tool_status})")
    return "\n".join(lines)


class AckManager:
    """Manages the ack/status message lifecycle for a single request.

    Sends initial status, edits it with progress, then marks done.
    """

    def __init__(self, sdk, account_id, conv_id, our_uri, no_ack=False):
        self.sdk = sdk
        self.account_id = account_id
        self.conv_id = conv_id
        self.bot_id = our_uri[:4]
        self.no_ack = no_ack
        self.ack_msg_id = None
        self.seen_model = ""
        self._last_edit_time = 0

    def send_initial(self):
        """Send the initial ack message."""
        if self.no_ack:
            return

        try:
            self.sdk.call(
                "sendMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": build_ack_body(self.bot_id, "in progress"),
                },
            )
            # Wait briefly for our own message notification to get the ID
            for _ in range(10):
                evt = self.sdk.get_notification(timeout=0.5)
                if evt and evt.get("method") == "onMessageReceived":
                    p = evt.get("params", {})
                    if p.get("body", "").startswith(ACK_PREFIX):
                        self.ack_msg_id = p.get("id")
                        break
        except Exception as e:
            print(f"[bot] ⚠️  Ack failed: {e}")

    def on_progress(self, state):
        """Edit ack message with streaming progress. Called by pi_client."""
        if self.no_ack or not self.ack_msg_id:
            return

        # Tool events force immediate update; token updates throttled to 10s
        force = state.pop("force_update", False)
        now = time.time()
        if not force and now - self._last_edit_time < 10:
            return
        self._last_edit_time = now

        model = state.get("model", "")
        if model:
            self.seen_model = model

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": build_ack_body(
                        self.bot_id,
                        "in progress",
                        model=self.seen_model,
                        tokens=state.get("tokens", 0),
                        tools=state.get("tools", []),
                    ),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass  # Best-effort progress update

    def mark_cancelled(self):
        """Edit ack message to show status: cancelled."""
        if self.no_ack or not self.ack_msg_id:
            return

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": build_ack_body(
                        self.bot_id, "cancelled", model=self.seen_model
                    ),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass

    def mark_done(self):
        """Edit ack message to show status: done."""
        if self.no_ack or not self.ack_msg_id:
            return

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": build_ack_body(self.bot_id, "done", model=self.seen_model),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass
