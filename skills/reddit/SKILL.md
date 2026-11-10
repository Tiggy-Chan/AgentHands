---
name: reddit
description: Personal account action framework for Reddit without corporate API contracts. Browse subreddits (hot, new, top), read post comments, submit text and link posts, reply to threads, cast upvotes/downvotes, and view user profiles. Triggers: "reddit", "Reddit", "发reddit", "reddit发帖", "reddit评论", "reddit投票", "查reddit".
---

# Reddit Personal Account Action Framework

Operate your personal Reddit account directly from AI agents.
Supports browsing community feeds, inspecting discussions, submitting submissions, replying to comments, and voting with zero developer paywalls.

## Key Capabilities

- **Community Discovery**: Browse subreddit posts (`hot`, `new`, `top`), view scores, comment counts, and selftexts.
- **Discussions**: Retrieve thread comments and replies.
- **Posting & Commenting**: Submit text posts, share links, reply to submissions or specific comments.
- **Voting**: Cast upvotes or downvotes on submissions and comments.
- **User Intelligence**: Inspect public karma, account age, and profiles.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`post`, `comment`, `vote`) are strictly protected against AI hallucination:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without submitting.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any change to title or content invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized pipeline, set `REDDIT_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/reddit/scripts/reddit.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login [--session S\|--token T]` | Save session credentials | Read |
| `whoami` | Inspect logged-in account profile | Read |
| `sub <name> [--sort S] [--limit N]` | Browse subreddit submissions | Read |
| `comments <post_id> [--limit N]` | Inspect comments on a submission | Read |
| `user <name>` | Lookup user profile | Read |
| `post <sub discipline> <title> [--text T\|--url U]` | Submit post | Write (Dual-gate) |
| `comment <parent_fullname> <text>` | Submit comment or reply | Write (Dual-gate) |
| `vote <fullname> (--up\|--down\|--clear)` | Cast vote | Write (Dual-gate) |

---

## Usage Examples

### 1. Browse Subreddit Posts
```bash
skills/reddit/scripts/reddit.py sub LocalLLaMA --sort hot --limit 5 --json
```

### 2. Submit a Post (Draft & Confirmation)
```bash
# Generate draft fingerprint
skills/reddit/scripts/reddit.py post LocalLLaMA "New benchmark findings" --text "Detailed evaluation..."

# Execute with confirmed fingerprint
skills/reddit/scripts/reddit.py post LocalLLaMA "New benchmark findings" --text "Detailed evaluation..." --execute --fingerprint 5c6d7e8f...
```

### 3. Upvote a Post
```bash
skills/reddit/scripts/reddit.py vote t3_1abcde --up --direct
```
