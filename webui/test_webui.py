"""Offline unit tests for AgentHands Management WebUI."""

from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from webui.app import app


class TestWebUI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_get_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("AgentHands Console", resp.text)

    def test_get_status(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("feishu", data)
        self.assertIn("bilibili", data)
        self.assertIn("xianyu", data)
        self.assertIn("xiaohongshu", data)

    def test_get_and_toggle_gates(self):
        resp = self.client.get("/api/gates")
        self.assertEqual(resp.status_code, 200)
        gates = resp.json()
        self.assertIn("BILIBILI_WRITE_ALLOWED", gates)

        # Toggle gate
        toggle_resp = self.client.post(
            "/api/gates/toggle",
            json={"gate": "BILIBILI_WRITE_ALLOWED", "enabled": True},
        )
        self.assertEqual(toggle_resp.status_code, 200)
        self.assertTrue(toggle_resp.json()["enabled"])

        # Freeze all
        freeze_resp = self.client.post("/api/gates/freeze")
        self.assertEqual(freeze_resp.status_code, 200)
        self.assertEqual(freeze_resp.json()["status"], "all_frozen")

    def test_manual_cookie_save(self):
        resp = self.client.post(
            "/api/login/manual",
            json={
                "platform": "bilibili",
                "cookies": "SESSDATA=mock_val; bili_jct=mock_csrf; DedeUserID=12345",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
