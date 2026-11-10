---
name: youtube
description: Personal account and creator action framework for YouTube via InnerTube without API quota caps. Inspect video metrics, update video titles, descriptions, and privacy, like/unlike videos, reply to comments, and post community updates. Triggers: "youtube", "YouTube", "油管", "youtube评论", "youtube视频", "youtube修改标题", "youtube点赞".
---

# YouTube Personal Creator Account Action Framework

Operate your YouTube personal creator channel directly from AI agents via internal InnerTube and YouTube Studio APIs.
Bypasses Google Cloud Data API v3 quota exhaustion (10,000 units/day) by utilizing authenticated session cookies.

## Key Capabilities

- **Video Intelligence**: Inspect video details, view counts, and authors.
- **Creator Metadata Control**: Modify video titles, descriptions, and privacy states (public/unlisted/private).
- **Engagement**: Like or remove like from videos, submit top-level comments and comment replies.
- **Community Posts**: Publish text updates to the channel Community tab.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`update-video`, `interact`, `comment`, `reply`, `community`) are strictly protected:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without dispatching network mutations.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any difference in text or parameters invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized pipeline, set `YOUTUBE_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/youtube/scripts/youtube.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login --cookies C` | Configure session cookies | Read |
| `whoami` | Inspect authenticated channel profile | Read |
| `video <video_id>` | Inspect video details and metrics | Read |
| `update-video <id> [--title T] [--desc D] [--privacy P]` | Modify video metadata | Write (Dual-gate) |
| `interact <id> (--like\|--removelike)` | Like or remove like | Write (Dual-gate) |
| `comment <video_id> <text>` | Post video comment | Write (Dual-gate) |
| `reply <comment_id> <text>` | Reply to a comment | Write (Dual-gate) |
| `community <text>` | Post community tab update | Write (Dual-gate) |

---

## Usage Examples

### 1. Check Channel Profile
```bash
skills/youtube/scripts/youtube.py whoami --json
```

### 2. Update Video Metadata (Draft & Execute)
```bash
# Generate draft fingerprint
skills/youtube/scripts/youtube.py update-video dQw4w9WgXcQ --title "New Updated Title"

# Execute with confirmed fingerprint
skills/youtube/scripts/youtube.py update-video dQw4w9WgXcQ --title "New Updated Title" --execute --fingerprint 7a8b9c0d...
```

### 3. Post Video Comment Directly
```bash
skills/youtube/scripts/youtube.py comment dQw4w9WgXcQ "Great analysis!" --direct
```
