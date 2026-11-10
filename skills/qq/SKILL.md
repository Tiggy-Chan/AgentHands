---
name: qq
description: Personal account action framework for QQ messaging via OneBot 11. Send private direct messages to friends, post messages to groups, recall sent messages, fetch group chat history, inspect friend/group lists, and perform group administration (mute/ban, kick members). Triggers: "qq", "QQ", "qq发消息", "qq群", "发qq", "qq禁言", "qq踢人", "qq好友".
---

# QQ Personal Account Action Framework

Operate your personal QQ account directly from AI agents via the OneBot 11 standard HTTP interface (supported by NapCat, LLOneBot, Lagrange).
No official enterprise robot approvals or sandboxed white-lists required.

## Key Capabilities

- **Direct Messaging**: Send private text and rich messages to friends.
- **Group Communication**: Send messages to groups, read recent group conversation history, recall messages.
- **Social Graph**: List friends and joined groups, inspect member lists with roles and titles.
- **Group Governance**: Mute/unmute members, kick disruptive members.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`send-private`, `send-group`, `delete`, `ban`, `kick`) are strictly protected against hallucinations:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without dispatching network actions.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any change to message text or targets invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized script, set `QQ_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/qq/scripts/qq.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `config --url URL [--token T]` | Configure OneBot HTTP API endpoint | Read |
| `whoami` | Inspect logged in QQ account details | Read |
| `friends` | List friends | Read |
| `groups` | List joined groups | Read |
| `members <group_id>` | List members in a group | Read |
| `history <group_id> [--limit N]` | Get group chat history | Read |
| `send-private <user_id> <msg>` | Send private message | Write (Dual-gate) |
| `send-group <group_id> <msg>` | Send message to group | Write (Dual-gate) |
| `delete <message_id>` | Recall a sent message | Write (Dual-gate) |
| `ban <group_id> <user_id> [--duration S]` | Mute member | Write (Dual-gate) |
| `kick <group_id> <user_id> [--reject-add]` | Kick member | Write (Dual-gate) |

---

## Usage Examples

### 1. Check Account Status
```bash
skills/qq/scripts/qq.py whoami --json
```

### 2. Send Group Message (Draft & Confirmation)
```bash
# Generate draft fingerprint
skills/qq/scripts/qq.py send-group 123456789 "Task completed successfully."

# Execute with confirmed fingerprint
skills/qq/scripts/qq.py send-group 123456789 "Task completed successfully." --execute --fingerprint 4a5b6c7d...
```

### 3. Send Private Message Directly
```bash
skills/qq/scripts/qq.py send-private 987654321 "Hello from AgentHands!" --direct
```
