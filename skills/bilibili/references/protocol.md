# Bilibili Protocol Reference

## Authentication Architecture

Bilibili uses HTTP Cookie-based session authentication with CSRF tokens for mutating actions.

### 1. Essential Credentials

| Cookie Name | Purpose | Required For |
|---|---|---|
| `SESSDATA` | Primary session token (HTTP-only) | All authenticated calls |
| `bili_jct` | CSRF token for mutating requests | Comments, likes, dynamics, uploads |
| `DedeUserID` | Account UID (decimal string) | Context and profile requests |
| `buvid3` | Device tracking identifier | Anti-bot verification |

### 2. Wbi Dynamic Sign

Modern Bilibili REST APIs require Wbi parameter signing:
- `w_rid`: MD5 hash of sorted query parameters salted with mixin key.
- `wts`: Current epoch timestamp in seconds.
- Subkey tables are dynamically resolved from `nav` API responses.

---

## Mutating API Endpoints (Write Actions)

### 1. Video Interactions
- **Like**: `POST https://api.bilibili.com/x/web-interface/archive/like`
- **Coin**: `POST https://api.bilibili.com/x/web-interface/coin/add`
- **Favorite**: `POST https://api.bilibili.com/x/v3/fav/resource/deal`
- **Triple Combo**: `POST https://api.bilibili.com/x/web-interface/archive/like/triple`
  - Parameters: `bvid`, `csrf=bili_jct`

### 2. Comments
- **Post Comment**: `POST https://api.bilibili.com/x/v2/reply/add`
  - Parameters: `oid` (aid), `type=1` (video), `message`, `csrf=bili_jct`
  - Floor replies require `root` (top-level rpid) and `parent` (immediate target rpid).

### 3. Dynamics
- **Create Text Dynamic**: `POST https://api.bilibili.com/x/dynamic/feed/create/dyn`
- **Delete Dynamic**: `POST https://api.bilibili.com/x/dynamic/feed/operate/remove`
  - Parameters: `dyn_id_str`, `csrf=bili_jct`

### 4. Private Messaging
- **Send Message**: `POST https://api.vc.bilibili.com/web_im/v1/web_im/send_msg`
  - Parameters: `msg[sender_uid]`, `msg[receiver_id]`, `msg[receiver_type]=1`, `msg[msg_type]=1`, `msg[content]={"content":"..."}`, `csrf=bili_jct`

---

## Common Error Codes

| Code | Meaning | Mitigation |
|---|---|---|
| `-101` | Account not logged in | Refresh session via `bilibili login --qr` |
| `-111` | CSRF token validation failed | Check `bili_jct` cookie freshness |
| `12002` | Video already liked / duplicate action | Ignore or check status first |
| `34005` | Comment text triggers keyword filter | Modify comment phrasing |
| `2100` | Account level insufficient for action | Check account level via `whoami` |
