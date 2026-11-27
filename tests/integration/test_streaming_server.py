"""P5 流式聊天（integration，真 HTTP 服务 + 假 chat_stream）。

覆盖：POST /api/chat/stream 的 SSE 响应头、事件按 data: 行
逐个到达、空消息 400、未登录 401、流中 error 事件收尾。
不调真模型：server_mod.chat_stream_with_events 被换成假生成器。
"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from tests.helpers import fresh_tenant_data, make_token
import agent.server as server_mod


def _parse_sse(body: bytes) -> list:
    events = []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        events.append(json.loads(block[len("data:"):].strip()))
    return events


class TestStreamingServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ctx = fresh_tenant_data()
        cls._ctx.__enter__()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         server_mod.Handler)
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

    def _post(self, path, token, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Tenant-Token"] = token
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp, raw

    def test_stream_happy_path(self):
        def fake_stream(tenant, text, session_id=None):
            yield {"type": "status", "stage": "received",
                   "text": "已收到，正在思考…"}
            yield {"type": "token", "text": "你好"}
            yield {"type": "token", "text": "呀"}
            yield {"type": "done", "reply": "你好呀", "events": [],
                   "session_id": "default"}

        with mock.patch.object(server_mod, "chat_stream_with_events",
                               side_effect=fake_stream):
            resp, raw = self._post("/api/chat/stream", self.demo_token,
                                   {"message": "你好"})
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type")
                        .startswith("text/event-stream"))
        events = _parse_sse(raw)
        self.assertEqual([e["type"] for e in events],
                         ["status", "token", "token", "done"])
        self.assertEqual(events[-1]["reply"], "你好呀")

    def test_stream_error_event_ends_stream(self):
        def fake_stream(tenant, text, session_id=None):
            yield {"type": "status", "stage": "received",
                   "text": "已收到，正在思考…"}
            yield {"type": "error", "code": "model_error",
                   "message": "模型调用失败：炸了"}

        with mock.patch.object(server_mod, "chat_stream_with_events",
                               side_effect=fake_stream):
            resp, raw = self._post("/api/chat/stream", self.demo_token,
                                   {"message": "你好"})
        self.assertEqual(resp.status, 200)
        events = _parse_sse(raw)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("炸了", events[-1]["message"])

    def test_stream_empty_message_400(self):
        resp, raw = self._post("/api/chat/stream", self.demo_token,
                               {"message": "   "})
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "bad_request")

    def test_stream_unauthenticated_401(self):
        resp, raw = self._post("/api/chat/stream", None,
                               {"message": "你好"})
        self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
