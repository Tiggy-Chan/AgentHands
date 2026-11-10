---
name: bilibili
description: Personal account action framework for Bilibili without developer APIs. Read videos, user profiles, comments, dynamics, and private message history; perform safe, dual-gated write operations including video interaction (like, coin, favorite, triple combo), posting comments, creating dynamic updates, sending private messages, and uploading videos. Triggers: "bilibili", "B站", "b站私信", "b站评论", "一键三连", "发动态", "B站投稿", "查B站视频".
---

# Bilibili Personal Account Action Framework

Take full control of your personal Bilibili account via authenticated browser sessions or cookie credentials.
No official developer enterprise application approval or API keys required.

## Key Capabilities

- **Account & Relations**: Inspect self/user profile, follow/unfollow creators.
- **Video Interaction**: View metrics, like, coin, favorite, or trigger one-click triple combos.
- **Comments**: Read hierarchical comment threads, post top-level comments or reply to specific floors.
- **Dynamic Feeds**: Read following feeds, post text/picture dynamics, delete posts.
- **Private Messaging**: Track conversation sessions, retrieve message history, send direct messages.
- **Video Upload**: Submit video files with full metadata, categories, tags, and custom cover images.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`interact`, `comment`, `dynamic post/delete`, `messages send`, `upload`, `follow/unfollow`) are strictly protected against LLM hallucination:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without modifying the account state.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any difference in text or parameters invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized environment or script, set `BILIBILI_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/bilibili/scripts/bilibili.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login [--qr\|--check]` | QR code scan login or credential validation | Read |
| `whoami` | Inspect current logged-in account details | Read |
| `user <uid>` | Retrieve public user information | Read |
| `video <bvid>` | Get video information and performance stats | Read |
| `comments <bvid>` | List comments and floor replies | Read |
| `interact <bvid> [--like\|--coin N\|--fav\|--triple]` | Like, coin, favorite, or triple combo | Write (Dual-gate) |
| `comment <bvid> <msg> [--root RPID] [--parent RPID]` | Post comment or floor reply | Write (Dual-gate) |
| `dynamic list [--uid UID]` | Fetch dynamic posts | Read |
| `dynamic post <text> [--images IMG...]` | Post text or picture dynamic update | Write (Dual-gate) |
| `dynamic delete <id>` | Delete dynamic post | Write (Dual-gate) |
| `messages sessions` | List private messaging sessions | Read |
| `messages history <uid>` | Retrieve direct message history | Read |
| `messages send <uid> <text>` | Send direct message | Write (Dual-gate) |
| `upload <file> --title T --desc D --tid N` | Upload video submission | Write (Dual-gate) |
| `search <query> [--type video\|user]` | Search videos or users | Read |

---

## Usage Examples

### 1. Check Account Status
```bash
skills/bilibili/scripts/bilibili.py whoami --json
```

### 2. Video Inspection & One-Click Triple Combo
```bash
# Read video metrics
skills/bilibili/scripts/bilibili.py video BV1xx411c7mD

# Trigger triple combo (generates draft fingerprint first)
skills/bilibili/scripts/bilibili.py interact BV1xx411c7mD --triple

# Execute with confirmed fingerprint
skills/bilibili/scripts/bilibili.py interact BV1xx411c7mD --triple --execute --fingerprint 7f8a9b2c...
```

### 3. Post a Comment
```bash
# Direct mode (when authorized)
skills/bilibili/scripts/bilibili.py comment BV1xx411c7mD "Interesting analysis!" --direct
```

### 4. Send a Private Message
```bash
skills/bilibili/scripts/bilibili.py messages send 12345678 "Hello from AgentHands!" --direct
```
