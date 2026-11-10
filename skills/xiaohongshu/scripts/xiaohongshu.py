#!/usr/bin/env python3
"""xiaohongshu — Personal account action CLI for Xiaohongshu without developer APIs.

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

from _xiaohongshu import (
    auth,
    config,
    creator_ops,
    note_ops,
    output,
    search_ops,
    security,
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
        prog="xiaohongshu",
        description="Xiaohongshu personal account action framework for AI agents.",
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
    p_login = sub.add_parser("login", help="Set credentials or validate session")
    p_login.add_argument("--manual", default=None, help="Raw cookie string")
    p_login.add_argument("--check", action="store_true", help="Validate existing cookies")

    # whoami
    sub.add_parser("whoami", help="Display current creator account profile")

    # publish image note
    p_pub = sub.add_parser("publish", help="Publish image-text note")
    p_pub.add_argument("content", help="Note body text")
    p_pub.add_argument("--title", required=True, help="Note title (max 20 chars)")
    p_pub.add_argument("--images", nargs="+", required=True, help="Image file paths (1-9)")
    p_pub.add_argument("--topics", nargs="*", default=[], help="Topic hashtags")
    p_pub.add_argument("--card-style", default="基础", help="Text card style")
    p_pub.add_argument("--draft", action="store_true", help="Save to drafts instead of publishing")
    _add_write_gate_args(p_pub)

    # publish video note
    p_pub_vid = sub.add_parser("publish-video", help="Publish video note")
    p_pub_vid.add_argument("content", help="Note body text")
    p_pub_vid.add_argument("--title", required=True, help="Note title (max 20 chars)")
    p_pub_vid.add_argument("--video", required=True, help="Video file path")
    p_pub_vid.add_argument("--cover", default=None, help="Custom cover image path")
    p_pub_vid.add_argument("--topics", nargs="*", default=[], help="Topic hashtags")
    p_pub_vid.add_argument("--draft", action="store_true", help="Save to drafts instead of publishing")
    _add_write_gate_args(p_pub_vid)

    # delete
    p_del = sub.add_parser("delete", help="Delete note")
    p_del.add_argument("note_id", help="Note ID")
    _add_write_gate_args(p_del)

    # note detail
    p_note = sub.add_parser("note", help="Read public note details")
    p_note.add_argument("note_id", help="Note ID")

    # search
    p_search = sub.add_parser("search", help="Search notes")
    p_search.add_argument("keyword", help="Search keywords")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    # comments
    p_comments = sub.add_parser("comments", help="Fetch comments on a note")
    p_comments.add_argument("note_id", help="Note ID")
    p_comments.add_argument("--limit", type=int, default=20, help="Max comments")

    # creator profile
    sub.add_parser("creator-profile", help="View creator analytics and profile")

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
            return await handle_login(args, fmt)
        elif args.subcommand == "whoami" or args.subcommand == "creator-profile":
            res = await creator_ops.get_creator_profile()
            _output_result(res, fmt, "creator_profile")
            return 0
        elif args.subcommand == "publish":
            res = await note_ops.publish_image_note(
                title=args.title,
                content=args.content,
                images=args.images,
                topics=args.topics,
                card_style=args.card_style,
                draft=args.draft,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "publish")
            return 0
        elif args.subcommand == "publish-video":
            res = await note_ops.publish_video_note(
                title=args.title,
                content=args.content,
                video_path=args.video,
                cover_path=args.cover,
                topics=args.topics,
                draft=args.draft,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "publish_video")
            return 0
        elif args.subcommand == "delete":
            res = await note_ops.delete_note(
                note_id=args.note_id,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "delete_note")
            return 0
        elif args.subcommand == "note":
            res = await note_ops.get_note_detail(args.note_id)
            _output_result(res, fmt, f"note:{args.note_id}")
            return 0
        elif args.subcommand == "search":
            res = await search_ops.search_notes(args.keyword, limit=args.limit)
            _output_result(res, fmt, f"search:{args.keyword}")
            return 0
        elif args.subcommand == "comments":
            res = await search_ops.get_comments(args.note_id, limit=args.limit)
            _output_result(res, fmt, f"comments:{args.note_id}")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except Exception as exc:
        output.note(f"Error: {output.scrubbed_exc(exc)}")
        return 1


async def handle_login(args: argparse.Namespace, fmt: str) -> int:
    if args.manual:
        auth.save_credentials(args.manual)
        output.note("Saved Xiaohongshu cookies.")
        return 0
    if args.check:
        creds = auth.load_credentials()
        if creds["cookies"]:
            output.note("Cookies found in configuration.")
            return 0
        output.note("No cookies configured.")
        return 2

    output.note("Pass --manual '<cookies>' or set XIAOHONGSHU_COOKIES in environment.")
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
                        for k, v in item.items() if k not in ("content", "message")
                    )
                    lines.append(f"{idx}. {preview}")
                    if "content" in item and item["content"]:
                        lines.append(output.untrusted(str(item["content"]), f"note:{item.get('note_id', idx)}"))
                    elif "message" in item and item["message"]:
                        lines.append(output.untrusted(str(item["message"]), f"comment:{idx}"))
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
