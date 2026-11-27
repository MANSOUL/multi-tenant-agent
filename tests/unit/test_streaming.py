"""P5 流式聊天（SSE 思考过程）单测。

覆盖：
- format_sse_event 的 wire 格式（data: {...}\\n\\n，可解析回原 dict）；
- _chunk_text / _chunk_reasoning（str 内容、parts 列表、推理内容）；
- stream_graph：fake 图按顺序吐 messages/updates、超时报 TimeoutError、
  图内异常原样上抛；
- ChatService.chat_stream：假图 happy path（status → token → done，
  reply 是 token 拼起来的）、澄清捷径不调图直接 done、图异常变 error 事件；
- 网关代理：上游 text/event-stream 时逐块透传（不读完再发），
  其它类型还是老路子一次读完。
"""
import json
import time
import unittest
from io import BytesIO
from unittest import mock

from langchain_core.messages import AIMessage, ToolMessage

from agent.server import format_sse_event
from app.chat_service import ChatService
from loop.config import AgentLoopConfig
from loop.loop import (
    _chunk_reasoning,
    _chunk_text,
    stream_graph,
)
from tests.helpers import fresh_tenant_data


class _FakeChunk:
    def __init__(self, content="", reasoning=None):
        self.content = content
        self.additional_kwargs = {}
        if reasoning is not None:
            self.additional_kwargs["reasoning_content"] = reasoning


class TestSseFormat(unittest.TestCase):
    def test_event_round_trips(self):
        ev = {"type": "token", "text": "你好，世界"}
        raw = format_sse_event(ev)
        self.assertTrue(raw.startswith(b"data: "))
        self.assertTrue(raw.endswith(b"\n\n"))
        self.assertEqual(json.loads(raw[len(b"data: "):].strip()), ev)

    def test_chunk_text(self):
        self.assertEqual(_chunk_text(_FakeChunk("你好")), "你好")
        self.assertEqual(_chunk_text(_FakeChunk(
            [{"type": "text", "text": "你"},
             {"type": "image_url", "image_url": {"url": "x"}},
             {"type": "text", "text": "好"}])), "你好")

    def test_chunk_reasoning(self):
        self.assertEqual(_chunk_reasoning(_FakeChunk("", "让我想想")), "让我想想")
        self.assertEqual(_chunk_reasoning(_FakeChunk("正文")), "")


class _FakeGraph:
    def __init__(self, items=None, exc=None, sleep=0.0):
        self._items = items or []
        self._exc = exc
        self._sleep = sleep
        self.calls = 0

    def stream(self, initial_state, config=None, stream_mode=None):
        self.calls += 1
        if self._sleep:
            time.sleep(self._sleep)
        if self._exc is not None:
            raise self._exc
        yield from self._items


class TestStreamGraph(unittest.TestCase):
    def test_yields_items_in_order(self):
        items = [
            ("messages", (_FakeChunk("你"), {"langgraph_node": "llm"})),
            ("updates", {"llm": {"messages": []}}),
            ("updates", {"save_memory": {}}),
        ]
        got = list(stream_graph(
            _FakeGraph(items), {}, AgentLoopConfig(timeout_seconds=5)))
        self.assertEqual(got, items)

    def test_timeout(self):
        with self.assertRaises(TimeoutError):
            list(stream_graph(
                _FakeGraph(sleep=1.0), {},
                AgentLoopConfig(timeout_seconds=0.1)))

    def test_graph_error_propagates(self):
        with self.assertRaises(RuntimeError):
            list(stream_graph(
                _FakeGraph(exc=RuntimeError("炸了")), {},
                AgentLoopConfig(timeout_seconds=5)))


class _StreamCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def _service_with(self, graph):
        service = ChatService(graph_builder=lambda tenant: graph)
        return service


