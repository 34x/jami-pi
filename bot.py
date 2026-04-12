#!/usr/bin/env python3
"""jami-pi: Jami <-> pi chat bridge.

Connects a Jami conversation to a pi coding agent, forwarding messages
and streaming back progress updates using editable ack messages.

See: python3 bot.py --help
"""

import argparse
import os
import sys
import threading

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
    parser.add_argument("--conversation", default=None, help="Conversation to monitor")
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
    parser.add_argument("--pi-args", default="", help="Extra pi args (space-separated)")
    parser.add_argument(
        "--bridge-args",
        default="",
        help="Extra bridge args (space-separated, e.g. '--auto-accept-from jami://abc')",
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

    pi_extra = args.pi_args.split() if args.pi_args else None
    use_sessions = not args.no_session
    os.makedirs(args.session_dir, exist_ok=True)

    # Track sender names for readable conversation formatting
    known_senders = {}

    # Resolve bridge args: --bridge-args string (space-split, like --pi-args)
    bridge_args = args.bridge_args.split() if args.bridge_args else []

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

    # Register our own name in the known senders
    known_senders[our_uri] = our_alias or "bot"

    # ── Build trigger names from alias + URI fragment ────────────────
    bot_names = []
    if our_alias:
        bot_names.append(our_alias.lower())
    # Add short URI fragment (last 8 chars) as a fallback name
    uri_short = our_uri.rsplit(":", 1)[-1][-8:] if ":" in our_uri else our_uri[-8:]
    if uri_short and uri_short not in bot_names:
        bot_names.append(uri_short.lower())
    # Track message IDs we send, for reply-to-bot detection
    our_message_ids = set()

    trigger = args.trigger
    bot_log(f"[bot] Account: {account_id}")
    bot_log(f"[bot] Our URI: {our_uri}")
    bot_log(f"[bot] Our alias: {our_alias}")
    bot_log(f"[bot] Trigger: {trigger} (names: {bot_names})")

    # ── Discover conversation ─────────────────────────────────────────
    has_auto_accept = any(
        a in ("--auto-accept", "--auto-accept-from") for a in bridge_args
    )

    if args.conversation:
        conv_id = args.conversation
    else:
        convs = sdk.call("listConversations", {"accountId": account_id})
        convs = convs.get("conversations", [])

        if not convs:
            if has_auto_accept:
                conv_id = None
                bot_log(
                    "[bot] No conversations yet — waiting for auto-accepted invite..."
                )
            else:
                print(
                    "No conversations found. Create one first, or use --bridge-args '--auto-accept' to auto-accept invites."
                )
                sys.exit(1)
        else:
            # Prefer conversation with >1 member
            multi = [c for c in convs if c.get("members", 1) > 1]
            if multi:
                conv_id = multi[0]["id"]
                conv_title = multi[0].get("title", "")
                bot_log(
                    f"[bot] Auto-selected: {conv_title or conv_id[:12]}... ({multi[0]['members']} members)"
                )
            else:
                conv_id = convs[0]["id"]
                bot_log("[bot] Only 1-member conversations found, using first")

    # Register other members' names and get member count
    if conv_id:
        conv_detail = sdk.call(
            "getConversation", {"accountId": account_id, "conversationId": conv_id}
        )
        member_count = conv_detail.get("memberCount", 2)
        for member in conv_detail.get("members", []):
            uri = member.get("uri", "")
            if uri and uri not in known_senders:
                known_senders[uri] = uri[-8:] if len(uri) > 8 else uri
    else:
        member_count = 0

    bot_log(
        f"[bot] Conversation: {conv_id or '(waiting for invite)'} ({member_count} members)"
    )
    if use_sessions:
        bot_log(f"[bot] Session: {session_path(conv_id, args.session_dir)}")
    else:
        bot_log("[bot] Sessions disabled (stateless mode)")
    bot_log(f"[bot] History: {args.history} messages as context")
    bot_log(f"[bot] Ack: {'disabled' if args.no_ack else 'enabled'}")

    # ── Send greeting ───────────────────────────────────────────────────
    greeting_text = None
    if args.greeting.lower() not in ("false", "no", "off", "0", "none"):
        greeting_text = args.greeting if args.greeting != "online" else "🟢 I'm online!"

    if greeting_text and conv_id:
        try:
            sdk.call(
                "sendMessage",
                {
                    "accountId": account_id,
                    "conversationId": conv_id,
                    "body": greeting_text,
                },
            )
            bot_log("[bot] 👋 Greeting sent")
        except Exception as e:
            bot_warn(f"[bot] ⚠️  Greeting failed: {e}")

    bot_log("[bot] Waiting for messages... (Ctrl+C to stop)")
    print()

    # ── Main event loop ────────────────────────────────────────────────
    def _on_new_conversation(new_conv_id: str):
        """Called when a conversation becomes ready (e.g. after auto-accept).

        Always registers the new conversation's members.
        Switches target conversation only if we don't have one yet.
        """
        nonlocal conv_id, member_count

        # Discover members of the new conversation
        try:
            conv_detail = sdk.call(
                "getConversation",
                {"accountId": account_id, "conversationId": new_conv_id},
            )
            new_member_count = conv_detail.get("memberCount", 2)
            for member in conv_detail.get("members", []):
                uri = member.get("uri", "")
                if uri and uri not in known_senders:
                    known_senders[uri] = uri[-8:] if len(uri) > 8 else uri
        except Exception as e:
            bot_warn(f"[bot] ⚠️  Failed to load conversation {new_conv_id}: {e}")
            new_member_count = 0

        if conv_id is not None:
            # Already monitoring a conversation — log but don't switch
            bot_log(
                f"[bot] 📨 New conversation ready: {new_conv_id[:12]}... ({new_member_count} members), staying on {conv_id[:12]}..."
            )
            return

        # No target yet — switch to this conversation
        conv_id = new_conv_id
        member_count = new_member_count
        bot_log(
            f"[bot] 📨 New conversation accepted: {conv_id} ({member_count} members)"
        )

        # Send greeting if configured
        if greeting_text:
            try:
                sdk.call(
                    "sendMessage",
                    {
                        "accountId": account_id,
                        "conversationId": conv_id,
                        "body": greeting_text,
                    },
                )
                bot_log("[bot] 👋 Greeting sent")
            except Exception as e:
                bot_warn(f"[bot] ⚠️  Greeting failed: {e}")

    try:
        while True:
            event = sdk.get_notification(timeout=1.0)

            if not event:
                continue

            method = event.get("method", "")
            params = event.get("params", {})

            # ── Handle conversation ready (new/accepted conversation) ───
            if method == "onConversationReady":
                ready_conv_id = params.get("conversationId", "")
                if ready_conv_id:
                    _on_new_conversation(ready_conv_id)
                continue

            # ── Handle member joins/leaves ───────────────────────────────
            if method == "onConversationMemberEvent":
                evt_conv_id = params.get("conversationId", "")
                member_uri = params.get("memberUri", "")
                evt_type = params.get("event", -1)
                # event: 0=add, 1=joins, 2=leave, 3=banned
                if evt_conv_id == conv_id and member_uri:
                    if evt_type in (0, 1):
                        # Added or joined — register sender name
                        if member_uri not in known_senders:
                            known_senders[member_uri] = member_uri[-8:]
                        action = "joined" if evt_type == 1 else "added"
                        bot_log(f"[bot] 👤 {known_senders[member_uri]} {action}")
                    elif evt_type == 2:
                        bot_log(
                            f"[bot] 👤 {known_senders.get(member_uri, member_uri[-8:])} left"
                        )
                    elif evt_type == 3:
                        bot_log(
                            f"[bot] 👤 {known_senders.get(member_uri, member_uri[-8:])} banned"
                        )
                continue

            # ── Skip message events if we have no target conversation ──
            if conv_id is None:
                continue

            if method == "onMessageReceived":
                sender_uri = params.get("from", "")
                body = params.get("body", "").strip()
                msg_type = params.get("type", "")
                conv_id_event = params.get("conversationId", "")
                msg_id = params.get("id", "")
                parent_id = params.get("parentId", "")

                # Only process text messages in our target conversation
                if msg_type != "text/plain" or not body or conv_id_event != conv_id:
                    continue

                # Skip our own messages — but track message IDs for reply detection
                if sender_uri == our_uri:
                    if msg_id:
                        our_message_ids.add(msg_id)
                    continue

                # Skip ack/status messages
                if body.startswith(ACK_PREFIX):
                    continue

                # ── Trigger gate ────────────────────────────────────
                decision = should_respond(
                    body, trigger, bot_names, parent_id, our_message_ids
                )
                if not decision:
                    bot_log(f"[bot] 🔄 Skipping (trigger={trigger}, no mention/reply)")
                    continue
                # (Future: add a lightweight pi call here to check relevance)

                if args.dry_run:
                    bot_log("[bot] (dry-run) Would send to pi")
                    continue

                # ── Send acknowledgment ───────────────────────────────
                ack = AckManager(sdk, account_id, conv_id, our_uri, no_ack=args.no_ack)
                ack.send_initial()

                # ── Prepare prompt with conversation context ──────────
                sfile = (
                    session_path(conv_id, args.session_dir) if use_sessions else None
                )
                first_message = sfile and is_new_session(sfile)

                if first_message:
                    # New session — load recent conversation history
                    try:
                        hist = sdk.call(
                            "loadMessages",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "count": args.history,
                            },
                        )
                        conversation_history = list(reversed(hist.get("messages", [])))
                    except Exception:
                        conversation_history = None
                    prompt = build_prompt(
                        params,
                        conversation_history,
                        our_uri,
                        known_senders,
                        member_count,
                    )
                elif use_sessions:
                    # Continued session — pi already has context
                    prompt = build_prompt(
                        params, None, our_uri, known_senders, member_count
                    )
                else:
                    # No session — include history every time
                    try:
                        hist = sdk.call(
                            "loadMessages",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "count": args.history,
                            },
                        )
                        conversation_history = list(reversed(hist.get("messages", [])))
                    except Exception:
                        conversation_history = None
                    prompt = build_prompt(
                        params,
                        conversation_history,
                        our_uri,
                        known_senders,
                        member_count,
                    )

                # ── Call pi (threaded, cancellable) ──────────────────────
                bot_log(
                    f"[bot] 🤖 Calling pi ({'new' if first_message else 'continued' if use_sessions else 'history'} session)..."
                )

                cancel = threading.Event()
                pi_result = [None]  # mutable container for thread return value
                partial_text = [""]  # track streaming text for cancel case

                def _on_progress(state):
                    """Wrap ack.on_progress to also capture partial text."""
                    if state.get("text"):
                        partial_text[0] = state["text"]
                    ack.on_progress(state)

                def _run_pi():
                    pi_result[0] = call_pi(
                        prompt,
                        session_file=sfile,
                        extra_args=pi_extra,
                        on_progress=_on_progress,
                        cancel=cancel,
                    )

                pi_thread = threading.Thread(target=_run_pi, daemon=True)
                pi_thread.start()

                # Poll for stop commands / busy-reject other messages while pi runs
                while pi_thread.is_alive():
                    evt = sdk.get_notification(timeout=0.5)
                    if not evt or evt.get("method") != "onMessageReceived":
                        continue
                    p = evt.get("params", {})
                    evt_body = p.get("body", "").strip()
                    evt_conv = p.get("conversationId", "")
                    evt_sender = p.get("from", "")
                    evt_type = p.get("type", "")

                    # Only process text messages in our target conversation
                    if evt_type != "text/plain" or evt_conv != conv_id:
                        continue
                    # Skip our own and ack messages
                    if evt_sender == our_uri or evt_body.startswith(ACK_PREFIX):
                        continue

                    # Same sender: check for stop command
                    if evt_sender == sender_uri and is_stop_command(evt_body):
                        bot_log("[bot] 🛑 Stop command received, cancelling pi...")
                        cancel.set()
                        pi_thread.join(timeout=5)
                        if partial_text[0].strip():
                            try:
                                sdk.call(
                                    "sendMessage",
                                    {
                                        "accountId": account_id,
                                        "conversationId": conv_id,
                                        "body": partial_text[0] + "\n[cancelled]",
                                    },
                                )
                            except Exception:
                                pass
                        ack.mark_cancelled()
                        break

                    # Any other message while busy: ask sender to retry later
                    busy_sender = format_sender(evt_sender, known_senders)
                    bot_log(f"[bot] ⏳ Busy — message from {busy_sender} rejected")
                    try:
                        sdk.call(
                            "sendMessage",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "body": "⏳ I'm still working on a request. Please resend your message later.",
                            },
                        )
                    except Exception:
                        pass

                if not cancel.is_set():
                    pi_thread.join()

                reply = pi_result[0]
                if reply is None:
                    # Thread didn't complete — shouldn't happen
                    bot_warn("[bot] ⚠️  pi thread returned no result")
                    ack.mark_done()
                    continue

                if reply == CANCELLED_MARKER:
                    # Already handled above, but just in case
                    continue

                reply_preview = reply[:100] + ("..." if len(reply) > 100 else "")
                bot_log(f"[bot] 🤖 Reply: {reply_preview}")

                # ── Handle silent response ──────────────────────
                if reply.strip() == SILENT_MARKER:
                    bot_log("[bot] 🤫 pi chose to stay silent")
                    ack.mark_done()
                    continue

                # ── Send reply ────────────────────────────────────────
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

    except KeyboardInterrupt:
        bot_log("\n[bot] Stopping...")
        sdk.stop()
        bot_log("[bot] Stopped.")


if __name__ == "__main__":
    main()
