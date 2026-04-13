# jami-pi: Jami ↔ pi Chat Bridge

A community-made bridge between Jami and pi — **not an official project of either**.

- Jami: https://jami.net
- pi: https://pi.dev

## Quick Start

### 1. Install jami-bridge

Download the latest distribution tarball from
[jami-bridge releases](https://github.com/34x/jami-bridge/releases) and extract it:

```bash
tar xzf jami-bridge-dist.tar.gz
cd jami-bridge-dist
ls
# jami-bridge  lib/
```

The `jami-bridge` binary expects `./lib/` next to it (RPATH is `$ORIGIN/lib`).
No extra packages needed on Fedora 43+ — everything is bundled.

### 2. Install pi

Follow the instructions at [pi.dev](https://pi.dev) to install the pi CLI and
configure your LLM provider.

### 3. Run the bot

Point `--jami` at the bridge binary you just extracted:

```bash
python3 bot.py --jami /path/to/jami-bridge-dist/jami-bridge
```

On first run with a fresh profile the bot creates a new Jami account and
prints its identity:

```
[bot] Account: <account-id>
[bot] Our URI: <your-bot-uri>
[bot] Our alias: bot
[bot] Waiting for messages...
```

Copy the **Our URI** value — this is how you add the bot to a group.
Note: use just the key part (e.g. `abc123def456`), not the `jami://` prefix, when passing to `--auto-accept-from`.

Alternatively, you can add the bridge binary to your PATH or set the
`JAMI_BRIDGE_PATH` environment variable:

```bash
export JAMI_BRIDGE_PATH=/path/to/jami-bridge-dist/jami-bridge
python3 bot.py
```

### 4. Add the bot to a Jami group

1. Open the **Jami** app on your phone or desktop
2. Open the group conversation you want the bot in (or create a new one)
3. Open the conversation settings → **Add member**
4. Enter the bot's URI (the value from `Our URI` above)
5. The bot needs to accept the invite. Use `--bridge-args` to pass
   auto-accept flags to the bridge. The bot will automatically start
   monitoring the conversation once the invite is accepted.

**Option A — Quick setup (accept all invites, e.g. for testing):**

```bash
python3 bot.py --bridge-args '--auto-accept'
```

**Option B — Accept only from your URI (production):**

```bash
python3 bot.py --bridge-args '--auto-accept-from your-uri-here'
```

**Option C — Start the bridge separately (HTTP mode):**

```bash
# Start the bridge in HTTP mode with auto-accept
/path/to/jami-bridge-dist/jami-bridge --auto-accept --port 8090

# (From another terminal) Add the bot to your group in the Jami app.
# The bridge will accept the invite automatically.
# Once accepted, stop the bridge and start the bot:
python3 bot.py --jami /path/to/jami-bridge-dist/jami-bridge
```

> **Tip:** For production use, use `--auto-accept-from <your-uri>` to only accept
> invites from you, or `--reject-unknown` to block all new invites.
> These are passed via `--bridge-args`, e.g. `--bridge-args '--auto-accept-from abc123def456'`.

### 5. Chat with the bot

Send a message in the group — the bot will respond:

```
📨 From alice: What is 2+2?
🤖 Reply: 2 + 2 = 4
✅ Reply sent
```

---

## How It Works

```
Jami user ↔ jami-bridge (STDIO) ↔ bot.py ↔ pi
```

The bot launches `jami-bridge --stdio` as a subprocess and communicates via
JSON-RPC over stdin/stdout. Events (messages, registration changes) are
pushed from bridge to bot in real-time — no polling needed.

## Requirements

- **[jami-bridge](https://github.com/34x/jami-bridge)** binary ([download from releases](https://github.com/34x/jami-bridge/releases), in PATH, set `JAMI_BRIDGE_PATH` env, or specify with `--jami`)
- **[pi](https://pi.dev)** CLI installed and configured
- **python3** ≥3.9 (stdlib only — no extra packages). Works on Linux, macOS, and Windows.

This is an **unofficial community project**. It is not affiliated with, endorsed by, or officially connected to the Jami or pi projects.

Built with [pi.dev](https://pi.dev) and **GLM-5.1**.

## Usage

```bash
# Start the bot (auto-detects account and conversation)
python3 bot.py

# Show version
python3 bot.py --version

# Or specify account explicitly
python3 bot.py --account <account-id>

# Create account from archive, or create new + export
python3 bot.py --account /tmp/bot-account.gz

# List available accounts
python3 bot.py --list-accounts

# Set bot display name (pushed to contacts)
python3 bot.py --alias "MyBot"

# Register a public Jami username (one-shot)
python3 bot.py --register-name mybot

# Dry-run (don't call pi, just log messages)
python3 bot.py --dry-run

# Stateless mode (no pi sessions, each call is blank slate)
python3 bot.py --no-session

# Disable acknowledgment messages
python3 bot.py --no-ack

# Custom history context (default: 20 messages)
python3 bot.py --history 50

# Set pi timeout (default: 300 seconds, 0 = no timeout)
python3 bot.py --timeout 600

# Group chat: only respond when mentioned or replied to
python3 bot.py --trigger mention

# Custom greeting (default: "🟢 I'm online!")
python3 bot.py --greeting "Hello world"
# Disable greeting
python3 bot.py --greeting false

# Pass extra args to pi
python3 bot.py --pi-args "--model gpt-4o"

# Pass extra args to jami-bridge
python3 bot.py --bridge-args '--auto-accept'

# Show bridge stderr (daemon logs)
python3 bot.py --verbose
```

## Features

### Real-Time Events via STDIO

The bot launches `jami-bridge --stdio` and communicates via JSON-RPC:

- **Requests**: bot → bridge (stdin)
  ```json
  {"jsonrpc":"2.0","method":"sendMessage","params":{"accountId":"...","conversationId":"...","body":"hello"},"id":1}
  ```

- **Responses**: bridge → bot (stdout)
  ```json
  {"jsonrpc":"2.0","id":1,"result":{"sent":true,"conversationId":"..."}}
  ```

- **Events**: bridge → bot (stdout, pushed)
  ```json
  {"jsonrpc":"2.0","method":"onMessageReceived","params":{"accountId":"...","conversationId":"...","from":"...","body":"hi!"}}
  ```

### Sessions & Conversation Memory

By default, each Jami conversation gets its own **pi session file** stored in
`--session-dir` (default: system temp dir + `/jami-pi-sessions/`). pi maintains full
conversation context across calls and autocompacts long histories automatically.

On the first message of a new session, the bot loads recent Jami messages (up
to `--history` count) and injects them as context so pi knows what was discussed
before. Subsequent messages continue the existing session.

Use `--no-session` to disable sessions (each call is a blank slate — the bot
injects history into every prompt instead).

### Acknowledgment & Progress Messages

When `--no-ack` is not set, the bot sends an editable status message on
receipt. The message uses the `[bot:XXXX]` prefix (first 4 chars of the bot's
Jami URI) and is progressively updated with:

- **status**: `in progress` → `done` (or `cancelled`)
- **model**: which LLM is generating
- **tokens**: generation progress count
- **tools**: which tools pi is using, with ✓/⟳ status icons

Example ack message:
```
[bot:ab12]
status: in progress
model: claude-sonnet-4-20250514
tokens: 127
tool: read config.py ✓
tool: edit bot.py ⟳
```

These ack messages are **filtered from pi's context** (using the `[bot:` prefix)
so they don't pollute the LLM conversation.

### Trigger Modes

Control when the bot responds:

- **`all`** (default): respond to every non-self, non-ack message
- **`mention`**: respond only when the bot's name is mentioned or the message
  is a reply to one of the bot's messages
- **`smart`**: like mention, but may add an LLM relevance check (future)

Bot names are derived from the account alias and a short URI fragment.

### Stop Commands

While pi is processing a request, the same sender can cancel it by sending
a single-word message: `stop`, `abort`, `cancel`, or `kill`. On cancellation,
any partial response is sent with a `[cancelled]` suffix, and the ack message
shows `status: cancelled`.

### Busy-Reject

If pi is already processing a request, incoming messages from other senders
receive a busy reply: *"I'm still working on a request. Please resend your
message later."*

### Silence Mode

pi is instructed that it may choose not to respond. In group chats, it can
stay silent when a message doesn't require its input. To stay silent, pi
responds with `__SILENT__` (exactly) and no Jami message is sent. In 1:1 chats,
pi always responds.

### Conversation Context

Messages sent to pi include the sender's name (`[bob]: hello!`) and, for new
sessions, recent conversation history so pi understands who's talking. The bot
detects whether the conversation is 1:1 or a group chat and includes this in
the prompt so pi can adjust its behavior.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--jami PATH` | `$JAMI_BRIDGE_PATH` or `jami-bridge` | Path to jami-bridge binary |
| `--account SPEC` | auto-detect (all accounts) | Account: hex ID, `jami://URI`, `/path.gz` (import or create+export), `new` |
| `--alias NAME` | (none) | Set bot display name (pushed to contacts via updateProfile) |
| `--register-name NAME` | (none) | Register a public Jami username (one-shot, then exit) |
| `--list-accounts` | off | List accounts and exit |
| `--history N` | `20` | Recent messages to include as context |
| `--session-dir DIR` | system temp + `/jami-pi-sessions` | Directory for pi session files |
| `--no-session` | off | Disable pi sessions (stateless) |
| `--no-ack` | off | Disable acknowledgment messages |
| `--greeting TEXT` | `online` | Startup greeting: `online` sends "🟢 I'm online!", custom text, or `false` to disable |
| `--trigger MODE` | `all` | When to respond: `all`, `mention`, or `smart` |
| `--pi-args ARGS` | (none) | Extra arguments passed to pi CLI |
| `--bridge-args ARGS` | (none) | Extra arguments passed to jami-bridge (space-separated) |
| `--timeout N` | `300` | pi call timeout in seconds (0 = no timeout) |
| `--verbose` | off | Show bridge stderr output (daemon logs) |
| `--quiet` | off | Suppress all non-essential bot output |
| `--dry-run` | off | Log messages without calling pi |

## Protocol Details

### JSON-RPC Methods

The bot uses these JSON-RPC methods (same as HTTP API):

| Method | Params | Returns |
|--------|--------|---------|
| `ping` | - | `{status, version}` |
| `shutdown` | - | `{status}` |
| `listAccounts` | - | `{accounts: [id]}` |
| `getAccountDetails` | `{accountId}` | `{details: map}` |
| `listConversations` | `{accountId}` | `{conversations: [info]}` |
| `getConversation` | `{accountId, conversationId}` | `{info, members}` |
| `sendMessage` | `{accountId, conversationId, body}` | `{sent, conversationId}` |
| `editMessage` | `{accountId, conversationId, messageId, body}` | `{edited}` |
| `loadMessages` | `{accountId, conversationId, count?, from?}` | `{messages: [msg]}` |
| `registerName` | `{accountId, name}` | `{registered}` |
| `updateProfile` | `{accountId, displayName?}` | `{updated}` |

### Event Notifications

Pushed by bridge when events occur:

| Method | Params | Description |
|--------|--------|-------------|
| `onReady` | - | Bridge is ready to accept commands |
| `onMessageReceived` | `{accountId, conversationId, from, body, id, type, timestamp}` | New message in conversation |
| `onRegistrationChanged` | `{accountId, state, code, detail}` | Account registration status changed |
| `onConversationRequestReceived` | `{accountId, conversationId}` | New conversation invite received |
| `onConversationReady` | `{accountId, conversationId}` | Conversation finished loading |
| `onConversationMemberEvent` | `{accountId, conversationId, memberUri, event}` | Member added/removed |
| `onMessageStatusChanged` | `{accountId, conversationId, peer, messageId, state}` | Message delivery status changed |
| `onTrustRequestReceived` | `{accountId, from, conversationId}` | Incoming contact request |
| `onNameRegistrationEnded` | `{accountId, state, name}` | Name registration result |

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐     ┌─────┐
│  Jami user   │◄───►│  jami-bridge │     │                     │     │     │
│  (phone/PC)  │     │  (STDIO)     │     │    bot.py           │     │ pi  │
└─────────────┘     └──────┬───────┘     │  (JSON-RPC events)  │     └─────┘
                           │              │                     │
                    ┌──────▼───────┐     │                     │
                    │  subprocess  │────►│  subprocess (stdin)  │
                    │  (stdout)     │◄────│  stdout (push)       │
                    └──────────────┘     └─────────────────────┘
```

- **STDIO mode**: jami-bridge runs as a subprocess of bot.py
- **JSON-RPC**: Newline-delimited JSON on stdin/stdout
- **Event push**: Bridge sends notifications to bot in real-time
- **No polling**: Bot waits for events instead of polling every 2s

## Benefits over HTTP Mode

1. **No port conflicts** — no HTTP server means no port binding issues
2. **Simpler deployment** — one process (bot) launches bridge as a subprocess
3. **Real-time events** — no polling delay; events pushed instantly
4. **Cleaner shutdown** — bot stops bridge subprocess when exiting
5. **No CORS** — no HTTP means no cross-origin issues

## Example Session

```
14:46:00.123 [bot] ━━ Startup Config ━━
14:46:00.123 [bot]   Bridge binary:  ./jami-bridge
14:46:00.123 [bot]   Bridge args:    ['--stdio', '--auto-accept']
14:46:00.123 [bot]   Account spec:   (auto-detect)
14:46:00.123 [bot]   Trigger:        all
14:46:00.123 [bot]   Pi args:        --model gpt-4o
14:46:00.123 [bot]   Session dir:    /tmp/jami-pi-sessions (active)
14:46:00.123 [bot]   History:        20 messages
14:46:00.123 [bot]   Greeting:       online
14:46:00.123 [bot]   Ack:             enabled
14:46:00.123 [bot]   Dry run:        False
14:46:00.123 [bot] ━━━━━━━━━━━━━━━━━━━━
14:46:01.234 [bot] Account: <account-id> (uri: <your-jami-uri>, alias: MyBot)
14:46:01.234 [bot] Trigger: all (names: ['mybot', 'a1b2c3d4'])
14:46:01.234 [bot] Conversation: <conv-id> (2 members)
14:46:01.234 [bot] Monitoring 1 conversation(s)
14:46:01.234 [bot] 👋 Greeting sent to 1 conversation(s)
14:46:01.234 [bot] Ready. (Ctrl+C to stop)

14:46:05.567 [bot] 🤖 Calling pi (new session) for <conv-id>...
14:46:05.567 [bot] 🤖 Reply sent to <conv-id>
```

## License

This project is licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later). See [LICENSE](LICENSE) for details.