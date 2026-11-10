# Telegram Protocol Reference

## Overview

Telegram supports two principal interaction modes for personal account actions without official bot sandbox restrictions:
1. **MTProto Userbot Protocol** (Telethon / Pyrogram / TDLib) - Acts directly as the user's authentic account identity.
2. **Local HTTP Bridge** (TDLib JSON HTTP server or local gateway) - Dispatches commands to an authenticated session running locally.

---

## Action Endpoints Reference

### 1. Messaging
- `sendMessage`: Dispatch message to user ID, chat ID, or public `@username`.
  - Parameters: `chat_id`, `text`, `reply_to_message_id`.
- `deleteMessage`: Revoke message from target chat.
  - Parameters: `chat_id`, `message_id`.
- `getHistory`: Retrieve chat timeline messages.
  - Parameters: `chat_id`, `limit`.

### 2. Chat & Profile
- `getChat`: Inspect chat/channel title, description, and type.
- `getMe`: Inspect current authenticated user or bot ID.

---

## Safety Architecture

- All mutating actions (`sendMessage`, `deleteMessage`) require explicit confirmation via `TELEGRAM_WRITE_ALLOWED` and SHA-256 fingerprint verification.
- Tokens, API hashes, and session strings are registered with the secret scrubber and redacted from outputs.
