"""Tests for formatting module — get_sender_name, format_conversation_for_pi, build_prompt."""

from formatting import build_prompt, format_conversation_for_pi, get_sender_name


class TestGetSenderName:
    def test_known_sender(self):
        senders = {"jami://alice": "Alice"}
        assert get_sender_name("jami://alice", senders) == "Alice"

    def test_unknown_sender_short_uri(self):
        senders = {}
        name = get_sender_name("short", senders)
        assert name == "short"
        assert senders["short"] == "short"

    def test_unknown_sender_long_uri(self):
        senders = {}
        long_uri = "jami://abcdefghijklmnopqrstuvwxyz0123456789"
        name = get_sender_name(long_uri, senders)
        assert len(name) == 8
        assert name == long_uri[-8:]

    def test_side_effect_stores_name(self):
        senders = {}
        name = get_sender_name("verylonguri12345", senders)
        assert "verylonguri12345" in senders
        assert senders["verylonguri12345"] == name

    def test_existing_name_returned(self):
        senders = {"uri1": "Alice"}
        assert get_sender_name("uri1", senders) == "Alice"
        assert senders["uri1"] == "Alice"  # unchanged


class TestFormatConversationForPi:
    def test_basic_message(self):
        messages = [{"from": "alice", "body": "Hello", "type": "text/plain"}]
        result = format_conversation_for_pi(messages, "bot_uri", {"alice": "Alice"})
        assert result == "[Alice]: Hello"

    def test_filters_ack_messages(self):
        messages = [
            {"from": "bot", "body": "[bot:ab12]\nstatus: in progress", "type": "text/plain"},
            {"from": "alice", "body": "Hello", "type": "text/plain"},
        ]
        result = format_conversation_for_pi(messages, "bot_uri", {"alice": "Alice", "bot": "Bot"})
        assert "[Bot]" not in result
        assert "[Alice]: Hello" in result

    def test_filters_non_text(self):
        messages = [
            {"from": "alice", "body": "", "type": "text/plain"},
            {"from": "alice", "body": "Hi", "type": "application/json"},
        ]
        result = format_conversation_for_pi(messages, "bot_uri", {})
        assert result == ""

    def test_multiple_messages(self):
        messages = [
            {"from": "alice", "body": "Hi", "type": "text/plain"},
            {"from": "bob", "body": "Hey", "type": "text/plain"},
        ]
        result = format_conversation_for_pi(
            messages, "bot_uri", {"alice": "Alice", "bob": "Bob"}
        )
        assert "[Alice]: Hi" in result
        assert "[Bob]: Hey" in result


class TestBuildPrompt:
    def test_1on1_chat_no_history(self):
        msg = {"from": "alice", "body": "Hello"}
        result = build_prompt(msg, None, "bot_uri", {"alice": "Alice"}, member_count=2)
        assert "(1:1 chat)" in result
        assert "[Alice]: Hello" in result

    def test_group_chat_no_history(self):
        msg = {"from": "alice", "body": "Hello"}
        result = build_prompt(msg, None, "bot_uri", {"alice": "Alice"}, member_count=5)
        assert "(group chat, 5 members)" in result

    def test_with_history(self):
        msg = {"from": "bob", "body": "What's up?"}
        history = [
            {"from": "alice", "body": "Hi", "type": "text/plain"},
        ]
        result = build_prompt(msg, history, "bot_uri", {"alice": "Alice", "bob": "Bob"}, member_count=2)
        assert "Recent conversation:" in result
        assert "New message from [Bob]" in result