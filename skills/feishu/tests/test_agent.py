"""Unit tests for the Feishu Command Agent Gateway.

Tests access control, topic boundaries, session management, stage-1 zero-token menu,
directive dispatching, and rate limiting / quota enforcement.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "vendor"))

from _feishu import agent_dispatcher, agent_rules, agent_runner, agent_session, config


class TestAgentRules(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.AGENT_RULES_FILE = self.tmp_dir / "agent_rules.json"
        config.AGENT_SESSIONS_DIR = self.tmp_dir / "agent_sessions"
        config.ensure_home()

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_and_save_rules(self):
        rules = agent_rules.load_rules()
        self.assertTrue(rules["enabled"])
        self.assertIn("请假", rules["blocked_topics"])

        rules["blocked_topics"].append("自定义敏感词")
        agent_rules.save_rules(rules)

        reloaded = agent_rules.load_rules()
        self.assertIn("自定义敏感词", reloaded["blocked_topics"])

    def test_chat_access_control(self):
        rules = {
            "enabled": True,
            "allowed_chats": ["chat_allowed_*", "c123"],
            "blocked_chats": ["*全员*", "c999"],
        }
        # Blocked takes precedence
        self.assertFalse(agent_rules.is_chat_allowed("c999", "技术支持群", rules=rules))
        self.assertFalse(agent_rules.is_chat_allowed("c100", "公司全员大群", rules=rules))

        # Allowed list
        self.assertTrue(agent_rules.is_chat_allowed("c123", "通用群", rules=rules))
        self.assertTrue(agent_rules.is_chat_allowed("chat_allowed_555", "项目群", rules=rules))
        self.assertFalse(agent_rules.is_chat_allowed("other_chat", "未放行群", rules=rules))

    def test_topic_boundary(self):
        rules = agent_rules.load_rules()

        ok, msg = agent_rules.check_topic_boundary("请问系统的服务通信异常怎么排查？", rules=rules)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # Trigger blocked keywords
        ok, msg = agent_rules.check_topic_boundary("你什么时候销病假回岗？这个排期今天几点给？", rules=rules)
        self.assertFalse(ok)
        self.assertIn("非技术问答范畴", msg)


class TestAgentSession(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.AGENT_RULES_FILE = self.tmp_dir / "agent_rules.json"
        config.AGENT_SESSIONS_DIR = self.tmp_dir / "agent_sessions"
        config.ensure_home()

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_session_lifecycle(self):
        s = agent_session.get_or_create_session("chat1", from_id="u1", from_name="张三", thread_id="t1")
        self.assertEqual(s["chat_id"], "chat1")
        self.assertEqual(s["from_name"], "张三")
        self.assertTrue(s["claude_session_id"])

        old_claude_id = s["claude_session_id"]
        agent_session.append_turn(s, "问题1", "回答1")

        loaded = agent_session.get_or_create_session("chat1", from_id="u1", thread_id="t1")
        self.assertEqual(len(loaded["history"]), 2)

        # Reset session
        reset_s = agent_session.reset_session("chat1", from_id="u1", thread_id="t1")
        self.assertNotEqual(reset_s["claude_session_id"], old_claude_id)
        self.assertEqual(len(reset_s["history"]), 0)

    def test_rate_limiting(self):
        rules = {
            "rate_limit": {
                "cooldown_seconds": 10,
                "max_daily_requests_per_user": 2,
            },
            "sponsor": {
                "notice": "额度耗尽 {used}/{max}",
                "qr_code_hint": "见赞助说明",
            }
        }
        s = agent_session.get_or_create_session("c1", from_id="u1")

        # First request allowed
        ok, reason = agent_session.check_rate_limit(s, rules)
        self.assertTrue(ok)
        agent_session.record_request(s)

        # Cooldown check
        ok, reason = agent_session.check_rate_limit(s, rules)
        self.assertFalse(ok)
        self.assertIn("过于频繁", reason)

        # Bypass cooldown for daily quota test
        s["last_request_time"] = time.time() - 20
        agent_session.record_request(s)  # Count 2

        s["last_request_time"] = time.time() - 20
        ok, reason = agent_session.check_rate_limit(s, rules)
        self.assertFalse(ok)
        self.assertIn("额度耗尽 2/2", reason)


class TestAgentRunner(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(agent_runner.parse_command(""), (None, ""))
        self.assertEqual(agent_runner.parse_command("普通技术提问"), (None, "普通技术提问"))
        self.assertEqual(agent_runner.parse_command("@测试助手 你好"), (None, "你好"))
        self.assertEqual(
            agent_runner.parse_command('<at user_id="123">@测试助手</at> /qa 为什么闪烁255'),
            ("qa", "为什么闪烁255")
        )
        self.assertEqual(
            agent_runner.parse_command("/diag 192.168.1.100 system"),
            ("diag", "192.168.1.100 system")
        )
        self.assertEqual(agent_runner.parse_command("/reset"), ("reset", ""))
        self.assertEqual(agent_runner.parse_command("/help"), ("help", ""))

    def test_stage1_menu(self):
        menu = agent_runner.get_stage1_menu("李工")
        self.assertIn("@李工", menu)
        self.assertIn("/qa", menu)
        self.assertIn("/diag", menu)
        self.assertIn("考勤、请假、交付排期", menu)


class TestAgentDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.AGENT_RULES_FILE = self.tmp_dir / "agent_rules.json"
        config.AGENT_SESSIONS_DIR = self.tmp_dir / "agent_sessions"
        config.ensure_home()

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_dispatch_stage1_zero_token_menu(self):
        event = {
            "kind": "message",
            "msg_id": "m100",
            "chat_id": "c1",
            "chat_type": 2,
            "at_me": True,
            "from_id": "u1",
            "from_name": "王工",
            "text": '<at user_id="me">@测试助手</at> 帮我看一下机器',
        }
        res = asyncio.run(agent_dispatcher.dispatch_event(event, send_reply=False))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["action"], "stage1_menu")
        self.assertIn("/qa", res["reply"])
        self.assertIn("/diag", res["reply"])

        # Verify quota NOT consumed by stage 1 menu
        s = agent_session.get_or_create_session("c1", from_id="u1")
        self.assertEqual(len(s.get("daily_counts", {})), 0)

    def test_dispatch_blocked_topic(self):
        event = {
            "kind": "message",
            "msg_id": "m101",
            "chat_id": "c1",
            "chat_type": 2,
            "at_me": True,
            "from_id": "u1",
            "text": '<at user_id="me">@测试助手</at> 什么时候销假来上班？排期催得急',
        }
        res = asyncio.run(agent_dispatcher.dispatch_event(event, send_reply=False))
        self.assertEqual(res["status"], "blocked_topic")
        self.assertEqual(res["action"], "topic_boundary")
        self.assertIn("非技术问答范畴", res["reply"])

    def test_dispatch_commands(self):
        event_help = {
            "kind": "message",
            "msg_id": "m102",
            "chat_id": "c1",
            "chat_type": 2,
            "at_me": True,
            "from_id": "u1",
            "text": "/help",
        }
        res = asyncio.run(agent_dispatcher.dispatch_event(event_help, send_reply=False))
        self.assertEqual(res["action"], "help")
        self.assertIn("使用指南", res["reply"])

        event_reset = {
            "kind": "message",
            "msg_id": "m103",
            "chat_id": "c1",
            "chat_type": 2,
            "at_me": True,
            "from_id": "u1",
            "text": "/reset",
        }
        res = asyncio.run(agent_dispatcher.dispatch_event(event_reset, send_reply=False))
        self.assertEqual(res["action"], "reset")
        self.assertIn("已成功重置", res["reply"])


if __name__ == "__main__":
    unittest.main()
