# Reddit Protocol Reference

## Overview

Reddit enforces strict rate limits and commercial pricing for its official API.
This framework provides both session-cookie (`reddit_session`) and script token compatibility for AI agents to participate in communities without corporate API contracts.

---

## HTTP Endpoints Reference

### 1. Reading Subreddits & Threads
- `GET https://www.reddit.com/r/{subreddit}/{sort}.json`: Fetch posts from subreddit (`hot`, `new`, `top`, `rising`).
- `GET https://www.reddit.com/comments/{post_id}.json`: Fetch post content and hierarchical comment tree.
- `GET https://www.reddit.com/user/{username}/about.json`: Public user karma and account stats.

### 2. Mutating Actions (Write Operations)
- `POST https://www.reddit.com/api/submit`: Submit text or link post.
  - Parameters: `api_type=json`, `sr` (subreddit), `title`, `kind` (`self` or `link`), `text` or `url`.
- `POST https://www.reddit.com/api/comment`: Post a comment or reply.
  - Parameters: `api_type=json`, `thing_id` (parent fullname, e.g. `t3_xxx` for post or `t1_yyy` for comment), `text`.
- `POST https://www.reddit.com/api/vote`: Upvote/downvote post or comment.
  - Parameters: `id` (fullname), `dir` (`1` for upvote, `-1` for downvote, `0` to unvote).

---

## Safety Architecture

- All mutating actions (`submit_post`, `submit_comment`, `vote`) are protected by `REDDIT_WRITE_ALLOWED` and SHA-256 fingerprint validation.
- Session tokens, passwords, and client secrets are registered with the secret scrubber to prevent trace leakage.
