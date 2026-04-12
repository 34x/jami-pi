# AGENTS.md — jami-bot Project Context

> This file provides project context for AI coding agents working on jami-bot.

## Project Overview

**jami-bot** is a Python chat bot that bridges Jami conversations to the pi
AI coding agent. It uses the `jami-sdk` binary as its messaging backend,
communicating via STDIO JSON-RPC.

## Architecture

```
Jami user ↔ jami-sdk (STDIO) ↔ bot.py ↔ pi
```

- The bot launches `jami-sdk --stdio` as a subprocess
- Communication via JSON-RPC 2.0 over stdin/stdout (newline-delimited JSON)
- Events (messages, invites, registration changes) are pushed from SDK to bot
- The bot calls pi CLI for AI responses and sends them back

## Dependencies

- **jami-sdk** binary — runtime dependency (not imported, just a subprocess)
  - Set path via `--jami PATH`, `JAMI_SDK_PATH` env var, or have it in PATH
- **pi** CLI — installed and configured for AI responses
- **python3** — stdlib only, no extra packages

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot script — STDIO client, pi integration, event loop |
| `README.md` | Usage docs, options, architecture |

## JSON-RPC Methods Used

| Method | Direction | Purpose |
|--------|-----------|---------|
| `listAccounts` | bot → SDK | Discover account ID |
| `getAccountDetails` | bot → SDK | Get our Jami URI, alias |
| `listConversations` | bot → SDK | Find conversations |
| `getConversation` | bot → SDK | Get members, mode |
| `sendMessage` | bot → SDK | Send reply to conversation |
| `loadMessages` | bot → SDK | Load recent history for context |
| `shutdown` | bot → SDK | Graceful shutdown |
| `onMessageReceived` | SDK → bot | New message notification |
| `onRegistrationChanged` | SDK → bot | Account status update |
| `onConversationRequestReceived` | SDK → bot | Incoming group invite |
| `onTrustRequestReceived` | SDK → bot | Incoming contact request |
| `sendFile` | bot → SDK | Send file to conversation |
| `downloadFile` | bot → SDK | Download file from conversation |
| `cancelTransfer` | bot → SDK | Cancel file transfer |
| `transferInfo` | bot → SDK | Get file transfer info |
| `onDataTransferEvent` | SDK → bot | File transfer status/progress |

## Key Bot Features

### Sessions & Conversation Memory

Each Jami conversation gets its own pi session file (in `--session-dir`).
On first message of a new session, recent Jami history is loaded as context.
Subsequent messages continue the existing session. pi auto-compacts long histories.

### Acknowledgment Messages

Bot sends "💭 received, thinking..." immediately. These are filtered from pi's
context using `ACK_PREFIX = "💭 "` so they don't pollute the LLM.

### Silence Mode

pi can respond with `__SILENT__` (exactly) to stay silent — no Jami message sent.
Useful in group chats where not every message needs a response.

### Sender Context

Messages include sender name (`[bob]: hello!`). The bot tracks known_senders
mapping URIs to short names.

## SDK Invite Policy

The bot relies on jami-sdk's invite policy flags when appropriate:
- `--auto-accept-from YOUR_URI` during setup (so only the owner can add the bot)
- `--reject-unknown` for production lockdown

The bot itself does NOT handle accept/decline — that's the SDK's job when
running in STDIO mode with policy flags set.

## Constraints

- **Never** use `rm -rf` with `-f` flag
- **Never** build/compile on host — only in podman containers
- **Never** use absolute paths — only relative
- Bot has no code imports from jami-sdk — it's a pure runtime dependency
- The bot is Python stdlib-only — no pip packages

## Relationship to jami-sdk

- **Runtime dependency only** — the bot needs the `jami-sdk` binary available
- **No code imports** — communication is via STDIO JSON-RPC
- **Contract**: JSON-RPC method names and notification formats (see jami-sdk AGENTS.md)
- **Version check**: `GET /api/version` or `listAccounts` + check version field