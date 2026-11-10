#!/usr/bin/env python3
"""bilibili — Personal account action CLI for Bilibili without developer APIs.

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

# Add script directory to sys.path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _bilibili import (
    auth,
    comment_ops,
    config,
    dynamic_ops,
    message_ops,
    output,
    search_ops,
    security,
    upload_ops,
    user_ops,
    video_ops,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilibili",
        description="Bilibili personal account action framework for AI agents.",
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
    p_login = sub.add_parser("login", help="Authenticate via QR code or manual credentials")
    p_login.add_argument("--qr", action="store_true", help="Trigger terminal QR code login flow")
    p_login.add_argument("--check", action="store_true", help="Validate existing credentials")

    # whoami
    sub.add_parser("whoami", help="Display currently logged in user info")

    # user
    p_user = sub.add_parser("user", help="Inspect public user profile")
    p_user.add_argument("uid", type=int, help="Target user UID / MID")

    # follow / unfollow
    p_follow = sub.add_parser("follow", help="Follow a user")
    p_follow.add_argument("uid", type=int, help="Target user UID")
    _add_write_gate_args(p_follow)

    p_unfollow = sub.add_parser("unfollow", help="Unfollow a user")
    p_unfollow.add_argument("uid", type=int, help="Target user UID")
    _add_write_gate_args(p_unfollow)

    # video
    p_video = sub.add_parser("video", help="Retrieve video details and metrics")
    p_video.add_argument("bvid", help="Video BV ID (e.g. BV1xx411c7mD)")

    # interact
    p_interact = sub.add_parser("interact", help="Interact with video (like, coin, fav, triple)")
    p_interact.add_argument("bvid", help="Video BV ID")
    p_interact.add_argument("--like", action="store_true", help="Like the video")
    p_interact.add_argument("--coin", type=int, default=0, help="Pay coins (1 or 2)")
    p_interact.add_argument("--fav", action="store_true", help="Favorite the video")
    p_interact.add_argument("--triple", action="store_true", help="One-click triple combo")
    _add_write_gate_args(p_interact)

    # comments
    p_comments = sub.add_parser("comments", help="List comments for a video")
    p_comments.add_argument("bvid", help="Video BV ID")
    p_comments.add_argument("--page", type=int, default=1, help="Page number")
    p_comments.add_argument("--limit", type=int, default=20, help="Page size")

    # comment
    p_comment = sub.add_parser("comment", help="Post a comment or reply")
    p_comment.add_argument("bvid", help="Video BV ID")
    p_comment.add_argument("message", help="Comment text")
    p_comment.add_argument("--root", type=int, default=None, help="Root comment RPID")
    p_comment.add_argument("--parent", type=int, default=None, help="Parent reply RPID")
    _add_write_gate_args(p_comment)

    # dynamic
    p_dynamic = sub.add_parser("dynamic", help="Dynamic feeds and posting")
    sub_dyn = p_dynamic.add_subparsers(dest="dynamic_action", required=True)

    p_dyn_list = sub_dyn.add_parser("list", help="List user dynamics")
    p_dyn_list.add_argument("--uid", type=int, default=None, help="Target user UID (default: self)")
    p_dyn_list.add_argument("--limit", type=int, default=10, help="Max items")

    p_dyn_post = sub_dyn.add_parser("post", help="Post text or picture dynamic")
    p_dyn_post.add_argument("text", help="Dynamic post content")
    p_dyn_post.add_argument("--images", nargs="*", default=[], help="Image file paths")
    _add_write_gate_args(p_dyn_post)

    p_dyn_del = sub_dyn.add_parser("delete", help="Delete a dynamic post")
    p_dyn_del.add_argument("dynamic_id", help="Dynamic post ID")
    _add_write_gate_args(p_dyn_del)

    # messages
    p_msg = sub.add_parser("messages", help="Private messaging operations")
    sub_msg = p_msg.add_subparsers(dest="messages_action", required=True)

    p_msg_sessions = sub_msg.add_parser("sessions", help="List message sessions")
    p_msg_sessions.add_argument("--limit", type=int, default=20, help="Max sessions")

    p_msg_hist = sub_msg.add_parser("history", help="Get conversation history")
    p_msg_hist.add_argument("receiver_id", type=int, help="Peer user UID")
    p_msg_hist.add_argument("--limit", type=int, default=20, help="Max messages")

    p_msg_send = sub_msg.add_parser("send", help="Send private message")
    p_msg_send.add_argument("receiver_id", type=int, help="Recipient user UID")
    p_msg_send.add_argument("text", help="Message body")
    _add_write_gate_args(p_msg_send)

    # upload
    p_upload = sub.add_parser("upload", help="Upload a video submission")
    p_upload.add_argument("video_file", help="Path to video file")
    p_upload.add_argument("--title", required=True, help="Video title")
    p_upload.add_argument("--desc", default="", help="Video description")
    p_upload.add_argument("--tid", type=int, required=True, help="Category TID")
    p_upload.add_argument("--tags", nargs="*", default=["原创"], help="Video tags")
    p_upload.add_argument("--cover", default=None, help="Cover image path")
    _add_write_gate_args(p_upload)

    # search
    p_search = sub.add_parser("search", help="Search videos or users")
    p_search.add_argument("keyword", help="Search query")
    p_search.add_argument("--type", choices=["video", "user"], default="video", help="Object type")
    p_search.add_argument("--page", type=int, default=1, help="Page number")

    return parser


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
        elif args.subcommand == "whoami":
            res = await user_ops.get_self_info()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "user":
            res = await user_ops.get_user_info(args.uid)
            _output_result(res, fmt, f"user:{args.uid}")
            return 0
        elif args.subcommand == "follow":
            res = await user_ops.follow_user(args.uid, args.execute, args.direct, args.fingerprint)
            _output_result(res, fmt, "follow")
            return 0
        elif args.subcommand == "unfollow":
            res = await user_ops.unfollow_user(args.uid, args.execute, args.direct, args.fingerprint)
            _output_result(res, fmt, "unfollow")
            return 0
        elif args.subcommand == "video":
            res = await video_ops.get_video_info(args.bvid)
            _output_result(res, fmt, f"video:{args.bvid}")
            return 0
        elif args.subcommand == "interact":
            res = await video_ops.interact_video(
                args.bvid,
                like=args.like,
                coin=args.coin,
                fav=args.fav,
                triple=args.triple,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "interact")
            return 0
        elif args.subcommand == "comments":
            res = await comment_ops.list_comments(args.bvid, args.page, args.limit)
            _output_result(res, fmt, f"comments:{args.bvid}")
            return 0
        elif args.subcommand == "comment":
            res = await comment_ops.send_comment(
                args.bvid,
                args.message,
                root_rpid=args.root,
                parent_rpid=args.parent,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "comment")
            return 0
        elif args.subcommand == "dynamic":
            return await handle_dynamic(args, fmt)
        elif args.subcommand == "messages":
            return await handle_messages(args, fmt)
        elif args.subcommand == "upload":
            res = await upload_ops.upload_video(
                file_path=args.video_file,
                title=args.title,
                desc=args.desc,
                tid=args.tid,
                tags=args.tags,
                cover_path=args.cover,
                execute=args.execute,
                direct=args.direct,
                expected_fp=args.fingerprint,
            )
            _output_result(res, fmt, "upload")
            return 0
        elif args.subcommand == "search":
            res = await search_ops.search_content(args.keyword, args.type, args.page)
            _output_result(res, fmt, f"search:{args.keyword}")
            return 0
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except Exception as exc:
        output.note(f"Error: {output.scrubbed_exc(exc)}")
        return 1


async def handle_login(args: argparse.Namespace, fmt: str) -> int:
    if args.check:
        try:
            info = await user_ops.get_self_info()
            output.note(f"Authenticated as: {info['name']} (UID: {info['mid']})")
            _output_result(info, fmt, "auth_check")
            return 0
        except Exception as e:
            output.note(f"Authentication invalid: {output.scrubbed_exc(e)}")
            return 2

    # QR Code Login
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel, QrCodeLoginEvents

    output.note("Generating Bilibili QR code...")
    login = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
    await login.generate_qrcode()

    pic = login.get_qrcode_picture()
    config.ensure_dirs()
    pic.to_file(str(config.QR_CODE_FILE))
    output.note(f"QR code image saved to: {config.QR_CODE_FILE}")
    output.note("Scan the QR code with Bilibili mobile app to authorize.")

    # Polling
    for _ in range(60):
        await asyncio.sleep(2)
        event = await login.check_state()
        if login.has_done():
            cred = login.get_credential()
            creds = {
                "sessdata": cred.sessdata or "",
                "bili_jct": cred.bili_jct or "",
                "buvid3": cred.buvid3 or "",
                "dedeuserid": cred.dedeuserid or "",
            }
            auth.save_credentials(creds)
            output.note("Login successful! Credentials saved securely.")
            info = await user_ops.get_self_info()
            _output_result(info, fmt, "login")
            return 0
        if event == QrCodeLoginEvents.TIMEOUT:
            output.note("QR code expired.")
            return 2

    output.note("Login timed out.")
    return 2


async def handle_dynamic(args: argparse.Namespace, fmt: str) -> int:
    if args.dynamic_action == "list":
        res = await dynamic_ops.list_dynamics(args.uid, args.limit)
        _output_result(res, fmt, "dynamic_list")
        return 0
    elif args.dynamic_action == "post":
        res = await dynamic_ops.post_dynamic(
            text=args.text,
            images=args.images,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "dynamic_post")
        return 0
    elif args.dynamic_action == "delete":
        res = await dynamic_ops.delete_dynamic(
            dynamic_id=args.dynamic_id,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "dynamic_delete")
        return 0
    return 0


async def handle_messages(args: argparse.Namespace, fmt: str) -> int:
    if args.messages_action == "sessions":
        res = await message_ops.list_sessions(args.limit)
        _output_result(res, fmt, "message_sessions")
        return 0
    elif args.messages_action == "history":
        res = await message_ops.get_history(args.receiver_id, args.limit)
        _output_result(res, fmt, f"message_history:{args.receiver_id}")
        return 0
    elif args.messages_action == "send":
        res = await message_ops.send_message(
            receiver_id=args.receiver_id,
            text=args.text,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "message_send")
        return 0
    return 0


def _output_result(data: Any, fmt: str, tag: str) -> None:
    if fmt == "json":
        output.emit_json(data)
    elif fmt == "jsonl" and isinstance(data, list):
        output.emit_jsonl(data)
    else:
        # Markdown rendering with untrusted delimitation
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
                    preview = " | ".join(f"{k}: {output.safe_label(str(v), 24)}" for k, v in item.items() if k != "message")
                    lines.append(f"{idx}. {preview}")
                    if "message" in item:
                        lines.append(output.untrusted(str(item["message"]), f"msg:{item.get('rpid', idx)}"))
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
