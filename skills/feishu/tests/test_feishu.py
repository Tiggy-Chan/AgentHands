"""Offline unit tests. Run: ./test.sh  (no network, no cookies)."""

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "vendor"))


class TestCookieParse(unittest.TestCase):
    def setUp(self):
        from lark_bridge.client import LarkBridge
        self.parse = LarkBridge._parse_cookies

    def test_basic(self):
        self.assertEqual(self.parse("a=1; b=2"), {"a": "1", "b": "2"})

    def test_value_with_equals(self):
        self.assertEqual(self.parse("session=abc=="), {"session": "abc=="})

    def test_malformed_segment_survives(self):
        # SimpleCookie.load() returns {} for this; our parser must not.
        out = self.parse("a=1; bad-segment; session=xyz")
        self.assertEqual(out["session"], "xyz")
        self.assertEqual(out["a"], "1")

    def test_no_space_separator(self):
        self.assertEqual(self.parse("a=1;b=2"), {"a": "1", "b": "2"})


class TestTransportPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from _feishu import transport
        cls.t = transport
        transport.configure()

    def test_read_gateway_cmds_allowed(self):
        for cmd in (8, 64, 5023, 11021):
            self.assertEqual(
                self.t.classify("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", cmd),
                "allow", f"cmd={cmd}")

    def test_send_cmd_gated(self):
        self.assertEqual(
            self.t.classify("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", 5),
            "send")

    def test_unknown_and_create_chat_denied(self):
        for cmd in (13, 1, 9999):
            self.assertEqual(
                self.t.classify("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", cmd),
                "deny", f"cmd={cmd}")

    def test_csrf_and_whoami_allowed(self):
        self.assertEqual(self.t.classify("POST", "https://internal-api-lark-api.feishu.cn/accounts/csrf"), "allow")
        self.assertEqual(self.t.classify("GET", "https://internal-api-lark-api.feishu.cn/accounts/web/user"), "allow")

    def test_ticket_allowed(self):
        self.assertEqual(
            self.t.classify("GET", "https://login.feishu.cn/suite/passport/frontier_ticket/?x=1"),
            "allow")

    def test_domain_reads(self):
        from _feishu import config
        d = config.DOMAIN
        self.assertEqual(self.t.classify("GET", f"https://{d}/space/api/explorer/v3/children/list/"), "allow")
        self.assertEqual(self.t.classify("GET", f"https://{d}/space/api/wiki/v2/tree/get_node/"), "allow")
        self.assertEqual(self.t.classify("GET", f"https://{d}/space/api/export/result/abc"), "allow")
        self.assertEqual(self.t.classify("POST", f"https://{d}/space/api/export/create/"), "allow")

    def test_domain_writes_denied(self):
        from _feishu import config
        d = config.DOMAIN
        self.assertEqual(self.t.classify("POST", f"https://{d}/space/api/explorer/v3/remove/"), "deny")
        self.assertEqual(self.t.classify("POST", f"https://{d}/space/api/import/create/"), "deny")
        self.assertEqual(self.t.classify("GET", f"https://{d}/other/path"), "deny")

    def test_stream_download_allowed_upload_denied(self):
        base = "https://internal-api-drive-stream.feishu.cn"
        self.assertEqual(self.t.classify("GET", f"{base}/space/api/box/stream/download/all/tk/"), "allow")
        self.assertEqual(self.t.classify("POST", f"{base}/space/api/box/stream/upload/all/"), "deny")

    def test_unknown_host_denied(self):
        self.assertEqual(self.t.classify("GET", "https://evil.example.com/"), "deny")

    def test_send_gate_requires_env_and_arm(self):
        from lark_bridge import _http
        os.environ.pop("FEISHU_ALLOW_SEND", None)
        self.t.arm_send_once()
        with self.assertRaises(Exception):
            _http.require_send_allowed()
        os.environ["FEISHU_ALLOW_SEND"] = "1"
        try:
            self.t.arm_send_once()
            _http.require_send_allowed()  # passes once
            with self.assertRaises(Exception):
                _http.require_send_allowed()  # one-shot: second call blocked
        finally:
            os.environ.pop("FEISHU_ALLOW_SEND", None)

    def test_strict_tls_default(self):
        import asyncio
        import ssl
        from lark_bridge import _http
        self.assertIs(_http._VERIFY, True)  # configure() must not weaken it

        async def check():
            c = _http.make_client()
            try:
                ctx = c._transport._pool._ssl_context
                self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            finally:
                await c.aclose()

        asyncio.run(check())

    def test_response_body_is_bounded(self):
        import asyncio
        import gzip
        import httpx
        from lark_bridge._http import BoundedAsyncClient, ResponseTooLarge, _decode_bounded

        async def handler(_request):
            return httpx.Response(200, content=b"x" * 100)

        async def check():
            client = BoundedAsyncClient(
                transport=httpx.MockTransport(handler), max_response_bytes=10
            )
            try:
                with self.assertRaises(ResponseTooLarge):
                    await client.get("https://example.test/")
            finally:
                await client.aclose()

            compressed = gzip.compress(b"hello")
            self.assertEqual(_decode_bounded(compressed, "gzip", 100), b"hello")

        asyncio.run(check())


