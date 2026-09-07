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

    def test_write_gate_requires_env_and_is_one_shot(self):
        from lark_bridge import _http
        os.environ.pop("FEISHU_ALLOW_WRITE", None)
        self.t.arm_write_once()
        with self.assertRaises(Exception):
            _http.require_write_allowed()
        os.environ["FEISHU_ALLOW_WRITE"] = "1"
        try:
            self.t.arm_write_once()
            _http.require_write_allowed()
            with self.assertRaises(Exception):
                _http.require_write_allowed()
        finally:
            os.environ.pop("FEISHU_ALLOW_WRITE", None)

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
            with self.assertRaises(ResponseTooLarge):
                _decode_bounded(gzip.compress(b"x" * 100_000), "gzip", 100)

        asyncio.run(check())

    def test_browser_route_guard_is_read_only(self):
        from types import SimpleNamespace
        from _feishu import browser_guard

        def request(method, url, command=""):
            return SimpleNamespace(
                method=method,
                url=url,
                headers={"x-command": str(command)},
            )

        self.assertTrue(browser_guard._is_safe_request(
            request("GET", "https://sample-tenant.feishu.cn/mail")
        ))
        self.assertTrue(browser_guard._is_safe_request(
            request("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", 3700)
        ))
        self.assertFalse(browser_guard._is_safe_request(
            request("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", 5)
        ))
        self.assertFalse(browser_guard._is_safe_request(
            request("POST", "https://internal-api-lark-api.feishu.cn/im/gateway/", 999999)
        ))
        self.assertFalse(browser_guard._is_safe_request(
            request("POST", "https://evil.example/v1/list")
        ))


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

        async def check():
            with patch.object(config, "SEARCH_MAX_PAGES", 1), \
                 patch.object(
                     gateway, "search_msg_ids_page",
                     new=AsyncMock(return_value={
                         "msg_ids": ["m1"], "has_more": True, "page_token": "next"
                     }),
                 ):
                return await gateway.search_msg_ids("c1", limit=10)

        res = asyncio.run(check())
        # Page budget is a host-protection bound, but hitting it must degrade
        # to a flagged partial result instead of failing the whole query.
        self.assertEqual(res["msg_ids"], ["m1"])
        self.assertTrue(res["page_limit_hit"])
        self.assertTrue(res["has_more"])

    def test_wire_walk_advances_after_printable_string(self):
        from _feishu.gateway import _walk_strings
        self.assertEqual(_walk_strings(b"\x0a\x05hello\x12\x03bye"), ["hello", "bye"])

    def test_browser_worker_command_has_cgroup_limits(self):
        from _feishu import browser_worker
        cmd = browser_worker.build_command("chats", "feishu-browser-test", 12.2)
        joined = " ".join(cmd)
        self.assertIn("--property=MemoryHigh=384M", joined)
        self.assertIn("--property=MemoryMax=512M", joined)
        self.assertIn("--property=TasksMax=128", joined)
        self.assertIn("--property=RuntimeMaxSec=13s", joined)
        self.assertNotIn("--eval", joined)

    def test_browser_without_systemd_fails_closed(self):
        import asyncio
        from _feishu import browser_worker
        from _feishu.errors import ResourceIsolationUnavailable
        with patch.object(browser_worker, "_systemd_user_available", return_value=False):
            with self.assertRaises(ResourceIsolationUnavailable):
                asyncio.run(browser_worker.run("chats"))

    def test_browser_cancellation_stops_transient_unit(self):
        import asyncio
        from _feishu import browser_worker

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.released = asyncio.Event()

            async def communicate(self, _input=b""):
                if self.killed:
                    return b"", b""
                await self.released.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.released.set()

        async def check():
            process = Process()
            with patch.object(browser_worker, "_systemd_user_available", return_value=True), \
                 patch.object(browser_worker.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)), \
                 patch.object(browser_worker, "_stop_unit", new=AsyncMock()) as stop:
                task = asyncio.create_task(browser_worker.run("chats", timeout=30))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                stop.assert_awaited_once()
                self.assertTrue(process.killed)

        asyncio.run(check())

    def test_ten_processes_only_one_gets_query_lock(self):
        import subprocess
        import tempfile
        from pathlib import Path

        script_dir = Path(__file__).resolve().parents[1] / "scripts"
        code = (
            "import sys, time\n"
            "sys.path[:0] = %r\n"
            "from _feishu import query_lock\n"
            "with query_lock.acquire('stress'):\n"
            "    time.sleep(0.15)\n"
        ) % ([str(script_dir), str(script_dir / "vendor")],)
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, FEISHU_HOME=tmp, FEISHU_NO_BOOTSTRAP="1")
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(10)
            ]
            results = [child.wait(timeout=5) for child in children]
        self.assertEqual(results.count(0), 1)
        self.assertEqual(results.count(1), 9)


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
        evs = self.es.read_events(limit=10000)
        self.assertGreater(len(evs), 5)
        # chronological and deduped even across rotated files
        self.assertEqual([e["msg_id"] for e in evs], sorted({e["msg_id"] for e in evs},
                          key=lambda m: int(m[1:])))
        self.assertIn("m29", [e["msg_id"] for e in evs])

    def test_read_tail_is_hard_bounded(self):
        for i in range(40):
            self.es.append_event(self._ev(f"m{i}", 1000 + i))
        evs = self.es.read_events(limit=7)
        self.assertEqual(len(evs), 7)
        self.assertEqual([e["msg_id"] for e in evs], [f"m{i}" for i in range(33, 40)])

    def test_zero_read_limit_rejected(self):
        with self.assertRaises(ValueError):
            self.es.read_events(limit=0)

    def test_encrypted_store_never_downgrades_when_keyring_is_unavailable(self):
        from _feishu import secure_store
        self.es.config.EVENTS_ENCRYPTED_FILE.write_text("{}\n")
        with patch.object(secure_store, "mode", return_value="plaintext_permissions"):
            self.assertTrue(self.es.storage_status()["encrypted_store_locked"])
            self.assertFalse(self.es.append_event(self._ev("blocked", 1000)))
            with self.assertRaises(secure_store.SecureStoreError):
                self.es.purge_before(1500)

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
        self.assertEqual(self.es.read_events(limit=10000)[0]["text"], "secret-body")
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
        self.assertEqual(self.es.read_events(limit=10000)[0]["msg_id"], "legacy")

    def test_quota_blocks_without_deleting_history(self):
        self.es.config.EVENTS_TOTAL_MAX_BYTES = 1
        self.assertTrue(self.es.append_event(self._ev("m1", 1000)))
        self.assertFalse(self.es.append_event(self._ev("m2", 2000)))
        self.assertEqual(len(self.es.read_events(limit=10000)), 1)

    def test_purge_moves_old_files_to_recoverable_archive(self):
        self.es.append_event(self._ev("old", 1000))
        self.es.append_event(self._ev("new", 2000))
        result = self.es.purge_before(1_500_000)
        self.assertEqual(result["removed"], 1)
        self.assertEqual([e["msg_id"] for e in self.es.read_events(limit=10000)], ["new"])
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

    def test_post_html_text_and_at_mention(self):
        from _feishu.message_decode import decode_message
        from lark_bridge.search import _ev

        body = (
            '<figure><img src="http://p1.pstatp.com/origin/demo"/></figure>'
            '<p>请看下<at user_id="ou_post_user">@张三</at>的结果<br/>第二行</p>\x00'
        ).encode()
        # Current type=2 messages store the post HTML in wire field 2, then
        # append opaque attachment metadata.
        content = b"\x0a\x00\x12" + _ev(len(body)) + body + b"\x32\x01x"
        out = decode_message({"type": 2, "content": content})
        self.assertEqual(out["text"], "[图片]\n请看下@张三的结果\n第二行")
        self.assertEqual(out["decode_status"], "ok")
        self.assertIn("ou_post_user", out["mention_user_ids"])

    def test_post_plain_body_fallback(self):
        from _feishu.message_decode import decode_message
        from lark_bridge.search import _ev

        body = "<p>普通富文本</p>".encode()
        out = decode_message({"type": 2, "content": b"\x12" + _ev(len(body)) + body})
        self.assertEqual(out["text"], "普通富文本")
        self.assertEqual(out["decode_status"], "ok")

    def test_nontext_type(self):
        from _feishu.message_decode import decode_message
        out = decode_message({"type": 5, "content": b"\x00\xff"})
        self.assertEqual(out["decode_status"], "nontext")
        self.assertEqual(out["msg_type_name"], "image")

    def test_garbage_does_not_crash(self):
        from _feishu.message_decode import decode_message
        out = decode_message({"type": 4, "content": os.urandom(64)})
        self.assertIn(out["decode_status"], ("ok", "undecoded", "empty"))

    def test_content_resource_metadata_and_safe_envelope(self):
        from _feishu import gateway
        from _feishu.message_model import public_message
        from lark_bridge._proto_utils import proto_to_dict
        from lark_bridge.proto import proto_pb2 as pb

        # Tenant-verified IMAGE layout: Content.f2 is a nested descriptor
        # {f1 key, f2 region, f4 repeated variant {f1 slot, f2 {f1 w, f2 h}}},
        # root f4 = byte size.  Build it on the wire, not via the vendored
        # (misaligned) Content schema.
        def ev(n):
            out = b""
            while True:
                b, n = n & 0x7F, n >> 7
                if n:
                    out += bytes([b | 0x80])
                else:
                    return out + bytes([b])

        def fld(fn, wt):
            return ev(fn << 3 | wt)

        key = "img_v3_0215a_00000000-0000-0000-0000-000000000001g"
        dims = fld(1, 0) + ev(1441) + fld(2, 0) + ev(508)
        variant = fld(1, 0) + ev(1) + fld(2, 2) + ev(len(dims)) + dims
        descriptor = (
            fld(1, 2) + ev(len(key)) + key.encode()
            + fld(2, 2) + ev(9) + b"eu_nc-cdn"
            + fld(4, 2) + ev(len(variant)) + variant
        )
        content = fld(2, 2) + ev(len(descriptor)) + descriptor + fld(4, 0) + ev(261038)

        raw = pb.entities.Message(
            id="m1", type=5, fromId="u1", chatId="c1", createTime=1,
            content=content,
        )
        message = gateway.normalize("m1", proto_to_dict(raw))
        self.assertEqual(message["schema"], 2)
        self.assertEqual(message["type"], {"code": 5, "name": "image"})
        self.assertEqual(message["decode"]["status"], "decoded")
        kinds = {item["kind"] for item in message["resources"]}
        self.assertEqual(kinds, {"image"})
        self.assertEqual(message["content"]["structured"]["image"], {"width": 1441, "height": 508})
        safe = public_message(message)
        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn(key, encoded)
        self.assertNotIn("_resource_keys", safe)

    def test_file_message_has_no_phantom_media_resources(self):
        """The vendored schema reads a FILE message's name/mime as
        imageKey/audioKey — those phantom resources must not appear."""
        from _feishu.message_decode import decode_message

        def ev(n):
            out = b""
            while True:
                b, n = n & 0x7F, n >> 7
                if n:
                    out += bytes([b | 0x80])
                else:
                    return out + bytes([b])

        def fld(fn, wt):
            return ev(fn << 3 | wt)

        def s(fn, text):
            return fld(fn, 2) + ev(len(text)) + text.encode()

        content = (
            s(1, "file_v3_00157_00000000-0000-0000-0000-000000000002g")
            + s(2, "firmware_update_v1.0.4.bin")
            + fld(3, 0) + ev(37472)
            + s(4, "application/octet-stream")
            + s(7, "eu_nc")
        )
        decoded = decode_message({"type": 3, "content": content})
        kinds = {r["kind"] for r in decoded["resources"]}
        self.assertEqual(kinds, {"file"})
        self.assertEqual(decoded["structured"]["fileName"], "firmware_update_v1.0.4.bin")
        self.assertEqual(decoded["structured"]["fileSize"], 37472)

    def test_file_token_in_content_text_is_a_downloadable_file_resource(self):
        from _feishu.message_decode import decode_message
        from _feishu.message_model import build_envelope

        token = "file_v3_00154_00000000-0000-0000-0000-000000000003g"
        decoded = decode_message({"type": 3, "content": b"\x0a" + bytes([len(token)]) + token.encode()})
        envelope = build_envelope(
            "m-file-token", {"type": 3, "fromId": "u", "chatId": "c", "createTime": 1}, decoded
        )
        self.assertEqual(envelope["decode"]["status"], "decoded")
        self.assertTrue(any(item["kind"] == "file" for item in envelope["resources"]))

    def test_unknown_type_is_opaque_and_has_bounded_summary(self):
        from _feishu.message_decode import decode_message

        out = decode_message({"type": 99, "content": b"\x0a\x05hello"})
        self.assertEqual(out["semantic_status"], "opaque")
        self.assertEqual(out["decode_status"], "nontext")
        self.assertEqual(out["raw_summary"]["bytes"], 7)
        self.assertNotIn("text_preview", out["raw_summary"]["fields"][0])

    def test_merge_forward_json_is_bounded_and_typed(self):
        from _feishu.message_decode import decode_message

        payload = json.dumps({"messages": [
            {"id": "m1", "type": 4, "text": "第一条"},
            {"id": "m2", "type": 5, "text": ""},
        ]}, ensure_ascii=False).encode()
        out = decode_message({"type": 11, "content": payload})
        self.assertEqual(out["semantic_status"], "decoded")
        self.assertEqual(len(out["nested_messages"]), 2)
        self.assertEqual(out["nested_messages"][1]["type"]["name"], "image")

    def test_all_proto_types_have_a_schema_type(self):
        from _feishu.message_decode import decode_message
        from _feishu.message_model import TYPE_NAMES, build_envelope, public_message

        for code, name in TYPE_NAMES.items():
            decoded = decode_message({"type": code, "content": b""})
            envelope = build_envelope(
                f"m-{code}",
                {
                    "type": code, "fromId": "u", "chatId": "c",
                    "createTime": 1, "chatType": 2,
                },
                decoded,
            )
            safe = public_message(envelope)
            self.assertEqual(safe["type"], {"code": code, "name": name})
            self.assertIn(safe["decode"]["status"], {"decoded", "opaque", "unavailable"})
            self.assertNotIn("_resource_keys", safe)

    def test_inline_post_image_is_a_processable_resource(self):
        from _feishu.message_decode import decode_message
        from _feishu.message_model import build_envelope
        from lark_bridge.search import _ev

        image = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        body = "<p>图片</p>".encode()
        content = b"\x12" + _ev(len(body)) + body + b"\x2a" + _ev(len(image)) + image
        decoded = decode_message({"type": 2, "content": content})
        envelope = build_envelope(
            "m-inline", {"type": 2, "fromId": "u", "chatId": "c", "createTime": 1}, decoded
        )
        self.assertTrue(any(item["kind"] == "image" for item in envelope["resources"]))
        self.assertTrue(envelope.get("_resource_data"))


