"""Ack/status message management — build and edit bot status messages in Jami."""

import time

from config import ACK_PREFIX


class AckManager:
    """Manages the ack/status message lifecycle for a single request.

    Accumulates all metadata (model, tokens, tools) cumulatively —
    once a field is known it is never dropped. The ack message always
    reflects the full known state.
    """

    def __init__(self, sdk, account_id, conv_id, our_uri, no_ack=False):
        self.sdk = sdk
        self.account_id = account_id
        self.conv_id = conv_id
        self.bot_id = our_uri[:4]
        self.no_ack = no_ack
        self.ack_msg_id = None
        self._last_edit_time = 0

        # Cumulative state — only grows, never shrinks
        self.status = ""
        self.model = ""
        self.tokens = 0
        self.tools = []  # list of (name, status) — appended on start, updated on end

    def _format(self):
        """Build the full cumulative ack message body."""
        lines = [f"[bot:{self.bot_id}]", f"status: {self.status}"]
        if self.model:
            lines.append(f"model: {self.model}")
        if self.tokens:
            lines.append(f"tokens: {self.tokens}")
        for label, tool_status in self.tools:
            icon = "✓" if tool_status == "done" else "⟳"
            lines.append(f"tool: {label} {icon}")
        return "\n".join(lines)

    def send_initial(self):
        """Send the initial ack message."""
        if self.no_ack:
            return

        self.status = "in progress"

        try:
            self.sdk.call(
                "sendMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
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
        """Edit ack message with streaming progress. Called by pi_client.

        Merges new data into cumulative state, then re-sends the full message.
        """
        if self.no_ack or not self.ack_msg_id:
            return

        # Merge state — each field only upgrades, never downgrades
        model = state.get("model", "")
        if model:
            self.model = model

        tokens = state.get("tokens", 0)
        if tokens:
            self.tokens = tokens

        tools = state.get("tools")
        if tools:
            self.tools = list(tools)  # pi_client already accumulates these

        # Tool events force immediate update; token updates throttled to 10s
        force = state.pop("force_update", False)
        now = time.time()
        if not force and now - self._last_edit_time < 10:
            return
        self._last_edit_time = now

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass  # Best-effort progress update

    def mark_cancelled(self):
        """Edit ack message to show status: cancelled (with all accumulated info)."""
        if self.no_ack or not self.ack_msg_id:
            return

        self.status = "cancelled"

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass

    def mark_done(self):
        """Edit ack message to show status: done (with all accumulated info)."""
        if self.no_ack or not self.ack_msg_id:
            return

        self.status = "done"

        try:
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )
        except Exception:
            pass