class TestQueryGuards(unittest.TestCase):
    def test_query_lock_rejects_second_process(self):
        import tempfile
        from pathlib import Path
        import importlib
        from _feishu import config, query_lock
        from _feishu.errors import QueryBusy

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FEISHU_HOME"] = tmp
            importlib.reload(config)
            importlib.reload(query_lock)
            try:
                with query_lock.acquire("history"):
                    with self.assertRaises(QueryBusy):
                        with query_lock.acquire("mentions"):
                            pass
            finally:
                os.environ.pop("FEISHU_HOME", None)

    def test_repeated_page_token_stops(self):
        import asyncio
        from _feishu import gateway
        from _feishu.errors import FeishuError

        page = AsyncMock(side_effect=[
            {"msg_ids": ["m1"], "has_more": True, "page_token": "next"},
            {"msg_ids": ["m2"], "has_more": True, "page_token": "next"},
        ])

        async def check():
            with patch.object(gateway, "search_msg_ids_page", page):
                with self.assertRaises(FeishuError):
                    await gateway.search_msg_ids("c1", limit=10)

        asyncio.run(check())

    def test_page_limit_stops(self):
        import asyncio
        from _feishu import config, gateway
        from _feishu.errors import FeishuError

        async def check():
            with patch.object(config, "SEARCH_MAX_PAGES", 1), \
                 patch.object(
                     gateway, "search_msg_ids_page",
                     new=AsyncMock(return_value={
                         "msg_ids": ["m1"], "has_more": True, "page_token": "next"
                     }),
                 ):
                with self.assertRaises(FeishuError):
                    await gateway.search_msg_ids("c1", limit=10)

        asyncio.run(check())

    def test_wire_walk_advances_after_printable_string(self):
        from _feishu.gateway import _walk_strings
        self.assertEqual(_walk_strings(b"\x0a\x05hello\x12\x03bye"), ["hello", "bye"])


class TestEventStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FEISHU_HOME"] = self.tmp.name
        import importlib
        from _feishu import config, event_store, secure_store
        importlib.reload(config)
        importlib.reload(secure_store)
        importlib.reload(event_store)
        self.es = event_store

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FEISHU_HOME", None)

    def _ev(self, msg_id, ts, at_me=False, text="hi", chat="c1", from_id="u1"):
        return {"schema": 1, "kind": "message", "msg_id": msg_id, "chat_id": chat,
                "from_id": from_id, "create_time": ts, "text": text, "at_me": at_me,
                "is_from_me": False, "decode_status": "ok"}

    def test_append_and_read(self):
        self.es.append_event(self._ev("m1", 1000))
        self.es.append_event(self._ev("m2", 2000, at_me=True))
        evs = self.es.read_events()
        self.assertEqual([e["msg_id"] for e in evs], ["m1", "m2"])

    def test_permissions(self):
        self.es.append_event(self._ev("m1", 1000))
        import stat
        active = self.es.storage_status()["active_file"]
        mode = stat.S_IMODE(os.stat(active).st_mode)
        self.assertEqual(mode, 0o600)

    def test_dedupe_by_msg_id(self):
        self.es.append_event(self._ev("m1", 1000))
        self.es.append_event(self._ev("m1", 1000))  # WS redelivery
        self.assertEqual(len(self.es.read_events()), 1)

    def test_mentions_filter(self):
        self.es.append_event(self._ev("m1", 1000))
        self.es.append_event(self._ev("m2", 2000, at_me=True))
        evs = self.es.read_events(mentions_only=True)
        self.assertEqual([e["msg_id"] for e in evs], ["m2"])

    def test_since_filter_ms(self):
        self.es.append_event(self._ev("m1", 1_000_000))       # s → 1e12 ms
        self.es.append_event(self._ev("m2", 9_000_000))       # s
        evs = self.es.read_events(since_ms=5_000_000_000)
        self.assertEqual([e["msg_id"] for e in evs], ["m2"])

    def test_string_create_time_from_ws(self):
        self.es.append_event(self._ev("m1", "1000000"))
        self.es.append_event(self._ev("m2", "9000000"))
        evs = self.es.read_events(since_ms=5_000_000_000)
        self.assertEqual([e["msg_id"] for e in evs], ["m2"])

    def test_exclude_mine(self):
        self.es.append_event(self._ev("m1", 1000))
        mine = self._ev("m2", 2000)
        mine["is_from_me"] = True
        self.es.append_event(mine)
        self.assertEqual([e["msg_id"] for e in self.es.read_events()], ["m1"])
        self.assertEqual(len(self.es.read_events(include_mine=True)), 2)

    def test_torn_tail_line(self):
        self.es.append_event(self._ev("m1", 1000))
        with open(self.es.config.EVENTS_FILE, "a") as f:
            f.write('{"msg_id": "m2", "creat')  # crash mid-write
        evs = self.es.read_events()
        self.assertEqual([e["msg_id"] for e in evs], ["m1"])

    def test_rotation_keeps_readable(self):
        self.es.config.EVENTS_MAX_BYTES = 600  # ~5 events per file
        for i in range(30):
            self.es.append_event(self._ev(f"m{i}", 1000 + i, text="x" * 50))
        evs = self.es.read_events(limit=0)
        self.assertGreater(len(evs), 5)
        # chronological and deduped even across rotated files
        self.assertEqual([e["msg_id"] for e in evs], sorted({e["msg_id"] for e in evs},
                          key=lambda m: int(m[1:])))
        self.assertIn("m29", [e["msg_id"] for e in evs])

    def test_no_secret_fields(self):
        ev = self._ev("m1", 1000)
        self.es.append_event(ev)
        active = self.es.storage_status()["active_file"]
        with open(active) as f:
            raw = f.read()
        for banned in ("cookie", "Cookie", "ticket", "access_key", "csrf"):
            self.assertNotIn(banned, raw)

    def test_encrypted_or_permission_fallback_is_reported(self):
        self.es.append_event(self._ev("m-secure", 1000, text="secret-body"))
        status = self.es.storage_status()
        self.assertIn(status["mode"], ("encrypted", "plaintext_permissions"))
        self.assertEqual(self.es.read_events(limit=0)[0]["text"], "secret-body")
        if status["encrypted"]:
            self.assertTrue(status["active_file"].endswith("events.v2.jsonl.enc"))
            with open(status["active_file"]) as f:
                self.assertNotIn("secret-body", f.read())

    def test_legacy_migration_is_recoverable(self):
        from _feishu import secure_store
        if secure_store.mode() != "encrypted":
            self.skipTest("Secret Service 不可用，跳过加密迁移测试")
        self.es.config.EVENTS_FILE.write_text(
            json.dumps(self._ev("legacy", 1000), ensure_ascii=False) + "\n"
        )
        result = self.es.prepare_storage()
        self.assertEqual(result["migrated"], 1)
        self.assertTrue(os.path.exists(self.es.config.EVENTS_LEGACY_FILE))
        self.assertEqual(self.es.read_events(limit=0)[0]["msg_id"], "legacy")

    def test_quota_blocks_without_deleting_history(self):
        self.es.config.EVENTS_TOTAL_MAX_BYTES = 1
        self.assertTrue(self.es.append_event(self._ev("m1", 1000)))
        self.assertFalse(self.es.append_event(self._ev("m2", 2000)))
        self.assertEqual(len(self.es.read_events(limit=0)), 1)

    def test_purge_moves_old_files_to_recoverable_archive(self):
        self.es.append_event(self._ev("old", 1000))
        self.es.append_event(self._ev("new", 2000))
        result = self.es.purge_before(1_500_000)
        self.assertEqual(result["removed"], 1)
        self.assertEqual([e["msg_id"] for e in self.es.read_events(limit=0)], ["new"])
        self.assertTrue(os.path.isdir(result["moved_to"]))


