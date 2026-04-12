#!/usr/bin/env python3
"""jami-bot-pi: Jami ↔ pi chat bridge using JSON-RPC over STDIO.

Usage:
    python3 bot.py [options]

Options:
    --jami PATH        Path to jami-sdk binary (default: jami-sdk in PATH)
    --account ID       Jami account ID (default: auto-detect first account)
    --conversation ID  Conversation ID to monitor (default: auto-detect)
    --history N        Number of recent messages to include as context (default: 20)
    --session-dir DIR  pi session directory (default: /tmp/jami-bot-sessions)
    --system-prompt TEXT System prompt for pi (default: system-prompt.md)
    --no-session       Don't use pi sessions (stateless, each call is blank slate)
    --no-ack           Don't send "received" acknowledgment messages
    --greeting TEXT    Send greeting on startup (default: "online", "false" to disable, or custom text)
    --pi-args ARGS     Extra arguments passed to pi
    --dry-run          Don't actually call pi, just print what would be sent
    --help             Show this help

Architecture:

    Jami user ↔ jami-sdk (STDIO) ↔ bot.py ↔ pi

The bot launches jami-sdk --stdio as a subprocess and communicates
via JSON-RPC over stdin/stdout. Events (messages, registration changes)
are pushed from the SDK to the bot in real-time — no polling needed.

Benefits over HTTP mode:
- No port conflicts
- No HTTP server needed
- Real-time event push (no polling)
- Simpler deployment (one process)
"""

import sys
import os
import json
import time
import subprocess
import argparse
import threading
import queue
import fcntl

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# JSON-RPC Helpers
# ---------------------------------------------------------------------------


def jsonrpc_request(method, params=None, id=None):
    """Create a JSON-RPC 2.0 request."""
    req = {"jsonrpc": "2.0", "method": method}
    if params:
        req["params"] = params
    if id is not None:
        req["id"] = id
    return req


def is_response(obj):
    """Check if a JSON object is a response (has 'id' and 'result' or 'error')."""
    return "jsonrpc" in obj and obj.get("jsonrpc") == "2.0" and "id" in obj


def is_notification(obj):
    """Check if a JSON object is a notification (has 'method' but no 'id')."""
    return (
        "jsonrpc" in obj
        and obj.get("jsonrpc") == "2.0"
        and "method" in obj
        and "id" not in obj
    )


# ---------------------------------------------------------------------------
# Jami SDK STDIO Client
# ---------------------------------------------------------------------------


