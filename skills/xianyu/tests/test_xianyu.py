"""Offline unit tests for Xianyu skill (CLI and operations).

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
from unittest.mock import AsyncMock, MagicMock, patch

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _xianyu import (
    auth,
    config,
    mtop,
    order_ops,
    output,
    product_ops,
    protocol,
    search_ops,
    security,
    user_ops,
)
import xianyu


class TestXianyuProtocol(unittest.TestCase):
    def test_generate_sign(self):
        token = "mock_token"
        t = "1700000000000"
        data = '{"itemId":"123"}'
        sign = protocol.generate_sign(t, token, data)
        import hashlib
        expected = hashlib.md5(f"{token}&{t}&{protocol.APP_KEY}&{data}".encode("utf-8")).hexdigest()
        self.assertEqual(sign, expected)

    def test_trans_and_format_cookies(self):
        cookie_str = "unb=12345; _m_h5_tk=abc_123; sgcookie=xyz"
        parsed = protocol.trans_cookies(cookie_str)
        self.assertEqual(parsed["unb"], "12345")
        self.assertEqual(parsed["_m_h5_tk"], "abc_123")
        formatted = protocol.format_cookies(parsed)
        self.assertIn("unb=12345", formatted)
        self.assertIn("_m_h5_tk=abc_123", formatted)


class TestXianyuSecurity(unittest.TestCase):
    def test_fingerprint_deterministic(self):
        fp1 = security.fingerprint("publish", {"price": 100, "title": "Phone"})
        fp2 = security.fingerprint("publish", {"title": "Phone", "price": 100})
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_require_write_allowed_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.WriteBlockedError):
                security.require_write_allowed("publish", {"price": 100}, execute=False, direct=False)

    def test_require_write_allowed_passes(self):
        security.require_write_allowed("publish", {"price": 100}, execute=False, direct=True)
        with patch.dict(os.environ, {config.WRITE_ALLOWED_ENV: "1"}):
            security.require_write_allowed("publish", {"price": 100}, execute=False, direct=False)


class TestXianyuOutput(unittest.TestCase):
    def test_secret_scrubbing(self):
        token = "my_private_cookie_segment_9988"
        output.register_secret(token)
        raw = f"Error with token: {token}"
        clean = output.scrub(raw)
        self.assertNotIn(token, clean)
        self.assertIn("***", clean)

    def test_untrusted_delimiters(self):
        boxed = output.untrusted("malicious << injection", "desc:1")
        self.assertTrue(boxed.startswith("<<<untrusted:xianyu:desc:1"))
        self.assertTrue(boxed.endswith("<<<end"))


class TestXianyuCLIExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.orig_home = config.HOME
        config.HOME = self.tmp_dir
        config.CONFIG_DIR = self.tmp_dir / "config"
        config.COOKIES_FILE = config.CONFIG_DIR / "cookies.json"

    def tearDown(self):
        config.HOME = self.orig_home
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("_xianyu.user_ops.get_self_info", new_callable=AsyncMock)
    def test_cli_whoami(self, mock_self_info):
        mock_self_info.return_value = {
            "user_id": "88776655",
            "status": "authenticated",
            "has_token": True,
        }
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xianyu.main_async(["whoami", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["user_id"], "88776655")

    @patch("_xianyu.product_ops.list_products", new_callable=AsyncMock)
    def test_cli_products_list(self, mock_list):
        mock_list.return_value = [
            {"item_id": "1001", "title": "Test Item 1", "price": "99.0", "status": "ONLINE"}
        ]
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xianyu.main_async(["products", "list", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["item_id"], "1001")

    def test_cli_products_publish_blocks_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rc = asyncio.run(xianyu.main_async([
                "products", "publish",
                "--title", "Mac mini",
                "--desc", "Good condition",
                "--price", "3500",
            ]))
            self.assertEqual(rc, 3)

    @patch("_xianyu.product_ops.publish_product", new_callable=AsyncMock)
    def test_cli_products_publish_direct(self, mock_pub):
        mock_pub.return_value = {"status": "ok", "action": "publish_product"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xianyu.main_async([
                "products", "publish",
                "--title", "Mac mini",
                "--desc", "Good condition",
                "--price", "3500",
                "--direct",
                "--json",
            ]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["status"], "ok")

    @patch("_xianyu.order_ops.ship_order", new_callable=AsyncMock)
    def test_cli_orders_ship_direct(self, mock_ship):
        mock_ship.return_value = {"status": "ok", "action": "ship_order", "order_id": "999"}
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xianyu.main_async([
                "orders", "ship", "999",
                "--waybill", "SF123456",
                "--company", "SF",
                "--direct",
                "--json",
            ]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out["order_id"], "999")

    @patch("_xianyu.search_ops.search_items", new_callable=AsyncMock)
    def test_cli_search(self, mock_search):
        mock_search.return_value = [
            {"item_id": "2002", "title": "Keyboard", "price": "199", "seller": "Alice"}
        ]
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rc = asyncio.run(xianyu.main_async(["search", "Keyboard", "--json"]))
        self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Keyboard")


if __name__ == "__main__":
    unittest.main()
