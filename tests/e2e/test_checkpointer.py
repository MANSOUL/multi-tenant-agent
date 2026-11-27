"""P4 checkpointer（e2e，走 ChatService 整条图，假模型替掉 ChatOpenAI）。

验收：同一会话连续两轮对话，第二轮能引用第一轮的工具结果。
假模型（_CheckpointLLM）只认本文件两个场景：第一轮调 remember_fact
记“彩虹密码 = 1234”，第二轮问“彩虹密码是多少？”时，它只在收到的
消息里找到上一轮的 ToolMessage（工具结果）才答得出 1234——系统提示
里的历史摘要不算：它不读系统提示，只认消息列表里的 ToolMessage。
所以第二轮答出 1234，就证明图内多轮消息真的靠 checkpointer
带过来了，而不是靠拼进提示词的历史。

隔离对照：换一个会话 id 问同样的问题，假模型收不到上一轮的
ToolMessage，必须答“不知道”——thread_id 按会话隔离。
"""
import sqlite3
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.helpers import fresh_tenant_data
import model.client as client_mod
from agent.tenant import checkpoint_db_path
from app.chat_service import ChatService
from session.session import SessionManager


class _CheckpointLLM:
    """按剧本回应的假模型：只认本文件两个场景。"""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        last_human = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                last_human = m.content if isinstance(m.content, str) else ""
        if "彩虹密码是多少" in last_human:
            # 只认消息列表里的上一轮工具结果，不读系统提示里的历史。
            for tm in tool_msgs:
                if "1234" in (tm.content or ""):
                    return AIMessage(
                        content="彩虹密码是 1234（上一轮工具刚记下的）")
            return AIMessage(content="我不知道彩虹密码。")
        if tool_msgs:
            # 工具执行完：回显工具结果，不编新内容。
            return AIMessage(content=f"好的，{tool_msgs[-1].content}")
        if "记住彩虹密码是 1234" in last_human:
            return AIMessage(content="", tool_calls=[{
                "name": "remember_fact",
                "args": {"key": "彩虹密码", "value": "1234"},
                "id": "call-1", "type": "tool_call"}])
        return AIMessage(content="好的。")


class _CheckpointCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._data_dir = self._ctx.__enter__()
        self._llm = mock.patch.object(client_mod, "ChatOpenAI",
                                      _CheckpointLLM)
        self._llm.start()
        self.service = ChatService(session_manager=SessionManager())

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestCheckpointerMultiTurn(_CheckpointCase):
    """同一会话连续两轮：第二轮引用第一轮的工具结果。"""

    def test_second_turn_references_first_turn_tool_result(self):
        session = self.service.sessions.create_session("demo")
        sid = session["id"]
        first = self.service.chat(
            "demo", "请记住彩虹密码是 1234", sid)
        self.assertIn("记住了", first["reply"])
        second = self.service.chat(
            "demo", "彩虹密码是多少？", sid)
        self.assertIn("1234", second["reply"])
        self.assertNotIn("不知道", second["reply"])
        # 第二轮的事件里没有第一轮的工具调用（since_index 切片生效）。
        self.assertEqual(second["events"], [])
        # checkpointer 真落盘了：checkpoint.db 和 langgraph 的表都在。
        ck_path = checkpoint_db_path("demo")
        self.assertTrue(ck_path.is_file())
        conn = sqlite3.connect(ck_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertIn("checkpoints", tables)

    def test_different_session_does_not_see_tool_result(self):
        session = self.service.sessions.create_session("demo")
        sid = session["id"]
        first = self.service.chat(
            "demo", "请记住彩虹密码是 1234", sid)
        self.assertIn("记住了", first["reply"])
        other = self.service.sessions.create_session("demo")
        reply = self.service.chat(
            "demo", "彩虹密码是多少？", other["id"])
        # 新会话的 thread 里没有上一轮的 ToolMessage，必须答不知道。
        self.assertIn("不知道", reply["reply"])
        self.assertNotIn("1234", reply["reply"])


if __name__ == "__main__":
    unittest.main()