class TestDecode(unittest.TestCase):
    def test_text_message_wire_field1(self):
        from _feishu.message_decode import decode_message
        # type=4, content with wire field 1 = "hello"
        content = b"\x0a\x05hello"
        out = decode_message({"type": 4, "content": content})
        self.assertEqual(out["text"], "hello")
        self.assertEqual(out["decode_status"], "ok")

    def test_richtext_at_mention(self):
        from lark_bridge.proto import proto_pb2 as pb
        from _feishu.message_decode import decode_message
        tc = pb.TextContent()
        tc.text = "@张三 看下这个"
        rt = tc.richText
        eid = "e1"
        tp = pb.TextProperty()
        tp.content = "ou_user_123"
        rt.elements.dictionary[eid].tag = 5  # AT
        rt.elements.dictionary[eid].property = tp.SerializeToString()
        rt.elementIds.append(eid)
        rt.atIds.append(eid)
        out = decode_message({"type": 4, "content": tc.SerializeToString()})
        self.assertIn("ou_user_123", out["mention_user_ids"])

    def test_card_at_mention(self):
        import json as j
        from _feishu.message_decode import decode_message
        from lark_bridge.search import _ev
        body = j.dumps({"body": {"elements": [
            {"tag": "markdown", "property": {"elements": [
                {"tag": "at", "property": {"userID": "ou_abc"}},
                {"tag": "plain_text", "property": {"content": "看下"}},
            ]}}]}}).encode()
        # wrap in proto: field 20 (wire 2) = json body; length is varint
        content = b"\xa2\x01" + _ev(len(body)) + body
        out = decode_message({"type": 14, "content": content})
        self.assertIn("ou_abc", out["mention_user_ids"])

    def test_nontext_type(self):
        from _feishu.message_decode import decode_message
        out = decode_message({"type": 5, "content": b"\x00\xff"})
        self.assertEqual(out["decode_status"], "nontext")
        self.assertEqual(out["msg_type_name"], "image")

    def test_garbage_does_not_crash(self):
        from _feishu.message_decode import decode_message
        out = decode_message({"type": 4, "content": os.urandom(64)})
        self.assertIn(out["decode_status"], ("ok", "undecoded", "empty"))


class TestListenerPatch(unittest.TestCase):
    def _frame(self, at_me: bool):
        from lark_bridge.proto import proto_pb2 as pb
        m = pb.entities.Message()
        m.id = "7" + "1" * 18
        m.type = 4
        m.fromId = "u2"
        m.chatId = "oc_chat"
        m.chatType = 2
        m.createTime = 1700000000
        m.atOutChatterIds.append("ou_me")
        tc = pb.TextContent()
        tc.text = "hi"
        m.content = b"\x0a\x02hi"
        push = pb.PushMessagesRequest()
        push.messages[m.id].CopyFrom(m)
        push.messagesAtMe[m.id] = at_me
        pkt = pb.Packet()
        pkt.cmd = 6
        pkt.payloadType = 1
        pkt.sid = "42"
        pkt.payload = push.SerializeToString()
        frame = pb.Frame()
        frame.payloadType = "pb"
        frame.payload = pkt.SerializeToString()
        return frame.SerializeToString()

    def test_at_me_fields_exposed(self):
        from lark_bridge.listener import _handle_frame
        sid, msgs = _handle_frame(self._frame(True), None)
        self.assertIn(str(sid), ("42", "0"))  # sid is a string field in proto
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0]["at_me"])
        self.assertEqual(msgs[0]["at_out_chatter_ids"], ["ou_me"])
        self.assertEqual(msgs[0]["text"], "hi")
        self.assertIn("raw", msgs[0])

    def test_watch_chats_filter(self):
        from lark_bridge.listener import _handle_frame
        _, msgs = _handle_frame(self._frame(False), ["oc_other"])
        self.assertEqual(msgs, [])


class TestDaemonFiltering(unittest.TestCase):
    def test_private_inbound_is_relevant(self):
        from _feishu.daemon import _should_store
        self.assertTrue(_should_store({"msg_id": "m", "from_id": "other", "chat_type": 1}, "me"))

    def test_group_without_mention_is_not_relevant(self):
        from _feishu.daemon import _should_store
        self.assertFalse(_should_store({"msg_id": "m", "from_id": "other", "chat_type": 2}, "me"))

    def test_group_mention_is_relevant(self):
        from _feishu.daemon import _should_store
        self.assertTrue(_should_store({
            "msg_id": "m", "from_id": "other", "chat_type": 2,
            "at_me": True,
        }, "me"))

    def test_own_message_is_not_relevant(self):
        from _feishu.daemon import _should_store
        self.assertFalse(_should_store({"msg_id": "m", "from_id": "me", "chat_type": 1}, "me"))


