"""Rules engine and access control for the Feishu Command Agent Gateway.

Defines group whitelist/blacklist, topic boundaries, anti-abuse rate limits,
and token sponsorship prompts.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any

from . import config

DEFAULT_RULES: dict[str, Any] = {
    "enabled": True,
    "assistant_name": "技术助手",
    # 允许接入的会话/群聊列表。为空表示允许所有未被屏蔽的群聊和私聊。
    # 支持具体 chat_id，或者群名称通配符（如 "*技术*", "*支持*", "*制备*"）。
    "allowed_chats": [],
    # 绝对静默群聊。即使 @本人 也保持绝对沉默，不触发任何机器消息。
    # 适用于全员大群、管理通报群、高管在场的敏感情报群等。
    "blocked_chats": [
        "*全员*",
        "*大群*",
        "*通知*",
        "*通报*",
    ],
    # 允许交互的用户名单。为空表示允许所有同事。
    "allowed_users": [],
    # 屏蔽用户名单（防恶意轰炸与骚扰）。
    "blocked_users": [],
    # 敏感与非技术管理话题黑名单。命中时拒绝代答，严守工作边界。
    "blocked_topics": [
        "请假", "假条", "出勤", "打卡", "考勤", "病假", "事假", "年假",
        "排期", "什么时候交", "截止日期", "deadline", "催", "几点给", "进度如何",
        "薪酬", "工资", "年终奖", "绩效", "背锅", "责任归属", "处分", "投诉",
    ],
    "boundary_message": (
        "【系统提示】此类排期、考勤及管理事宜属于非技术问答范畴，助手无法代答。\n"
        "请直接联系项目负责人、直属主管，或待本人回岗后当面沟通。"
    ),
    # 防滥用与频控配置
    "rate_limit": {
        "cooldown_seconds": 30,             # 单人两次指令间最小冷却间隔
        "max_daily_requests_per_user": 6,   # 单人每日最大免费指令次数
        "max_daily_requests_per_chat": 30,  # 单群每日最大指令次数
    },
    # Token 赞助 / 咖啡投喂指引（防滥用与趣味门槛）
    "sponsor": {
        "enabled": True,
        "notice": (
            "💡 今日技术支援免费额度已用完（{used}/{max} 次）。\n"
            "若属业务极其紧急的阻塞，请扫码投喂 Token 咖啡提频，或联系负责人加急处理。"
        ),
        "qr_code_hint": "收款码可在飞书个人签名或输入 /sponsor 查看",
    },
}


def load_rules() -> dict[str, Any]:
    """Load agent rules from JSON file, merging with default rules."""
    config.ensure_home()
    if not config.AGENT_RULES_FILE.exists():
        save_rules(DEFAULT_RULES)
        return dict(DEFAULT_RULES)
    try:
        data = json.loads(config.AGENT_RULES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_RULES)
        merged = dict(DEFAULT_RULES)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged
    except (OSError, ValueError):
        return dict(DEFAULT_RULES)


def save_rules(rules: dict[str, Any]) -> None:
    """Save agent rules atomically with 0600 permissions."""
    config.ensure_home()
    tmp_path = config.AGENT_RULES_FILE.with_suffix(".tmp")
    text = json.dumps(rules, ensure_ascii=False, indent=2) + "\n"
    tmp_path.write_text(text, encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, config.AGENT_RULES_FILE)


def _matches_any(target: str, patterns: list[str]) -> bool:
    if not target or not patterns:
        return False
    for pat in patterns:
        if pat == target or fnmatch.fnmatch(target, pat):
            return True
    return False


def is_chat_allowed(chat_id: str, chat_name: str = "", rules: dict[str, Any] | None = None) -> bool:
    """Check if a chat is permitted under the allowed/blocked rules."""
    r = rules or load_rules()
    if not r.get("enabled", True):
        return False

    blocked = r.get("blocked_chats") or []
    if _matches_any(chat_id, blocked) or (chat_name and _matches_any(chat_name, blocked)):
        return False

    allowed = r.get("allowed_chats") or []
    if not allowed:
        return True

    return _matches_any(chat_id, allowed) or (bool(chat_name) and _matches_any(chat_name, allowed))


def is_user_allowed(user_id: str, user_name: str = "", rules: dict[str, Any] | None = None) -> bool:
    """Check if a user is permitted to interact with the agent."""
    r = rules or load_rules()
    if not r.get("enabled", True):
        return False

    blocked = r.get("blocked_users") or []
    if _matches_any(user_id, blocked) or (user_name and _matches_any(user_name, blocked)):
        return False

    allowed = r.get("allowed_users") or []
    if not allowed:
        return True

    return _matches_any(user_id, allowed) or (bool(user_name) and _matches_any(user_name, allowed))


def check_topic_boundary(text: str, rules: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Inspect text against blocked topics.

    Returns:
        (is_allowed, boundary_message)
    """
    if not text:
        return True, ""
    r = rules or load_rules()
    topics = r.get("blocked_topics") or []
    lower_text = text.lower()
    for topic in topics:
        if topic.lower() in lower_text:
            msg = r.get("boundary_message") or (
                "【系统提示】此类事项属于管理事宜，助手无法代答。请直接联系项目负责人或待本人回岗当面沟通。"
            )
            return False, msg
    return True, ""
