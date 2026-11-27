"""会话层（integration）：创建/恢复/fork/归档 + 图按会话复用。

会话元数据存在租户库的 sessions 表（session/store.py）；
图复用是进程内缓存（session/session.py 的 SessionManager），
同一会话连续请求复用同一张图实例，一把锁保护。
"""
import unittest
from unittest import mock

from tests.helpers import fresh_tenant_data
import model.client as client_mod
from agent.tenant import tenant_db
from app.chat_service import ChatService
from infra.graph import build_graph
from langchain_core.messages import AIMessage
from session.session import SessionManager
from store import create_store
from session.store import update_summary


class _FakeLLM:
    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content="好的。")


class _SessionCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        self._llm = mock.patch.object(client_mod, "ChatOpenAI", _FakeLLM)
        self._llm.start()
        self.manager = SessionManager()

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestSessionLifecycle(_SessionCase):
    """会话创建/恢复/fork/归档。"""

    def test_default_session_auto_created(self):
        session = self.manager.ensure_session("demo")
        self.assertEqual(session["id"], "default")
        self.assertEqual(session["status"], "active")
        again = self.manager.ensure_session("demo")
        self.assertEqual(again["id"], "default")

    def test_explicit_session_restored(self):
        created = self.manager.create_session("demo")
        restored = self.manager.ensure_session("demo", created["id"])
        self.assertEqual(restored["id"], created["id"])

    def test_fork_copies_summary_not_full_history(self):
        src = self.manager.create_session("demo")
        with tenant_db("demo") as conn:
            update_summary(create_store(conn), src["id"],
                           "摘要：用户喜欢喝水")
        forked = self.manager.fork_session("demo", src["id"])
        self.assertNotEqual(forked["id"], src["id"])
        self.assertEqual(forked["summary"], "摘要：用户喜欢喝水")
        self.assertEqual(forked["status"], "active")
        # 事实是租户级的：fork 后新会话引用同一份 facts，不复制全文。
        with tenant_db("demo") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
                ("喜好", "喝水", "2026-11-27T00:00:00+00:00"))
        restored = self.manager.ensure_session("demo", forked["id"])
        self.assertEqual(restored["summary"], "摘要：用户喜欢喝水")

    def test_fork_unknown_session_raises(self):
        with self.assertRaises(KeyError):
            self.manager.fork_session("demo", "no-such-session")

    def test_archive_makes_session_read_only(self):
        session = self.manager.create_session("demo")
        self.assertTrue(
            self.manager.archive_session("demo", session["id"]))
        self.assertTrue(
            self.manager.is_archived("demo", session["id"]))
        service = ChatService(session_manager=self.manager)
        with self.assertRaises(RuntimeError):
            service.chat("demo", "hi", session["id"])

    def test_chat_without_session_id_uses_default(self):
        service = ChatService(session_manager=self.manager)
        result = service.chat("demo", "hi")
        self.assertEqual(result["session_id"], "default")
        self.assertEqual(result["reply"], "好的。")

    def test_chat_with_session_id_returns_it(self):
        session = self.manager.create_session("demo")
        service = ChatService(session_manager=self.manager)
        result = service.chat("demo", "hi", session["id"])
        self.assertEqual(result["session_id"], session["id"])


class TestGraphReuse(_SessionCase):
    """图按会话复用：同一会话同一张图，不同会话不同图。"""

    def test_same_session_reuses_graph(self):
        g1 = self.manager.get_graph("demo", "default", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        g2 = self.manager.get_graph("demo", "default", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        self.assertIs(g1, g2)

    def test_different_sessions_get_different_graphs(self):
        g1 = self.manager.get_graph("demo", "default", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        g2 = self.manager.get_graph("demo", "sess-other", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        self.assertIsNot(g1, g2)

    def test_drop_graph_rebuilds(self):
        g1 = self.manager.get_graph("demo", "default", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        self.manager.drop_graph("demo", "default")
        g2 = self.manager.get_graph("demo", "default", build_graph,
                                    salt=id(client_mod.ChatOpenAI))
        self.assertIsNot(g1, g2)