class TestContextCompaction(unittest.TestCase):
    def test_context_has_no_raw_payload_and_marks_limit(self):
        from _feishu.context import compact_context
        ctx = {
            "target": {"msg_id": "m1"}, "root": {"msg_id": "m0"},
            "chain": [{"msg_id": "m0", "text": "root", "raw": b"secret"}],
            "replies": [{"msg_id": "m1", "text": "reply"}, {"msg_id": "m2", "text": "reply2"}],
            "window": [], "complete": True, "truncated": False,
            "missing_parent_ids": [], "search_window": {},
        }
        out = compact_context(ctx, max_messages=2, max_bytes=10000)
        self.assertEqual([m["msg_id"] for m in out["messages"]], ["m0", "m1"])
        self.assertTrue(out["truncated"])
        self.assertNotIn("raw", out["messages"][0])


class TestDrivePagination(unittest.TestCase):
    def test_last_label_pagination_is_bounded(self):
        import asyncio
        from lark_bridge import drive

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Client:
            def __init__(self):
                self.params = []

            async def get(self, _url, params):
                self.params.append(dict(params))
                if len(self.params) == 1:
                    return Response({"code": 0, "data": {
                        "node_list": ["n1"],
                        "entities": {"nodes": {"n1": {"token": "n1", "name": "a", "type": 0}}},
                        "has_more": True, "last_label": "next",
                    }})
                return Response({"code": 0, "data": {
                    "node_list": ["n2"],
                    "entities": {"nodes": {"n2": {"token": "n2", "name": "b", "type": 3}}},
                    "has_more": False,
                }})

        async def check():
            client = Client()
            rows = await drive._paged_node_list(client, "https://example.test/list", {"length": "50"})
            self.assertEqual([row["token"] for row in rows], ["n1", "n2"])
            self.assertEqual(client.params[1]["last_label"], "next")

        asyncio.run(check())


class TestSendGate(unittest.TestCase):
    def test_draft_fingerprint_stable(self):
        from _feishu.send import fingerprint
        a = fingerprint("c1", "正文", "", ["u1"])
        b = fingerprint("c1", "正文", "", ["u1"])
        c = fingerprint("c1", "改动", "", ["u1"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_send_without_confirm_blocked(self):
        import asyncio
        from _feishu.errors import SendBlocked
        from _feishu.send import send
        with self.assertRaises(SendBlocked):
            asyncio.run(send("c1", "正文"))

    def test_wrong_fingerprint_blocked(self):
        import asyncio
        from _feishu.errors import SendBlocked
        from _feishu.send import send
        with self.assertRaises(SendBlocked):
            asyncio.run(send("c1", "正文", confirm="0000"))

    def test_vendor_sender_gate(self):
        import asyncio
        from lark_bridge import _http, sender
        os.environ.pop("FEISHU_ALLOW_SEND", None)
        with self.assertRaises(Exception):
            asyncio.run(sender.send_message("cookie", "c1", "hi"))

    def test_direct_gate_is_one_shot(self):
        import asyncio
        from _feishu import transport
        from lark_bridge import _http

        transport.configure()
        os.environ.pop("FEISHU_ALLOW_SEND", None)
        transport.arm_direct_send_once()
        _http.require_send_allowed()
        with self.assertRaises(Exception):
            _http.require_send_allowed()


class TestOutput(unittest.TestCase):
    def test_parse_when(self):
        from _feishu.output import parse_when, now_ms
        t0 = now_ms()
        self.assertAlmostEqual(parse_when("30m"), t0 - 30 * 60 * 1000, delta=2000)
        self.assertAlmostEqual(parse_when("2h"), t0 - 7200 * 1000, delta=2000)
        self.assertEqual(parse_when("1700000000"), 1700000000 * 1000)
        self.assertIsNone(parse_when(""))
        with self.assertRaises(ValueError):
            parse_when("随便")

    def test_scrub(self):
        from _feishu.output import register_secret, scrub
        register_secret("s3cr3t-value")
        self.assertEqual(scrub("cookie is s3cr3t-value ok"), "cookie is *** ok")

    def test_fmt_ts_units(self):
        from _feishu.output import fmt_ts
        self.assertEqual(fmt_ts(1700000000), fmt_ts(1700000000000))  # s == ms
        self.assertEqual(fmt_ts(0), "-")


if __name__ == "__main__":
    unittest.main()
