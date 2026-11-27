"""P1 传输层（integration，真 HTTP + 假网关上游）。

覆盖：路由分发、统一错误码结构（401/403/404/400/500 的
{"error": {"code": ..., "message": ...}}）、静态文件读取
（agent/web/chat.html、gateway/web/login.html）、事件结构
（有工具调用 / 有提醒送达时 events 数组的内容）、健康检查内容不变、
/api/metrics 的 Prometheus 文本。
agent server 本地起在 127.0.0.1 随机端口，TENANT_ID=demo，数据用临时
目录，不碰项目 data/；网关部分不用真 Docker，用 test_files.py 同款
假 handler 测 _proxy / do_GET。
"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from tests.helpers import fresh_tenant_data, make_token
from agent.graph import collect_events
import agent.server as server_mod
import agent.tenant as tenant_mod
from errors.codes import ApiError, ErrorCategory, error_body
import gateway.main as gateway_mod
from observability import metrics as obs_metrics


class TestTransportHttp(unittest.TestCase):
    """agent 传输层：真起一个 HTTP 服务测路由/错误码/静态页/事件。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = fresh_tenant_data()
        cls._ctx.__enter__()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.demo_token = make_token("demo")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._ctx.__exit__(None, None, None)

    def _req(self, method, path, token="USE_DEMO", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if token == "USE_DEMO":
            token = self.demo_token
        if token:
            headers["X-Tenant-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body, ensure_ascii=False)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type")
        conn.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return resp.status, ctype, raw, parsed

    # -- 静态文件：能读到，内容和原来内嵌字符串一致 ----------------------
    def test_root_serves_chat_html_file_bytes(self):
        status, ctype, raw, _ = self._req("GET", "/")
        chat_html = (Path(server_mod.__file__).parent / "web" / "chat.html"
                     ).read_bytes()
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertEqual(raw, chat_html)

    def test_chat_html_file_has_original_content(self):
        # 原来 PAGE 字符串里的关键内容一个不少：标题、linkify、/files/ 链、
        # 输入框和发送逻辑；只有读错误的一行按新错误结构改过
        #（d.error.message，见 agent/web/chat.html 的注释级改动）。
        page = (Path(server_mod.__file__).parent / "web" / "chat.html"
                ).read_text(encoding="utf-8")
        self.assertIn("<title>多租户 Agent</title>", page)
        self.assertIn("linkify", page)
        self.assertIn("[/]files[/]", page)
        self.assertIn('id="q"', page)
        self.assertIn("d.error.message", page)
        # 内嵌 HTML 字符串已消失：server.py 里不再有 PAGE = ...
        server_src = Path(server_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PAGE =", server_src)

    def test_login_html_file_has_original_content(self):
        page = (Path(gateway_mod.__file__).parent / "web" / "login.html"
                ).read_text(encoding="utf-8")
        self.assertIn("<title>登录 · 多租户 Agent</title>", page)
        self.assertIn("linkify", page)
        self.assertIn("[/]files[/]", page)
        self.assertIn("mt_token", page)
        self.assertIn("d.error.message", page)
        gateway_src = Path(gateway_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("LOGIN_PAGE =", gateway_src)

    # -- 健康检查与指标：内容不变、只挪位置 ------------------------------
    def test_health_payload_unchanged(self):
        # 内容和原来 server.py 里的一致：字段、engine、ok 都不变。
        # tenant 取 server 当前的 TENANT_ID（调度器线程测试会把它 patch 成
        # "sched-tenant" 且不还原，见 test_reminder_tools.py，不硬编码 demo）。
        status, _, _, body = self._req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "ok": True, "model": body["model"], "engine": "langgraph",
            "tenant": server_mod.TENANT_ID or None,
            "single_tenant": bool(server_mod.TENANT_ID),
            "sandbox_mode": body["sandbox_mode"]})
        self.assertIsInstance(body["model"], str)
        self.assertTrue(body["model"])

    def test_metrics_endpoint_prometheus_text(self):
        status, ctype, raw, _ = self._req("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/plain"))
        text = raw.decode()
        for name in ("mt_requests_total", "mt_request_failures_total",
                     "mt_model_calls_total"):
            self.assertIn(f"# TYPE {name} counter", text)
            line = [l for l in text.splitlines()
                    if l.startswith(name + " ")][0]
            self.assertGreaterEqual(int(line.split()[1]), 0)

    def test_metrics_requests_counter_increases(self):
        before = obs_metrics.snapshot()["requests"]
        self._req("GET", "/api/health")
        after = obs_metrics.snapshot()["requests"]
        self.assertEqual(after, before + 1)

    # -- 统一错误码结构 ---------------------------------------------------
    def test_me_unauthenticated_error_structure(self):
        status, ctype, _, body = self._req("GET", "/api/me", token=None)
        self.assertEqual(status, 401)
        self.assertTrue(ctype.startswith("application/json"))
        self.assertEqual(body["error"]["code"], "unauthenticated")
        self.assertIn("未登录", body["error"]["message"])
        self.assertEqual(set(body["error"]), {"code", "message"})

    def test_unknown_route_404_error_structure(self):
        status, _, _, body = self._req("GET", "/nope", token=None)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_files_missing_404_error_structure(self):
        # 以前是 plain text "not found"，现在是统一 JSON 错误结构。
        status, ctype, _, body = self._req("GET", "/files/nope.html")
        self.assertEqual(status, 404)
        self.assertTrue(ctype.startswith("application/json"))
        self.assertEqual(body["error"]["code"], "not_found")

    def test_files_traversal_403_error_structure(self):
        status, _, _, body = self._req("GET", "/files/../memory.db")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_chat_empty_message_400_error_structure(self):
        status, _, _, body = self._req(
            "POST", "/api/chat", body={"message": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertEqual(body["error"]["message"], "消息是空的")

    def test_chat_invalid_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/chat", body="not json",
                     headers={"X-Tenant-Token": self.demo_token,
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_chat_model_error_500_structure(self):
        # 以前模型失败返回 HTTP 200 + {"error": 字符串}，现在是 500 +
        # {"error": {"code": "model_error", ...}}（状态码语义归位）。
        with mock.patch.object(
                server_mod, "chat_with_events",
                side_effect=RuntimeError("boom")):
            status, _, _, body = self._req(
                "POST", "/api/chat", body={"message": "hi"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "model_error")
        self.assertIn("boom", body["error"]["message"])

    # -- 事件结构 ----------------------------------------------------------
    def test_chat_events_with_tool_call(self):
        fake = {"reply": "ok", "events": [
            {"type": "tool_call", "name": "list_todos", "args": {}}]}
        with mock.patch.object(server_mod, "chat_with_events",
                               return_value=fake):
            status, _, _, body = self._req(
                "POST", "/api/chat", body={"message": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "ok")
        self.assertEqual(body["tenant"], "demo")
        self.assertEqual(body["events"][0]["type"], "tool_call")
        self.assertEqual(body["events"][0]["name"], "list_todos")

    def test_chat_events_with_reminder_delivered(self):
        fake = {"reply": "提醒你喝水", "events": [
            {"type": "reminder_delivered", "notification_id": 3,
             "text": "喝水", "due_at": "2026-11-27T19:20:00+08:00"}]}
        with mock.patch.object(server_mod, "chat_with_events",
                               return_value=fake):
            _, _, _, body = self._req(
                "POST", "/api/chat", body={"message": "hi"})
        ev = body["events"][0]
        self.assertEqual(ev["type"], "reminder_delivered")
        self.assertEqual(ev["notification_id"], 3)
        self.assertEqual(ev["text"], "喝水")
        self.assertEqual(ev["due_at"], "2026-11-27T19:20:00+08:00")

    def test_chat_events_empty_array_when_none(self):
        # 协议写死：没有事件时 events 是空数组，不省略。
        with mock.patch.object(server_mod, "chat_with_events",
                               return_value={"reply": "ok", "events": []}):
            _, _, _, body = self._req(
                "POST", "/api/chat", body={"message": "hi"})
        self.assertEqual(body["events"], [])

    def test_collect_events_reminders_before_tool_calls(self):
        result = {
            "due_notifications": [
                {"id": 7, "text": "喝水", "due_at": "2026-11-27T19:20:00+08:00"}],
            "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "add_reminder",
                     "args": {"text": "开会", "time_text": "晚上8点"},
                     "id": "call-1", "type": "tool_call"}]),
                AIMessage(content="好的"),
            ],
        }
        events = collect_events(result)
        self.assertEqual([e["type"] for e in events],
                         ["reminder_delivered", "tool_call"])
        self.assertEqual(events[0]["notification_id"], 7)
        self.assertEqual(events[1]["name"], "add_reminder")
        self.assertEqual(events[1]["args"]["text"], "开会")

    def test_collect_events_empty(self):
        self.assertEqual(
            collect_events({"due_notifications": [],
                            "messages": [AIMessage(content="你好")]}), [])

    # -- 错误码包本身 ------------------------------------------------------
    def test_error_body_shape_and_codes(self):
        self.assertEqual(error_body("not_found", "x"),
                         {"error": {"code": "not_found", "message": "x"}})
        err = ApiError("model_error", "模型挂了")
        self.assertEqual(err.status, 500)
        self.assertEqual(err.category, ErrorCategory.MODEL_FAILURE)
        self.assertTrue(err.retryable)
        err = ApiError("tenant_isolation", "跨租户")
        self.assertEqual(err.status, 403)
        self.assertEqual(err.category, ErrorCategory.TENANT_ISOLATION)
        self.assertFalse(err.retryable)


class TestGatewayTransport(unittest.TestCase):
    """网关传输层：登录页走静态文件、代理鉴权失败走统一错误结构。"""

    def _fake_handler(self, path="/", headers=None):
        h = object.__new__(gateway_mod.Handler)
        h.path = path
        h.headers = dict(headers or {})
        h.command = "GET"
        h.rfile = BytesIO(b"")
        h.sent = []
        h._send = lambda code, body, ctype="text/html; charset=utf-8", extra=None: \
            h.sent.append((code, body, ctype))
        h._send_error = lambda err: h.sent.append(
            (err.status, json.dumps(err.body(), ensure_ascii=False),
             "application/json"))
        return h

    def test_gateway_root_serves_login_html_file(self):
        handler = self._fake_handler("/")
        handler.do_GET()
        login_html = (Path(gateway_mod.__file__).parent / "web" / "login.html"
                      ).read_text(encoding="utf-8")
        self.assertEqual(handler.sent[0][0], 200)
        self.assertEqual(handler.sent[0][1], login_html)

    def test_gateway_proxy_unauthenticated_error_structure(self):
        handler = self._fake_handler("/files/calculator.html", {})
        handler._proxy()
        self.assertEqual(handler.sent[0][0], 401)
        body = json.loads(handler.sent[0][1])
        self.assertEqual(body["error"]["code"], "unauthenticated")
        self.assertEqual(set(body["error"]), {"code", "message"})
