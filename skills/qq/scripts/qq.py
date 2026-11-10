#!/usr/bin/env python3
"""qq — Personal account action CLI for QQ messaging via OneBot 11.

Data output on stdout (Markdown or JSON). Progress and diagnostics on stderr.
Exit codes:
  0: Success
  1: Error
  2: Authentication missing or connection failed
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

from _qq import (
    auth,
    config,
    group_ops,
    message_ops,
    output,
    security,
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
        prog="qq",
        description="QQ personal account action framework for AI agents via OneBot 11.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "jsonl"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument("--json", action="store_true", help="Shortcut for --format json")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # config
    p_cfg = sub.add_parser("config", help="Set OneBot server endpoint URL and access token")
    p_cfg.add_argument("--url", required=True, help="OneBot 11 HTTP URL (e.g. http://127.0.0.1:3000)")
    p_cfg.add_argument("--token", default="", help="Optional access token")

    # whoami
    sub.add_parser("whoami", help="Display logged in QQ account details")

    # send-private
    p_sp = sub.add_parser("send-private", help="Send private message to QQ friend")
    p_sp.add_argument("user_id", type=int, help="Target QQ number")
    p_sp.add_argument("message", help="Message text")
    _add_write_gate_args(p_sp)

    # send-group
    p_sg = sub.add_parser("send-group", help="Send message to QQ group")
    p_sg.add_argument("group_id", type=int, help="Target group number")
    p_sg.add_argument("message", help="Message text")
    _add_write_gate_args(p_sg)

    # delete
    p_del = sub.add_parser("delete", help="Recall sent message")
    p_del.add_argument("message_id", type=int, help="Message ID")
    _add_write_gate_args(p_del)

    # history
    p_hist = sub.add_parser("history", help="Get chat history for a group")
    p_hist.add_argument("group_id", type=int, help="Group ID")
    p_hist.add_argument("--limit", type=int, default=20, help="Number of messages")

    # friends
    sub.add_parser("friends", help="List friends")

    # groups
    sub.add_parser("groups", help="List joined groups")

    # members
    p_mem = sub.add_parser("members", help="List members of a group")
    p_mem.add_argument("group_id", type=int, help="Target group ID")

    # ban
    p_ban = sub.add_parser("ban", help="Mute a group member")
    p_ban.add_argument("group_id", type=int, help="Group ID")
    p_ban.add_argument("user_id", type=int, help="Target member QQ")
    p_ban.add_argument("--duration", type=int, default=1800, help="Duration in seconds (0 to unmute)")
    _add_write_gate_args(p_ban)

    # kick
    p_kick = sub.add_parser("kick", help="Kick a member from group")
    p_kick.add_argument("group_id", type=int, help="Group ID")
    p_kick.add_argument("user_id", type=int, help="Target member QQ")
    p_kick.add_argument("--reject-add", action="store_true", help="Reject future requests to join")
    _add_write_gate_args(p_kick)

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
        if args.subcommand == "config":
            auth.save_credentials(args.url, args.token)
            output.note(f"Configured OneBot server: {args.url}")
            return 0
        elif args.subcommand == "whoami":
            res = await user_ops.get_self_info()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "send-private":
            res = await message_ops.send_private_message(
                user_id=args.user_id,
                message=args.message,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "send_private")
            return 0
        elif args.subcommand == "send-group":
            res = await message_ops.send_group_message(
                group_id=args.group_id,
                message=args.message,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "send_group")
            return 0
        elif args.subcommand == "delete":
            res = await message_ops.delete_message(
                message_id=args.message_id,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "delete_message")
            return 0
        elif args.subcommand == "history":
            res = await message_ops.get_history(args.group_id, args.limit)
            _output_result(res, fmt, f"history:{args.group_id}")
            return 0
        elif args.subcommand == "friends":
            res = await user_ops.list_friends()
            _output_result(res, fmt, "friends")
            return 0
        elif args.subcommand == "groups":
            res = await group_ops.list_groups()
            _output_result(res, fmt, "groups")
            return 0
        elif args.subcommand == "members":
            res = await group_ops.get_group_members(args.group_id)
            _output_result(res, fmt, f"members:{args.group_id}")
            return 0
        elif args.subcommand == "ban":
            res = await group_ops.set_group_ban(
                group_id=args.group_id,
                user_id=args.user_id,
                duration=args.duration,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "ban")
            return 0
        elif args.subcommand == "kick":
            res = await group_ops.set_group_kick(
                group_id=args.group_id,
                user_id=args.user_id,
                reject_add_request=args.reject_add,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "kick")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except ConnectionError as exc:
        output.note(f"Connection error: {exc}")
        return 2
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
                        for k, v in item.items() if k not in ("raw_message", "message")
                    )
                    lines.append(f"{idx}. {preview}")
                    if "raw_message" in item and item["raw_message"]:
                        lines.append(output.untrusted(str(item["raw_message"]), f"msg:{item.get('message_id', idx)}"))
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