class TestMessageResourcesAndMedia(unittest.TestCase):
    def test_resource_id_ignores_volatile_rendered_url_query(self):
        from _feishu.message_model import resource_id

        first = resource_id("image", "https://example.feishu.cn/image/a.png?token=one", 0)
        second = resource_id("image", "https://example.feishu.cn/image/a.png?token=two", 0)
        self.assertEqual(first, second)

    def test_post_image_selector_uses_message_position_when_url_rotates(self):
        from _feishu.message_decode import _resource

        first = _resource(
            "image", "https://example.feishu.cn/render/one/signed-a", ordinal=0, source="post.img"
        )
        second = _resource(
            "image", "https://example.feishu.cn/render/two/signed-b", ordinal=0, source="post.img"
        )
        self.assertEqual(first["resource_id"], second["resource_id"])
        self.assertNotEqual(first["_key"], second["_key"])

    def test_resource_selection_and_known_endpoint_boundary(self):
        from _feishu import message_resources
        from _feishu.errors import EndpointNotAdapted, ResourceError

        message = {
            "resources": [
                {"resource_id": "file:one", "kind": "file", "name": "a.txt", "_key": "file-key"},
                {"resource_id": "image:two", "kind": "image", "name": "a.png",
                 "_key": "img_v3_0215a_00000000-0000-0000-0000-000000000001g"},
                {"resource_id": "sticker:three", "kind": "sticker", "_key": "set:stk"},
            ],
            "_resource_keys": {},
        }
        with self.assertRaises(ResourceError):
            message_resources.select_resource(message)
        file_resource = message_resources.select_resource(message, "file:one")
        self.assertIn("internal-api-drive-stream.feishu.cn", message_resources._url_for(file_resource))
        # Registered 2026-09-07 via media-observe probe (verified 200 image/jpeg).
        image_resource = message_resources.select_resource(message, "image:two")
        url = message_resources._url_for(image_resource)
        self.assertIn("internal-api-lark-file.feishu.cn/static-resource/v1/", url)
        self.assertTrue(url.endswith("~"))
        # Sticker download stays closed until its route is observed.
        with self.assertRaises(EndpointNotAdapted):
            message_resources._url_for(message_resources.select_resource(message, "sticker:three"))

    def test_media_worker_command_is_cgroup_bounded(self):
        from _feishu import media_worker

        command = media_worker.build_command("transcribe", "unit-test", 600)
        self.assertIn("--property=MemoryMax=1G", command)
        self.assertIn("--property=TasksMax=64", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn("--property=RuntimeMaxSec=600s", command)

    def test_media_inspect_does_not_require_optional_models(self):
        import tempfile
        from pathlib import Path
        from _feishu import media_processing

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.bin"
            path.write_bytes(b"not a media container")
            result = media_processing.inspect(path, {"kind": "file", "mime": "application/octet-stream"})
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["info"]["bytes"], 21)

    def test_bounded_streaming_download_writes_requested_output(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from _feishu import message_resources

        class Response:
            status_code = 200
            headers = {"content-type": "text/plain", "content-length": "10"}

            async def aiter_raw(self):
                yield b"hello"
                yield b"world"

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        async def check(root):
            target = root / "download.txt"
            resource = {"resource_id": "file:test", "kind": "file", "_key": "file-key", "name": "x.txt"}
            with patch.object(message_resources.transport, "stream_request", return_value=Context()):
                result = await message_resources.download_resource(
                    {}, resource, out=str(target)
                )
            self.assertEqual(target.read_bytes(), b"helloworld")
            self.assertEqual(result["bytes"], 10)
            self.assertEqual(result["status"], "downloaded")

        with tempfile.TemporaryDirectory() as tmp:
            old_home = message_resources.config.HOME
            try:
                message_resources.config.HOME = Path(tmp)
                asyncio.run(check(Path(tmp)))
            finally:
                message_resources.config.HOME = old_home

    def test_inline_resource_download_does_not_touch_network(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from _feishu import message_resources

        async def check(root):
            rid = "image:inline"
            raw = b"\x89PNG\r\n\x1a\ninline"
            message = {
                "resources": [{"resource_id": rid, "kind": "image", "name": "x.png"}],
                "_resource_data": {rid: raw},
            }
            resource = message_resources.select_resource(message, rid)
            target = root / "inline.png"
            result = await message_resources.download_resource(message, resource, out=str(target))
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(result["bytes"], len(raw))

        with tempfile.TemporaryDirectory() as tmp:
            old_home = message_resources.config.HOME
            try:
                message_resources.config.HOME = Path(tmp)
                asyncio.run(check(Path(tmp)))
            finally:
                message_resources.config.HOME = old_home

    def test_resource_probe_returns_adapter_status_without_body(self):
        import asyncio
        from _feishu import message_resources

        async def check():
            message = {
                "type": {"code": 3, "name": "file"},
                "resources": [
                    {"resource_id": "file:test", "kind": "file", "name": "x.txt", "_key": "file-key"},
                    {"resource_id": "image:test", "kind": "image", "name": "x.png", "_key": "image-key"},
                ],
                "_resource_keys": {"file:test": "file-key", "image:test": "image-key"},
            }
            with patch.object(message_resources, "fetch_message", new=AsyncMock(return_value=message)):
                result = await message_resources.probe_metadata("m1")
            self.assertFalse(result["contains_bodies"])
            self.assertFalse(result["contains_credentials"])
            self.assertEqual(result["resources"][0]["endpoint_status"], "registered")
            # image is registered since 2026-09-07 (static-resource route)
            self.assertEqual(result["resources"][1]["endpoint_status"], "registered")
            self.assertNotIn("file-key", json.dumps(result, ensure_ascii=False))

        asyncio.run(check())

    def test_media_cache_requires_encryption(self):
        import tempfile
        from pathlib import Path
        from _feishu import media_store, secure_store
        from _feishu.errors import ResourceError

        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("FEISHU_HOME")
            os.environ["FEISHU_HOME"] = tmp
            import importlib
            from _feishu import config
            importlib.reload(config)
            importlib.reload(media_store)
            source = Path(tmp) / "source.bin"
            source.write_bytes(b"data")
            try:
                with patch.object(secure_store, "mode", return_value="plaintext_permissions"):
                    with self.assertRaises(ResourceError):
                        media_store.store(source, "file:test")
            finally:
                if old_home is None:
                    os.environ.pop("FEISHU_HOME", None)
                else:
                    os.environ["FEISHU_HOME"] = old_home
                importlib.reload(config)
                importlib.reload(media_store)

    def test_chunked_media_encryption_round_trips_without_plaintext_file(self):
        import tempfile
        from pathlib import Path
        from _feishu import secure_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            encrypted = root / "source.media.enc"
            restored = root / "restored.bin"
            source.write_bytes((b"0123456789" * 200_000) + b"tail")
            with patch.object(secure_store, "_load_key", return_value=b"k" * 32):
                secure_store.encrypt_file(source, encrypted, chunk_size=64 * 1024)
                secure_store.decrypt_file(encrypted, restored, max_output_bytes=4 * 1024 * 1024)
            self.assertEqual(restored.read_bytes(), source.read_bytes())
            self.assertNotIn(b"0123456789", encrypted.read_bytes())


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

    def test_build_event_contains_safe_schema2_message_envelope(self):
        from _feishu import daemon
        from lark_bridge._proto_utils import proto_to_dict
        from lark_bridge.proto import proto_pb2 as pb

        content = pb.Content(
            fileKey="private-file-key",
            fileName="x.txt",
            fileMime="text/plain",
            fileSize=4,
        )
        raw = pb.entities.Message(
            id="m1", type=3, fromId="other", chatId="c1", chatType=1,
            createTime=1, content=content.SerializeToString(),
        )
        event = daemon._build_event(
            {
                "msg_id": "m1", "from_id": "other", "chat_id": "c1", "chat_type": 1,
                "type": 3, "create_time": 1, "at_me": False,
                "at_out_chatter_ids": [], "raw": proto_to_dict(raw),
            },
            "me",
        )
        self.assertEqual(event["schema"], 2)
        self.assertEqual(event["message"]["schema"], 2)
        self.assertEqual(event["message"]["type"]["name"], "file")
        self.assertNotIn("private-file-key", json.dumps(event, ensure_ascii=False))


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

    def test_live_context_is_bounded_and_strips_transport_fields(self):
        from _feishu.context import bound_context
        source = {
            "target": {"msg_id": "target", "text": "x" * 10000, "raw": "secret"},
            "root": {"msg_id": "root"},
            "chain": [{"msg_id": "root", "text": "root", "raw": "secret"}],
            "replies": [], "window": [], "complete": True, "truncated": False,
            "missing_parent_ids": [], "search_window": {},
        }
        out = bound_context(source, max_messages=10, max_bytes=500)
        self.assertTrue(out["truncated"])
        self.assertNotIn("raw", out["target"])
        self.assertLessEqual(
            len(json.dumps(out["target"], ensure_ascii=False).encode()), 500
        )


class TestMailCalendarContracts(unittest.TestCase):
    def test_mail_write_defaults_to_draft(self):
        import asyncio
        from _feishu.errors import SendBlocked
        from _feishu import mail
        with self.assertRaises(SendBlocked) as caught:
            asyncio.run(mail.send_message(["recipient"], "主题", "正文"))
        self.assertIn("mail_send", caught.exception.hint)

    def test_calendar_write_defaults_to_draft(self):
        import asyncio
        from _feishu.errors import SendBlocked
        from _feishu import calendar_ops as calendar
        with self.assertRaises(SendBlocked) as caught:
            asyncio.run(calendar.create_event("联调", "2026-08-31T10:00:00+08:00", "2026-08-31T11:00:00+08:00"))
        self.assertIn("calendar_create", caught.exception.hint)

    def test_ui_fingerprint_and_calendar_time_helpers(self):
        from _feishu import calendar_ops as calendar
        from _feishu import mail
        a = {"id": "", "subject": "主题", "sender": "甲", "date": "10:00", "desc": "正文", "text": "主题\n正文"}
        self.assertEqual(mail._row_id(a), mail._row_id(a))
        self.assertTrue(mail._row_id(a).startswith("ui:"))
        self.assertEqual(
            calendar._end_from_title("例会, 10:40 - 11:10", "2026-08-31T10:40:00+08:00"),
            "2026-08-31T11:10:00+08:00",
        )


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


class TestDocumentContracts(unittest.TestCase):
    def test_space_listing_reports_partial_permission_failure(self):
        import asyncio
        from _feishu import documents

        async def ok(*_args):
            return [{"token": "mine", "name": "mine", "type": "folder"}]

        async def denied(*_args):
            raise PermissionError("denied")

        async def check():
            with patch.object(documents, "_creds", return_value=("", {}, "tenant.test")), \
                 patch.object(documents.drive, "list_my_space", new=ok), \
                 patch.object(documents.drive, "list_shared_folders", new=denied):
                result = await documents.list_spaces()
            self.assertFalse(result["complete"])
            self.assertTrue(result["incomplete"])
            self.assertEqual(result["my_space"][0]["token"], "mine")
            self.assertEqual(result["errors"][0]["space"], "shared_space")

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
        self.assertAlmostEqual(parse_when("+7d"), t0 + 7 * 86400 * 1000, delta=2000)
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

    def test_local_message_time_window(self):
        from _feishu.messages import in_time_window
        self.assertTrue(in_time_window({"create_time": "1000"}, 900, 1100))
        self.assertFalse(in_time_window({"create_time": "1000"}, 1001, 0))


class TestAttribution(unittest.TestCase):
    """Bug: p2p history showed every sender as a garbage heuristic name
    ("xi tong xiao xi"), making it impossible to tell 我 from 对方."""

    def setUp(self):
        import tempfile
        import importlib
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FEISHU_HOME"] = self.tmp.name
        from _feishu import config, messages, gateway
        importlib.reload(config)
        importlib.reload(messages)
        self.config = config
        self.messages = messages
        self.gateway = gateway

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FEISHU_HOME", None)

    def _write_names(self, users=None, chats=None):
        self.config.ensure_home()
        self.config.NAMES_CACHE.write_text(json.dumps(
            {"users": users or {}, "chats": chats or {}}, ensure_ascii=False))

    def test_load_names_purges_poisoned_entries(self):
        self._write_names(users={"u1": "xi tong xiao xi", "u2": "张伟", "u3": ""},
                          chats={"c1": "系统消息", "c2": "真实的群"})
        names = self.messages._load_names()
        self.assertEqual(names["users"], {"u2": "张伟"})
        self.assertEqual(names["chats"], {"c2": "真实的群"})

    def test_resolve_names_never_caches_garbage(self):
        async def fake_user_name(uid, cid="0"):
            return "xi tong xiao xi"  # the observed poison value
        with patch.object(self.messages.gateway, "get_user_name", fake_user_name), \
             patch.object(self.messages.gateway, "get_group_name", AsyncMock(return_value="")):
            msgs = [{"msg_id": "m1", "chat_id": "c1", "from_id": "u1"}]
            import asyncio
            asyncio.run(self.messages.resolve_names(msgs))
        self.assertEqual(msgs[0]["from_name"], "")
        self.assertEqual(msgs[0]["name_status"], "unresolved")
        self.assertEqual(self.messages._load_names()["users"], {})

    def test_whoami_self_name_wins_over_cache(self):
        self._write_names(users={"me1": "xi tong xiao xi"})
        msgs = [{"msg_id": "m1", "chat_id": "c1", "from_id": "me1"}]
        import asyncio
        with patch.object(self.messages.gateway, "get_group_name", AsyncMock(return_value="")):
            asyncio.run(self.messages.resolve_names(msgs, my_id="me1", my_name="号主"))
        self.assertEqual(msgs[0]["from_name"], "号主")
        self.assertEqual(self.messages._load_names()["users"]["me1"], "号主")

    def test_uniform_name_across_users_is_dropped(self):
        async def same_for_all(uid, cid="0"):
            return "同名"
        with patch.object(self.messages.gateway, "get_user_name", same_for_all), \
             patch.object(self.messages.gateway, "get_group_name", AsyncMock(return_value="")):
            msgs = [{"msg_id": f"m{i}", "chat_id": "c1", "from_id": f"u{i}"} for i in range(4)]
            import asyncio
            asyncio.run(self.messages.resolve_names(msgs))
        # one string covering many distinct ids is a poison signature
        self.assertEqual(self.messages._load_names()["users"], {})
        self.assertTrue(all(m["from_name"] == "" for m in msgs))

    def test_apply_direction_labels_roles(self):
        msgs = [
            {"msg_id": "m1", "from_id": "me1"},
            {"msg_id": "m2", "from_id": "peer1"},
        ]
        self.messages.apply_direction(msgs, "me1")
        self.assertTrue(msgs[0]["is_me"])
        self.assertEqual(msgs[0]["direction"], "outgoing")
        self.assertFalse(msgs[1]["is_me"])
        self.assertEqual(msgs[1]["direction"], "incoming")

    def test_sender_label_p2p_roles_cannot_swap(self):
        me = {"from_id": "me1", "chat_type": 1, "is_me": True, "from_name": "号主"}
        peer = {"from_id": "peer1", "chat_type": 1, "is_me": False, "from_name": ""}
        poisoned = {"from_id": "peer1", "chat_type": 1, "is_me": False,
                    "from_name": "xi tong xiao xi"}
        self.assertEqual(self.messages._sender_label(me), "我（号主）")
        self.assertEqual(self.messages._sender_label(peer), "对方（id:peer1）")
        # denylisted garbage must never be displayed as a name
        self.assertEqual(self.messages._sender_label(poisoned), "对方（id:peer1）")

    def test_sender_label_group_falls_back_to_id(self):
        m = {"from_id": "u9", "chat_type": 2, "is_me": False, "from_name": ""}
        self.assertEqual(self.messages._sender_label(m), "u9")

    def test_name_endpoint_dead_short_circuits(self):
        self.gateway._NAME_ENDPOINT["dead"] = False
        self.gateway._NAME_ENDPOINT["noted"] = False
        calls = []

        async def fake_call(cmd, req, rid):
            calls.append(cmd)
            pkt = self.gateway.pb.Packet()
            pkt.payloadType = 5  # observed tenant behavior: no payload at all
            return pkt.SerializeToString()

        async def run():
            with patch.object(self.gateway.transport, "gateway_call", fake_call):
                self.assertEqual(await self.gateway.get_user_name("u1", "c1"), "")
                self.assertEqual(await self.gateway.get_user_name("u2", "c1"), "")
                self.assertEqual(await self.gateway.get_group_name("c1"), "")

        import asyncio
        asyncio.run(run())
        # first failure marks the endpoint dead; later lookups skip the network
        self.assertEqual(len(calls), 1)

    def test_harvest_chat_names_infers_p2p_peer(self):
        rows = [
            {"chat_id": "c_p2p", "name": "张伟"},
            {"chat_id": "c_grp", "name": "项目群"},
        ]
        summaries = {
            "c_p2p": {"chat_type": 1, "senders": ["me1", "peer1"]},
            "c_grp": {"chat_type": 2, "senders": ["u1", "u2"]},
        }
        report = self.messages.harvest_chat_names(rows, summaries=summaries, my_id="me1")
        names = self.messages._load_names()
        self.assertEqual(names["chats"]["c_p2p"], "张伟")
        self.assertEqual(names["chats"]["c_grp"], "项目群")
        self.assertEqual(names["users"]["peer1"], "张伟")
        self.assertEqual(report["peer_names"], 1)
        # group chat names must not become user names
        self.assertNotIn("u1", names["users"])

    def test_harvest_chat_names_ambiguous_p2p_skipped(self):
        rows = [{"chat_id": "c_p2p", "name": "某人"}]
        summaries = {"c_p2p": {"chat_type": 1, "senders": ["a", "b", "c"]}}
        self.messages.harvest_chat_names(rows, summaries=summaries, my_id="me1")
        self.assertEqual(self.messages._load_names()["users"], {})

    def test_harvest_mentions_resolves_group_senders(self):
        import asyncio
        msgs = [
            {
                "msg_id": "m1",
                "from_id": "u100",
                "chat_id": "c_grp",
                "chat_type": 2,
                "text": '<at user_id="u200">@李四</at> 请看下',
            },
            {
                "msg_id": "m2",
                "from_id": "u200",
                "chat_id": "c_grp",
                "chat_type": 2,
                "text": '<at user_id="u100">@林小明</at> 收到',
            },
            {
                "msg_id": "m3",
                "from_id": "u300",
                "chat_id": "c_grp",
                "chat_type": 2,
                "text": '<at user_id="all">@所有人</at> 忽略通用at',
            },
        ]
        asyncio.run(self.messages.resolve_names(msgs, my_id="my_id", my_name="号主"))
        names = self.messages._load_names()
        self.assertEqual(names["users"]["u100"], "林小明")
        self.assertEqual(names["users"]["u200"], "李四")
        self.assertNotIn("all", names["users"])
        # Both senders must be resolved in place
        self.assertEqual(msgs[0]["from_name"], "林小明")
        self.assertEqual(msgs[1]["from_name"], "李四")
        self.assertEqual(self.messages._sender_label(msgs[0]), "林小明")
        self.assertEqual(self.messages._sender_label(msgs[1]), "李四")

    def test_media_video_message_decode(self):
        from _feishu.message_decode import decode_message
        def ev(n):
            out = b""
            while True:
                b, n = n & 0x7F, n >> 7
                if n:
                    out += bytes([b | 0x80])
                else:
                    return out + bytes([b])

        def fld(fn, wt):
            return ev(fn << 3 | wt)

        # Wire layout: f1=key, f2=name, f3=size, f4=mime, f6=cover, f7=duration
        key = "file_v3_0015a_00000000-0000-0000-0000-000000000004g"
        name = "test.mov"
        mime = "video/quicktime"
        cover_key = "img_v3_0215a_00000000-0000-0000-0000-000000000005g"
        raw_key = b"K" * 32
        raw_nonce = b"N" * 12
        crypto_proto = fld(1, 2) + ev(32) + raw_key + fld(2, 2) + ev(12) + raw_nonce
        cover_payload = (
            fld(1, 2) + ev(len(cover_key)) + cover_key.encode()
            + fld(9, 2) + ev(len(crypto_proto)) + crypto_proto
        )
        f6_payload = fld(2, 2) + ev(len(cover_payload)) + cover_payload

        content = (
            fld(1, 2) + ev(len(key)) + key.encode()
            + fld(2, 2) + ev(len(name)) + name.encode()
            + fld(3, 0) + ev(1024)
            + fld(4, 2) + ev(len(mime)) + mime.encode()
            + fld(6, 2) + ev(len(f6_payload)) + f6_payload
            + fld(7, 0) + ev(5000)
        )
        out = decode_message({"type": 15, "content": content})
        self.assertEqual(out["semantic_status"], "decoded")
        self.assertEqual(out["decode_status"], "nontext")
        self.assertEqual(out["text"], "[视频: test.mov]")
        kinds = {r["kind"] for r in out["resources"]}
        self.assertIn("media", kinds)
        self.assertIn("image", kinds)
        cover_res = next(r for r in out["resources"] if r["source"] == "content.media_cover")
        self.assertEqual(cover_res["_crypto"]["algo"], "aes-256-gcm")
        self.assertEqual(cover_res["_crypto"]["key"], raw_key.hex())
        self.assertEqual(cover_res["_crypto"]["nonce"], raw_nonce.hex())


class TestSearchPageLimit(unittest.TestCase):
    """Bug: thread/history failed outright when a search needed >8 pages."""

    def test_page_limit_degrades_to_partial_result(self):
        import asyncio
        from _feishu import gateway, config

        async def fake_page(chat_id, start, end, from_id, mention_user_id, page_token):
            # Server always has another page: the page budget must run out.
            return {
                "msg_ids": [f"m-{page_token}-{i}" for i in range(3)],
                "has_more": True,
                "page_token": f"{page_token}x",
            }

        old = config.SEARCH_MAX_PAGES
        config.SEARCH_MAX_PAGES = 2
        try:
            with patch.object(gateway, "search_msg_ids_page", fake_page):
                res = asyncio.run(gateway.search_msg_ids("c1", 0, 0, limit=200))
        finally:
            config.SEARCH_MAX_PAGES = old
        self.assertTrue(res["page_limit_hit"])
        self.assertTrue(res["has_more"])
        self.assertEqual(len(res["msg_ids"]), 6)  # 2 pages x 3 ids, not an error

    def test_within_budget_has_no_page_limit_flag(self):
        import asyncio
        from _feishu import gateway

        async def fake_page(chat_id, start, end, from_id, mention_user_id, page_token):
            return {"msg_ids": ["m1", "m2"], "has_more": False, "page_token": ""}

        with patch.object(gateway, "search_msg_ids_page", fake_page):
            res = asyncio.run(gateway.search_msg_ids("c1", 0, 0, limit=30))
        self.assertFalse(res["page_limit_hit"])
        self.assertFalse(res["has_more"])


if __name__ == "__main__":
    unittest.main()
