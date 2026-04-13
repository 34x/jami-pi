"""Tests for AckManager."""

import contextlib
import io
import time
from unittest.mock import MagicMock, call

from ack import AckManager


def _make_ack(no_ack=False):
    """Create an AckManager with a mock SDK."""
    sdk = MagicMock()
    sdk.call.return_value = {"sent": True}
    ack = AckManager(sdk, "acct123", "conv456", "long_bot_uri", no_ack=no_ack)
    return ack, sdk


class TestAckManagerInit:
    def test_bot_id_is_uri_prefix(self):
        ack, _ = _make_ack()
        assert ack.bot_id == "long"

    def test_no_ack_mode(self):
        ack, sdk = _make_ack(no_ack=True)
        ack.send_initial()
        sdk.call.assert_not_called()


class TestSendInitial:
    def test_sends_in_progress(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        assert ack.status == "in progress"
        sdk.call.assert_called_once()
        args = sdk.call.call_args
        assert args[0][0] == "sendMessage"
        body = args[0][1]["body"]
        assert "[bot:long]" in body
        assert "in progress" in body


class TestOnProgress:
    def test_queued_before_msg_id(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        # No ack_msg_id yet — should queue
        ack.on_progress({"tokens": 50, "force_update": True})
        # Only the initial send, not an edit
        assert sdk.call.call_count == 1

        # Now set ack_msg_id — should flush
        ack.ack_msg_id = "msg1"
        ack._flush_pending()
        assert sdk.call.call_count == 2
        edit_call = sdk.call.call_args_list[1]
        assert edit_call[0][0] == "editMessage"

    def test_updates_tokens(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        ack.ack_msg_id = "msg1"
        # Reset mock to track new calls
        sdk.call.reset_mock()

        # Set last_edit_time far in the past to test throttle
        ack._last_edit_time = time.time() - 20
        ack.on_progress({"tokens": 100})
        # Should send because >10s since last edit
        assert sdk.call.call_count == 1

        # Now within throttle window
        ack.on_progress({"tokens": 150})
        # Throttled — no new call
        assert sdk.call.call_count == 1

    def test_model_and_tools(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        ack.ack_msg_id = "msg1"
        # Bypass throttle
        ack._last_edit_time = 0
        ack.on_progress({"model": "gpt-4o", "tokens": 10, "force_update": True})
        assert ack.model == "gpt-4o"
        assert ack.tokens == 10

        # Tools
        ack._last_edit_time = 0
        ack.on_progress({"tools": [("read x.py", "running")], "force_update": True})
        assert len(ack.tools) == 1


class TestMarkDone:
    def test_edits_to_done(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        ack.ack_msg_id = "msg1"
        ack.mark_done()
        assert ack.status == "done"
        # send_initial + mark_done
        assert sdk.call.call_count == 2
        edit_call = sdk.call.call_args_list[1]
        assert edit_call[0][0] == "editMessage"
        body = edit_call[0][1]["body"]
        assert "done" in body


class TestMarkCancelled:
    def test_edits_to_cancelled(self):
        ack, sdk = _make_ack()
        ack.send_initial()
        ack.ack_msg_id = "msg1"
        ack.mark_cancelled()
        assert ack.status == "cancelled"
        body = sdk.call.call_args_list[1][0][1]["body"]
        assert "cancelled" in body


class TestFormat:
    def test_minimal_format(self):
        ack, _ = _make_ack()
        ack.status = "testing"
        result = ack._format()
        assert result.startswith("[bot:long]")
        assert "status: testing" in result

    def test_full_format(self):
        ack, _ = _make_ack()
        ack.status = "in progress"
        ack.model = "claude-sonnet-4-20250514"
        ack.tokens = 42
        ack.tools = [("read bot.py", "done"), ("edit bot.py", "running")]
        result = ack._format()
        assert "model: claude-sonnet-4-20250514" in result
        assert "tokens: 42" in result
        assert "tool: read bot.py ✓" in result
        assert "tool: edit bot.py ⟳" in result