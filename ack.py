"""Ack/status message management — build and edit bot status messages in Jami."""

import contextlib
import time


class AckManager:
    """Manages the ack/status message lifecycle for a single request.

    Accumulates all metadata (model, tokens, tools) cumulatively —
    once a field is known it is never dropped. The ack message always
    reflects the full known state.

    Progress updates arriving before the ack message ID is captured
    are queued and flushed once the ID becomes available, so early
    progress (model, first tools) is no longer silently dropped.
    """

    def __init__(self, sdk, account_id, conv_id, our_uri, no_ack=False):
        self.sdk = sdk
        self.account_id = account_id
        self.conv_id = conv_id
        self.bot_id = our_uri[:4]
        self.no_ack = no_ack
        self.ack_msg_id = None
        self._last_edit_time = 0
        self._pending_edits = []  # queued progress calls before ack_msg_id is known

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
        """Send the initial ack message.

        The ack_msg_id is NOT set here — it's captured later by the
        main event loop when the bot's own ack message notification arrives.
        This avoids swallowing notifications that the main loop should handle.
        """
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
        except Exception as e:
            print(f"[bot] ⚠️  Ack failed: {e}")

    def _flush_pending(self):
        """Flush queued progress edits now that ack_msg_id is known."""
        for state in self._pending_edits:
            self._apply_edit(state)
        self._pending_edits = None  # no longer queueing

    def _apply_edit(self, state):
        """Merge state and send the editMessage call."""
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
        force = state.get("force_update", False)
        now = time.time()
        if not force and now - self._last_edit_time < 10:
            return
        self._last_edit_time = now

        with contextlib.suppress(Exception):  # Best-effort progress update
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )

    def on_progress(self, state):
        """Edit ack message with streaming progress. Called by pi_client.

        Merges new data into cumulative state, then re-sends the full message.
        If the ack message ID hasn't been captured yet, queues the update
        and flushes it later once the ID arrives.
        """
        if self.no_ack:
            return

        # If ack_msg_id is not yet known, queue the state for later flush
        if self.ack_msg_id is None:
            if self._pending_edits is not None:
                self._pending_edits.append(state)
            return

        self._apply_edit(state)

    def mark_cancelled(self):
        """Edit ack message to show status: cancelled (with all accumulated info)."""
        if self.no_ack or not self.ack_msg_id:
            return

        self.status = "cancelled"

        with contextlib.suppress(Exception):
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )

    def mark_done(self):
        """Edit ack message to show status: done (with all accumulated info)."""
        if self.no_ack or not self.ack_msg_id:
            return

        self.status = "done"

        with contextlib.suppress(Exception):
            self.sdk.call(
                "editMessage",
                {
                    "accountId": self.account_id,
                    "conversationId": self.conv_id,
                    "body": self._format(),
                    "messageId": self.ack_msg_id,
                },
            )
