#!/usr/bin/env python3
"""jami-bot-pi: Jami <-> pi chat bridge.

Connects a Jami conversation to a pi coding agent, forwarding messages
and streaming back progress updates using editable ack messages.

Usage:
    python3 bot.py --jami /path/to/jami-sdk --account <id-or-uri> [options]

See: python3 bot.py --help
"""

import argparse
import os
import sys
import threading


from jami_client import JamiStdioClient
from pi_client import call_pi
from config import (
    ACK_PREFIX,
    SILENT_MARKER,
    CANCELLED_MARKER,
    DEFAULT_SESSION_DIR,
    DEFAULT_HISTORY,
    load_system_prompt,
    session_path,
    is_new_session,
    is_stop_command,
)
from formatting import build_prompt, format_sender
from ack import AckManager


def main():
    parser = argparse.ArgumentParser(description="jami-bot-pi: Jami <-> pi chat bridge")
    parser.add_argument(
        "--jami",
        default=None,
        help="Path to jami-sdk binary (or set JAMI_SDK_PATH env)",
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
        "--system-prompt",
        default=None,
        help="System prompt for pi (default: system-prompt.md)",
    )
    parser.add_argument(
        "--no-session", action="store_true", help="Don't use pi sessions (stateless)"
    )
    parser.add_argument(
        "--no-ack", action="store_true", help="Don't send acknowledgment messages"
    )
    parser.add_argument(
        "--greeting",
        default="online",
        help='Send a greeting on startup: "online" (default), custom text, or "false" to disable',
    )
    parser.add_argument("--pi-args", default="", help="Extra pi args (space-separated)")
    parser.add_argument("--dry-run", action="store_true", help="Don't call pi")
    args = parser.parse_args()

    # Resolve jami-sdk binary path: --jami flag > JAMI_SDK_PATH env > PATH lookup
    jami_binary = args.jami or os.environ.get("JAMI_SDK_PATH") or "jami-sdk"

    system_prompt = load_system_prompt(args.system_prompt)
    pi_extra = args.pi_args.split() if args.pi_args else None
    use_sessions = not args.no_session
    os.makedirs(args.session_dir, exist_ok=True)

    # Track sender names for readable conversation formatting
    known_senders = {}

    # ── Launch jami-sdk in stdio mode ────────────────────────────────────
    sdk = JamiStdioClient(jami_binary=jami_binary)
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

    print(f"[bot] Account: {account_id}")
    print(f"[bot] Our URI: {our_uri}")
    print(f"[bot] Our alias: {our_alias}")

    # ── Discover conversation ─────────────────────────────────────────
    if args.conversation:
        conv_id = args.conversation
    else:
        convs = sdk.call("listConversations", {"accountId": account_id})
        convs = convs.get("conversations", [])
        if not convs:
            print("No conversations found. Create one first.")
            sys.exit(1)

        # Prefer conversation with >1 member
        multi = [c for c in convs if c.get("members", 1) > 1]
        if multi:
            conv_id = multi[0]["id"]
            conv_title = multi[0].get("title", "")
            print(
                f"[bot] Auto-selected: {conv_title or conv_id[:12]}... ({multi[0]['members']} members)"
            )
        else:
            conv_id = convs[0]["id"]
            print("[bot] Only 1-member conversations found, using first")

    # Register other members' names and get member count
    conv_detail = sdk.call(
        "getConversation", {"accountId": account_id, "conversationId": conv_id}
    )
    member_count = conv_detail.get("memberCount", 2)
    for member in conv_detail.get("members", []):
        uri = member.get("uri", "")
        if uri and uri not in known_senders:
            known_senders[uri] = uri[-8:] if len(uri) > 8 else uri

    print(f"[bot] Conversation: {conv_id} ({member_count} members)")
    if use_sessions:
        print(f"[bot] Session: {session_path(conv_id, args.session_dir)}")
    else:
        print("[bot] Sessions disabled (stateless mode)")
    print(f"[bot] History: {args.history} messages as context")
    print(f"[bot] Ack: {'disabled' if args.no_ack else 'enabled'}")

    # ── Send greeting ───────────────────────────────────────────────────
    greeting_text = None
    if args.greeting.lower() not in ("false", "no", "off", "0", "none"):
        greeting_text = args.greeting if args.greeting != "online" else "🟢 I'm online!"

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
            print("[bot] 👋 Greeting sent")
        except Exception as e:
            print(f"[bot] ⚠️  Greeting failed: {e}")

    print("[bot] Waiting for messages... (Ctrl+C to stop)")
    print()

    # ── Main event loop ────────────────────────────────────────────────
    try:
        while True:
            event = sdk.get_notification(timeout=1.0)

            if not event:
                continue

            method = event.get("method", "")
            params = event.get("params", {})

            if method == "onMessageReceived":
                sender_uri = params.get("from", "")
                body = params.get("body", "").strip()
                msg_type = params.get("type", "")
                conv_id_event = params.get("conversationId", "")

                # Only process text messages in our target conversation
                if msg_type != "text/plain" or not body or conv_id_event != conv_id:
                    continue

                # Skip our own messages
                if sender_uri == our_uri:
                    continue

                # Skip ack/status messages
                if body.startswith(ACK_PREFIX):
                    continue

                sender_name = format_sender(sender_uri, known_senders)
                print(f"[bot] 📨 From {sender_name}: {body}")

                if args.dry_run:
                    print("[bot] (dry-run) Would send to pi")
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
                    sp = system_prompt
                elif use_sessions:
                    # Continued session — pi already has context
                    prompt = build_prompt(
                        params, None, our_uri, known_senders, member_count
                    )
                    sp = None
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
                    sp = system_prompt

                # ── Call pi (threaded, cancellable) ──────────────────────
                print(
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
                        system_prompt=sp,
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
                        print("[bot] 🛑 Stop command received, cancelling pi...")
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
                    print(f"[bot] ⏳ Busy — message from {busy_sender} rejected")
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
                    print("[bot] ⚠️  pi thread returned no result")
                    ack.mark_done()
                    continue

                if reply == CANCELLED_MARKER:
                    # Already handled above, but just in case
                    continue

                reply_preview = reply[:100] + ("..." if len(reply) > 100 else "")
                print(f"[bot] 🤖 Reply: {reply_preview}")

                # ── Handle silent response ──────────────────────
                if reply.strip() == SILENT_MARKER:
                    print("[bot] 🤫 pi chose to stay silent")
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
                    print("[bot] ✅ Reply sent")
                    ack.mark_done()
                except Exception as e:
                    print(f"[bot] ❌ Failed to send reply: {e}")

    except KeyboardInterrupt:
        print("\n[bot] Stopping...")
        sdk.stop()
        print("[bot] Stopped.")


if __name__ == "__main__":
    main()
