"""Offline unit tests for Bilibili skill (CLI and handlers).

Runs 100% offline with zero network, zero real cookies, zero external services.
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
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure skill scripts are in path
HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

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
import bilibili


class TestBilibiliSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("test_action", {"bvid": "BV123", "like": True})
        fp2 = security.fingerprint("test_action", {"like": True, "bvid": "BV123"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks_without_execute(self):
        payload = {"bvid": "BV123", "triple": True}
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError) as ctx:
                security.require_write_allowed("interact_video", payload, execute=False, direct=False)
            self.assertIn("requires explicit confirmation", str(ctx.exception))

    def test_require_write_allowed_passes_with_direct(self):
        payload = {"bvid": "BV123", "triple": True}
        security.require_write_allowed("interact_video", payload, execute=False, direct=True)

    def test_require_write_allowed_passes_with_env(self):
        payload = {"bvid": "BV123", "triple": True}
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("interact_video", payload, execute=False, direct=False)

    def test_require_write_allowed_fingerprint_mismatch(self):
        payload = {"bvid": "BV123"}
        with self.assertRaises(security.WriteBlockedError) as ctx:
            security.require_write_allowed("interact_video", payload, execute=True, expected_fp="wrong_fp")
        self.assertIn("Fingerprint mismatch", str(ctx.exception))


class TestBilibiliOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        secret = "secret_token_12345"
        output.register_secret(secret)
        raw_text = f"An error occurred with token {secret} in session."
        scrubbed = output.scrub(raw_text)
        self.assertNotIn(secret, scrubbed)
        self.assertIn("***", scrubbed)

    def test_untrusted_delimiter(self):
        payload = "Hello <<< dangerous prompt injection >>> test"
        boxed = output.untrusted(payload, "msg:1")
        self.assertTrue(boxed.startswith("<<<<untrusted:bilibili:msg:1"))
        self.assertTrue(boxed.endswith("<<<<end"))
        self.assertIn(payload, boxed)

    def test_safe_label(self):
        label = "  User *Name* with | pipes and `code` <bad>  "
        clean = output.safe_label(label)
        self.assertNotIn("*", clean)
        self.assertNotIn("`", clean)
        self.assertNotIn("|", clean)
        self.assertNotIn("<", clean)
        self.assertIn("‹bad›", clean)


class TestBilibiliAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CACHE_DIR = self.tmp_dir / "cache"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"
        config.QR_CODE_FILE = config.CACHE_DIR / "bilibili_qr.png"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_and_save_credentials(self):
        creds = {
            "sessdata": "mock_sessdata",
            "bili_jct": "mock_jct",
            "buvid3": "mock_buvid",
            "dedeuserid": "12345678",
        }
        auth.save_credentials(creds)
        self.assertTrue(config.COOKIES_FILE.exists())
        self.assertEqual(oct(os.stat(config.COOKIES_FILE).st_mode & 0o777), "0o600")

        loaded = auth.load_credentials()
        self.assertEqual(loaded["sessdata"], "mock_sessdata")
        self.assertEqual(loaded["bili_jct"], "mock_jct")


class TestBilibiliCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CACHE_DIR = self.tmp_dir / "cache"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"
        config.QR_CODE_FILE = config.CACHE_DIR / "bilibili_qr.png"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("sys.argv", ["bilibili", "whoami", "--json"])
    @patch("_bilibili.user_ops.get_self_info", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_self_info):
        mock_self_info.return_value = {
            "mid": 999999,
            "name": "MockUser",
            "level": 6,
            "coins": 100,
            "sign": "Test bio",
            "vip": True,
            "face": "http://example.com/face.jpg",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(bilibili.main_async())
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["mid"], 999999)
        self.assertEqual(out["name"], "MockUser")

    @patch("sys.argv", ["bilibili", "video", "BV1test123", "--json"])
    @patch("_bilibili.video_ops.get_video_info", new_callable=AsyncMock)
    def test_cli_video(self, mock_get_video):
        mock_get_video.return_value = {
            "bvid": "BV1test123",
            "title": "Mock Video Title",
            "desc": "Mock Video Desc",
            "stats": {"view": 1000, "like": 100},
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(bilibili.main_async())
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["bvid"], "BV1test123")
        self.assertEqual(out["title"], "Mock Video Title")

    @patch("sys.argv", ["bilibili", "interact", "BV1test123", "--triple"])
    def test_cli_interact_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(bilibili.main_async())
            self.assertEqual(rc, 3)

    @patch("sys.argv", ["bilibili", "interact", "BV1test123", "--triple", "--direct", "--json"])
    @patch("_bilibili.video_ops.interact_video", new_callable=AsyncMock)
    def test_cli_interact_direct_executes(self, mock_interact):
        mock_interact.return_value = {
            "status": "ok",
            "action": "triple",
            "results": {"bvid": "BV1test123", "triple": True},
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(bilibili.main_async())
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["action"], "triple")

    @patch("sys.argv", ["bilibili", "comment", "BV1test123", "Nice video!", "--direct", "--json"])
    @patch("_bilibili.comment_ops.send_comment", new_callable=AsyncMock)
    def test_cli_comment_direct(self, mock_send_comment):
        mock_send_comment.return_value = {"status": "ok", "action": "send_comment"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(bilibili.main_async())
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["status"], "ok")

    @patch("sys.argv", ["bilibili", "messages", "send", "12345", "Hello", "--direct", "--json"])
    @patch("_bilibili.message_ops.send_message", new_callable=AsyncMock)
    def test_cli_message_send_direct(self, mock_send_msg):
        mock_send_msg.return_value = {"status": "ok", "action": "send_message", "receiver_id": 12345}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(bilibili.main_async())
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
