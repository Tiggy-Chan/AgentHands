"""Offline unit tests for Xiaohongshu skill (CLI and handlers).

Runs 100% offline with zero network, zero real cookies.
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

from _xiaohongshu import (
    auth,
    config,
    creator_ops,
    note_ops,
    output,
    search_ops,
    security,
)
import xiaohongshu


class TestXiaohongshuSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("publish", {"title": "OOTD", "content": "Hello"})
        fp2 = security.fingerprint("publish", {"content": "Hello", "title": "OOTD"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("publish", {"title": "OOTD"}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("publish", {"title": "OOTD"}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("publish", {"title": "OOTD"}, execute=False, direct=False)


class TestXiaohongshuOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        cookie = "web_session=mock_secret_session_123"
        output.register_secret(cookie)
        raw = f"Error with {cookie} in log"
        clean = output.scrub(raw)
        self.assertNotIn(cookie, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("content with << special", "note:1")
        self.assertTrue(boxed.startswith("<<<untrusted:xiaohongshu:note:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestXiaohongshuAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_and_save_credentials(self):
        raw_cookie = "web_session=abc1234; a1=xyz5678"
        auth.save_credentials(raw_cookie)
        self.assertTrue(config.COOKIES_FILE.exists())

        loaded = auth.load_credentials()
        self.assertEqual(loaded["cookies"], raw_cookie)

        cookie_list = auth.parse_cookie_list()
        self.assertEqual(len(cookie_list), 2)
        self.assertEqual(cookie_list[0]["name"], "web_session")
        self.assertEqual(cookie_list[0]["value"], "abc1234")


class TestXiaohongshuCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_xiaohongshu.creator_ops.get_creator_profile", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_profile):
        mock_profile.return_value = {
            "creator_name": "TestCreator",
            "followers": "12.5k",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xiaohongshu.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["creator_name"], "TestCreator")

    def test_cli_publish_blocks_by_default(self):
        dummy_img = self.tmp_dir / "pic1.jpg"
        dummy_img.write_text("fake_image_content")
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(xiaohongshu.main_async([
                "publish", "My daily note",
                "--title", "OOTD",
                "--images", str(dummy_img),
            ]))
            self.assertEqual(rc, 3)

    @patch("_xiaohongshu.note_ops.publish_image_note", new_callable=AsyncMock)
    def test_cli_publish_direct(self, mock_pub):
        mock_pub.return_value = {"status": "ok", "action": "published", "title": "OOTD"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xiaohongshu.main_async([
                "publish", "My daily note",
                "--title", "OOTD",
                "--images", "pic1.jpg",
                "--direct",
                "--json",
            ]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["action"], "published")

    @patch("_xiaohongshu.note_ops.delete_note", new_callable=AsyncMock)
    def test_cli_delete_direct(self, mock_del):
        mock_del.return_value = {"status": "ok", "action": "delete_note", "note_id": "nid_123"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xiaohongshu.main_async([
                "delete", "nid_123",
                "--direct",
                "--json",
            ]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["note_id"], "nid_123")

    @patch("_xiaohongshu.search_ops.search_notes", new_callable=AsyncMock)
    def test_cli_search(self, mock_search):
        mock_search.return_value = [
            {"note_id": "67890", "title": "Autumn Look", "author": "Bob", "likes": "500"}
        ]
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xiaohongshu.main_async(["search", "Autumn Look", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Autumn Look")


if __name__ == "__main__":
    unittest.main()
