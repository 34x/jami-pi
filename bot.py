#!/usr/bin/env python3
"""jami-pi: Jami <-> pi chat bridge.

Connects Jami conversations to a pi coding agent, forwarding messages
and streaming back progress updates using editable ack messages.

Supports multiple conversations simultaneously — each conversation gets
its own pi session, member tracking, and independent processing.

See: python3 bot.py --help
"""

import argparse
import os
import queue
import shlex
import sys
import threading
import time

from ack import AckManager
from config import (
    ACK_PREFIX,
    CANCELLED_MARKER,
    DEFAULT_HISTORY,
    DEFAULT_SESSION_DIR,
    SILENT_MARKER,
    TRIGGER_ALL,
    TRIGGER_MODES,
    is_new_session,
    is_stop_command,
    session_path,
    should_respond,
)
from formatting import build_prompt, format_sender
from jami_client import JamiStdioClient
from pi_client import call_pi


# ── Logging helpers ──────────────────────────────────────────────────────
# Module-level flags set once from args.
_quiet = False
_verbose = False


def bot_log(msg):
    """Print info-level bot message. Suppressed by --quiet."""
    if not _quiet:
        print(msg)


def bot_warn(msg):
    """Print warning/eorror. Always visible."""
    print(msg, file=sys.stderr)


def bot_verbose(msg):
    """Print debug/verbose message. Only shown with --verbose."""
    if _verbose:
        print(msg, file=sys.stderr)


# ── Per-conversation state ──────────────────────────────────────────────


class Conversation:
    """Tracks state for a single Jami conversation."""

    def __init__(self, conv_id, member_count=0):
        self.conv_id = conv_id
        self.member_count = member_count
        self.our_message_ids = set()  # bot's own message IDs (for reply detection)
        self.busy = False  # True while pi is processing a request
        self.cancel = None  # threading.Event to cancel current pi call
        self.sender_uri = None  # who we're currently processing
        self.greeted = False  # True after greeting sent (avoid duplicates)
        self.ack = None  # current AckManager for progress updates


