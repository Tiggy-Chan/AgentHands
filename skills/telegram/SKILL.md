---
name: telegram
description: Personal account action framework for Telegram without developer sandbox restrictions. Send messages to users, groups, or channels, recall sent messages, inspect chat details, and fetch conversation history. Triggers: "telegram", "Telegram", "tg", "电报", "发telegram", "tg发消息", "telegram群".
---

# Telegram Personal Account Action Framework

Operate your Telegram account directly from AI agents via local HTTP bridge, MTProto user session, or gateway.
Bypasses typical Bot API restrictions (such as inability to message non-contacts or participate in private groups without admin invites).

## Key Capabilities

- **Direct & Group Messaging**: Send messages to private chats, groups, and channels.
- **Message Recall**: Delete sent messages.
- **History Retrieval**: Inspect recent chat timeline and conversation context.
- **Chat Intelligence**: View channel metadata, member counts, and descriptions.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`send`, `delete`) are strictly protected against hallucinations:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without dispatching network actions.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any change to message text or targets invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized workflow, set `TELEGRAM_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/telegram/scripts/telegram.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `config [--token T\|--bridge B\|--session S]` | Save credentials or bridge URL | Read |
| `whoami` | Inspect authenticated identity | Read |
| `chat <chat_id>` | Inspect chat or channel details | Read |
| `history <chat_id> [--limit N]` | Get chat message history | Read |
| `send <chat_id> <text> [--reply-to ID]` | Send message | Write (Dual-gate) |
| `delete <chat_id> <message_id>` | Recall a message | Write (Dual-gate) |

---

## Usage Examples

### 1. Check Identity
```bash
skills/telegram/scripts/telegram.py whoami --json
```

### 2. Send Message (Draft & Confirmation)
```bash
# Generate draft fingerprint
skills/telegram/scripts/telegram.py send @username "Analysis completed."

# Execute with confirmed fingerprint
skills/telegram/scripts/telegram.py send @username "Analysis completed." --execute --fingerprint 9a8b7c6d...
```

### 3. Send Message Directly
```bash
skills/telegram/scripts/telegram.py send -1001234567890 "Build status: green" --direct
```
