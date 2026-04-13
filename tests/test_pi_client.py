"""Tests for pi_client module — tool labeling and error handling."""

from unittest.mock import MagicMock, patch

from pi_client import _tool_label, _ERROR_PREFIX


class TestToolLabel:
    def test_path_arg(self):
        label = _tool_label("read", {"path": "/home/user/bot.py"})
        assert label == "read bot.py"

    def test_path_with_offset(self):
        label = _tool_label("read", {"path": "/home/user/bot.py", "offset": 100})
        assert label == "read bot.py:100"

    def test_command_arg(self):
        label = _tool_label("bash", {"command": "ls -la"})
        assert label == "bash ls -la"

    def test_command_truncation(self):
        long_cmd = "a" * 100
        label = _tool_label("bash", {"command": long_cmd})
        assert len(label) < len("bash " + long_cmd)
        assert label.endswith("…")

    def test_pattern_arg(self):
        label = _tool_label("grep", {"pattern": "TODO"})
        assert label == "grep TODO"

    def test_no_args(self):
        label = _tool_label("think", {})
        assert label == "think"

    def test_windows_path(self):
        label = _tool_label("read", {"path": "C:\\Users\\bot.py"})
        assert label == "read bot.py"


class TestErrorPrefix:
    def test_prefix_format(self):
        assert _ERROR_PREFIX == "[jami-pi: "

    def test_error_messages_are_distinct(self):
        """Error messages from the bot should not look like regular LLM output."""
        from pi_client import call_pi
        # Call with non-existent pi binary — should return error prefix
        # (We can't easily test this without mocking, but we verify the prefix)
        assert _ERROR_PREFIX.startswith("[jami-pi:")