def main():
    parser = argparse.ArgumentParser(description="jami-pi: Jami <-> pi chat bridge")
    parser.add_argument(
        "--jami",
        default=None,
        help="Path to jami-bridge binary (or set JAMI_BRIDGE_PATH env)",
    )
    parser.add_argument(
        "--account", default=None, help="Account ID or URI (auto-detect)"
    )
    parser.add_argument(
        "--list-accounts", action="store_true", help="List accounts and exit"
    )
    parser.add_argument(
        "--alias",
        default=None,
        help="Set the bot display name (profile alias) pushed to contacts.",
    )
    parser.add_argument(
        "--register-name",
        default=None,
        help="Register a public Jami username (one-shot action).",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=DEFAULT_HISTORY,
        help=f"Recent messages to include as context (default: {DEFAULT_HISTORY})",
    )
    parser.add_argument(
        "--session-dir",
        default=DEFAULT_SESSION_DIR,
        help=f"pi session directory (default: {DEFAULT_SESSION_DIR})",
    )
    parser.add_argument(
        "--no-session", action="store_true", help="Disable pi sessions (stateless)"
    )
    parser.add_argument(
        "--no-ack", action="store_true", help="Disable acknowledgment messages"
    )
    parser.add_argument(
        "--greeting",
        default="online",
        help='Send a greeting on startup: "online" (default), custom text, or "false" to disable',
    )
    parser.add_argument(
        "--pi-args",
        default="",
        help="Extra pi args (space-separated, shell quoting). "
        "Multi-word values need inner quotes: "
        "--pi-args='--system-prompt \"You are a bot\"'",
    )
    parser.add_argument(
        "--bridge-args",
        default="",
        help="Extra jami-bridge args (space-separated). "
        "Use '=' if value starts with '--': --bridge-args='--auto-accept' "
        "or --bridge-args='--auto-accept-from abc123'",
    )
    parser.add_argument(
        "--trigger",
        default=TRIGGER_ALL,
        choices=sorted(TRIGGER_MODES),
        help="When to respond: all (every msg), mention (bot name/reply), smart (mention+LLM check)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't call pi")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show bridge stderr output (daemon logs, etc.)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress all non-essential bot output"
    )
    args = parser.parse_args()

    # Set module-level log flags
    global _quiet, _verbose
    _quiet = args.quiet
    _verbose = args.verbose

    # Resolve jami-bridge binary path: --jami flag > JAMI_BRIDGE_PATH env > PATH lookup
    jami_binary = args.jami or os.environ.get("JAMI_BRIDGE_PATH") or "jami-bridge"

    pi_extra = shlex.split(args.pi_args) if args.pi_args else None
    use_sessions = not args.no_session
    os.makedirs(args.session_dir, exist_ok=True)

    # Resolve bridge args: --bridge-args string (space-split, like --pi-args)
    bridge_args = shlex.split(args.bridge_args) if args.bridge_args else []

    # ── Launch jami-bridge in stdio mode ────────────────────────────────────
    sdk = JamiStdioClient(
        jami_binary=jami_binary,
        bridge_args=bridge_args,
        verbose_bridge=args.verbose,
    )
    sdk.start()

    # ── Discover account ─────────────────────────────────────────────
    result = sdk.call("listAccounts")
    accounts = result.get("accounts", [])

    if args.list_accounts:
        if not accounts:
            print("No accounts found.")
        else:
            for aid in accounts:
                details = sdk.call("getAccountDetails", {"accountId": aid})
                d = details.get("details") or {}
                uri = d.get("Account.username", "?")
                alias = d.get("Account.alias", "?")
                print(f"{aid}  uri={uri}  alias={alias}")
        sdk.stop()
        return

    # Resolve account: by ID, by URI, or auto-detect
    if args.account:
        if args.account in accounts:
            account_id = args.account
        else:
            # Try matching by URI
            account_id = None
            for aid in accounts:
                details = sdk.call("getAccountDetails", {"accountId": aid})
                d = details.get("details") or {}
                if d.get("Account.username") == args.account:
                    account_id = aid
                    break
            if not account_id:
                print(f"Account not found: {args.account}")
                print(f"Available accounts: {', '.join(accounts)}")
                sys.exit(1)
    else:
        if not accounts:
            print("No accounts found. Create one first.")
            sys.exit(1)
        account_id = accounts[0]

    # Get our own URI so we can ignore our own messages
    details = sdk.call("getAccountDetails", {"accountId": account_id})
    d = details.get("details") or {}
    our_uri = d.get("Account.username", "")
    our_alias = d.get("Account.alias", "bot")

    # ── Register name (one-shot, exit after) ──────────────────────────────
    if args.register_name:
        bot_log(f"[bot] Registering name '{args.register_name}'...")
        sdk.call(
            "registerName",
            {"accountId": account_id, "name": args.register_name},
        )
        bot_log("[bot] Waiting for registration result...")
        deadline = time.time() + 15
        while time.time() < deadline:
            evt = sdk.get_notification(timeout=1)
            if evt and evt.get("method") == "onNameRegistrationEnded":
                state = evt.get("params", {}).get("state", -1)
                reg_name = evt.get("params", {}).get("name", "")
                state_names = {
                    0: "success",
                    1: "invalid name",
                    2: "already taken",
                    3: "error",
                    4: "unsupported",
                }
                state_desc = state_names.get(state, f"unknown({state})")
                if state == 0:
                    print(f"✅ Name '{reg_name}' registered successfully!")
                else:
                    print(f"❌ Name '{reg_name}' registration failed: {state_desc}")
                break
        else:
            print("❌ Registration timed out (15 seconds).")
        sdk.stop()
        return

    # ── Apply alias if specified ────────────────────────────────────────
    if args.alias is not None and args.alias != our_alias:
        bot_log(f"[bot] Setting alias: {our_alias} → {args.alias}")
        sdk.call(
            "updateProfile",
            {"accountId": account_id, "displayName": args.alias},
        )
        our_alias = args.alias

    # ── Build trigger names from alias + URI fragment ────────────────
    bot_names = []
    if our_alias:
        bot_names.append(our_alias.lower())
    # Add short URI fragment (last 8 chars) as a fallback name
    uri_short = our_uri.rsplit(":", 1)[-1][-8:] if ":" in our_uri else our_uri[-8:]
    if uri_short and uri_short not in bot_names:
        bot_names.append(uri_short.lower())

    trigger = args.trigger
    bot_log(f"[bot] Account: {account_id}")
    bot_log(f"[bot] Our URI: {our_uri}")
    bot_log(f"[bot] Our alias: {our_alias}")
    bot_log(f"[bot] Trigger: {trigger} (names: {bot_names})")

    # ── Shared state ─────────────────────────────────────────────────
    # known_senders is global across conversations (URIs are unique)
    known_senders = {our_uri: our_alias or "bot"}

    # Per-conversation state: conv_id -> Conversation
    conversations = {}

    # Pi result queue: (conv_id, reply_or_CANCELLED_MARKER, ack, partial_text)
    pi_results = queue.Queue()

    # ── Greeting ─────────────────────────────────────────────────────
    greeting_text = None
    if args.greeting.lower() not in ("false", "no", "off", "0", "none"):
        greeting_text = args.greeting if args.greeting != "online" else "🟢 I'm online!"

    # ── Conversation helpers ─────────────────────────────────────────

    def _register_conversation(conv_id: str) -> Conversation:
        """Register a conversation and discover its members. Returns the Conversation object."""
        conv = Conversation(conv_id)
        conversations[conv_id] = conv
        try:
            conv_detail = sdk.call(
                "getConversation", {"accountId": account_id, "conversationId": conv_id}
            )
            conv.member_count = conv_detail.get("memberCount", 2)
            for member in conv_detail.get("members", []):
                uri = member.get("uri", "")
                if uri and uri not in known_senders:
                    known_senders[uri] = uri[-8:] if len(uri) > 8 else uri
        except Exception as e:
            bot_warn(f"[bot] ⚠️  Failed to load conversation {conv_id}: {e}")
        return conv

    def _short_id(conv_id: str) -> str:
        """Short conversation ID for display."""
        return conv_id[:12]

    # ── Discover conversations ────────────────────────────────────────
    has_auto_accept = any(
        a in ("--auto-accept", "--auto-accept-from") for a in bridge_args
    )

    convs_raw = sdk.call("listConversations", {"accountId": account_id})
    convs_list = convs_raw.get("conversations", [])

    if not convs_list:
        if has_auto_accept:
            bot_log("[bot] No conversations yet — waiting for auto-accepted invite...")
        else:
            print(
                "No conversations found. Create one first, or use --bridge-args '--auto-accept' to auto-accept invites."
            )
            sys.exit(1)
    else:
        for c in convs_list:
            cid = c.get("id", "")
            if cid:
                conv = _register_conversation(cid)
                title = c.get("title", "") or cid[:12]
                bot_log(f"[bot] Conversation: {title}... ({conv.member_count} members)")

    bot_log(f"[bot] Monitoring {len(conversations)} conversation(s)")
    if use_sessions:
        bot_log(f"[bot] Session dir: {args.session_dir}")
    else:
        bot_log("[bot] Sessions disabled (stateless mode)")
    bot_log(f"[bot] History: {args.history} messages as context")
    bot_log(f"[bot] Ack: {'disabled' if args.no_ack else 'enabled'}")

    # Send greeting to all known conversations now (they're already loaded).
    # For conversations that sync later (e.g. auto-accepted), the
    # onConversationReady handler in the event loop sends the greeting.
    if greeting_text:
        greeted_count = 0
        for conv_id, conv in conversations.items():
            conv.greeted = True
            try:
                sdk.call(
                    "sendMessage",
                    {
                        "accountId": account_id,
                        "conversationId": conv_id,
                        "body": greeting_text,
                    },
                )
                greeted_count += 1
            except Exception as e:
                bot_warn(f"[bot] ⚠️  Greeting failed in {_short_id(conv_id)}: {e}")
        if greeted_count:
            bot_log(f"[bot] 👋 Greeting sent to {greeted_count} conversation(s)")

    bot_log("[bot] Ready. (Ctrl+C to stop)")
    print()

    # ── Pi call helpers ──────────────────────────────────────────────

    def _start_pi_for_conversation(conv: Conversation, params: dict):
        """Start a pi call for a conversation. Called from the main loop.

        History loading is deferred to the pi thread to avoid blocking
        the event loop. The ack message ID is captured asynchronously
        by the main loop when our own ack message notification arrives.
        """
        conv.busy = True
        conv.cancel = threading.Event()
        conv.sender_uri = params.get("from", "")

        # Send acknowledgment (from main loop — safe to call sdk)
        ack = AckManager(sdk, account_id, conv.conv_id, our_uri, no_ack=args.no_ack)
        conv.ack = ack
        ack.send_initial()

        # Determine session mode (fast — just a file existence check)
        sfile = session_path(conv.conv_id, args.session_dir) if use_sessions else None
        first_message = sfile and is_new_session(sfile)

        mode_label = (
            "new" if first_message else "continued" if use_sessions else "history"
        )
        bot_log(
            f"[bot] 🤖 Calling pi ({mode_label} session) for {_short_id(conv.conv_id)}..."
        )

        # Mutable containers for the pi thread to write results into
        pi_result_box = [None]
        partial_text_box = [""]

        def _on_progress(state):
            """Wrap ack.on_progress to also capture partial text for cancellation."""
            if state.get("text"):
                partial_text_box[0] = state["text"]
            ack.on_progress(state)

        def _run_pi():
            """Run pi in a background thread with history loading."""
            # Load history in thread — doesn't block the main event loop.
            # sdk.call is thread-safe (uses lock for stdin writes and unique IDs).
            conversation_history = None
            if first_message or not use_sessions:
                try:
                    hist = sdk.call(
                        "loadMessages",
                        {
                            "accountId": account_id,
                            "conversationId": conv.conv_id,
                            "count": args.history,
                        },
                    )
                    conversation_history = list(reversed(hist.get("messages", [])))
                except Exception:
                    conversation_history = None

            prompt = build_prompt(
                params, conversation_history, our_uri, known_senders, conv.member_count
            )

            pi_result_box[0] = call_pi(
                prompt,
                session_file=sfile,
                extra_args=pi_extra,
                on_progress=_on_progress,
                cancel=conv.cancel,
            )
            # Put result for the main loop to pick up
            pi_results.put((conv.conv_id, pi_result_box[0], ack, partial_text_box[0]))

        t = threading.Thread(target=_run_pi, daemon=True)
        t.start()

    def _send_reply(conv_id: str, reply: str, ack: AckManager):
        """Send a pi reply to a conversation (called from main loop)."""
        conv = conversations.get(conv_id)
        if not conv:
            return

        # Handle silent response
        if reply.strip() == SILENT_MARKER:
            bot_log("[bot] 🤫 pi chose to stay silent")
            ack.mark_done()
            conv.ack = None
            conv.busy = False
            conv.cancel = None
            conv.sender_uri = None
            return

        reply_preview = reply[:100] + ("..." if len(reply) > 100 else "")
        bot_log(f"[bot] 🤖 Reply for {_short_id(conv_id)}: {reply_preview}")

        try:
            sdk.call(
                "sendMessage",
                {
                    "accountId": account_id,
                    "conversationId": conv_id,
                    "body": reply,
                },
            )
            bot_log("[bot] ✅ Reply sent")
            ack.mark_done()
        except Exception as e:
            bot_warn(f"[bot] ❌ Failed to send reply: {e}")

        conv.ack = None
        conv.busy = False
        conv.cancel = None
        conv.sender_uri = None

    # ── Main event loop ────────────────────────────────────────────────
    try:
        while True:
            # ── Drain pi results first ──────────────────────────────────
            while not pi_results.empty():
                conv_id, reply, ack, _ = pi_results.get_nowait()
                conv = conversations.get(conv_id)
                if not conv:
                    continue
                if reply == CANCELLED_MARKER:
                    # Already handled when cancel.set() was called —
                    # conversation was freed immediately. Just discard.
                    continue
                if reply is None:
                    bot_warn(
                        f"[bot] ⚠️  pi thread returned no result for {_short_id(conv_id)}"
                    )
                    ack.mark_done()
                    conv.ack = None
                    conv.busy = False
                    conv.cancel = None
                    conv.sender_uri = None
                    continue
                _send_reply(conv_id, reply, ack)

            # ── Get next event ───────────────────────────────────────────
            event = sdk.get_notification(timeout=0.5)
            if not event:
                continue

            method = event.get("method", "")
            params = event.get("params", {})

            # ── Handle conversation ready (new/accepted conversation) ───
            if method == "onConversationReady":
                ready_conv_id = params.get("conversationId", "")
                if not ready_conv_id:
                    continue
                # Register if new
                if ready_conv_id not in conversations:
                    conv = _register_conversation(ready_conv_id)
                    bot_log(
                        f"[bot] 📨 New conversation ready: {_short_id(ready_conv_id)}... ({conv.member_count} members)"
                    )
                # Send greeting (once per conversation, when it's actually ready)
                if greeting_text and not conversations[ready_conv_id].greeted:
                    conversations[ready_conv_id].greeted = True
                    try:
                        sdk.call(
                            "sendMessage",
                            {
                                "accountId": account_id,
                                "conversationId": ready_conv_id,
                                "body": greeting_text,
                            },
                        )
                        bot_log(f"[bot] 👋 Greeting sent to {_short_id(ready_conv_id)}")
                    except Exception as e:
                        bot_warn(f"[bot] ⚠️  Greeting failed: {e}")
                continue

            # ── Handle member joins/leaves ───────────────────────────────
            if method == "onConversationMemberEvent":
                evt_conv_id = params.get("conversationId", "")
                member_uri = params.get("memberUri", "")
                evt_type = params.get("event", -1)
                # event: 0=add, 1=joins, 2=leave, 3=banned
                conv = conversations.get(evt_conv_id)
                if conv and member_uri:
                    if evt_type in (0, 1):
                        if member_uri not in known_senders:
                            known_senders[member_uri] = member_uri[-8:]
                        action = "joined" if evt_type == 1 else "added"
                        bot_log(
                            f"[bot] 👤 {known_senders[member_uri]} {action} in {_short_id(evt_conv_id)}"
                        )
                    elif evt_type == 2:
                        bot_log(
                            f"[bot] 👤 {known_senders.get(member_uri, member_uri[-8:])} left {_short_id(evt_conv_id)}"
                        )
                    elif evt_type == 3:
                        bot_log(
                            f"[bot] 👤 {known_senders.get(member_uri, member_uri[-8:])} banned from {_short_id(evt_conv_id)}"
                        )
                continue

            # ── Handle messages ───────────────────────────────────────────
            if method != "onMessageReceived":
                continue

            sender_uri = params.get("from", "")
            body = params.get("body", "").strip()
            msg_type = params.get("type", "")
            conv_id_event = params.get("conversationId", "")
            msg_id = params.get("id", "")
            parent_id = params.get("parentId", "")

            # Only process text messages
            if msg_type != "text/plain" or not body:
                continue

            # Track our own message IDs for reply detection (all conversations)
            if sender_uri == our_uri:
                conv = conversations.get(conv_id_event)
                if conv and msg_id:
                    conv.our_message_ids.add(msg_id)
                    # Capture ack message ID for progress updates
                    if (
                        body.startswith(ACK_PREFIX)
                        and conv.ack is not None
                        and conv.ack.ack_msg_id is None
                    ):
                        conv.ack.ack_msg_id = msg_id
                        bot_verbose(f"[bot] 📌 Captured ack ID: {msg_id}")
                continue

            # Skip ack/status messages
            if body.startswith(ACK_PREFIX):
                continue

            # Find (or auto-register) the conversation
            conv = conversations.get(conv_id_event)
            if conv is None:
                # Auto-register conversations we haven't seen yet
                # (e.g. 1:1 convs created while bot was running)
                conv = _register_conversation(conv_id_event)
                bot_log(
                    f"[bot] 📩 New conversation discovered: {_short_id(conv_id_event)}... ({conv.member_count} members)"
                )

            # ── Handle busy conversation ──────────────────────────────
            if conv.busy:
                # Anyone in the conversation can stop (not just the sender)
                if is_stop_command(body):
                    bot_log(
                        f"[bot] 🛑 Stop command in {_short_id(conv_id_event)}, "
                        "cancelling pi..."
                    )
                    conv.cancel.set()
                    # Mark ack as cancelled if we have the message ID
                    if conv.ack:
                        if conv.ack.ack_msg_id:
                            conv.ack.mark_cancelled()
                        conv.ack = None
                    # Release conversation immediately — don't wait for pi
                    # thread to finish. The CANCELLED_MARKER result will be
                    # silently discarded when the main loop drains pi_results.
                    conv.busy = False
                    conv.sender_uri = None
                    conv.cancel = None
                    # Confirm cancellation to the user
                    try:
                        sdk.call(
                            "sendMessage",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id_event,
                                "body": "🛑 Stopped.",
                            },
                        )
                    except Exception:
                        pass
                    continue

                # Other messages while busy: ask sender to retry later
                busy_sender = format_sender(sender_uri, known_senders)
                bot_log(
                    f"[bot] ⏳ Busy in {_short_id(conv_id_event)} — "
                    f"message from {busy_sender} rejected"
                )
                try:
                    sdk.call(
                        "sendMessage",
                        {
                            "accountId": account_id,
                            "conversationId": conv_id_event,
                            "body": (
                                "⏳ I'm still working on a request. "
                                "Send 'stop' to cancel, or resend later."
                            ),
                        },
                    )
                except Exception:
                    pass
                continue

            # ── Trigger gate ──────────────────────────────────────────
            decision = should_respond(
                body, trigger, bot_names, parent_id, conv.our_message_ids
            )
            # Note: trigger=smart returns "smart" (truthy) — an LLM
            # relevance check could be added here in the future.
            if not decision:
                bot_verbose(
                    f"[bot] 🔄 Skipping (trigger={trigger}, no mention/reply) in {_short_id(conv_id_event)}"
                )
                continue

            if args.dry_run:
                bot_log(
                    f"[bot] (dry-run) Would send to pi for {_short_id(conv_id_event)}"
                )
                continue

            # ── Start pi call ─────────────────────────────────────────
            _start_pi_for_conversation(conv, params)

    except KeyboardInterrupt:
        bot_log("\n[bot] Stopping...")
        # Cancel any running pi calls
        for conv in conversations.values():
            if conv.busy and conv.cancel:
                conv.cancel.set()
        sdk.stop()
        bot_log("[bot] Stopped.")


if __name__ == "__main__":
    main()
