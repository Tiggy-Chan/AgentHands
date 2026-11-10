"""Offline unit tests for Telegram skill.

Runs 100% offline with zero network and mock responses.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

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
import telegram


class TestTelegramSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("send_message", {"chat_id": "@user", "text": "hi"})
        fp2 = security.fingerprint("send_message", {"text": "hi", "chat_id": "@user"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("send_message", {"chat_id": "@user"}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("send_message", {"chat_id": "@user"}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("send_message", {"chat_id": "@user"}, execute=False, direct=False)


class TestTelegramOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        secret_token = "tg_bot_token_secret_123"
        output.register_secret(secret_token)
        raw = f"Error with token: {secret_token}"
        clean = output.scrub(raw)
        self.assertNotIn(secret_token, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("hello << injection", "msg:1")
        self.assertTrue(boxed.startswith("<<<untrusted:telegram:msg:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestTelegramAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "session.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_credentials(self):
        auth.save_credentials({"bot_token": "mock_token_123", "bridge_url": "http://127.0.0.1:8081"})
        self.assertTrue(config.CONFIG_FILE.exists())
        creds = auth.load_credentials()
        self.assertEqual(creds["bot_token"], "mock_token_123")
        self.assertEqual(creds["bridge_url"], "http://127.0.0.1:8081")


class TestTelegramCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "session.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_telegram.user_ops.get_me", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_me):
        mock_me.return_value = {
            "id": 998877,
            "username": "tester",
            "first_name": "Test User",
            "is_bot": False,
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(telegram.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["username"], "tester")

    def test_cli_send_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(telegram.main_async(["send", "@user", "hello"]))
            self.assertEqual(rc, 3)

    @patch("_telegram.message_ops.send_message", new_callable=AsyncMock)
    def test_cli_send_direct(self, mock_send):
        mock_send.return_value = {
            "status": "ok",
            "action": "send_message",
            "chat_id": "@user",
            "message_id": 1234,
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(telegram.main_async(["send", "@user", "hello", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["message_id"], 1234)

    @patch("_telegram.chat_ops.get_chat", new_callable=AsyncMock)
    def test_cli_chat(self, mock_chat):
        mock_chat.return_value = {
            "id": -100123456,
            "title": "Dev Chat",
            "type": "supergroup",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(telegram.main_async(["chat", "-100123456", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["title"], "Dev Chat")


if __name__ == "__main__":
    unittest.main()
