"""计量 + Turn 记录 + 控制面用量汇总（integration，P3）。

- 计量：每次模型调用记一笔（租户、模型、时间、估算 token，
  成本先留 0.0，best-effort 不承诺计费级精度），存在 metering 表，
  按租户汇总可查。
- Turn：每轮聊完记一条（会话 id、工作目录、审批/沙箱策略、模型、
  本轮 token、开始/结束时间），存在 turns 表。
- 用量汇总：control/tenants.py 的 usage_summary 接上真实数据
  （注入 metering_reader，或 metering_dir 只读汇总）；没配计量
  来源时仍返回 P2 的空统计（见 test_gateway.py 那项不断言变的测试）。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import fresh_tenant_data
import model.client as client_mod
from agent.graph import _system_prompt, chat
from agent.tenant import tenant_db
from app.chat_service import ChatService
from gateway.control.tenants import TenantControl
from langchain_core.messages import AIMessage
from session.session import SessionManager
from store.sqlite_store import SQLiteStore


class _FakeLLM:
    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content="好的。")


class _MeteringCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        self._llm = mock.patch.object(client_mod, "ChatDeepSeek", _FakeLLM)
        self._llm.start()

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestMetering(_MeteringCase):
    """计量：记一笔、按租户汇总、成本留 0.0。"""

    def test_record_and_summarize(self):
        with tenant_db("demo") as conn:
            store = SQLiteStore(conn)
            store.record_metering("demo", "gpt-4o-mini",
                                  "你好", "好的")
            store.record_metering("demo", "gpt-4o-mini",
                                  "再问一句", "好的好的")
            summary = store.metering_summary("demo")
        self.assertEqual(summary["model_calls"], 2)
        self.assertGreater(summary["tokens_total"], 0)
        self.assertEqual(summary["metering"], "available")
        # 成本先留 0.0：没有计价表，不编成本数字。
        self.assertEqual(summary["cost_estimate"], 0.0)

    def test_summarize_is_per_tenant(self):
        with tenant_db("demo") as conn:
            SQLiteStore(conn).record_metering("demo", "m", "a", "b")
        with tenant_db("tenant-a") as conn:
            SQLiteStore(conn).record_metering("tenant-a", "m", "a", "b")
            SQLiteStore(conn).record_metering("tenant-a", "m", "a", "b")
        with tenant_db("demo") as conn:
            demo_calls = SQLiteStore(conn).metering_summary("demo")[
                "model_calls"]
        with tenant_db("tenant-a") as conn:
            a_calls = SQLiteStore(conn).metering_summary("tenant-a")[
                "model_calls"]
        self.assertEqual(demo_calls, 1)
        self.assertEqual(a_calls, 2)

    def test_chat_records_metering(self):
        chat("demo", "hi")
        with tenant_db("demo") as conn:
            summary = SQLiteStore(conn).metering_summary("demo")
        self.assertGreaterEqual(summary["model_calls"], 1)
        self.assertGreater(summary["tokens_total"], 0)

    def test_empty_tenant_summarizes_zero(self):
        with tenant_db("demo") as conn:
            summary = SQLiteStore(conn).metering_summary("demo")
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["tokens_total"], 0)


class TestTurns(_MeteringCase):
    """Turn 记录：每轮一条，字段齐全。"""

    def test_chat_records_one_turn(self):
        service = ChatService(session_manager=SessionManager())
        service.chat("demo", "hi")
        with tenant_db("demo") as conn:
            turns = SQLiteStore(conn).list_turns("demo")
        self.assertEqual(len(turns), 1)
        (tid, session_id, working_dir, approval, sandbox, model,
         tokens, started, ended) = turns[0]
        self.assertEqual(session_id, "default")
        self.assertIn("demo", working_dir)
        self.assertIn("deny>ask>allow", approval)
        self.assertTrue(model)
        self.assertGreater(tokens, 0)
        self.assertLessEqual(started, ended)

    def test_turn_records_explicit_session(self):
        service = ChatService(session_manager=SessionManager())
        session = service.sessions.create_session("demo")
        service.chat("demo", "hi", session["id"])
        with tenant_db("demo") as conn:
            turns = SQLiteStore(conn).list_turns("demo")
        self.assertEqual(turns[0][1], session["id"])


class TestUsageSummaryWiring(unittest.TestCase):
    """控制面用量汇总接上真实数据。"""

    def test_usage_summary_with_reader_returns_real_data(self):
        control = TenantControl(
            users={"demo": "demo123"},
            metering_reader=lambda tenant: {
                "tenant": tenant, "model_calls": 7,
                "tokens_input": 100, "tokens_output": 50,
                "tokens_total": 150, "cost_estimate": 0.0,
                "metering": "available"})
        summary = control.usage_summary("demo")
        self.assertEqual(summary["model_calls"], 7)
        self.assertEqual(summary["tokens_total"], 150)
        self.assertEqual(summary["metering"], "available")
        self.assertIn("best-effort", summary["note"])

    def test_usage_summary_with_metering_dir_reads_db(self):
        with tempfile.TemporaryDirectory(prefix="mt-metering-") as d:
            data_dir = Path(d)
            # 在临时数据目录里造一个带计量行的租户库。
            import agent.config as config_mod
            import agent.tenant as tenant_mod
            with (mock.patch.object(tenant_mod, "DATA_DIR", data_dir),
                  mock.patch.object(config_mod, "DATA_DIR", data_dir)):
                with tenant_db("demo") as conn:
                    SQLiteStore(conn).record_metering(
                        "demo", "gpt-4o-mini", "hello", "hi")
            control = TenantControl(users={"demo": "demo123"},
                                    metering_dir=data_dir)
            summary = control.usage_summary("demo")
            self.assertEqual(summary["model_calls"], 1)
            self.assertEqual(summary["metering"], "available")

    def test_usage_summary_without_source_stays_empty(self):
        control = TenantControl(users={"demo": "demo123"})
        summary = control.usage_summary("demo")
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["metering"], "not_available")


class TestGuardrailNoteInPrompt(unittest.TestCase):
    """注入标注进系统提示：命中时追加标注，不命中不追加。"""

    def test_note_appended_to_system_prompt(self):
        from security.guardrails import guardrail_note
        state = {"due_notifications": [], "summary": "", "facts": "",
                 "history_text": "", "todos": "", "reminders": "",
                 "guardrail_note": guardrail_note(
                     ["ignore_previous_instructions"])}
        prompt = _system_prompt(state)
        self.assertIn("【安全标注】", prompt)
        self.assertIn("ignore_previous_instructions", prompt)

    def test_no_note_without_hit(self):
        state = {"due_notifications": [], "summary": "", "facts": "",
                 "history_text": "", "todos": "", "reminders": "",
                 "guardrail_note": ""}
        self.assertNotIn("【安全标注】", _system_prompt(state))
