# jami-pi: Jami ↔ pi Chat Bridge

A bot that bridges Jami conversations to the pi AI coding agent.

## How It Works

```
Jami user ↔ jami-bridge (STDIO) ↔ bot.py ↔ pi
```

The bot launches `jami-bridge --stdio` as a subprocess and communicates via
JSON-RPC over stdin/stdout. Events (messages, registration changes) are
pushed from bridge to bot in real-time — no polling needed.

## Requirements

- **jami-bridge** binary (in PATH, set `JAMI_BRIDGE_PATH` env, or specify with `--jami`)
- **pi** CLI installed and configured
- **python3** ≥3.9 (stdlib only — no extra packages). **Linux/macOS required** (uses `fcntl`).

## Usage

```bash
# Start the bot (auto-detects account and conversation)
python3 bot.py

# Or specify explicitly
python3 bot.py \
  --account <account-id> \
  --conversation <conversation-id>

# List available accounts
python3 bot.py --list-accounts

# Dry-run (don't call pi, just log messages)
python3 bot.py --dry-run

# Stateless mode (no pi sessions, each call is blank slate)
python3 bot.py --no-session

# Disable acknowledgment messages
python3 bot.py --no-ack

# Custom history context (default: 20 messages)
python3 bot.py --history 50

# Group chat: only respond when mentioned or replied to
python3 bot.py --trigger mention

# Custom greeting (default: "🟢 I'm online!")
python3 bot.py --greeting "Hello world"
# Disable greeting
python3 bot.py --greeting false

# Pass extra args to pi
python3 bot.py --pi-args "--model gpt-4o"
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
`--session-dir` (default: `/tmp/jami-bot-sessions/`). pi maintains full
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
| `--account ID` | auto-detect | Account ID or URI |
| `--list-accounts` | off | List accounts and exit |
| `--conversation ID` | auto-detect | Conversation to monitor |
| `--history N` | `20` | Recent messages to include as context |
| `--session-dir DIR` | `/tmp/jami-bot-sessions` | Directory for pi session files |
| `--no-session` | off | Disable pi sessions (stateless) |
| `--no-ack` | off | Disable acknowledgment messages |
| `--greeting TEXT` | `online` | Startup greeting: `online` sends "🟢 I'm online!", custom text, or `false` to disable |
| `--trigger MODE` | `all` | When to respond: `all`, `mention`, or `smart` |
| `--pi-args ARGS` | (none) | Extra arguments passed to pi CLI |
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

### Event Notifications

Pushed by bridge when events occur:

| Method | Params | Description |
|--------|--------|-------------|
| `onReady` | - | Bridge is ready to accept commands |
| `onMessageReceived` | `{accountId, conversationId, from, body, id, type, timestamp}` | New message in conversation |
| `onRegistrationChanged` | `{accountId, state, code, detail}` | Account registration status changed |
| `onConversationRequestReceived` | `{accountId, conversationId}` | New conversation invite received |

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
[bot] Account: <account-id>
[bot] Our URI: <your-jami-uri>
[bot] Our alias: MyBot
[bot] Trigger: all (names: ['mybot', 'a1b2c3d4'])
[bot] Conversation: <conversation-id> (2 members)
[bot] Session: /tmp/jami-bot-sessions/<conversation-id>.json
[bot] History: 20 messages as context
[bot] Ack: enabled
[bot] Waiting for messages... (Ctrl+C to stop)

[bot] 📨 From <sender>: hello!
[bot] 📬 Ack sent
[bot] 🤖 Calling pi (new session)...
[bot] 🤖 Reply: Hi there! How can I assist you today?
[bot] ✅ Reply sent
```

## License

This project is licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later). See [LICENSE](LICENSE) for details.