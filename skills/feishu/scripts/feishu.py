#!/usr/bin/env python3
"""feishu — 用网页版会话 cookie 接管飞书个人账号（读为主）。

数据走 stdout（markdown/JSON/JSONL），进度走 stderr。
退出码：0 正常 / 1 失败 / 2 会话失效 / 3 发送被闸门拦下 / 4 接口未适配 / 5 策略拦截。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FEISHU_HOME = os.environ.get("FEISHU_HOME", os.path.expanduser("~/.local/share/feishu"))

# venv 自举：自包含在 _feishu.venv_bootstrap 中，零外部依赖
sys.path.insert(0, os.path.join(HERE, "_feishu"))
from venv_bootstrap import VenvError, in_venv, ensure as _ensure_venv, reexec as _reexec, venv_python  # noqa: E402

VENV_PY = str(venv_python(FEISHU_HOME))

_PIP_PACKAGES = [
    "playwright==1.62.0", "protobuf", "httpx", "websockets",
    "keyring>=25,<27", "secretstorage>=3.3", "cryptography>=43,<47",
]


# PEP 668: system python can't pip-install; hop into our own venv which has
# protobuf/httpx/websockets/playwright. Recreate it if the deps are missing.
def _bootstrap() -> None:
    if not os.path.exists(VENV_PY):
        if os.environ.get("FEISHU_NO_BOOTSTRAP"):
            return
        try:
            _ensure_venv(FEISHU_HOME, _PIP_PACKAGES)
        except VenvError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1)
    _reexec(FEISHU_HOME, os.path.abspath(__file__))


# Refused here, before the venv is built: bootstrapping would spend a 500MB
# download to reach a secretstorage that cannot talk to a Secret Service, and
# the first real command would then fail again inside systemd-run. One sentence
# up front beats either of those.
if not sys.platform.startswith("linux"):
    print(
        "feishu skill 只支持 Linux：守护进程与浏览器/媒体沙箱靠 systemd --user\n"
        "cgroup，凭据存 Linux Secret Service，macOS 两者都没有。",
        file=sys.stderr,
    )
    raise SystemExit(1)

if not in_venv(FEISHU_HOME):
    if os.path.exists(VENV_PY):
        _reexec(FEISHU_HOME, os.path.abspath(__file__))
    else:
        # --help must never trigger a 500MB download
        if any(a in ("-h", "--help") for a in sys.argv[1:]):
            pass  # fall through with system python; deps import lazily
        else:
            _bootstrap()

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "vendor"))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402


def _apply_process_limit(command: str) -> None:
    """Cap heavy CLI processes so a broken endpoint cannot exhaust the host."""
    # Playwright starts a Node driver and Chromium children.  Those commands
    # are launched by browser_worker.py under a systemd cgroup instead of an
    # inherited RLIMIT_AS (Node reserves a normal CodeRange at startup).
    if command not in {"history", "mentions", "thread", "message", "media", "doc", "mail", "calendar"}:
        return
    try:
        import resource

        max_bytes = int(os.environ.get("FEISHU_PROCESS_MEMORY_MAX_MB", "1024")) * 1024 * 1024
        max_bytes = min(max(max_bytes, 256 * 1024 * 1024), 4 * 1024 * 1024 * 1024)
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        target = max_bytes if hard == resource.RLIM_INFINITY else min(max_bytes, hard)
        if soft == resource.RLIM_INFINITY or soft > target:
            resource.setrlimit(resource.RLIMIT_AS, (target, target if hard == resource.RLIM_INFINITY else hard))
    except (ImportError, OSError, ValueError):
        # systemd still protects the daemon; this is best-effort for a CLI
        # launched on a platform without RLIMIT_AS.
        pass


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["markdown", "json", "jsonl"], default="markdown")
    p.add_argument("--json", action="store_true", help="同 --format json")
    p.add_argument("--unattended", action="store_true", help="cron 模式：会话过期不等人，notify-send 后退出码 2")
    p.add_argument("--headed", action="store_true", help="login 时显示浏览器（默认就是 headed，此旗为文档性）")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="feishu.py",
        description="飞书个人账号接管（cookie 会话，读为主）",
        epilog="""环境变量：
  FEISHU_HOME          数据目录（venv/profile/cookie/事件池），默认 ~/.local/share/feishu
  FEISHU_DOMAIN        租户域名，默认 www.feishu.cn（会话 cookie 是 .feishu.cn 域，一般不用改）
  FEISHU_CA_BUNDLE     企业 CA 证书路径（默认严格系统 TLS，绝不关校验）
  FEISHU_ALLOW_SEND=1   聊天发送双重闸门之一
  FEISHU_ALLOW_WRITE=1  邮件/日历写入双重闸门之一
  FEISHU_CALENDAR_ID    日历默认目标（命令显式 --calendar-id 优先）
  FEISHU_TIMEZONE       日历无时区输入的默认时区，默认 Asia/Shanghai
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="扫码登录（默认）或 --manual 粘贴 cookie")
    p.add_argument("--manual", action="store_true", help="从 DevTools 粘贴 cookie（隐藏输入）")

    sub.add_parser("whoami", help="探活并显示当前账号")

    p = sub.add_parser("chats", help="列 Messenger 会话（网页 DOM；--from-events 是事件降级视图）")
    p.add_argument("--from-events", action="store_true", help="从守护进程见过的事件聚合")

    p = sub.add_parser("history", help="读群聊历史")
    p.add_argument("chat_id")
    p.add_argument("--since", default="24h", help="30m/2h/7d、ISO 日期或 epoch")
    p.add_argument("--until", default="")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("mentions", help="谁 @ 了我（历史检索；实时用 daemon+events）")
    p.add_argument("--since", default="24h")
    p.add_argument("--until", default="")
    p.add_argument("--chat-id", action="append", default=[], help="限定群，可多次；不给则查 daemon 见过的群")
    p.add_argument("--limit", type=int, default=15, help="每群上限")

    p = sub.add_parser("thread", help="还原一条消息的话题串和前后文")
    p.add_argument("msg_id")
    p.add_argument("--window", type=int, default=15, help="前后时间窗（分钟）")
    p.add_argument("--limit", type=int, default=60)

    p = sub.add_parser("message", help="读取一条消息的统一结构和资源元数据")
    p.add_argument("msg_id")
    p.add_argument(
        "--resolve-resources",
        action="store_true",
        help="显式标记需要解析资源元数据（消息正文读取本身已包含资源摘要）",
    )

    p = sub.add_parser("media", help="按需下载或在本机处理消息媒体")
    p.add_argument("msg_id")
    p.add_argument("--resource-id", default="", help="消息中的 resource_id；多资源时处理动作必须指定")
    p.add_argument(
        "--action",
        choices=["metadata", "download", "inspect", "ocr", "transcribe"],
        default="metadata",
    )
    p.add_argument("--keep-media", action="store_true", help="将原始媒体保存为加密缓存")
    p.add_argument("--out", default="", help="下载到指定文件；拒绝覆盖已有文件")

    p = sub.add_parser("media-cache", help="查看或可恢复清理加密媒体缓存")
    p.add_argument("action", choices=["status", "purge"])
    p.add_argument("--confirm-purge", action="store_true", help="确认把媒体缓存移动到可恢复归档")

    p = sub.add_parser("doc", help="读/导出文档，--list/--walk 云空间，或 import/replace 写文档")
    p.add_argument("ref", nargs="?", help="docx/wiki/sheets 链接、裸 token 或 import/replace 操作")
    p.add_argument("file_path", nargs="?", help="导入文件，或 replace 时的目标文档引用")
    p.add_argument("replacement_file", nargs="?", help="replace 时用于覆盖正文的本地 Markdown 文件")
    p.add_argument("--import-file", "--import", dest="import_file", default="", help="要导入的本地文件路径")
    p.add_argument("--export", dest="fmt", default="markdown",
                   choices=["markdown", "docx", "pdf", "xlsx"])
    p.add_argument("--out", default="", help="二进制导出必须写文件；markdown 默认 stdout")
    p.add_argument("--strict", action="store_true", help="导出失败时直接报错，不降级为浏览器 DOM 文本")
    p.add_argument("--screenshot-dir", default="", help="抓取文档正文全部图片到指定目录（受限浏览器滚动渲染，原图优先）")
    p.add_argument("--list", action="store_true", help="列我的空间+共享空间根节点")
    p.add_argument("--walk", action="store_true", help="遍历全部可见文档建索引（docs-index.jsonl）")
    p.add_argument("--max-nodes", type=int, default=5000)
    p.add_argument("--folder", default="", help="导入目标文件夹 token 或 URL（默认我的空间根目录）")
    p.add_argument("--title", "--name", dest="title", default="", help="导入文档标题（默认取文件名）")
    p.add_argument("--urls", nargs="*", default=[], help="linkify：要插入为引用卡片的飞书文档 URL")
    p.add_argument("--images", nargs="*", default=[], help="insert-images/edit：要插入文档的本地图片（png/jpg/webp/gif/bmp）")
    p.add_argument("--attach", default="", help="insert-file：要作为附件块插入文档的本地任意文件（随附件入云盘）")
    p.add_argument("--find", default="", help="edit：锚点块文本（叶块归一化文本精确匹配，须全文唯一）")
    p.add_argument("--insert-after", action="store_true", help="edit：在锚点块之后插入内容")
    p.add_argument("--insert-before", action="store_true", help="edit：在锚点块之前插入内容")
    p.add_argument("--replace", dest="edit_replace", action="store_true", help="edit：替换锚点块内容")
    p.add_argument("--delete", dest="edit_delete", action="store_true", help="edit：删除锚点块")
    p.add_argument("--delete-next", dest="edit_delete_next", action="store_true", help="edit：删除锚点块的下一个块（用于图片等无文本块）")
    p.add_argument("--text", default="", help="edit：要插入/替换的文本内容（支持 Markdown）")
    p.add_argument("--text-file", default="", help="edit：从本地 Markdown 文件读入内容")
    p.add_argument("--urls-file", default="", help="linkify：从文件读 URL 列表（每行一个，- 表示 stdin）")
    p.add_argument("--after-heading", default="", help="linkify：插入到包含该文本的正文块之后（默认文档末尾）")
    p.add_argument("--direct", action="store_true", help="直接写入模式：用户已明确授权，免指纹核对；仍需 FEISHU_ALLOW_WRITE=1")
    p.add_argument("--confirm-write", default="", help="写入确认指纹")

    p = sub.add_parser("docs-search", help="搜文档（远程网页搜索；--local 搜本地索引/缓存）")
    p.add_argument("keyword")
    p.add_argument("--local", action="store_true")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("probe", help="安全抓取网页接口元数据（不保存正文或凭证）")
    p.add_argument("--scope", choices=["chats", "docs-search", "mail", "calendar", "message-media", "message-send", "media-observe", "all"], default="all")
    p.add_argument("--keyword", default="测试", help="docs-search probe 使用的搜索词")
    p.add_argument("--msg-id", default="", help="message-media probe 使用的消息 ID")
    p.add_argument("--interact", action="store_true",
                   help="media-observe：点击文件卡片触发真实下载（配合 --headed 可手动点）")
    p.add_argument("--capture", action="store_true",
                   help="calendar/mail/message-send 写接口抓包：headed 浏览器里由用户手动操作一次，只记脱敏元数据与结构摘要，不留正文")
    p.add_argument("--out", default="", help="可选：写脱敏 JSON 摘要文件")

    p = sub.add_parser("send", help="默认只产草稿；--confirm-send 或用户明确指定 --direct 才真发")
    p.add_argument("--chat-id", required=True)
    p.add_argument("--text", default="", help="正文（慎用，会进 shell history；优先 --text-file）")
    p.add_argument("--text-file", default="", help="从文件读正文（- 表示 stdin）")
    p.add_argument("--file", default="", help="要发送的本地文件路径")
    p.add_argument("--sticker-from-message", default="", metavar="MSG_ID",
                   help="从账号可见的 type=10 sticker 消息复制发送")
    p.add_argument("--sticker-set-id", default="", metavar="SET_ID",
                   help="sticker set id（需与 --sticker-id 成对使用）")
    p.add_argument("--sticker-id", default="", metavar="STICKER_ID",
                   help="sticker id（需与 --sticker-set-id 成对使用）")
    p.add_argument("--reply-to", default="", help="被回复消息的 msg_id")
    p.add_argument("--at-user-id", action="append", default=[])
    p.add_argument("--confirm-send", default="", metavar="FINGERPRINT")
    p.add_argument("--direct", action="store_true",
                   help="用户已明确授权时直接发送一条；免指纹核对，仍需 FEISHU_ALLOW_SEND=1；仍会探活、单次放行且不自动重试")

    p = sub.add_parser("daemon", help="WebSocket 守护进程：start|stop|restart|status|tail|run")
    p.add_argument("action", choices=["start", "stop", "restart", "status", "tail", "run"])
    p.add_argument("-n", "--lines", type=int, default=50, help="tail 行数")

    p = sub.add_parser("events", help="查本地事件池（daemon 落盘的消息）")
    p.add_argument("action", nargs="?", choices=["read", "status", "migrate", "purge"], default="read",
                   help="read（默认）、status、migrate 或 purge")
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--chat-id", default="")
    p.add_argument("--mentions-only", action="store_true")
    p.add_argument("--include-mine", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--before", default="", help="purge 截止时间（如 30d 或 ISO 日期）")
    p.add_argument("--confirm-purge", action="store_true", help="确认执行可恢复的本地事件清理")

    p = sub.add_parser("mail", help="读取邮件；写操作默认只产草稿")
    mail_sub = p.add_subparsers(dest="mail_action", required=True)
    q = mail_sub.add_parser("list", help="列邮件")
    q.add_argument("--folder", default="inbox")
    q.add_argument("--since", default="24h")
    q.add_argument("--until", default="")
    q.add_argument("--limit", type=int, default=30)
    q = mail_sub.add_parser("search", help="搜索邮件")
    q.add_argument("keyword")
    q.add_argument("--limit", type=int, default=30)
    q = mail_sub.add_parser("get", help="读取一封邮件")
    q.add_argument("message_id")
    q = mail_sub.add_parser("send", help="发邮件；默认只产草稿")
    q.add_argument("--to", action="append", required=True)
    q.add_argument("--subject", required=True)
    q.add_argument("--text-file", required=True)
    q.add_argument("--confirm-write", default="", metavar="FINGERPRINT")
    q = mail_sub.add_parser("reply", help="回复邮件；默认只产草稿")
    q.add_argument("--message-id", required=True)
    q.add_argument("--text-file", required=True)
    q.add_argument("--confirm-write", default="", metavar="FINGERPRINT")

    p = sub.add_parser("calendar", help="读取日历；写操作默认只产草稿")
    cal_sub = p.add_subparsers(dest="calendar_action", required=True)
    q = cal_sub.add_parser("calendars", help="列出可用日历及主日历 ID")
    q = cal_sub.add_parser("list", help="列日程事件")
    q.add_argument("--since", default="24h")
    q.add_argument("--until", default="+7d", help="截止时间；+7d 表示未来 7 天")
    q.add_argument("--limit", type=int, default=50)
    q = cal_sub.add_parser("get", help="读取会议详情")
    q.add_argument("event_id")
    q = cal_sub.add_parser("create", help="创建日程事件；默认只产草稿")
    q.add_argument("--title", required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", required=True)
    q.add_argument("--calendar-id", default="auto", help="目标日历 id；默认显式环境变量，否则自动选择主日历")
    q.add_argument("--description", default="")
    q.add_argument("--confirm-write", default="", metavar="FINGERPRINT")
    q = cal_sub.add_parser("update", help="修改会议；默认只产草稿")
    q.add_argument("event_id")
    q.add_argument("--title", default="")
    q.add_argument("--start", default="")
    q.add_argument("--end", default="")
    q.add_argument("--confirm-write", default="", metavar="FINGERPRINT")
    q = cal_sub.add_parser("delete", help="删除日程；默认只产草稿")
    q.add_argument("--event-uuid", required=True, help="创建时返回的 event_uuid")
    q.add_argument("--start-epoch", type=int, required=True, help="事件的 start_epoch（秒）")
    q.add_argument("--calendar-id", default="", help="目标日历 id（或设 FEISHU_CALENDAR_ID）")
    q.add_argument("--confirm-write", default="", metavar="FINGERPRINT")
    q = cal_sub.add_parser("sync", help="gateway 增量同步日历事件（含 event_uuid）")
    q.add_argument("--calendar-id", default="", help="目标日历 id（或设 FEISHU_CALENDAR_ID）")
    q.add_argument("--since-micros", type=int, default=0, help="增量起点（微秒）；0=全量")

    p = sub.add_parser("agent", help="Command Agent 网关（规则过滤、两阶段指令、Session 隔离）")
    agent_sub = p.add_subparsers(dest="agent_action", required=True)
    q = agent_sub.add_parser("status", help="查看 Agent 配置、会话统计与状态")
    q = agent_sub.add_parser("rules", help="查看或更新 Agent 规则与群组白名单")
    q.add_argument("--json-output", action="store_true", help="以 JSON 格式输出")
    q.add_argument("--set-enabled", choices=["true", "false"], help="设置 Agent 总开关")
    q.add_argument("--add-allowed-chat", help="添加放行群聊（ID 或通配符）")
    q.add_argument("--add-blocked-chat", help="添加屏蔽群聊（ID 或通配符）")
    q = agent_sub.add_parser("process", help="单次处理一条消息事件（用于联调与测试）")
    q.add_argument("msg_id", help="要处理的消息 ID")
    q.add_argument("--send", action="store_true", help="允许真正将生成的回复发送出去（需 FEISHU_ALLOW_SEND=1）")
    q = agent_sub.add_parser("watch", help="监听事件池实时处理 @me 与 P2P 指令")
    q.add_argument("--once", action="store_true", help="仅单次轮询后退出")
    q.add_argument("--poll-interval", type=float, default=2.0, help="轮询间隔（秒）")
    q.add_argument("--send", action="store_true", help="允许真正将生成的回复发送出去（需 FEISHU_ALLOW_SEND=1）")
    return ap


async def _run(args) -> int:
    from _feishu import auth, calendar_ops as calendar, chats, context, daemon, docs_search, documents, mail, media_store, media_worker, message_resources, mentions, messages, probe, send, transport
    from _feishu.errors import AuthRequired, SendBlocked
    from _feishu import config, event_store
    from _feishu.output import emit, emit_json, emit_jsonl, fmt_ts, note, parse_when
    from _feishu import query_lock
    fmt = "json" if args.json else args.format

    if args.cmd == "probe":
        capture = getattr(args, "capture", False)
        if capture and not args.out:
            note("✗ --capture 结果含截断请求 body，必须用 --out 写文件，不落 stdout")
            return 2
        if capture:
            what = (
                "创建一个测试日程（标题如「测试会议」，今天，30 分钟）"
                if args.scope == "calendar"
                else "在测试群发送一条 sticker（只做一次，不要重复操作）"
                if args.scope == "message-send"
                else "发送一封测试邮件（发给自己即可）"
            )
            note(f"浏览器即将打开（有界面）。请在 240 秒内手动{what}；")
            note("抓到写请求后再等 20 秒窗口即自动结束。结果只写入 --out 文件。")
        with query_lock.acquire("probe"):
            result = await asyncio.wait_for(
                probe.run(args.scope, args.keyword, headed=args.headed, msg_id=args.msg_id,
                          interact=getattr(args, "interact", False), capture=capture),
                timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS + (240 if capture else 0),
            )
        if args.out:
            probe.save(result, args.out)
            note(f"✓ 已写入脱敏 probe 摘要：{args.out}")
        if capture:
            emit(
                f"probe scope={result['scope']} requests={len(result.get('requests', []))} "
                f"body_redacted={result.get('body_redacted', False)} "
                f"credentials={result.get('contains_credentials', False)}"
            )
            return 0
        emit_json(result) if fmt == "json" else emit(
            f"probe scope={result['scope']} requests={len(result['requests'])} "
            f"bodies={result['contains_bodies']} credentials={result['contains_credentials']}"
        )
        return 0

    transport.configure()
    if args.cmd == "login":
        with query_lock.acquire("login"):
            me = await (auth.login_manual() if args.manual else auth.login_playwright())
        if fmt == "json":
            emit_json(me)
        return 0

    if args.cmd == "daemon":
        if args.action == "run":
            # _run itself is already inside asyncio.run() (see main).  Calling
            # daemon.run() here would nest asyncio.run() and crash under both
            # systemd and the nohup fallback.
            await daemon.run_async()  # foreground; called by systemd/nohup
        elif args.action == "start":
            daemon.start()
        elif args.action == "stop":
            daemon.stop()
        elif args.action == "restart":
            daemon.stop()
            daemon.start()
        elif args.action == "status":
            st = daemon.status()
            emit_json(st) if fmt == "json" else emit(
                f"管理器: {st.get('manager')}｜运行中: {st.get('running')}\n"
                f"账号: {st.get('my_name') or '?'}｜事件数: {st.get('events_written', 0)}"
                f"｜最近事件: {st.get('last_event_at', '-')}"
            )
        elif args.action == "tail":
            daemon.tail(args.lines)
        return 0

    if args.cmd == "events":
        if args.action == "status":
            status = event_store.storage_status()
            if fmt == "json":
                emit_json(status)
            else:
                emit(
                    f"存储模式: {status['mode']}｜加密: {status['encrypted']}｜"
                    f"文件: {status['active_file']}｜占用: {status['bytes']} / {status['quota_bytes']} 字节"
                )
            return 0
        if args.action == "migrate":
            with query_lock.acquire("events-migrate"):
                result = event_store.prepare_storage()
            emit_json(result) if fmt == "json" else emit(
                f"存储模式: {result['mode']}｜迁移记录: {result.get('migrated', 0)}｜"
                f"旧文件归档: {result.get('archive') or '-'}"
            )
            return 0
        if args.action == "purge":
            if not args.confirm_purge:
                raise SendBlocked(
                    "清理事件池需要 --confirm-purge；旧文件只会移动到可恢复归档目录",
                    hint="例如：events purge --before 30d --confirm-purge",
                )
            before = parse_when(args.before)
            if not before:
                raise ValueError("events purge 需要 --before")
            with query_lock.acquire("events-purge"):
                result = event_store.purge_before(before)
            emit_json(result) if fmt == "json" else emit(
                f"保留 {result['kept']} 条，归档 {result['removed']} 条：{result['moved_to']}"
            )
            return 0
        if not 1 <= args.limit <= config.MAX_EVENT_READ_LIMIT:
            raise ValueError(f"events limit 必须在 1..{config.MAX_EVENT_READ_LIMIT} 之间")
        evs = event_store.read_events(
            since_ms=parse_when(args.since) or 0,
            until_ms=parse_when(args.until) or 0,
            chat_id=args.chat_id,
            mentions_only=args.mentions_only,
            include_mine=args.include_mine,
            limit=args.limit,
        )
        if fmt == "jsonl":
            emit_jsonl(evs)
        elif fmt == "json":
            emit_json(evs)
        else:
            # Events are stored without display names; resolve from the cache
            # (whoami-seeded self + DOM-harvested chat/peer names) at render.
            # This handler runs before auth.prepare, so my_id comes from the
            # daemon state file (offline-friendly).
            msg_evs = [e for e in evs if e.get("kind", "message") == "message"]
            my_id, my_name = chats._daemon_identity()
            await messages.resolve_names(msg_evs, my_id=my_id, my_name=my_name)
            messages.apply_direction(msg_evs, my_id)
            emit(messages.render_markdown(evs, title="事件池") if evs
                 else "（事件池为空——先 feishu.py daemon start 攒消息）")
        return 0

    if args.cmd == "docs-search" and args.local:
        res = await docs_search.search(args.keyword, local=True, limit=args.limit)
        if fmt == "json":
            emit_json(res)
        else:
            for h in res["hits"]:
                from _feishu.output import safe_label
                emit(f"- [{h.get('type','?')}] {safe_label(h.get('name') or '?')} {h.get('url') or h.get('path','')}")
            note(res.get("note", ""))
        return 0

    if args.cmd == "send":
        # Draft is a local operation — no session needed. Cookies are only
        # loaded inside send.send() once the confirmation gates are satisfied.
        text = args.text or ""
        if args.text_file:
            text = (sys.stdin.read() if args.text_file == "-"
                    else open(args.text_file, encoding="utf-8").read())
        has_sticker = bool(
            args.sticker_from_message or args.sticker_set_id or args.sticker_id
        )
        if not text.strip() and not args.file and not has_sticker:
            note("正文、文件和 sticker 均为空，拒绝（空消息没有草稿价值）")
            return 1
        result = await send.send(
            args.chat_id, text, reply_id=args.reply_to,
            at_user_ids=args.at_user_id, file_path=args.file,
            confirm=args.confirm_send, direct=args.direct,
            sticker_from_message=args.sticker_from_message,
            sticker_set_id=args.sticker_set_id,
            sticker_id=args.sticker_id,
        )
        emit_json(result)
        return 0

    if args.cmd == "media":
        if args.action == "download" and not args.out and not args.keep_media:
            raise ValueError("media download 需要 --out 或 --keep-media，否则下载结果不会保留")
        with query_lock.acquire("media"):
            result = await media_worker.run(
                args.action,
                args.msg_id,
                resource_id=args.resource_id,
                keep_media=args.keep_media,
                out=args.out,
            )
        if fmt == "json":
            emit_json(result)
        else:
            emit(media_worker.render_markdown(result))
        return 0

    if args.cmd == "media-cache":
        if args.action == "status":
            result = media_store.status()
        else:
            if not args.confirm_purge:
                raise ValueError("media-cache purge 需要 --confirm-purge")
            with query_lock.acquire("media-cache-purge"):
                result = media_store.purge_all()
        if fmt == "json":
            emit_json(result)
        else:
            emit("\n".join(f"- {key}: {value}" for key, value in result.items()))
        return 0

    if args.cmd == "mail" and args.mail_action in {"send", "reply"}:
        if args.confirm_write:
            await auth.prepare()  # 确认执行前必须绑定会话，否则网关 401
        body = open(args.text_file, encoding="utf-8").read()
        if args.mail_action == "send":
            result = await mail.send_message(
                args.to, args.subject, body, confirm_write=args.confirm_write
            )
        else:
            result = await mail.reply_message(
                args.message_id, body, confirm_write=args.confirm_write
            )
        emit_json(result)
        return 0

    if args.cmd == "calendar" and args.calendar_action in {"create", "update", "delete"}:
        me = {}
        if args.confirm_write:
            me = await auth.prepare()  # 确认执行前必须绑定会话，否则网关 401（payloadType=5）
        if args.calendar_action == "create":
            result = await calendar.create_event(
                args.title, args.start, args.end, confirm_write=args.confirm_write,
                calendar_id=args.calendar_id, description=args.description,
                my_id=me.get("user_id", ""), my_name=me.get("name", ""),
            )
        elif args.calendar_action == "delete":
            result = await calendar.delete_event(
                args.event_uuid, args.start_epoch, confirm_write=args.confirm_write,
                calendar_id=args.calendar_id,
            )
        else:
            result = await calendar.update_event(
                args.event_id, args.title, args.start, args.end,
                confirm_write=args.confirm_write,
            )
        emit_json(result)
        return 0

    if args.cmd == "calendar" and args.calendar_action == "sync":
        me = await auth.prepare()
        result = await calendar.sync_events(
            calendar_id=args.calendar_id, since_micros=args.since_micros,
            my_id=me.get("user_id", ""), my_name=me.get("name", ""))
        emit_json(result)
        return 0

    if args.cmd == "agent":
        from _feishu import agent_dispatcher, agent_rules, agent_worker
        if args.agent_action == "status":
            st = agent_worker.get_agent_status()
            if fmt == "json":
                emit_json(st)
            else:
                emit("Command Agent 网关状态：")
                emit(f"- 运行开关: {'开启' if st['enabled'] else '关闭'}")
                emit(f"- 活跃会话数: {st['total_active_sessions']}")
                emit(f"- 放行群聊: {', '.join(st['allowed_chats']) if st['allowed_chats'] else '所有群聊（排除屏蔽项）'}")
                emit(f"- 屏蔽群聊: {', '.join(st['blocked_chats']) if st['blocked_chats'] else '无'}")
                emit(f"- 敏感话题词数: {st['blocked_topics_count']}")
                emit(f"- 单人每日上限: {st['max_daily_per_user']} 次 (冷却间隔 {st['cooldown_seconds']}s)")
                emit(f"- 规则文件: {st['rules_file']}")
            return 0
        if args.agent_action == "rules":
            rules = agent_rules.load_rules()
            changed = False
            if getattr(args, "set_enabled", None) is not None:
                rules["enabled"] = (args.set_enabled == "true")
                changed = True
            if getattr(args, "add_allowed_chat", None):
                rules.setdefault("allowed_chats", []).append(args.add_allowed_chat)
                changed = True
            if getattr(args, "add_blocked_chat", None):
                rules.setdefault("blocked_chats", []).append(args.add_blocked_chat)
                changed = True
            if changed:
                agent_rules.save_rules(rules)
                note("✓ 已更新 Agent 规则配置")
            if fmt == "json" or getattr(args, "json_output", False):
                emit_json(rules)
            else:
                emit(json.dumps(rules, ensure_ascii=False, indent=2))
            return 0
        if args.agent_action == "process":
            if getattr(args, "send", False):
                await auth.prepare()
            res = await agent_worker.process_msg_id(args.msg_id, send_reply=getattr(args, "send", False))
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"处理结果: status={res.get('status')} action={res.get('action')}")
                if res.get("reply"):
                    emit(f"\n回复内容:\n{res['reply']}")
            return 0
        if args.agent_action == "watch":
            if getattr(args, "send", False):
                await auth.prepare()
            await agent_worker.watch_events_loop(
                poll_interval=getattr(args, "poll_interval", 2.0),
                once=getattr(args, "once", False),
                send_reply=getattr(args, "send", False),
            )
            return 0

    # everything below needs a live session
    try:
        me = await auth.prepare()
    except AuthRequired as e:
        if args.unattended:
            import shutil
            import subprocess
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", "飞书 feishu skill", f"会话失效：{e}"], check=False)
            raise
        note(f"会话不可用：{e}。{e.hint or '跑 feishu.py login 重新登录'}")
        raise

    if args.cmd == "whoami":
        if fmt == "json":
            emit_json(me)
        else:
            emit(f"{me['name']}（user_id: {me['user_id']}）")
        return 0

    if args.cmd == "message":
        with query_lock.acquire("message"):
            message = await asyncio.wait_for(
                message_resources.fetch_message(args.msg_id),
                timeout=config.QUERY_TIMEOUT_SECONDS,
            )
            await messages.resolve_names([message], my_id=me["user_id"], my_name=me.get("name", ""))
            messages.apply_direction([message], me["user_id"])
        if fmt == "json":
            emit_json(messages.render_json([message])[0])
        else:
            emit(messages.render_markdown([message], title="消息"))
        return 0

    if args.cmd == "mail":
        with query_lock.acquire(f"mail-{args.mail_action}"):
            if args.mail_action == "list":
                result = await asyncio.wait_for(
                    mail.list_messages(
                        folder=args.folder,
                        start_time=parse_when(args.since) // 1000 if parse_when(args.since) else 0,
                        end_time=parse_when(args.until) // 1000 if parse_when(args.until) else 0,
                        limit=args.limit,
                    ),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
            elif args.mail_action == "search":
                result = await asyncio.wait_for(
                    mail.search_messages(args.keyword, limit=args.limit),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
            else:
                result = await asyncio.wait_for(
                    mail.get_message(args.message_id),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
        if fmt == "json":
            emit_json(result)
        elif fmt == "jsonl":
            emit_jsonl(result.get("items", [result]))
        else:
            emit(mail.render_markdown(result))
        return 0

    if args.cmd == "calendar":
        with query_lock.acquire(f"calendar-{args.calendar_action}"):
            if args.calendar_action == "calendars":
                result = await asyncio.wait_for(
                    calendar.list_calendars(
                        my_id=me["user_id"], my_name=me.get("name", "")
                    ),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
            elif args.calendar_action == "list":
                result = await asyncio.wait_for(
                    calendar.list_events(
                        start_time=parse_when(args.since) // 1000 if parse_when(args.since) else 0,
                        end_time=parse_when(args.until) // 1000 if parse_when(args.until) else 0,
                        limit=args.limit,
                    ),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
            else:
                result = await asyncio.wait_for(
                    calendar.get_event(args.event_id),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
        if fmt == "json":
            emit_json(result)
        elif fmt == "jsonl":
            emit_jsonl(result.get("items", [result]))
        else:
            emit(calendar.render_markdown(result))
        return 0

    if args.cmd == "chats":
        with query_lock.acquire("chats"):
            lst = await asyncio.wait_for(
                chats.list_chats(from_events=args.from_events),
                timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
            )
        if fmt == "json":
            emit_json(lst)
        elif fmt == "jsonl":
            emit_jsonl(lst)
        else:
            from _feishu.output import safe_label

            lines = []
            for c in lst:
                lines.append(
                    f"- `{c['chat_id']}` {safe_label(c.get('name') or '（群名未取到）')}"
                    f" [{c.get('chat_type','?')}] 最近: {fmt_ts(c.get('last_time'))} {c.get('last_text','')}"
                )
            emit("\n".join(lines) or "（空）")
        return 0

    if args.cmd == "history":
        with query_lock.acquire("history"):
            res = await asyncio.wait_for(
                messages.history(
                    args.chat_id,
                    parse_when(args.since) // 1000 if parse_when(args.since) else 0,
                    parse_when(args.until) // 1000 if parse_when(args.until) else 0,
                    limit=args.limit,
                    my_id=me["user_id"],
                    my_name=me.get("name", ""),
                ),
                timeout=config.QUERY_TIMEOUT_SECONDS,
            )
        if fmt == "json":
            emit_json({
                "messages": messages.render_json(res["messages"]),
                "has_more": res["has_more"],
                "page_limit_hit": res.get("page_limit_hit", False),
                "search_returned_count": res.get("search_returned_count", 0),
                "returned_count": res.get("returned_count", len(res["messages"])),
                "filtered_count": res.get("filtered_count", 0),
                "source": res.get("source", "gateway_11021"),
                "index_lag_suspected": res.get("index_lag_suspected", False),
            })
        else:
            emit(messages.render_markdown(res["messages"], title=f"群 {args.chat_id} 历史"))
            if res.get("page_limit_hit"):
                note(f"（消息太多，搜索到达 {config.SEARCH_MAX_PAGES} 页上限，以上是部分结果；"
                     "收窄时间窗可拿全，或设 FEISHU_SEARCH_MAX_PAGES 提高页数上限）")
            elif res["has_more"]:
                note("（还有更多，调大 --limit 或收窄时间窗）")
            if res.get("index_lag_suspected"):
                note("（搜索索引可能有延迟：返回的消息被时间窗过滤或暂未命中；不要据此断言刚发送失败）")
        return 0

    if args.cmd == "mentions":
        chat_ids = args.chat_id or await chats.known_chat_ids()
        if not chat_ids:
            note("还没有 daemon 事件可推断群列表；先 daemon start，或用 --chat-id 指定")
            return 1
        with query_lock.acquire("mentions"):
            res = await asyncio.wait_for(
                mentions.find_mentions(
                    me["user_id"], chat_ids,
                    parse_when(args.since) // 1000 if parse_when(args.since) else 0,
                    parse_when(args.until) // 1000 if parse_when(args.until) else 0,
                    limit_per_chat=args.limit,
                    my_name=me.get("name", ""),
                ),
                timeout=config.QUERY_TIMEOUT_SECONDS,
            )
        if fmt == "json":
            emit_json(res)
        else:
            emit(messages.render_markdown(res["mentions"], title="@我 的消息"))
            note(f"检索了 {res['chats_searched']} 个群"
                 + (f"，{len(res['chats_failed'])} 个失败" if res["chats_failed"] else "")
                 + ("，群列表已截断" if res.get("chats_truncated") else ""))
        return 0

    if args.cmd == "thread":
        with query_lock.acquire("thread"):
            ctx = await asyncio.wait_for(
                context.thread_context(args.msg_id, window_min=args.window, limit=args.limit,
                                       my_id=me["user_id"], my_name=me.get("name", "")),
                timeout=config.QUERY_TIMEOUT_SECONDS,
            )
        emit_json(ctx) if fmt == "json" else emit(context.render_markdown(ctx))
        return 0

    if args.cmd == "doc":
        if args.ref == "linkify":
            if not args.file_path:
                note("用法：feishu.py doc linkify <文档链接或token> [--urls URL ...] [--urls-file 文件] [--after-heading 文本]")
                return 2
            urls = list(args.urls)
            if args.urls_file:
                if args.urls_file != "-" and os.path.getsize(args.urls_file) > 256 * 1024:
                    note("linkify --urls-file 文件过大（上限 256 KiB）")
                    return 2
                src = sys.stdin if args.urls_file == "-" else open(args.urls_file, encoding="utf-8")
                with src:
                    urls.extend(line.strip() for line in src if line.strip() and not line.startswith("#"))
            if not urls:
                note("linkify 需要至少一个 URL（--urls 或 --urls-file）")
                return 2
            if len(urls) > documents.MAX_LINKIFY_URLS:
                note(f"linkify 一次最多 {documents.MAX_LINKIFY_URLS} 个 URL（当前 {len(urls)} 个）")
                return 2
            with query_lock.acquire("doc-linkify"):
                res = await documents.linkify_document(
                    ref=args.file_path,
                    urls=urls,
                    after_heading=args.after_heading,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 引用卡片插入完成（{res.get('status')}）：{res.get('inserted')}/{res.get('url_count')} 成功"
                     + (f"，失败 {len(res.get('failed') or [])} 个" if res.get("failed") else "")
                     + (f"，跳过 {len(res.get('skipped') or [])} 个" if res.get("skipped") else "")
                     + f"；重载复核 {len(res.get('reload_verified') or [])} 条、导出复核 {len(res.get('export_verified') or [])} 条"
                     + f"\n- 链接：{res.get('url')}")
                for fail in res.get("failed") or []:
                    emit(f"  ✗ 未转换（已回退并确认恢复原样）：{fail}")
                rb = res.get("rollback_unconfirmed")
                if rb:
                    emit(f"  ⚠️ 回退未能确认：{rb.get('url')}，块 {rb.get('block_id')} 需人工检查")
                    emit(f"     期望：{json.dumps(rb.get('expected'), ensure_ascii=False)}")
                    emit(f"     实际：{json.dumps(rb.get('actual'), ensure_ascii=False)}")
            ok = (res.get("status") == "linkified" and res.get("verified")
                  and not res.get("rollback_unconfirmed"))
            return 0 if ok else 1

        if args.ref == "block-map":
            if not args.file_path:
                note("用法：feishu.py doc block-map <文档链接或token>")
                return 2
            with query_lock.acquire("doc-block-map"):
                res = await documents.block_map(args.file_path)
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"# {res.get('title')}（{res.get('count')} 个叶块）")
                for b in res.get("blocks", []):
                    emit(f"[{b['i']:>3}] {b['type']:<8} #{b['id']}  {b['text']}")
            return 0

        if args.ref == "edit":
            if not args.file_path:
                note("用法：feishu.py doc edit <文档链接或token> --find 锚点 --insert-after|--replace|--delete|--delete-next [--text 文本|--text-file 文件|--images 图片...]")
                return 2
            ops = [name for name, flag in (("insert-after", args.insert_after), ("insert-before", args.insert_before),
                                           ("replace", args.edit_replace),
                                           ("delete", args.edit_delete), ("delete-next", args.edit_delete_next)) if flag]
            if len(ops) != 1:
                note("doc edit 需要且只能指定一种操作：--insert-after / --insert-before / --replace / --delete / --delete-next")
                return 2
            if not args.find:
                note("doc edit 需要 --find 锚点文本")
                return 2
            with query_lock.acquire("doc-block-edit"):
                res = await documents.edit_block(
                    ref=args.file_path,
                    find=args.find,
                    op=ops[0],
                    text=args.text,
                    text_file=args.text_file,
                    images=args.images,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 块级编辑完成（{res.get('status')}）：{res.get('op')} — {res.get('detail')}"
                     + f"\n- 锚点：{res.get('anchor')}"
                     + f"\n- 链接：{res.get('url')}"
                     + ("" if res.get("reload_verified") else "\n  ⚠️ 重载复核未通过，请人工确认"))
            return 0 if res.get("reload_verified") else 1

        if args.ref == "insert-images":
            if not args.file_path:
                note("用法：feishu.py doc insert-images <文档链接或token> --images 图片... [--after-heading 文本]")
                return 2
            if not args.images:
                note("insert-images 需要 --images 指定至少一张本地图片")
                return 2
            if len(args.images) > documents.MAX_INSERT_IMAGES:
                note(f"insert-images 一次最多 {documents.MAX_INSERT_IMAGES} 张（当前 {len(args.images)} 张）")
                return 2
            with query_lock.acquire("doc-insert-images"):
                res = await documents.insert_images(
                    ref=args.file_path,
                    images=args.images,
                    after_heading=args.after_heading,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 图片插入完成（{res.get('status')}）：成功 {len(res.get('inserted') or [])} 张"
                     + (f"，失败 {len(res.get('failed') or [])} 张" if res.get("failed") else "")
                     + f"；重载复核 {len(res.get('reload_verified') or [])} 张"
                     + f"\n- 链接：{res.get('url')}")
                for fail in res.get("failed") or []:
                    emit(f"  ✗ 未插入（已回退）：{fail}")
                rb = res.get("rollback_unconfirmed")
                if rb:
                    emit(f"  ⚠️ 回退未能确认：{rb.get('image')}，块 {rb.get('block_id')} 需人工检查")
            return 0 if res.get("verified") else 1

        if args.ref == "insert-file":
            if not args.file_path:
                note("用法：feishu.py doc insert-file <文档链接或token> --attach 本地文件 [--after-heading 文本]")
                return 2
            if not args.attach:
                note("insert-file 需要 --attach 指定本地文件")
                return 2
            with query_lock.acquire("doc-insert-file"):
                res = await documents.insert_file(
                    ref=args.file_path,
                    file_path=args.attach,
                    after_heading=args.after_heading,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 附件插入完成（{res.get('status')}）：{', '.join(res.get('inserted') or []) or '无'}"
                     + (f"，失败：{', '.join(res.get('failed') or [])}" if res.get("failed") else "")
                     + f"\n- 链接：{res.get('url')}")
                rb = res.get("rollback_unconfirmed")
                if rb:
                    emit(f"  ⚠️ 回退未能确认：{rb.get('file')}，块 {rb.get('block_id')} 需人工检查")
            return 0 if res.get("verified") else 1

        if args.ref in ("replace", "update"):
            if not args.file_path or not args.replacement_file:
                note("用法：feishu.py doc replace <文档链接或token> <本地Markdown文件>")
                return 2
            with query_lock.acquire("doc-replace"):
                res = await documents.replace_document(
                    ref=args.file_path,
                    file_path=args.replacement_file,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 文档已原地更新！\n- 链接：{res.get('url')}\n- Token：`{res.get('token')}`")
            return 0

        import_target = (
            args.import_file
            or (args.file_path if args.ref in ("import", "create", "write", "upload") else "")
            or (args.ref if args.ref and os.path.isfile(args.ref) else "")
        )
        if import_target:
            with query_lock.acquire("doc-import"):
                res = await documents.import_file(
                    file_path=import_target,
                    title=args.title,
                    folder=args.folder,
                    confirm=args.confirm_write,
                    direct=args.direct,
                )
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 文档导入成功！\n- 标题：{res.get('name')}\n- 链接：{res.get('url')}\n- Token：`{res.get('token')}`")
            return 0

        if args.list:
            with query_lock.acquire("doc-list"):
                spaces = await asyncio.wait_for(
                    documents.list_spaces(), timeout=config.QUERY_TIMEOUT_SECONDS
                )
            if fmt == "json":
                emit_json(spaces)
            else:
                from _feishu.output import safe_label

                for label, key in (("我的空间", "my_space"), ("共享空间", "shared_space")):
                    emit(f"\n## {label}")
                    for n in spaces[key]:
                        emit(f"- [{n['type']}] {safe_label(n['name'])} `{n['token']}` {n.get('url','')}")
                for error in spaces.get("errors", []):
                    emit(f"- ⚠️ {error.get('space', '?')}：{error.get('error', 'unknown')}")
            return 0 if (spaces.get("complete", False) or spaces.get("my_space")) else 1
        if args.walk:
            note("遍历云空间（进度在 stderr）…")
            with query_lock.acquire("doc-walk"):
                res = await asyncio.wait_for(
                    documents.walk(max_nodes=args.max_nodes),
                    timeout=config.WALK_TIMEOUT_SECONDS,
                )
            note(f"节点 {len(res['nodes'])}，失败 {len(res['errors'])}，"
                 f"{'完整' if res['complete'] else '不完整'}")
            if fmt == "json":
                emit_json(res)
            else:
                for n in res["nodes"]:
                    emit(f"- [{n['type']}] {n['path']} `{n['token']}`")
            return 0 if (res.get("nodes") or res.get("complete")) else 1
        if args.screenshot_dir:
            if not args.ref:
                note("doc --screenshot-dir 需要一个文档链接/token")
                return 2
            with query_lock.acquire("doc-screenshot"):
                res = await documents.screenshot_document(args.ref, args.screenshot_dir)
            if fmt == "json":
                emit_json(res)
            else:
                emit(f"✅ 抓取完成：{len(res.get('images', []))} 张图片 → {res.get('out_dir')}")
                for im in res.get("images", []):
                    emit(f"  - {im}")
            return 0 if res.get("images") else 1

        if not args.ref:
            note("doc 需要一个文档链接/token，或 --list / --walk")
            return 2
        with query_lock.acquire("doc-export"):
            data = await asyncio.wait_for(
                documents.export_document(args.ref, args.fmt, fallback=not getattr(args, "strict", False)),
                timeout=config.QUERY_TIMEOUT_SECONDS,
            )
        if args.fmt == "markdown" and not args.out:
            # 文档正文是第三方可控内容：打到 stdout（agent 会读）时包显式定界；
            # --out 落盘的是原文，不带标记。
            from _feishu.output import untrusted

            emit(untrusted(data.decode("utf-8", errors="replace"), f"doc:{args.ref}"))
        else:
            if args.out:
                out = args.out
            else:
                import hashlib
                safe_ref = hashlib.sha256(args.ref.encode()).hexdigest()[:24]
                out = f"feishu-{safe_ref}.{args.fmt}"
            with open(out, "wb") as f:
                f.write(data)
            note(f"✓ 已写入 {out}（{len(data)} 字节）")
        return 0

    if args.cmd == "docs-search":
        if args.local:
            res = await docs_search.search(args.keyword, local=True, limit=args.limit)
        else:
            with query_lock.acquire("docs-search"):
                res = await asyncio.wait_for(
                    docs_search.search(args.keyword, local=False, limit=args.limit),
                    timeout=config.BROWSER_QUERY_TIMEOUT_SECONDS,
                )
        if fmt == "json":
            emit_json(res)
        else:
            for h in res["hits"]:
                from _feishu.output import safe_label
                emit(f"- [{h.get('type','?')}] {safe_label(h.get('name') or '?')} {h.get('url') or h.get('path','')}")
            note(res.get("note", ""))
        return 0

    return 1


_GLOBAL_VALUE_FLAGS = ("--format",)
_GLOBAL_BOOL_FLAGS = ("--json", "--unattended", "--headed")


def _split_global_flags(argv: list[str]) -> tuple[dict, list[str]]:
    """Pull global flags out of argv wherever they appear.

    argparse subparsers only accept options defined on the top-level parser
    *before* the subcommand, so ``feishu.py message <id> --format json`` used
    to die with an opaque usage error.  Hoisting the four global flags here
    makes ``feishu.py --format json message <id>`` and the more natural
    trailing form equivalent.  Only exact flag tokens are touched, so values
    belonging to other options (``--text "--format"``) are left alone.
    """
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


def main() -> int:
    globals_, rest = _split_global_flags(sys.argv[1:])
    args = build_parser().parse_args(rest)
    for key, value in globals_.items():
        setattr(args, key, value)
    if getattr(args, "format", "markdown") not in ("markdown", "json", "jsonl"):
        print(f"✗ --format 只支持 markdown|json|jsonl（收到 {args.format!r}）", file=sys.stderr)
        return 2
    _apply_process_limit(args.cmd)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        from _feishu.errors import FeishuError, SendBlocked
        from _feishu.output import scrub, scrubbed_exc

        if isinstance(e, SendBlocked) and e.hint:
            # draft path: the draft JSON is data → stdout
            print(e.hint)
        msg = scrubbed_exc(e) if not isinstance(e, FeishuError) else scrub(str(e))
        print(f"✗ {msg}", file=sys.stderr)
        if isinstance(e, FeishuError) and e.hint and not isinstance(e, SendBlocked):
            print(f"  提示：{scrub(e.hint)}", file=sys.stderr)
        return e.exit_code if isinstance(e, FeishuError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
