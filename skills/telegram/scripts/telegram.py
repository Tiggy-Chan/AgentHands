#!/usr/bin/env python3
"""telegram — Personal account action CLI for Telegram without enterprise approvals.

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

from _telegram import (
    auth,
    chat_ops,
    client,
    config,
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
        prog="telegram",
        description="Telegram personal account action framework for AI agents.",
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
    p_cfg = sub.add_parser("config", help="Set credentials or bridge URL")
    p_cfg.add_argument("--token", default=None, help="Bot token fallback")
    p_cfg.add_argument("--bridge", default=None, help="HTTP bridge URL")
    p_cfg.add_argument("--session", default=None, help="Session string")

    # whoami
    sub.add_parser("whoami", help="Check current user identity")

    # send
    p_send = sub.add_parser("send", help="Send message to chat or user")
    p_send.add_argument("chat_id", help="Chat ID or @username")
    p_send.add_argument("text", help="Message text")
    p_send.add_argument("--reply-to", type=int, default=None, help="Reply message ID")
    _add_write_gate_args(p_send)

    # delete
    p_del = sub.add_parser("delete", help="Delete a message")
    p_del.add_argument("chat_id", help="Chat ID")
    p_del.add_argument("message_id", type=int, help="Message ID")
    _add_write_gate_args(p_del)

    # history
    p_hist = sub.add_parser("history", help="Retrieve message history")
    p_hist.add_argument("chat_id", help="Chat ID")
    p_hist.add_argument("--limit", type=int, default=20)

    # chat
    p_chat = sub.add_parser("chat", help="Inspect chat details")
    p_chat.add_argument("chat_id", help="Chat ID")

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
            creds = {}
            if args.token:
                creds["bot_token"] = args.token
            if args.bridge:
                creds["bridge_url"] = args.bridge
            if args.session:
                creds["session_string"] = args.session
            auth.save_credentials(creds)
            output.note("Saved Telegram credentials.")
            return 0
        elif args.subcommand == "whoami":
            res = await user_ops.get_me()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "send":
            res = await message_ops.send_message(
                chat_id=args.chat_id,
                text=args.text,
                reply_to=args.reply_to,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "send_message")
            return 0
        elif args.subcommand == "delete":
            res = await message_ops.delete_message(
                chat_id=args.chat_id,
                message_id=args.message_id,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "delete_message")
            return 0
        elif args.subcommand == "history":
            res = await message_ops.get_history(args.chat_id, args.limit)
            _output_result(res, fmt, f"history:{args.chat_id}")
            return 0
        elif args.subcommand == "chat":
            res = await chat_ops.get_chat(args.chat_id)
            _output_result(res, fmt, f"chat:{args.chat_id}")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except client.TelegramClientError as exc:
        output.note(f"Telegram API Error: {exc}")
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
                        lines.append(output.untrusted(str(item["text"]), f"msg:{item.get('message_id', idx)}"))
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
