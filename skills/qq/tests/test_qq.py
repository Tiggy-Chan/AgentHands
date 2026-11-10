"""Offline unit tests for QQ OneBot skill.

Runs 100% offline with zero network and mock HTTP responses.
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

from _qq import (
    auth,
    client,
    config,
    group_ops,
    message_ops,
    output,
    security,
    user_ops,
)
import qq


class TestQQSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("send_group", {"group_id": 123, "message": "hello"})
        fp2 = security.fingerprint("send_group", {"message": "hello", "group_id": 123})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("send_group", {"group_id": 123}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("send_group", {"group_id": 123}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("send_group", {"group_id": 123}, execute=False, direct=False)


class TestQQOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        token = "qq_token_abc_secret"
        output.register_secret(token)
        raw = f"Auth failed with token {token}"
        clean = output.scrub(raw)
        self.assertNotIn(token, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("hello << injection", "msg:1")
        self.assertTrue(boxed.startswith("<<<untrusted:qq:msg:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestQQAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "config.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_credentials(self):
        auth.save_credentials("http://127.0.0.1:5700", "my_token")
        self.assertTrue(config.CONFIG_FILE.exists())
        creds = auth.load_credentials()
        self.assertEqual(creds["url"], "http://127.0.0.1:5700")
        self.assertEqual(creds["token"], "my_token")


class TestQQCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "config.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_qq.user_ops.get_self_info", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_self):
        mock_self.return_value = {"user_id": 10001, "nickname": "TestBot"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(qq.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["user_id"], 10001)

    def test_cli_send_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(qq.main_async(["send-group", "12345", "test message"]))
            self.assertEqual(rc, 3)

    @patch("_qq.message_ops.send_group_message", new_callable=AsyncMock)
    def test_cli_send_direct(self, mock_send):
        mock_send.return_value = {"status": "ok", "action": "send_group_message", "message_id": 9999}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(qq.main_async(["send-group", "12345", "test message", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["message_id"], 9999)

    @patch("_qq.group_ops.set_group_ban", new_callable=AsyncMock)
    def test_cli_ban_direct(self, mock_ban):
        mock_ban.return_value = {"status": "ok", "action": "set_group_ban"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(qq.main_async(["ban", "12345", "67890", "--duration", "60", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
