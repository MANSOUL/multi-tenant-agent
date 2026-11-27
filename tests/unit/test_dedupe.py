"""去重：同一段历史只发一遍（消息列表里有了，系统提示里就不拼）。

验收：
- build_system_prompt(include_history=False) 不拼“最近的对话”，
  但摘要/事实/待办/提醒照常在；
- messages_cover_history：空列表 / 只有本轮一条消息 → False，
  带往轮消息 → True；
- e2e：同一会话第二轮，系统提示里没有“最近的对话”（历史在消息
  列表里），但事实还在（彩虹密码进系统提示），回答仍能引用上一轮
  的工具结果；换一个新会话问同样的问题，系统提示里有“最近的对话”
  （跨会话历史不丢）。
"""
import unittest
from unittest import mock

from langchain_core.messages import (
    AIMessage, HumanMessage, SystemMessage, ToolMessage)

from context.assembler import (
    build_system_prompt, messages_cover_history)
from tests.helpers import fresh_tenant_data
import model.client as client_mod
from app.chat_service import ChatService
from session.session import SessionManager

SEEN_SYSTEM_PROMPTS = []


class _RecordingLLM:
    """按剧本回应的假模型，同时记下每次收到的系统提示。"""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        system = next((m.content for m in messages
                       if isinstance(m, SystemMessage)), "")
        SEEN_SYSTEM_PROMPTS.append(system)
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        last_human = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                last_human = m.content if isinstance(m.content, str) else ""
        if "彩虹密码是多少" in last_human:
            for tm in tool_msgs:
                if "1234" in (tm.content or ""):
                    return AIMessage(content="彩虹密码是 1234")
            return AIMessage(content="我不知道彩虹密码。")
        if tool_msgs:
            return AIMessage(content=f"好的，{tool_msgs[-1].content}")
        if "记住彩虹密码是 1234" in last_human:
            return AIMessage(content="", tool_calls=[{
                "name": "remember_fact",
                "args": {"key": "彩虹密码", "value": "1234"},
                "id": "call-1", "type": "tool_call"}])
        return AIMessage(content="好的。")


class TestBuildSystemPrompt(unittest.TestCase):
    def test_without_history_drops_only_history(self):
        ctx = {"summary": "早先聊过天气", "facts": "彩虹密码：1234",
               "history_text": "用户：你好\n助手：你好呀",
               "todos": "买牛奶", "reminders": "8点喝水"}
        prompt = build_system_prompt(ctx, include_history=False)
        self.assertNotIn("最近的对话", prompt)
        self.assertNotIn("你好呀", prompt)
        self.assertIn("早先聊过天气", prompt)
        self.assertIn("彩虹密码：1234", prompt)
        self.assertIn("买牛奶", prompt)
        self.assertIn("8点喝水", prompt)

    def test_with_history_by_default(self):
        ctx = {"history_text": "用户：你好\n助手：你好呀"}
        self.assertIn("最近的对话",
                      build_system_prompt(ctx))
        self.assertIn("最近的对话",
                      build_system_prompt(ctx, include_history=True))

    def test_messages_cover_history(self):
        self.assertFalse(messages_cover_history([]))
        self.assertFalse(messages_cover_history(None))
        self.assertFalse(messages_cover_history(
            [HumanMessage(content="你好")]))
        self.assertTrue(messages_cover_history(
            [HumanMessage(content="你好"),
             AIMessage(content="你好呀")]))
        self.assertTrue(messages_cover_history(
            [HumanMessage(content="记住"), AIMessage(content=""),
             ToolMessage(content="记住了", tool_call_id="call-1")]))


class _DedupeCase(unittest.TestCase):
    def setUp(self):
        SEEN_SYSTEM_PROMPTS.clear()
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        self._llm = mock.patch.object(client_mod, "ChatDeepSeek",
                                      _RecordingLLM)
        self._llm.start()
        self.service = ChatService(session_manager=SessionManager())

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestDedupeEndToEnd(_DedupeCase):
    def test_second_turn_system_prompt_has_no_history_text(self):
        session = self.service.sessions.create_session("demo")
        sid = session["id"]
        first = self.service.chat("demo", "请记住彩虹密码是 1234", sid)
        self.assertIn("记住了", first["reply"])
        second = self.service.chat("demo", "彩虹密码是多少？", sid)
        self.assertIn("1234", second["reply"])
        # 第二轮的系统提示（最后一次 invoke 收到的）：没有历史文本，
        # 但事实还在——记忆没丢，只是没发两遍。
        system2 = SEEN_SYSTEM_PROMPTS[-1]
        self.assertNotIn("最近的对话", system2)
        self.assertNotIn("请记住彩虹密码是 1234", system2)
        self.assertIn("彩虹密码", system2)

    def test_new_session_still_gets_history_text(self):
        session = self.service.sessions.create_session("demo")
        sid = session["id"]
        self.service.chat("demo", "请记住彩虹密码是 1234", sid)
        other = self.service.sessions.create_session("demo")
        before = len(SEEN_SYSTEM_PROMPTS)
        self.service.chat("demo", "彩虹密码是多少？", other["id"])
        system_other = SEEN_SYSTEM_PROMPTS[before]
        # 新会话的消息列表里没有往轮，历史仍走系统提示。
        self.assertIn("最近的对话", system_other)
        self.assertIn("请记住彩虹密码是 1234", system_other)


if __name__ == "__main__":
    unittest.main()
