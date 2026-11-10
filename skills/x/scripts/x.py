#!/usr/bin/env python3
"""x — Personal account action CLI for X (Twitter) without developer API fees.

Data output on stdout (Markdown or JSON). Progress and diagnostics on stderr.
Exit codes:
  0: Success
  1: Error
  2: Authentication missing or expired
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

from _x import (
    auth,
    bookmark_ops,
    client,
    config,
    output,
    security,
    tweet_ops,
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
        prog="x",
        description="X personal account action framework for AI agents without official API fees.",
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
    p_login = sub.add_parser("login", help="Set auth_token and ct0 session cookies")
    p_login.add_argument("--auth-token", required=True, help="auth_token cookie from browser")
    p_login.add_argument("--ct0", required=True, help="ct0 csrf cookie from browser")

    # whoami
    sub.add_parser("whoami", help="Check credentials and auth status")

    # user
    p_user = sub.add_parser("user", help="Lookup public user profile")
    p_user.add_argument("screen_name", help="User handle (without @)")

    # post
    p_post = sub.add_parser("post", help="Post a new tweet")
    p_post.add_argument("text", help="Tweet text content")
    p_post.add_argument("--media-ids", nargs="*", default=[], help="Media IDs to attach")
    _add_write_gate_args(p_post)

    # reply
    p_reply = sub.add_parser("reply", help="Reply to a tweet")
    p_reply.add_argument("tweet_id", help="Target Tweet ID")
    p_reply.add_argument("text", help="Reply text content")
    _add_write_gate_args(p_reply)

    # delete
    p_del = sub.add_parser("delete", help="Delete a tweet")
    p_del.add_argument("tweet_id", help="Target Tweet ID")
    _add_write_gate_args(p_del)

    # interact
    p_inter = sub.add_parser("interact", help="Like or retweet a tweet")
    p_inter.add_argument("tweet_id", help="Target Tweet ID")
    p_inter_group = p_inter.add_mutually_exclusive_group(required=True)
    p_inter_group.add_argument("--like", action="store_true", help="Like the tweet")
    p_inter_group.add_argument("--retweet", action="store_true", help="Retweet the tweet")
    _add_write_gate_args(p_inter)

    # bookmarks
    p_bm = sub.add_parser("bookmarks", help="Fetch personal bookmarks")
    p_bm.add_argument("--limit", type=int, default=20, help="Max items")

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
            auth.save_credentials(args.auth_token, args.ct0)
            output.note("Saved X credentials successfully.")
            return 0
        elif args.subcommand == "whoami":
            creds = auth.load_credentials()
            has_auth = bool(creds.get("auth_token") and creds.get("ct0"))
            res = {
                "status": "authenticated" if has_auth else "unauthenticated",
                "has_auth_token": bool(creds.get("auth_token")),
                "has_ct0": bool(creds.get("ct0")),
            }
            _output_result(res, fmt, "whoami")
            return 0 if has_auth else 2
        elif args.subcommand == "user":
            res = await user_ops.get_user_info(args.screen_name)
            _output_result(res, fmt, f"user:{args.screen_name}")
            return 0
        elif args.subcommand == "post":
            res = await tweet_ops.post_tweet(
                text=args.text,
                media_ids=args.media_ids,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "post")
            return 0
        elif args.subcommand == "reply":
            res = await tweet_ops.reply_tweet(
                tweet_id=args.tweet_id,
                text=args.text,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "reply")
            return 0
        elif args.subcommand == "delete":
            res = await tweet_ops.delete_tweet(
                tweet_id=args.tweet_id,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "delete")
            return 0
        elif args.subcommand == "interact":
            action = "like" if args.like else "retweet"
            res = await tweet_ops.interact_tweet(
                tweet_id=args.tweet_id,
                action=action,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, f"interact_{action}")
            return 0
        elif args.subcommand == "bookmarks":
            res = await bookmark_ops.list_bookmarks(args.limit)
            _output_result(res, fmt, "bookmarks")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except client.XClientError as exc:
        output.note(f"X API Error: {exc}")
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
                        for k, v in item.items() if k != "text"
                    )
                    lines.append(f"{idx}. {preview}")
                    if "text" in item and item["text"]:
                        lines.append(output.untrusted(str(item["text"]), f"tweet:{item.get('tweet_id', idx)}"))
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
