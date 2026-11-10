#!/usr/bin/env python3
"""reddit — Personal account action CLI for Reddit without restricted API tiers.

Data output on stdout (Markdown or JSON). Progress and diagnostics on stderr.
Exit codes:
  0: Success
  1: Error
  2: Authentication missing or connection error
  3: Write action blocked by safety gate
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _reddit import (
    auth,
    client,
    config,
    output,
    post_ops,
    security,
    sub_ops,
    user_ops,
)

_GLOBAL_VALUE_FLAGS = ("--format",)
_GLOBAL_BOOL_FLAGS = ("--json",)


def _split_global_flags(argv: list[str]) -> tuple[dict[str, object], list[str]]:
    found: dict[str, object] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        matched = False
        for flag in _GLOBAL_VALUE_FLAGS:
            if token == flag and i + 1 < len(argv):
                found[flag.lstrip("-").replace("-", "_")] = argv[i + 1]
                i += 2
                matched = True
                break
            if token.startswith(flag + "="):
                found[flag.lstrip("-").replace("-", "_")] = token.split("=", 1)[1]
                i += 1
                matched = True
                break
        if matched:
            continue
        if token in _GLOBAL_BOOL_FLAGS:
            found[token.lstrip("-").replace("-", "_")] = True
            i += 1
            continue
        rest.append(token)
        i += 1
    return found, rest


def _add_write_gate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--direct", action="store_true", help="Bypass draft verification and execute directly")
    p.add_argument("--execute", action="store_true", help="Execute write action with confirmed fingerprint")
    p.add_argument("--fingerprint", default=None, help="Confirmed SHA256 draft fingerprint")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit",
        description="Reddit personal account action framework for AI agents.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "jsonl"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument("--json", action="store_true", help="Shortcut for --format json")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # login
    p_login = sub.add_parser("login", help="Set session cookie or bearer token")
    p_login.add_argument("--session", default=None, help="reddit_session cookie value")
    p_login.add_argument("--token", default=None, help="OAuth bearer token")

    # whoami
    sub.add_parser("whoami", help="Check logged-in user profile")

    # sub
    p_sub = sub.add_parser("sub", help="Browse subreddit submissions")
    p_sub.add_argument("subreddit", help="Subreddit name (e.g. ArtificialInteligence)")
    p_sub.add_argument("--sort", choices=["hot", "new", "top", "rising"], default="hot")
    p_sub.add_argument("--limit", type=int, default=25)

    # comments
    p_comm = sub.add_parser("comments", help="Inspect comments on a submission")
    p_comm.add_argument("post_id", help="Submission ID (e.g. 1abcde or t3_1abcde)")
    p_comm.add_argument("--limit", type=int, default=25)

    # user
    p_user = sub.add_parser("user", help="Lookup public user profile")
    p_user.add_argument("username", help="Reddit username")

    # post
    p_post = sub.add_parser("post", help="Submit a post to a subreddit")
    p_post.add_argument("subreddit", help="Target subreddit")
    p_post.add_argument("title", help="Submission title")
    p_post.add_argument("--text", default="", help="Selftext markdown body")
    p_post.add_argument("--url", default="", help="Link URL (if link submission)")
    _add_write_gate_args(p_post)

    # comment
    p_comment = sub.add_parser("comment", help="Submit a comment or reply")
    p_comment.add_argument("parent", help="Fullname of post or comment (e.g. t3_xxx or t1_yyy)")
    p_comment.add_argument("text", help="Comment text")
    _add_write_gate_args(p_comment)

    # vote
    p_vote = sub.add_parser("vote", help="Cast vote on a post or comment")
    p_vote.add_argument("fullname", help="Fullname (e.g. t3_xxx)")
    p_vote_dir = p_vote.add_mutually_exclusive_group(required=True)
    p_vote_dir.add_argument("--up", action="store_true", help="Upvote")
    p_vote_dir.add_argument("--down", action="store_true", help="Downvote")
    p_vote_dir.add_argument("--clear", action="store_true", help="Clear vote")
    _add_write_gate_args(p_vote)

    return parser


async def main_async(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    globals_, rest = _split_global_flags(raw_argv)
    parser = build_parser()
    args = parser.parse_args(rest)
    for k, v in globals_.items():
        setattr(args, k, v)
    fmt = "json" if getattr(args, "json", False) else getattr(args, "format", "markdown")

    try:
        if args.subcommand == "login":
            creds = {}
            if args.session:
                creds["session"] = args.session
            if args.token:
                creds["token"] = args.token
            auth.save_credentials(creds)
            output.note("Saved Reddit credentials.")
            return 0
        elif args.subcommand == "whoami":
            res = await user_ops.get_me()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "sub":
            res = await sub_ops.list_posts(args.subreddit, args.sort, args.limit)
            _output_result(res, fmt, f"r/{args.subreddit}")
            return 0
        elif args.subcommand == "comments":
            res = await sub_ops.get_comments(args.post_id, args.limit)
            _output_result(res, fmt, f"comments:{args.post_id}")
            return 0
        elif args.subcommand == "user":
            res = await user_ops.get_user_profile(args.username)
            _output_result(res, fmt, f"u/{args.username}")
            return 0
        elif args.subcommand == "post":
            kind = "link" if args.url else "self"
            res = await post_ops.submit_post(
                subreddit=args.subreddit,
                title=args.title,
                text=args.text,
                url=args.url,
                kind=kind,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "submit_post")
            return 0
        elif args.subcommand == "comment":
            res = await post_ops.submit_comment(
                parent_fullname=args.parent,
                text=args.text,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "submit_comment")
            return 0
        elif args.subcommand == "vote":
            direction = 1 if args.up else (-1 if args.down else 0)
            res = await post_ops.vote(
                fullname=args.fullname,
                direction=direction,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "vote")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except client.RedditClientError as exc:
        output.note(f"Reddit API Error: {exc}")
        return 1
    except Exception as exc:
        output.note(f"Error: {output.scrubbed_exc(exc)}")
        return 1


def _output_result(data: Any, fmt: str, tag: str) -> None:
    if fmt == "json":
        output.emit_json(data)
    elif fmt == "jsonl" and isinstance(data, list):
        output.emit_jsonl(data)
    else:
        if isinstance(data, dict):
            lines = [f"# {tag}"]
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"## {k}")
                    lines.append(f"```json\n{output.scrub(str(v))}\n```")
                else:
                    lines.append(f"- **{k}**: {output.safe_label(str(v))}")
            output.emit("\n".join(lines))
        elif isinstance(data, list):
            lines = [f"# {tag} (Count: {len(data)})"]
            for idx, item in enumerate(data, start=1):
                if isinstance(item, dict):
                    preview = " | ".join(
                        f"{k}: {output.safe_label(str(v), 24)}"
                        for k, v in item.items() if k not in ("selftext", "body")
                    )
                    lines.append(f"{idx}. {preview}")
                    if "selftext" in item and item["selftext"]:
                        lines.append(output.untrusted(str(item["selftext"]), f"post:{item.get('id', idx)}"))
                    elif "body" in item and item["body"]:
                        lines.append(output.untrusted(str(item["body"]), f"comm:{item.get('id', idx)}"))
                else:
                    lines.append(f"{idx}. {output.safe_label(str(item))}")
            output.emit("\n".join(lines))
        else:
            output.emit(output.untrusted(str(data), tag))


def main() -> None:
    try:
        rc = asyncio.run(main_async())
        sys.exit(rc)
    except KeyboardInterrupt:
        output.note("Interrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
