"""Tests for config module — BoundedSet, is_stop_command, should_respond, session_path."""

import os
import tempfile

from config import (
    BoundedSet,
    CANCELLED_MARKER,
    DEFAULT_HISTORY,
    DEFAULT_PI_TIMEOUT,
    DEFAULT_SESSION_DIR,
    SILENT_MARKER,
    TRIGGER_ALL,
    TRIGGER_MENTION,
    TRIGGER_SMART,
    is_new_session,
    is_stop_command,
    session_path,
    should_respond,
)


class TestBoundedSet:
    def test_add_and_contains(self):
        s = BoundedSet(max_size=5)
        s.add("a")
        s.add("b")
        assert "a" in s
        assert "b" in s
        assert "c" not in s

    def test_evicts_oldest(self):
        s = BoundedSet(max_size=3)
        s.add("1")
        s.add("2")
        s.add("3")
        s.add("4")  # evicts "1"
        assert "1" not in s
        assert "4" in s
        assert len(s) == 3

    def test_move_to_end(self):
        s = BoundedSet(max_size=3)
        s.add("1")
        s.add("2")
        s.add("3")
        s.add("1")  # refresh "1" — now "2" is oldest
        s.add("4")  # evicts "2"
        assert "1" in s
        assert "2" not in s
        assert "3" in s
        assert "4" in s

    def test_default_size(self):
        s = BoundedSet()
        assert s._max_size == 500

    def test_len(self):
        s = BoundedSet(max_size=10)
        assert len(s) == 0
        s.add("x")
        assert len(s) == 1

    def test_repr(self):
        s = BoundedSet(max_size=10)
        s.add("a")
        r = repr(s)
        assert "a" in r


class TestIsStopCommand:
    def test_stop_words(self):
        for word in ["stop", "abort", "cancel", "kill"]:
            assert is_stop_command(word) is True
            assert is_stop_command(word.upper()) is True

    def test_non_stop_words(self):
        assert is_stop_command("hello") is False
        assert is_stop_command("stop doing that") is False
        assert is_stop_command("") is False

    def test_whitespace(self):
        assert is_stop_command("  stop  ") is True
        assert is_stop_command("\tcancel\n") is True


class TestShouldRespond:
    def test_trigger_all(self):
        assert should_respond("hello", TRIGGER_ALL, ["bot"]) is True

    def test_trigger_mention_match(self):
        assert should_respond("hey bot", TRIGGER_MENTION, ["bot"]) is True

    def test_trigger_mention_no_match(self):
        assert should_respond("hello", TRIGGER_MENTION, ["bot"]) is False

    def test_trigger_mention_reply_to_bot(self):
        our_ids = {"msg123"}
        assert should_respond("hello", TRIGGER_MENTION, ["bot"], "msg123", our_ids) is True

    def test_trigger_mention_reply_not_to_bot(self):
        our_ids = {"msg123"}
        assert should_respond("hello", TRIGGER_MENTION, ["bot"], "msg999", our_ids) is False

    def test_trigger_smart(self):
        assert should_respond("hey bot", TRIGGER_SMART, ["bot"]) == "smart"
        assert should_respond("whatever", TRIGGER_SMART, ["bot"]) is False

    def test_empty_bot_names(self):
        assert should_respond("hello", TRIGGER_ALL, []) is True
        assert should_respond("hello", TRIGGER_MENTION, []) is False


class TestSessionPath:
    def test_path_format(self):
        p = session_path("abc123", "/tmp/sessions")
        assert p == "/tmp/sessions/abc123.json"

    def test_is_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test.json")
            assert is_new_session(p) is True
            with open(p, "w") as f:
                f.write("{}")
            assert is_new_session(p) is False


class TestConstants:
    def test_defaults(self):
        assert DEFAULT_HISTORY == 20
        assert DEFAULT_PI_TIMEOUT == 300
        assert CANCELLED_MARKER == "[CANCELLED]"
        assert SILENT_MARKER == "__SILENT__"