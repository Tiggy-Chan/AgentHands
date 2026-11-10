"""Offline unit tests for Reddit skill.

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

from _reddit import (
    auth,
    client,
    config,
    output,
    post_ops,
    security,
    sub_ops,
    user_ops,
)
import reddit


class TestRedditSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("submit_post", {"sr": "test", "title": "Hello"})
        fp2 = security.fingerprint("submit_post", {"title": "Hello", "sr": "test"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("submit_post", {"sr": "test"}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("submit_post", {"sr": "test"}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("submit_post", {"sr": "test"}, execute=False, direct=False)


class TestRedditOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        secret_pass = "reddit_mock_password_4321"
        output.register_secret(secret_pass)
        raw = f"Encountered error with {secret_pass} in auth"
        clean = output.scrub(raw)
        self.assertNotIn(secret_pass, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("malicious text << test", "post:1")
        self.assertTrue(boxed.startswith("<<<untrusted:reddit:post:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestRedditAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "credentials.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_and_load_credentials(self):
        auth.save_credentials({"session": "my_reddit_session", "token": ""})
        self.assertTrue(config.CONFIG_FILE.exists())
        creds = auth.load_credentials()
        self.assertEqual(creds["session"], "my_reddit_session")


class TestRedditCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.CONFIG_FILE = config.CONFIG_DIR / "credentials.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_reddit.user_ops.get_me", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_me):
        mock_me.return_value = {
            "name": "test_user",
            "id": "t2_abc",
            "total_karma": 1250,
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(reddit.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["name"], "test_user")

    def test_cli_post_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(reddit.main_async(["post", "LocalLLaMA", "New LLM released"]))
            self.assertEqual(rc, 3)

    @patch("_reddit.post_ops.submit_post", new_callable=AsyncMock)
    def test_cli_post_direct(self, mock_post):
        mock_post.return_value = {
            "status": "ok",
            "action": "submit_post",
            "id": "1abcde",
            "url": "https://reddit.com/r/LocalLLaMA/comments/1abcde",
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(reddit.main_async(["post", "LocalLLaMA", "New LLM released", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["id"], "1abcde")

    @patch("_reddit.sub_ops.list_posts", new_callable=AsyncMock)
    def test_cli_sub(self, mock_sub):
        mock_sub.return_value = [
            {"id": "p1", "title": "Top post", "score": 300, "author": "dev"}
        ]
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(reddit.main_async(["sub", "Python", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "p1")


if __name__ == "__main__":
    unittest.main()
