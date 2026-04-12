# AGENTS.md — jami-pi Project Context

> This file provides project context for AI coding agents working on jami-pi.

## Project Overview

**jami-pi** is a Python chat bot that bridges Jami conversations to the pi
AI coding agent. It uses the `jami-bridge` binary as its messaging backend,
communicating via STDIO JSON-RPC.

## Architecture

```
Jami user ↔ jami-bridge (STDIO) ↔ bot.py ↔ pi
```

- The bot launches `jami-bridge --stdio` as a subprocess
- Communication via JSON-RPC 2.0 over stdin/stdout (newline-delimited JSON)
- Events (messages, invites, registration changes) are pushed from bridge to bot
- The bot calls pi CLI for AI responses and sends them back

## Dependencies

- **jami-bridge** binary — runtime dependency (not imported, just a subprocess)
  - Set path via `--jami PATH`, `JAMI_BRIDGE_PATH` env var, or have it in PATH
- **pi** CLI — installed and configured for AI responses
- **python3** — stdlib only, no extra packages. Note: uses `fcntl` (Linux/macOS only)

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot script — CLI args, event loop, message routing |
| `config.py` | Constants, helpers, trigger logic, system prompt loader |
| `jami_client.py` | JSON-RPC 2.0 stdio client for jami-bridge |
| `pi_client.py` | pi CLI interface — call pi in JSON mode, stream progress |
| `formatting.py` | Message formatting — sender names, history, prompt building |
| `ack.py` | Ack/status message management — editable status messages in Jami |
| `system-prompt.md` | System prompt for pi (bundled, not configurable via CLI) |

## JSON-RPC Methods Used

| Method | Direction | Purpose |
|--------|-----------|---------|
| `listAccounts` | bot → bridge | Discover account ID |
| `getAccountDetails` | bot → bridge | Get our Jami URI, alias |
| `listConversations` | bot → bridge | Find conversations |
| `getConversation` | bot → bridge | Get members, mode |
| `sendMessage` | bot → bridge | Send reply to conversation |
| `editMessage` | bot → bridge | Edit ack/status message |
| `loadMessages` | bot → bridge | Load recent history for context |
| `shutdown` | bot → bridge | Graceful shutdown |
| `onReady` | bridge → bot | Bridge is ready |
| `onMessageReceived` | bridge → bot | New message notification |
| `onRegistrationChanged` | bridge → bot | Account status update |
| `onConversationRequestReceived` | bridge → bot | Incoming group invite |

## Key Bot Features

### Sessions & Conversation Memory

Each Jami conversation gets its own pi session file (in `--session-dir`).
On first message of a new session, recent Jami history is loaded as context.
Subsequent messages continue the existing session. pi auto-compacts long histories.

### Acknowledgment Messages

Bot sends an editable status message immediately on receipt (`[bot:XXXX]`
prefix with status, model, token count, and tool progress). These ack messages
are filtered from pi's context using `ACK_PREFIX`.

### Trigger Modes

- `--trigger all` (default): respond to every non-self message
- `--trigger mention`: respond only when bot name is mentioned or message is a reply to bot
- `--trigger smart`: like mention, but may add LLM relevance check in the future

Bot names are derived from the account alias and a short URI fragment.

### Silence Mode

pi can respond with `__SILENT__` (exactly) to stay silent — no Jami message sent.
Useful in group chats where not every message needs a response.

### Stop Commands

Sending a single stop word (`stop`, `abort`, `cancel`, `kill`) cancels a running
pi task. The same sender who triggered the task can cancel it.

### Busy-Reject

If pi is already processing a request, new messages from other senders get a
busy reply asking them to resend later.

### Sender Context

Messages include sender name (`[bob]: hello!`). The bot tracks known_senders
mapping URIs to short names.

## Bridge Invite Policy

The bot relies on jami-bridge's invite policy flags when appropriate:
- `--auto-accept-from YOUR_URI` during setup (so only the owner can add the bot)
- `--reject-unknown` for production lockdown

The bot itself does NOT handle accept/decline — that's the bridge's job when
running in STDIO mode with policy flags set.

## Constraints

- **Never** use `rm -rf` with `-f` flag
- **Never** build/compile on host — only in podman containers
- **Never** use absolute paths — only relative
- Bot has no code imports from jami-bridge — it's a pure runtime dependency
- The bot is Python stdlib-only — no pip packages

## Relationship to jami-bridge

- **Runtime dependency only** — the bot needs the `jami-bridge` binary available
- **No code imports** — communication is via STDIO JSON-RPC
- **Contract**: JSON-RPC method names and notification formats (see jami-bridge AGENTS.md)