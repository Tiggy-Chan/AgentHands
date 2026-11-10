---
name: xiaohongshu
description: Personal account action framework for Xiaohongshu (小红书 / RedNote) without developer APIs. Publish image-text notes and video notes via creator center automation, manage drafts, delete notes, retrieve public note details, search feeds, and fetch creator analytics. Triggers: "小红书", "xiaohongshu", "rednote", "发小红书", "发布小红书", "小红书图文", "小红书视频", "小红书搜笔记".
---

# Xiaohongshu Personal Account Action Framework

Operate your Xiaohongshu personal creator account directly through authenticated browser sessions.
Enables AI Agents to publish image notes, upload videos, track interactions, and manage content with zero enterprise developer approvals.

## Key Capabilities

- **Image-Text Note Publishing**: Automated multi-image upload (1-9 images), title formatting (max 20 chars), content editing, rich card styles, and topic tags.
- **Video Note Publishing**: MP4/MOV upload with custom cover image and automated `<xhs-publish-btn>` Shadow DOM form submission.
- **Content Discovery**: Search notes by keyword, inspect public note details and interactions (likes, collects), and fetch comment threads.
- **Creator Metrics**: View follower statistics, interaction trends, and creator center analytics.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`publish`, `publish-video`, `delete`) are strictly protected against LLM hallucination:

1. **Draft Fingerprinting**: By default, write commands output a SHA256 content fingerprint draft and exit without mutating account content.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any change to title or content invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized pipeline, set `XIAOHONGSHU_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/xiaohongshu/scripts/xiaohongshu.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login [--manual COOKIES\|--check]` | Save cookies or check session validity | Read |
| `whoami` | Inspect current creator account profile | Read |
| `publish <content> --title T --images I1...` | Publish image-text note | Write (Dual-gate) |
| `publish-video <content> --title T --video V` | Publish video note | Write (Dual-gate) |
| `delete <note_id>` | Delete published note | Write (Dual-gate) |
| `note <note_id>` | Read public note details and stats | Read |
| `search <keyword> [--limit N]` | Search notes by query | Read |
| `comments <note_id> [--limit N]` | Fetch comments on note | Read |
| `creator-profile` | View creator analytics and stats | Read |

---

## Usage Examples

### 1. Check Creator Account Status
```bash
skills/xiaohongshu/scripts/xiaohongshu.py whoami --json
```

### 2. Search Notes
```bash
skills/xiaohongshu/scripts/xiaohongshu.py search "Mac mini M4" --limit 5
```

### 3. Publish Image Note (Draft & Confirmation)
```bash
# Generate draft fingerprint
skills/xiaohongshu/scripts/xiaohongshu.py publish "日常分享..." \
  --title "今日穿搭分享" \
  --images /path/to/pic1.jpg /path/to/pic2.jpg

# Execute with confirmed fingerprint
skills/xiaohongshu/scripts/xiaohongshu.py publish "日常分享..." \
  --title "今日穿搭分享" \
  --images /path/to/pic1.jpg /path/to/pic2.jpg \
  --execute --fingerprint a9b8c7d6...
```