class JamiStdioClient:
    """JSON-RPC client that communicates with jami-sdk --stdio over stdin/stdout.

    A single reader thread reads JSON lines from stdout and dispatches:
    - Responses (with matching id) → unblock the pending call()
    - Notifications (events) → put on the notification queue for the main loop
    """

    def __init__(self, jami_binary="jami-sdk"):
        self.jami_binary = jami_binary
        self.proc = None
        self.next_id = 1
        self.pending = {}  # id -> threading.Event
        self.pending_results = {}  # id -> result or error
        self.lock = threading.Lock()
        self.notification_queue = queue.Queue()

    def start(self):
        """Start the jami-sdk --stdio subprocess."""
        if self.proc:
            return

        # Build environment: inherit from parent + add lib/ dir to LD_LIBRARY_PATH
        # for extra compatibility (if RPATH is not set, this is needed).
        env = os.environ.copy()
        binary_dir = os.path.dirname(os.path.abspath(self.jami_binary))
        lib_dir = os.path.join(binary_dir, "lib")
        if os.path.isdir(lib_dir):
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing}" if existing else lib_dir

        # Start jami-sdk in stdio mode (binary mode; we read with os.read for reliable pipe I/O)
        self.proc = subprocess.Popen(
            [self.jami_binary, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        # Single reader thread: non-blocking polling read.
        # NOTE: Do NOT use select.select() with this pipe — it stops reporting readiness
        # after the initial burst of data, causing the reader to miss onReady and all
        # subsequent responses. This appears to be a kernel pipe buffering quirk where
        # select doesn't wake up even though data is available. Non-blocking os.read()
        # with a short sleep on EAGAIN works reliably instead.
        def reader():
            fd = self.proc.stdout.fileno()
            # Set non-blocking
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            buf = ""
            while True:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    time.sleep(0.1)
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # skip non-JSON lines (e.g. pjlib init message)

                    if is_response(obj):
                        rid = obj.get("id")
                        with self.lock:
                            if rid in self.pending:
                                if "result" in obj:
                                    self.pending_results[rid] = obj["result"]
                                elif "error" in obj:
                                    self.pending_results[rid] = Exception(
                                        obj["error"].get("message", str(obj["error"]))
                                    )
                                self.pending[rid].set()
                            # else: response for unknown id, ignore
                    elif is_notification(obj):
                        self.notification_queue.put(obj)
                    # else: unknown JSON object, ignore

        threading.Thread(target=reader, daemon=True).start()

        # Wait for the SDK's onReady notification (sent when stdin loop starts)
        if not self._wait_ready(timeout=15):
            self.stop()
            raise Exception("jami-sdk did not become ready within timeout")

    def _wait_ready(self, timeout=15):
        """Wait for the SDK's onReady notification (sent when stdin loop starts)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.get_notification(timeout=min(2.0, deadline - time.time()))
            if event and event.get("method") == "onReady":
                return True
            # Discard other notifications during startup (e.g. onRegistrationChanged)
        return False

    def stop(self):
        """Stop the subprocess."""
        if self.proc:
            try:
                self.call("shutdown", {}, id=0)
            except Exception:
                pass
            self.proc.terminate()
            self.proc = None

    def call(self, method, params=None, id=None, timeout=10.0):
        """Send a JSON-RPC request and wait for the response.

        Returns the 'result' dict on success.
        Raises Exception on JSON-RPC error or timeout.
        """
        if id is None:
            id = self.next_id
            self.next_id += 1

        req = jsonrpc_request(method, params, id)
        req_json = json.dumps(req)

        event = threading.Event()
        with self.lock:
            self.pending[id] = event
            self.pending_results[id] = None

        # Write to stdin
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((req_json + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        else:
            raise Exception("jami-sdk subprocess not running")

        # Block until the reader thread dispatches our response
        if not event.wait(timeout=timeout):
            with self.lock:
                self.pending.pop(id, None)
                self.pending_results.pop(id, None)
            raise Exception(f"Timeout waiting for response to {method} (id={id})")

        with self.lock:
            result = self.pending_results.pop(id, None)
            self.pending.pop(id, None)

        if isinstance(result, Exception):
            raise result
        return result

    def get_notification(self, timeout=1.0):
        """Get the next event notification (if any).

        Returns the notification dict, or None on timeout.
        """
        try:
            return self.notification_queue.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "system-prompt.md"
)

DEFAULT_SYSTEM_PROMPT = None  # loaded lazily from system-prompt.md


def _load_system_prompt():
    """Load the system prompt from the bundled .md file."""
    try:
        with open(SYSTEM_PROMPT_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return (
            "You are a chat bot on Jami messenger. "
            "Respond when appropriate. "
            "To stay silent, respond with exactly: __SILENT__"
        )


SILENT_MARKER = "__SILENT__"

# Prefix for acknowledgment messages — used to filter them from pi context
ACK_PREFIX = "[bot"


def session_path(conv_id, session_dir):
    """Return the pi session file path for a conversation."""
    return os.path.join(session_dir, f"{conv_id}.json")


def is_new_session(session_file):
    """Check if this will be a new pi session (file doesn't exist yet)."""
    return not os.path.exists(session_file)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_sender(uri, known_senders):
    """Return a short name for a sender URI. Track names in known_senders dict."""
    if uri in known_senders:
        return known_senders[uri]
    # Use last 8 chars as short ID
    short = uri[-8:] if len(uri) > 8 else uri
    known_senders[uri] = short
    return short


def format_conversation_for_pi(messages, our_uri, known_senders):
    """Format recent Jami messages into a conversation transcript for pi.

    Filters out ack messages (💭 ...) so they don't pollute pi's context.
    """
    lines = []
    for msg in messages:
        sender_uri = msg.get("from", "")
        body = msg.get("body", "").strip()
        msg_type = msg.get("type", "")

        # Only include text messages
        if msg_type != "text/plain" or not body:
            continue

        # Filter out ack messages
        if body.startswith(ACK_PREFIX):
            continue

        sender = format_sender(sender_uri, known_senders)
        lines.append(f"[{sender}]: {body}")

    return "\n".join(lines)


def build_prompt(
    new_message, conversation_history, our_uri, known_senders, member_count=2
):
    """Build the prompt to send to pi."""
    sender_uri = new_message.get("from", "")
    body = new_message.get("body", "").strip()
    sender = format_sender(sender_uri, known_senders)

    context_lines = []
    if member_count == 2:
        context_lines.append("(1:1 chat)")
    else:
        context_lines.append(f"(group chat, {member_count} members)")

    if conversation_history:
        history_text = format_conversation_for_pi(
            conversation_history, our_uri, known_senders
        )
        context_lines.append(f"Recent conversation:\n{history_text}")
        context = "\n".join(context_lines)
        return f"{context}\n\nNew message from [{sender}]: {body}"
    else:
        return f"[{sender}]: {body}"


# ---------------------------------------------------------------------------
# pi CLI interface
# ---------------------------------------------------------------------------


def call_pi(
    prompt, session_file=None, system_prompt=None, extra_args=None, on_progress=None
):
    """Call pi in non-interactive JSON mode and return the assistant's reply text.

    on_progress: optional callback(state) called during streaming.
                 state is a dict with keys: tokens, text, tools, model.
                 tools is a list of (name, status) tuples.
    """
    cmd = ["pi", "--print", "--mode", "json"]

    if session_file:
        cmd.extend(["--session", session_file])
    else:
        cmd.append("--no-session")

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if extra_args:
        cmd.extend(extra_args)

    cmd.append(prompt)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
    except FileNotFoundError:
        return "[pi not found — is it installed?]"

    reply = None
    token_count = 0
    text_so_far = ""
    last_progress = 0
    model = ""
    tools = []  # list of (name, status) — status is "running" or "done"

    state = {"tokens": 0, "text": "", "tools": [], "model": ""}

    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        evt = event.get("assistantMessageEvent", {})

        # Capture model from message_start
        if etype == "message_start":
            msg = event.get("message", {})
            if not model and msg.get("model"):
                model = msg["model"]
                state["model"] = model

        # Track streaming text deltas
        if evt.get("type") == "text_delta":
            text_so_far += evt.get("delta", "")
            token_count += 1
            state["tokens"] = token_count
            state["text"] = text_so_far
        elif evt.get("type") == "text_start":
            text_so_far = ""
            token_count = 0
            state["tokens"] = 0
            state["text"] = ""

        # Track tool calls
        elif etype == "tool_execution_start":
            tools.append((event.get("toolName", "?"), "running"))
            state["tools"] = list(tools)
            state["force_update"] = True
            if on_progress:
                on_progress(state)
        elif etype == "tool_execution_end":
            name = event.get("toolName", "?")
            for i in range(len(tools) - 1, -1, -1):
                if tools[i][0] == name and tools[i][1] == "running":
                    tools[i] = (name, "done")
                    break
            state["tools"] = list(tools)
            state["force_update"] = True
            if on_progress:
                on_progress(state)

        # Report progress every ~50 tokens
        if on_progress and token_count > 0 and token_count - last_progress >= 50:
            state["tokens"] = token_count
            state.pop("force_update", None)
            on_progress(state)
            last_progress = token_count

        # Extract final reply from agent_end
        if etype == "agent_end":
            for msg in reversed(event.get("messages", [])):
                if msg.get("role") == "assistant":
                    for part in msg.get("content", []):
                        if part.get("type") == "text":
                            reply = part["text"]
                            break
                    break

    proc.wait()
    if proc.returncode != 0 and not reply:
        return f"[pi exited with code {proc.returncode}]"

    # Final progress report
    if on_progress:
        state["tokens"] = token_count
        state.pop("force_update", None)
        on_progress(state)

    return reply or "[pi returned no text response]"


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------


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
        default=20,
        help="Recent messages to include as context (default: 20)",
    )
    parser.add_argument(
        "--session-dir",
        default="/tmp/jami-bot-sessions",
        help="pi session directory (default: /tmp/jami-bot-sessions)",
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
        help='Send a greeting on startup to all conversations: "online" (default), custom text, or "false" to disable',
    )
    parser.add_argument("--pi-args", default="", help="Extra pi args (space-separated)")
    parser.add_argument("--dry-run", action="store_true", help="Don't call pi")
    args = parser.parse_args()

    # Resolve jami-sdk binary path: --jami flag > JAMI_SDK_PATH env > PATH lookup
    jami_binary = args.jami or os.environ.get("JAMI_SDK_PATH") or "jami-sdk"

    system_prompt = args.system_prompt or _load_system_prompt()
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
        # Check if it's a valid account ID first
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
        # Only greet the conversation we're actually monitoring
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
            # Get next event from SDK (blocking with timeout)
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

                # Skip ack messages
                if body.startswith(ACK_PREFIX):
                    continue

                sender_name = format_sender(sender_uri, known_senders)
                print(f"[bot] 📨 From {sender_name}: {body}")

                if args.dry_run:
                    print("[bot] (dry-run) Would send to pi")
                    continue

                # ── Send acknowledgment ───────────────────────────────
                ack_msg_id = None  # used by on_progress closure below
                if not args.no_ack:
                    try:
                        result = sdk.call(
                            "sendMessage",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "body": f"[bot:{our_uri[:4]}]\nstatus: in progress",
                            },
                        )
                        print("[bot] 📬 Ack sent")
                        # Wait briefly for our own message notification to get the ID
                        for _ in range(10):
                            evt = sdk.get_notification(timeout=0.5)
                            if evt and evt.get("method") == "onMessageReceived":
                                p = evt.get("params", {})
                                if p.get("from") == our_uri and p.get(
                                    "body", ""
                                ).startswith(ACK_PREFIX):
                                    ack_msg_id = p.get("id")
                                    break
                    except Exception as e:
                        print(f"[bot] ⚠️  Ack failed: {e}")

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

                # ── Call pi (with streaming progress) ───────────────────
                print(
                    f"[bot] 🤖 Calling pi ({'new' if first_message else 'continued' if use_sessions else 'history'} session)..."
                )

                last_edit_time = [0]  # throttle timestamp
                seen_model = [""]  # mutable closure for model name

                def on_progress(state):
                    """Edit ack message with streaming progress."""
                    import time as _time

                    # Tool events force immediate update; token updates throttled to 10s
                    force = state.pop("force_update", False)
                    now = _time.time()
                    if not force and now - last_edit_time[0] < 10:
                        return
                    last_edit_time[0] = now

                    if not ack_msg_id:
                        return

                    tokens = state.get("tokens", 0)
                    tools = state.get("tools", [])
                    model = state.get("model", "")
                    if model:
                        seen_model[0] = model

                    bot_id = our_uri[:4]
                    lines = [f"[bot:{bot_id}]", "status: in progress"]
                    if seen_model[0]:
                        lines.append(f"model: {seen_model[0]}")
                    if tokens:
                        lines.append(f"tokens: {tokens}")
                    for name, status in tools:
                        lines.append(f"tool: {name} ({status})")

                    body = ACK_PREFIX + "\n".join(lines)
                    try:
                        sdk.call(
                            "editMessage",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "body": body,
                                "messageId": ack_msg_id,
                            },
                        )
                    except Exception:
                        pass  # Best-effort progress update

                # Mark ack message as done after reply is sent
                def mark_done():
                    if not ack_msg_id:
                        return
                    bot_id = our_uri[:4]
                    lines = [f"[bot:{bot_id}]", "status: done"]
                    if seen_model[0]:
                        lines.append(f"model: {seen_model[0]}")
                    body = ACK_PREFIX + "\n".join(lines)
                    try:
                        sdk.call(
                            "editMessage",
                            {
                                "accountId": account_id,
                                "conversationId": conv_id,
                                "body": body,
                                "messageId": ack_msg_id,
                            },
                        )
                    except Exception:
                        pass

                reply = call_pi(
                    prompt,
                    session_file=sfile,
                    system_prompt=sp,
                    extra_args=pi_extra,
                    on_progress=on_progress,
                )
                reply_preview = reply[:100] + ("..." if len(reply) > 100 else "")
                print(f"[bot] 🤖 Reply: {reply_preview}")

                # ── Handle silent response ──────────────────────
                if reply.strip() == SILENT_MARKER:
                    print("[bot] 🤫 pi chose to stay silent")
                    mark_done()
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
                    mark_done()
                except Exception as e:
                    print(f"[bot] ❌ Failed to send reply: {e}")

    except KeyboardInterrupt:
        print("\n[bot] Stopping...")
        sdk.stop()
        print("[bot] Stopped.")


if __name__ == "__main__":
    main()
