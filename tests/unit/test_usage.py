"""P6 真实 Token 计量（usage）单测。

覆盖：
- pricing：Flash 非高峰/高峰/周末/缓存命中/未知模型不编价；
- extract_usage：完整 / 部分 / 没有 usage_metadata；
- metering.record_call：真实用量记真实数 + 成本 > 0，老表补列迁移；
- ChatService.chat / chat_stream：用量加总、usage 事件在 done 之前、
  done 带 usage + 租户累计；
- 两个聊天页都渲染用量行。
"""
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

from langchain_core.messages import AIMessage

import app.chat_service as chat_service_mod
from app.chat_service import ChatService
from model import metering as metering_mod
from model.metering import extract_usage
from model.pricing import cost_usd, is_peak
from tests.helpers import fresh_tenant_data


class TestPricing(unittest.TestCase):
    def test_flash_offpeak(self):
        # 1000 输入（全未命中）+ 500 输出，非高峰：
        # (1000*0.15 + 500*0.60)/1e6 = 0.00045
        offpeak = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)  # 周五 12:00 UTC
        self.assertFalse(is_peak(offpeak))
        self.assertAlmostEqual(
            cost_usd("deepseek-reasoner", 1000, 0, 500, offpeak), 0.00045)

    def test_flash_cached_and_peak(self):
        # 1000 输入里 800 命中缓存 + 500 输出：
        # (800*0.003 + 200*0.15 + 500*0.60)/1e6 = 0.0003324
        offpeak = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            cost_usd("deepseek-chat", 1000, 800, 500, offpeak), 0.0003324)
        # 高峰（周五 02:00 UTC）正好翻倍
        peak = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(is_peak(peak))
        self.assertAlmostEqual(
            cost_usd("deepseek-chat", 1000, 800, 500, peak), 0.0006648)

    def test_weekend_is_offpeak(self):
        saturday = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(saturday.weekday(), 5)
        self.assertFalse(is_peak(saturday))

    def test_unknown_model_costs_zero(self):
        self.assertEqual(cost_usd("gpt-4o-mini", 1000, 0, 500), 0.0)


