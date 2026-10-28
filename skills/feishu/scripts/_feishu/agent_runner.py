"""Execution handlers for Feishu Command Agent directives.

Implements:
- parse_command: Directive extractor (/qa, /diag, /doc, /reset, /help, /sponsor)
- get_stage1_menu: Zero-token menu prompt for bare @mentions
- handle_help: User guide and quota inquiry
- handle_sponsor: Token sponsorship and coffee tip card
- handle_reset: Session renewal
- handle_doc: Knowledge base search
- handle_diag: Local/remote diagnostic tool runner
- handle_qa: Claude Code headless session execution with domain prompt
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .output import note, scrub


def parse_command(text: str) -> tuple[str | None, str]:
    """Extract command and argument from input text.

    Strips <at user_id="...">...</at> and leading '@name' prefixes.
    Returns:
        (command_name_lower, argument_text) if a slash command is found.
        (None, cleaned_text) if no command is specified.
    """
    if not text:
        return None, ""

    # Strip XML-style <at ...>...</at>
    cleaned = re.sub(r"<at\s+[^>]*>.*?</at>", "", text, flags=re.DOTALL)
    # Strip leading @name patterns
    cleaned = re.sub(r"^\s*@[^\s]+\s*", "", cleaned)
    cleaned = cleaned.strip()

    # Match /command [arguments]
    match = re.match(r"^/([a-zA-Z0-9_\-]+)(?:\s+(.*))?$", cleaned, flags=re.DOTALL)
    if match:
        cmd = match.group(1).lower()
        arg = (match.group(2) or "").strip()
        return cmd, arg

    return None, cleaned


def get_stage1_menu(user_name: str = "", assistant_name: str = "自动化技术助手") -> str:
    """Return the zero-token instruction menu when mentioned without a command."""
    greeting = f"@{user_name} " if user_name else ""
    return (
        f"{greeting}🤖 {assistant_name}在线中（本人当前离线/忙碌）：\n\n"
        "如需自动化技术协助，请在群内回复时带上对应指令：\n\n"
        "🔹 [/qa <问题>] — 技术问答（基于源码与技术知识库回答疑问）\n"
        "🔹 [/diag <IP或SN> [check|system]] — 设备排查（调用底层工具自动诊断）\n"
        "🔹 [/doc <关键词>] — 知识库检索（查询技术排查手册与协议规范）\n"
        "🔹 [/reset] — 重置当前对话会话记忆\n"
        "🔹 [/help] — 查看指令完整说明与今日剩余额度\n"
        "🔹 [/sponsor] — Token 成本说明与咖啡投喂\n\n"
        "⚠️ 考勤、请假、交付排期等非技术管理事宜请直接联系项目负责人或待本人回岗。"
    )


def handle_help(session: dict[str, Any], rules: dict[str, Any]) -> str:
    """Generate command manual and quota status."""
    today = time.strftime("%Y-%m-%d")
    counts = session.get("daily_counts") or {}
    today_count = int(counts.get(today, 0))
    rl = rules.get("rate_limit") or {}
    max_daily = int(rl.get("max_daily_requests_per_user") or 6)
    cooldown = int(rl.get("cooldown_seconds") or 30)

    return (
        "📖 飞书技术助手使用指南：\n\n"
        "【可用指令】\n"
        "1. /qa <技术问题>：针对系统架构、驱动与代码实现进行技术咨询；\n"
        "2. /diag <IP或SN> [check|system]：自动执行关键硬件/系统链路体检并出具定性结论；\n"
        "3. /doc <关键词>：检索在线技术排查手册与通信协议文档；\n"
        "4. /reset：清空当前会话记忆，分配全新的 Claude 会话；\n"
        "5. /sponsor：查看 Token 消耗说明与咖啡投喂说明。\n\n"
        f"📊 今日额度状态：已用 {today_count} / {max_daily} 次（每次最小间隔 {cooldown} 秒）\n"
        "⚠️ 仅限技术研发与业务协同，管理及排期事项请待本人回岗。"
    )


def handle_sponsor(session: dict[str, Any], rules: dict[str, Any]) -> str:
    """Generate token sponsorship and cost explanation card."""
    today = time.strftime("%Y-%m-%d")
    counts = session.get("daily_counts") or {}
    today_count = int(counts.get(today, 0))
    rl = rules.get("rate_limit") or {}
    max_daily = int(rl.get("max_daily_requests_per_user") or 6)

    sponsor = rules.get("sponsor") or {}
    qr_hint = sponsor.get("qr_code_hint") or "请见飞书个人签名或联系本人"

    return (
        "☕ Token 赞助与技术支持说明：\n\n"
        "为了保证关键产线与紧急排查资源不被滥用，助手为每位同事提供每日免费额度。\n"
        f"当前您的今日用量：{today_count} / {max_daily} 次。\n\n"
        "如遇突发严重业务阻塞且额度耗尽，可扫码投喂 Token 咖啡提频，或联系负责人加急处理：\n"
        f"👉 收款码：{qr_hint}\n\n"
        "感谢对技术工作的理解与支持！"
    )


def handle_reset(session: dict[str, Any]) -> str:
    """Reset session context."""
    from . import agent_session
    chat_id = session.get("chat_id", "")
    from_id = session.get("from_id", "")
    thread_id = session.get("thread_id", "")
    agent_session.reset_session(chat_id, from_id=from_id, thread_id=thread_id)
    return "✅ 当前对话上下文已成功重置，已为您分配全新的 Claude 会话。现在可以提出新的技术问题。"


async def handle_doc(query: str, rules: dict[str, Any] | None = None) -> str:
    """Perform document search across local index and cached docs."""
    q = query.strip()
    if not q:
        return "用法: /doc <关键词>\n示例: /doc 架构设计\n      /doc 排查手册"

    # Search local index if available
    hits = []
    if config.DOCS_INDEX.exists():
        try:
            with open(config.DOCS_INDEX, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        doc = json.loads(line)
                        title = doc.get("title") or doc.get("name") or ""
                        url = doc.get("url") or ""
                        token = doc.get("token") or ""
                        if q.lower() in title.lower():
                            hits.append({"title": title, "url": url or f"https://{config.DOMAIN}/docx/{token}"})
                    except ValueError:
                        continue
        except OSError:
            pass

    # Optional preset documentation references from rules
    preset_docs = (rules or {}).get("preset_docs", [])
    matched_presets = []
    for doc in preset_docs:
        keywords = doc.get("keywords", [])
        if any(kw.lower() in q.lower() for kw in keywords):
            matched_presets.append(doc)

    all_hits = matched_presets + [h for h in hits if h.get("url") not in [p.get("url") for p in matched_presets]]
    if not all_hits:
        return f"未检索到与「{q}」直接匹配的技术文档。建议精简关键词重试。"

    lines = [f"📚 检索到与「{q}」相关的技术排查文档："]
    for i, item in enumerate(all_hits[:5], 1):
        lines.append(f"{i}. [{item['title']}]({item['url']})")
    lines.append("\n可直接点击链接查看完整内容。")
    return "\n".join(lines)


async def handle_diag(arg: str) -> str:
    """Run diagnostics on a target machine and produce concise summary."""
    tokens = arg.strip().split()
    if not tokens:
        return (
            "用法: /diag <IP或SN> [check|system]\n"
            "示例:\n"
            "  /diag 192.168.1.100 check     (执行基础健康检查)\n"
            "  /diag 192.168.1.100 system    (执行系统链路全量诊断)"
        )

    target = tokens[0]
    subsystem = tokens[1].lower() if len(tokens) > 1 else "check"

    # Search for diagnostic tools in environment or standard paths
    env_tool_dir = os.environ.get("AGENT_DIAG_TOOL_DIR")
    tool_dirs = [Path(env_tool_dir)] if env_tool_dir else []
    tool_dirs.extend([
        Path.home() / ".local" / "bin",
        Path("/usr/local/bin"),
    ])

    diag_tool = None
    for d in tool_dirs:
        cand = d / f"diag-{subsystem}.sh"
        if cand.is_file():
            diag_tool = cand
            break
        cand_default = d / "device-check.sh"
        if cand_default.is_file():
            diag_tool = cand_default
            break

    if not diag_tool:
        return f"【设备排查】已接收目标 [{target}] 的排查指令 ({subsystem})。未在系统路径找到配置的设备诊断执行脚本。"

    try:
        proc = await asyncio.create_subprocess_exec(
            str(diag_tool), target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35.0)
        out_text = stdout.decode(errors="replace")
        return f"🔍 目标设备 [{target}] 自动化排查报告：\n\n{out_text[-1200:].strip()}"
    except asyncio.TimeoutError:
        return f"【设备排查超时】对目标 [{target}] 排查超时（>35s），请确认设备是否开机或网络通畅。"
    except Exception as e:
        return f"【设备排查异常】启动体检工具失败: {e}"


async def handle_qa(question: str, session: dict[str, Any]) -> str:
    """Run Claude Code session or fallback knowledge responder."""
    q = question.strip()
    if not q:
        return "用法: /qa <技术问题>\n示例: /qa 系统在网络丢包时如何自愈？"

    claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    claude_session_id = session.get("claude_session_id")

    from_display = session.get("from_name") or session.get("from_id") or "user"
    session_name = f"feishu-{from_display}"

    system_prompt = (
        "你是自动化技术助手。你的受众是团队研发工程师与技术同行。\n"
        "回答原则：\n"
        "1. 纯硬核技术论证，严禁寒暄客套（不要说'你好'、'很高兴为你解答'、'祝您生活愉快'）；\n"
        "2. 简练自然、直击要点，像真人资深研发打字，不堆砌排比句，不写AI模板腔；\n"
        "3. 严格禁止就排期、考勤、请假、人员管理、责任分摊发表任何承诺或看法；\n"
        "4. 专业知识涵盖：系统架构、通信协议、软硬件接口与技术排查手册。"
    )

    if os.path.isfile(claude_bin):
        base_cmd = [
            claude_bin,
            "-p", q,
            "--append-system-prompt", system_prompt,
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--name", session_name,
        ]
        history = session.get("history") or []
        cmd = list(base_cmd)
        if history and claude_session_id:
            cmd.extend(["--resume", claude_session_id])
        elif claude_session_id:
            cmd.extend(["--session-id", claude_session_id])

        run_cwd = os.environ.get("AGENT_WORKSPACE_DIR") or os.getcwd()

        try:
            # Run claude -p with 120s timeout
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            raw_out = stdout.decode(errors="replace").strip()
            err_text = stderr.decode(errors="replace").strip()

            # If resume failed because session was not found, retry once with a fresh session ID
            if "No conversation found with session ID" in (raw_out + err_text):
                import uuid
                new_sid = str(uuid.uuid4())
                session["claude_session_id"] = new_sid
                session["history"] = []
                cmd_retry = list(base_cmd) + ["--session-id", new_sid]
                proc = await asyncio.create_subprocess_exec(
                    *cmd_retry,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=run_cwd,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                raw_out = stdout.decode(errors="replace").strip()

            # Clean harness debug banners like [claude-code:...]
            cleaned_lines = []
            for line in raw_out.splitlines():
                if line.startswith("[claude-code:") or line.startswith("[claude:"):
                    continue
                cleaned_lines.append(line)
            reply = "\n".join(cleaned_lines).strip()
            if reply:
                return reply
        except asyncio.TimeoutError:
            return "⚠️ 会话响应超时（>120s）。问题较复杂时请拆分后重新提问。"
        except Exception as e:
            note(f"claude -p execution failed: {e}")

    # Fallback knowledge responder when Claude CLI is not executable
    return (
        f"已收到技术疑问：\n> {q}\n\n"
        "💡 常见参考：\n"
        "- 请在本地知识库或排查手册中检索相关模块文档（输入 /doc <关键词>）；\n"
        "- 若需更深入代码走读，请待本人回岗后连线确认。"
    )