class TestChatStream(_StreamCase):
    def test_happy_path_status_token_done(self):
        ai = AIMessage(content="你好呀")
        items = [
            ("messages", (_FakeChunk("你好"), {"langgraph_node": "llm"})),
            ("messages", (_FakeChunk("呀"), {"langgraph_node": "llm"})),
            ("updates", {"recall_memory": {"due_notifications": []}}),
            ("updates", {"llm": {"messages": [ai]}}),
            ("updates", {"save_memory": {}}),
        ]
        service = self._service_with(_FakeGraph(items))
        events = list(service.chat_stream("demo", "你好"))
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(events[0]["stage"], "received")
        tokens = [e["text"] for e in events if e["type"] == "token"]
        self.assertEqual(tokens, ["你好", "呀"])
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["reply"], "你好呀")
        self.assertEqual(done["session_id"], "default")

    def test_tool_events_live(self):
        ai = AIMessage(content="", tool_calls=[{
            "id": "call-1", "name": "remember_fact",
            "args": {"key": "城市", "value": "上海"}}])
        tool_msg = ToolMessage(content="记住了", tool_call_id="call-1")
        items = [
            ("updates", {"llm": {"messages": [ai]}}),
            ("updates", {"tools": {"messages": [tool_msg]}}),
            ("updates", {"save_memory": {}}),
        ]
        service = self._service_with(_FakeGraph(items))
        events = list(service.chat_stream("demo", "记住城市是上海"))
        kinds = [e["type"] for e in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        tc = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(tc["name"], "remember_fact")
        tr = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual(tr["name"], "remember_fact")
        self.assertEqual(tr["result"], "记住了")

    def test_clarification_shortcut_never_calls_graph(self):
        from agent.tenant import tenant_db
        from datetime import datetime, timezone
        from store import create_store

        now = datetime.now(timezone.utc).isoformat()
        with tenant_db("demo") as conn:
            store = create_store(conn)
            store.append_chat(now, "user", "8点提醒我开会")
            store.append_chat(now, "assistant",
                              "8点是早上 8 点还是晚上 8 点？")
        graph = _FakeGraph([])
        service = self._service_with(graph)
        events = list(service.chat_stream("demo", "晚上"))
        self.assertEqual(graph.calls, 0)
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertIn("20:00", done["reply"])

    def test_graph_error_becomes_error_event(self):
        service = self._service_with(
            _FakeGraph(exc=RuntimeError("模型挂了")))
        events = list(service.chat_stream("demo", "你好"))
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "model_error")
        self.assertIn("模型挂了", events[-1]["message"])


class _FakeStreamResp:
    def __init__(self, chunks, ctype="text/event-stream"):
        self._chunks = list(chunks)
        self.status = 200
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        if n is None or n < 0:
            out = b"".join(self._chunks)
            self._chunks = []
            return out
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHandler:
    def __init__(self, path="/api/chat/stream"):
        self.path = path
        self.command = "POST"
        self.headers = {"Content-Length": "0",
                        "Content-Type": "application/json"}
        self.rfile = BytesIO(b"")
        self.stream_started = None
        self.stream_chunks = []
        self.stream_ended = False
        self.sent = None

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.sent = (code, body, ctype)

    def _send_error(self, err):
        self.sent = (err.status, err.body(), "application/json")

    def _send_stream_start(self, code, ctype):
        self.stream_started = (code, ctype)

    def _send_stream_chunk(self, data):
        self.stream_chunks.append(data)

    def _send_stream_end(self):
        self.stream_ended = True


class TestGatewayProxyStreaming(unittest.TestCase):
    def test_sse_is_forwarded_chunk_by_chunk(self):
        from gateway.edge import proxy as proxy_mod

        chunks = [b"data: {\"type\": \"status\"}\n\n",
                  "data: {\"type\": \"token\", \"text\": \"你\"}\n\n"
                  .encode("utf-8"),
                  b"data: {\"type\": \"done\"}\n\n"]
        resp = _FakeStreamResp(chunks)
        handler = _FakeHandler()
        with mock.patch.object(
                proxy_mod.urllib.request, "urlopen",
                return_value=resp), \
            mock.patch.object(proxy_mod, "ensure_tenant_container"), \
            mock.patch.object(proxy_mod, "agent_url",
                              return_value="http://x:8081"), \
            mock.patch.object(proxy_mod, "token_from", return_value="t"), \
            mock.patch.object(proxy_mod, "touch"):
            proxy_mod.proxy_request(handler, "demo")
        self.assertEqual(handler.stream_started,
                         (200, "text/event-stream"))
        self.assertEqual(handler.stream_chunks, chunks)
        self.assertTrue(handler.stream_ended)
        self.assertIsNone(handler.sent)

    def test_non_sse_still_buffered(self):
        from gateway.edge import proxy as proxy_mod

        resp = _FakeStreamResp([b'{"reply": "hi"}'],
                               ctype="application/json")
        handler = _FakeHandler(path="/api/chat")
        with mock.patch.object(
                proxy_mod.urllib.request, "urlopen",
                return_value=resp), \
            mock.patch.object(proxy_mod, "ensure_tenant_container"), \
            mock.patch.object(proxy_mod, "agent_url",
                              return_value="http://x:8081"), \
            mock.patch.object(proxy_mod, "token_from", return_value="t"), \
            mock.patch.object(proxy_mod, "touch"):
            proxy_mod.proxy_request(handler, "demo")
        self.assertIsNone(handler.stream_started)
        self.assertEqual(handler.sent[0], 200)
        self.assertEqual(handler.sent[1], b'{"reply": "hi"}')


if __name__ == "__main__":
    unittest.main()
