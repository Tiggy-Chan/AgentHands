#!/usr/bin/env python3
"""youtube — Personal account and creator action CLI for YouTube via InnerTube.

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

from _youtube import (
    auth,
    channel_ops,
    client,
    comment_ops,
    config,
    output,
    security,
    video_ops,
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
        prog="youtube",
        description="YouTube personal creator account action framework for AI agents.",
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
    p_login = sub.add_parser("login", help="Set session cookies")
    p_login.add_argument("--cookies", required=True, help="Raw cookie header string")

    # whoami
    sub.add_parser("whoami", help="Inspect authenticated channel profile")

    # video
    p_vid = sub.add_parser("video", help="Get video details and metrics")
    p_vid.add_argument("video_id", help="YouTube Video ID")

    # update-video
    p_upd = sub.add_parser("update-video", help="Update video metadata (Studio)")
    p_upd.add_argument("video_id", help="YouTube Video ID")
    p_upd.add_argument("--title", default=None, help="New video title")
    p_upd.add_argument("--desc", default=None, help="New video description")
    p_upd.add_argument("--privacy", choices=["public", "unlisted", "private"], default=None, help="Privacy state")
    _add_write_gate_args(p_upd)

    # interact
    p_inter = sub.add_parser("interact", help="Like or remove like from video")
    p_inter.add_argument("video_id", help="YouTube Video ID")
    p_inter_group = p_inter.add_mutually_exclusive_group(required=True)
    p_inter_group.add_argument("--like", action="store_true", help="Like video")
    p_inter_group.add_argument("--removelike", action="store_true", help="Remove like")
    _add_write_gate_args(p_inter)

    # comment
    p_comm = sub.add_parser("comment", help="Post a comment on a video")
    p_comm.add_argument("video_id", help="YouTube Video ID")
    p_comm.add_argument("text", help="Comment text")
    _add_write_gate_args(p_comm)

    # reply
    p_rep = sub.add_parser("reply", help="Reply to a comment")
    p_rep.add_argument("comment_id", help="Comment ID")
    p_rep.add_argument("text", help="Reply text")
    _add_write_gate_args(p_rep)

    # community
    p_com = sub.add_parser("community", help="Post an update to Community tab")
    p_com.add_argument("text", help="Post content text")
    _add_write_gate_args(p_com)

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
            auth.save_credentials(args.cookies)
            output.note("Saved YouTube cookies successfully.")
            return 0
        elif args.subcommand == "whoami":
            res = await channel_ops.get_channel_info()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "video":
            res = await video_ops.get_video_details(args.video_id)
            _output_result(res, fmt, f"video:{args.video_id}")
            return 0
        elif args.subcommand == "update-video":
            res = await video_ops.update_video_metadata(
                video_id=args.video_id,
                title=args.title,
                description=args.desc,
                privacy=args.privacy,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "update_video")
            return 0
        elif args.subcommand == "interact":
            action = "like" if args.like else "removelike"
            res = await video_ops.interact_video(
                video_id=args.video_id,
                action=action,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, f"interact_{action}")
            return 0
        elif args.subcommand == "comment":
            res = await comment_ops.post_comment(
                video_id=args.video_id,
                text=args.text,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "comment")
            return 0
        elif args.subcommand == "reply":
            res = await comment_ops.reply_comment(
                comment_id=args.comment_id,
                text=args.text,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "reply")
            return 0
        elif args.subcommand == "community":
            res = await channel_ops.post_community_update(
                text=args.text,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "community")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except client.YouTubeClientError as exc:
        output.note(f"YouTube API Error: {exc}")
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
                        for k, v in item.items() if k not in ("short_description", "description")
                    )
                    lines.append(f"{idx}. {preview}")
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
