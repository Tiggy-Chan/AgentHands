---
name: x
description: Personal account action framework for X (Twitter) without official developer API fees. Post tweets, reply to tweets, delete tweets, like/retweet, inspect public user profiles, and retrieve personal bookmarks via internal GraphQL endpoints. Triggers: "x", "twitter", "推特", "发推", "推特发推", "x发推", "twitter bookmarks", "推特书签".
---

# X (Twitter) Personal Account Action Framework

Take full control of your personal X account from AI agents without paying official API enterprise fees.
Uses authenticated web session cookies (`auth_token` and `ct0`) with internal GraphQL queries and mutations.

## Key Capabilities

- **Post & Reply**: Create new tweets, reply to existing tweet IDs, delete tweets.
- **Interactions**: Like (favorite) and retweet posts.
- **Bookmarks**: Fetch personal saved bookmarks with cursor pagination.
- **User Intelligence**: Inspect public profiles and follower stats by screen name.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`post`, `reply`, `delete`, `interact`) are strictly protected:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without publishing.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any change to text or target IDs invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized workflow, set `X_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/x/scripts/x.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login --auth-token T --ct0 C` | Configure session cookies | Read |
| `whoami` | Inspect credential validity | Read |
| `user <screen_name>` | Lookup user profile | Read |
| `bookmarks [--limit N]` | Fetch personal bookmarks | Read |
| `post <text>` | Publish new tweet | Write (Dual-gate) |
| `reply <tweet_id> <text>` | Reply to a tweet | Write (Dual-gate) |
| `delete <tweet_id>` | Delete a tweet | Write (Dual-gate) |
| `interact <tweet_id> (--like\|--retweet)` | Like or retweet a tweet | Write (Dual-gate) |

---

## Usage Examples

### 1. Check Authentication Status
```bash
skills/x/scripts/x.py whoami --json
```

### 2. Post a Tweet (Draft & Execute)
```bash
# Generate draft fingerprint
skills/x/scripts/x.py post "Exploring decentralized autonomous agents with AgentHands."

# Execute with confirmed fingerprint
skills/x/scripts/x.py post "Exploring decentralized autonomous agents with AgentHands." --execute --fingerprint e3b0c442...
```

### 3. Fetch Bookmarks
```bash
skills/x/scripts/x.py bookmarks --limit 10 --json
```
