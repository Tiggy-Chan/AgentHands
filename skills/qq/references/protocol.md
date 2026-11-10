# OneBot 11 Protocol Reference for QQ

## Overview

QQ personal messaging automation is achieved through the OneBot 11 protocol specification implemented by backends such as NapCat, LLOneBot, or Lagrange.
The framework connects directly to the HTTP API exposed by the local or remote OneBot server.

---

## OneBot 11 HTTP Endpoints Reference

### 1. Messaging Endpoints
- `POST /send_private_msg`: Send direct message to a user.
  - Parameters: `user_id` (int), `message` (string or array of segments), `auto_escape` (bool).
- `POST /send_group_msg`: Send message to a group.
  - Parameters: `group_id` (int), `message` (string or array of segments), `auto_escape` (bool).
- `POST /delete_msg`: Recall sent message by ID.
  - Parameters: `message_id` (int).
- `POST /get_group_msg_history`: Retrieve historical messages from group.
  - Parameters: `group_id` (int), `count` (int).

### 2. Group Management Endpoints
- `POST /get_group_list`: List joined groups.
- `POST /get_group_info`: Retrieve group details.
- `POST /get_group_member_list`: List all members in a group.
- `POST /set_group_ban`: Mute member for specified duration.
  - Parameters: `group_id` (int), `user_id` (int), `duration` (seconds, 0 to unmute).
- `POST /set_group_kick`: Kick member from group.
  - Parameters: `group_id` (int), `user_id` (int), `reject_add_request` (bool).

### 3. Account & Friends
- `POST /get_login_info`: Get currently authenticated QQ number and nickname.
- `POST /get_friend_list`: Get friend list.

---

## Safety Architecture

- All mutating actions (`send_private_msg`, `send_group_msg`, `delete_msg`, `set_group_ban`, `set_group_kick`) are subject to the `QQ_WRITE_ALLOWED` environment gate and SHA256 draft fingerprint validation.
- Sensitive tokens and credentials configured via `QQ_ACCESS_TOKEN` are scrubbed from outputs and exception logs.