class TestExtractUsage(unittest.TestCase):
    def test_full(self):
        msg = AIMessage(content="hi", usage_metadata={
            "input_tokens": 45, "output_tokens": 192, "total_tokens": 237,
            "input_token_details": {"cache_read": 128},
            "output_token_details": {"reasoning": 127}})
        self.assertEqual(extract_usage(msg), {
            "input_tokens": 45, "output_tokens": 192, "total_tokens": 237,
            "reasoning_tokens": 127, "cached_tokens": 128})

    def test_partial_details_missing(self):
        msg = AIMessage(content="hi", usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        usage = extract_usage(msg)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertEqual(usage["cached_tokens"], 0)

    def test_no_usage_returns_none(self):
        self.assertIsNone(extract_usage(AIMessage(content="hi")))
        self.assertIsNone(extract_usage(object()))


class TestMeteringRealUsage(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_record_real_usage(self):
        usage = {"input_tokens": 45, "output_tokens": 192,
                 "total_tokens": 237, "reasoning_tokens": 127,
                 "cached_tokens": 128}
        with mock.patch.object(chat_service_mod, "MODEL", "deepseek-reasoner"):
            row_id = metering_mod.record_call(
                self.conn, "demo", "deepseek-reasoner", usage=usage)
        self.assertEqual(row_id, 1)
        row = self.conn.execute(
            "SELECT tokens_input, tokens_output, tokens_total,"
            " tokens_reasoning, tokens_cached, cost_estimate"
            " FROM metering WHERE id = 1").fetchone()
        self.assertEqual(row[:5], (45, 192, 237, 127, 128))
        self.assertGreater(row[5], 0.0)
        summary = metering_mod.summarize(self.conn, "demo")
        self.assertEqual(summary["tokens_reasoning"], 127)
        self.assertEqual(summary["tokens_cached"], 128)
        self.assertEqual(summary["model_calls"], 1)

    def test_old_table_gets_migrated(self):
        # 老表（没有 P6 的两列）补列后照常用。
        self.conn.execute(
            "CREATE TABLE metering (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " tenant TEXT, model TEXT, ts TEXT, tokens_input INTEGER,"
            " tokens_output INTEGER, tokens_total INTEGER,"
            " cost_estimate REAL)")
        metering_mod.ensure_metering_table(self.conn)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(metering)").fetchall()}
        self.assertIn("tokens_reasoning", cols)
        self.assertIn("tokens_cached", cols)
        metering_mod.record_call(self.conn, "demo", "deepseek-chat",
                                 input_text="hello", output_text="hi")
        self.assertEqual(
            metering_mod.summarize(self.conn, "demo")["model_calls"], 1)

    def test_no_usage_falls_back_to_estimate(self):
        metering_mod.record_call(self.conn, "demo", "deepseek-chat",
                                 input_text="hello world", output_text="hi")
        summary = metering_mod.summarize(self.conn, "demo")
        self.assertGreater(summary["tokens_input"], 0)
        self.assertEqual(summary["cost_estimate"], 0.0)


class _UsageChunk:
    def __init__(self, content="", usage=None, reasoning=None):
        self.content = content
        self.usage_metadata = usage
        self.additional_kwargs = {}
        if reasoning is not None:
            self.additional_kwargs["reasoning_content"] = reasoning


class _StreamGraph:
    def __init__(self, items):
        self._items = items

    def stream(self, initial_state, config=None, stream_mode=None):
        yield from self._items


class _InvokeGraph:
    def __init__(self, result):
        self._result = result

    def invoke(self, initial_state, config=None):
        return self._result


class _StreamCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)


class TestChatUsage(_StreamCase):
    def test_stream_usage_event_before_done(self):
        usage = {"input_tokens": 45, "output_tokens": 192,
                 "total_tokens": 237,
                 "input_token_details": {"cache_read": 0},
                 "output_token_details": {"reasoning": 127}}
        items = [
            ("messages", (_UsageChunk("你好", usage=usage),
                          {"langgraph_node": "llm"})),
            ("updates", {"save_memory": {}}),
        ]
        service = ChatService(graph_builder=lambda tenant: _StreamGraph(items))
        with mock.patch.object(chat_service_mod, "MODEL", "deepseek-reasoner"):
            events = list(service.chat_stream("demo", "你好"))
        kinds = [e["type"] for e in events]
        self.assertLess(kinds.index("usage"), kinds.index("done"))
        usage_ev = next(e for e in events if e["type"] == "usage")
        self.assertEqual(usage_ev["input_tokens"], 45)
        self.assertEqual(usage_ev["reasoning_tokens"], 127)
        self.assertGreater(usage_ev["cost_usd"], 0.0)
        done = events[-1]
        self.assertEqual(done["usage"]["total_tokens"], 237)
        self.assertIn("tenant_usage", done)
        # 假图不走真 llm 节点，所以不记 metering 行；累计读到的是
        # “0 次调用”的汇总（metering="available"，不是没计量）。
        self.assertEqual(done["tenant_usage"]["metering"], "available")
        self.assertEqual(done["tenant_usage"]["model_calls"], 0)

    def test_chat_returns_usage(self):
        ai = AIMessage(content="你好", usage_metadata={
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "input_token_details": {"cache_read": 10},
            "output_token_details": {"reasoning": 20}})
        result = {"messages": [ai], "due_notifications": []}
        service = ChatService(graph_builder=lambda tenant: _InvokeGraph(result))
        with mock.patch.object(chat_service_mod, "MODEL", "deepseek-reasoner"):
            out = service.chat("demo", "你好")
        self.assertEqual(out["usage"]["input_tokens"], 100)
        self.assertEqual(out["usage"]["reasoning_tokens"], 20)
        self.assertEqual(out["usage"]["cached_tokens"], 10)
        self.assertEqual(out["tenant_usage"]["metering"], "available")


class TestPagesShowUsage(unittest.TestCase):
    def test_both_pages_render_usage(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for rel in ("agent/web/chat.html", "gateway/web/login.html"):
            html = (root / rel).read_text(encoding="utf-8")
            self.assertIn("usageText", html, rel)
            self.assertIn("showUsage", html, rel)
            self.assertIn("Token：输入", html, rel)


if __name__ == "__main__":
    unittest.main()
