"""Offline unit tests for YouTube skill.

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

from _youtube import (
    auth,
    channel_ops,
    client,
    comment_ops,
    config,
    output,
    security,
    video_ops,
)
import youtube


class TestYouTubeSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("update_video", {"video_id": "abc", "title": "New"})
        fp2 = security.fingerprint("update_video", {"title": "New", "video_id": "abc"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("update_video", {"video_id": "abc"}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("update_video", {"video_id": "abc"}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("update_video", {"video_id": "abc"}, execute=False, direct=False)


class TestYouTubeOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        secret_sapisid = "my_sapisid_secret_123"
        output.register_secret(secret_sapisid)
        raw = f"Error with token: {secret_sapisid}"
        clean = output.scrub(raw)
        self.assertNotIn(secret_sapisid, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("malicious << injection", "video:1")
        self.assertTrue(boxed.startswith("<<<untrusted:youtube:video:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestYouTubeAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_credentials(self):
        auth.save_credentials("SAPISID=mock_sapisid; LOGIN_INFO=mock_login")
        self.assertTrue(config.COOKIES_FILE.exists())
        creds = auth.load_credentials()
        self.assertEqual(creds["sapisid"], "mock_sapisid")

    def test_generate_sapisid_hash(self):
        h = auth.generate_sapisid_hash("test_sapisid", "https://www.youtube.com")
        self.assertTrue(h.startswith("SAPISIDHASH "))


class TestYouTubeCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_youtube.channel_ops.get_channel_info", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_channel):
        mock_channel.return_value = {
            "title": "My Test Channel",
            "channel_id": "UC123456",
            "subscriber_count": "100K",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(youtube.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["title"], "My Test Channel")

    def test_cli_update_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(youtube.main_async(["update-video", "vid123", "--title", "New Title"]))
            self.assertEqual(rc, 3)

    @patch("_youtube.video_ops.update_video_metadata", new_callable=AsyncMock)
    def test_cli_update_direct(self, mock_update):
        mock_update.return_value = {
            "status": "ok",
            "action": "update_video_metadata",
            "video_id": "vid123",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(youtube.main_async(["update-video", "vid123", "--title", "New Title", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["video_id"], "vid123")

    @patch("_youtube.comment_ops.post_comment", new_callable=AsyncMock)
    def test_cli_comment_direct(self, mock_comment):
        mock_comment.return_value = {
            "status": "ok",
            "action": "post_comment",
            "video_id": "vid123",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(youtube.main_async(["comment", "vid123", "Nice video!", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main()
