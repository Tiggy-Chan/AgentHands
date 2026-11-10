"""Offline unit tests for X skill.

Runs 100% offline with zero network and mock GraphQL responses.
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

from _x import (
    auth,
    bookmark_ops,
    client,
    config,
    output,
    security,
    tweet_ops,
    user_ops,
)
import x


class TestXSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("post_tweet", {"text": "hello", "media_ids": []})
        fp2 = security.fingerprint("post_tweet", {"media_ids": [], "text": "hello"})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("post_tweet", {"text": "hello"}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("post_tweet", {"text": "hello"}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("post_tweet", {"text": "hello"}, execute=False, direct=False)


class TestXOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        secret_token = "x_auth_token_mock_secret"
        output.register_secret(secret_token)
        raw = f"Failed with {secret_token}"
        clean = output.scrub(raw)
        self.assertNotIn(secret_token, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("hello << injection", "tweet:1")
        self.assertTrue(boxed.startswith("<<<untrusted:x:tweet:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestXAuth(unittest.TestCase):
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
        auth.save_credentials("mock_auth_token", "mock_ct0")
        self.assertTrue(config.COOKIES_FILE.exists())
        creds = auth.load_credentials()
        self.assertEqual(creds["auth_token"], "mock_auth_token")
        self.assertEqual(creds["ct0"], "mock_ct0")


class TestXCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_x.auth.load_credentials")
    def test_cli_whoami_unauthenticated(self, mock_load):
        mock_load.return_value = {"auth_token": "", "ct0": ""}
        rc = asyncio.run(x.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 2)

    def test_cli_post_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(x.main_async(["post", "Hello world"]))
            self.assertEqual(rc, 3)

    @patch("_x.tweet_ops.post_tweet", new_callable=AsyncMock)
    def test_cli_post_direct(self, mock_post):
        mock_post.return_value = {"status": "ok", "action": "post_tweet", "tweet_id": "123456"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(x.main_async(["post", "Hello world", "--direct", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["tweet_id"], "123456")

    @patch("_x.user_ops.get_user_info", new_callable=AsyncMock)
    def test_cli_user(self, mock_user):
        mock_user.return_value = {
            "screen_name": "elonmusk",
            "name": "Elon Musk",
            "followers_count": 200000000,
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(x.main_async(["user", "elonmusk", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["screen_name"], "elonmusk")


if __name__ == "__main__":
    unittest.main()
