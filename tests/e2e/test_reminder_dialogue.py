"""原对话逐句场景（e2e，走 chat() 整条图，假模型替掉 ChatOpenAI）。

场景来源：VERIFY.md 修复一节“仍需服务器实测”列的原对话。
假模型（ScriptedLLM）只按剧本回应：它按上一条用户消息和系统提示里的
历史决定问上下午、还是调 add_reminder / update_reminder、还是回显工具结果。
**断言边界**：只断言工具调用链和落盘结果（问没问上下午、行里的时间对
不对、是不是只改不新增），不硬断言模型原话——“设完不反问明天”“歧义只
问一次”这类措辞是模型行为，假模型测不出来，对应的规则由
tests/unit/test_reminder_rules.py 的提示词断言覆盖（核对规则、顺延规则、
只问一次），这里不写假通过的断言。
"""
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.helpers import fresh_tenant_data, reminder_rows
import model.client as client_mod
from agent.graph import chat
from agent.memory import parse_reminder_time
from agent.tools import make_tools


class ScriptedLLM:
    """按剧本回应的假模型：只认本文件 5 个场景的输入。"""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        sys_text = ""
        last_human = ""
        tool_msgs = []
        for m in messages:
            if isinstance(m, SystemMessage):
                sys_text = m.content if isinstance(m.content, str) else ""
            elif isinstance(m, HumanMessage):
                last_human = m.content if isinstance(m.content, str) else ""
            elif isinstance(m, ToolMessage):
                tool_msgs.append(m)
        if tool_msgs:
            # 工具执行完：回显工具结果，不编新内容。
            return AIMessage(content=f"好的，{tool_msgs[-1].content}")
        if "七点二十分提醒我喝水" in last_human:
            return AIMessage(content="早上七点二十分还是晚上七点二十分？")
        if last_human.strip() == "晚上":
            if "8点提醒我开会" in sys_text:
                return AIMessage(content="", tool_calls=[{
                    "name": "add_reminder",
                    "args": {"text": "开会", "time_text": "晚上8点"},
                    "id": "call-1", "type": "tool_call"}])
            if "七点二十分" in sys_text:
                return AIMessage(content="", tool_calls=[{
                    "name": "add_reminder",
                    "args": {"text": "喝水", "time_text": "晚上7点20分"},
                    "id": "call-1", "type": "tool_call"}])
            return AIMessage(content="好的。")
        if "重设成晚上七点二十分" in last_human:
            return AIMessage(content="", tool_calls=[{
                "name": "update_reminder",
                "args": {"text": "喝水", "new_time_text": "晚上七点二十分"},
                "id": "call-1", "type": "tool_call"}])
        if "晚上8点提醒我开会" in last_human:
            return AIMessage(content="", tool_calls=[{
                "name": "add_reminder",
                "args": {"text": "开会", "time_text": "晚上8点"},
                "id": "call-1", "type": "tool_call"}])
        if "8点提醒我开会" in last_human:
            return AIMessage(content="早上 8 点还是晚上 8 点？")
        return AIMessage(content="好的。")


class BuggyConfirmLLM:
    """模拟 P3 之前有 bug 的真模型行为：用户答完上下午后不调工具，
    反而再问一遍“确认一下吗”。P3 的澄清捷径（ChatService 里）
    应该在模型看到“晚上”之前就直接设好，让这句确认根本没机会出现。
    第一轮行为和 ScriptedLLM 一样（问上下午）。"""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        last_human = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                last_human = m.content if isinstance(m.content, str) else ""
        if "七点二十分提醒我喝水" in last_human:
            return AIMessage(content="早上七点二十分还是晚上七点二十分？")
        if "8点提醒我开会" in last_human:
            return AIMessage(content="早上 8 点还是晚上 8 点？")
        if last_human.strip() == "晚上":
            return AIMessage(content="确认一下：晚上提醒你，对吗？")
        return AIMessage(content="好的。")


class _BuggyDialogueCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        self._llm = mock.patch.object(client_mod, "ChatOpenAI",
                                      BuggyConfirmLLM)
        self._llm.start()

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestClarificationSetsDirectly(_BuggyDialogueCase):
    """P3 bug 1：答完上下午还要再确认一次——澄清回答后直接设，
    不许再问确认（澄清捷径，见 app/chat_service.py）。"""

    def test_evening_answer_sets_1920_without_reconfirm(self):
        chat("demo", "七点二十分提醒我喝水")
        reply = chat("demo", "晚上")
        self.assertIn("19:20", reply)
        self.assertNotIn("确认", reply)
        rows = reminder_rows("demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "喝水")
        self.assertEqual(rows[0][2], _expected_due("晚上7点20分"))

    def test_bare_8_evening_answer_sets_2000_without_reconfirm(self):
        chat("demo", "8点提醒我开会")
        reply = chat("demo", "晚上")
        self.assertIn("20:00", reply)
        self.assertNotIn("确认", reply)
        rows = [r for r in reminder_rows("demo") if r[1] == "开会"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], _expected_due("晚上8点"))


def _expected_due(text):
    """以同一份解析在断言时刻算期望（工具层用真实当前时间，不硬编码日期）。"""
    return parse_reminder_time(text)["due_at"]


class _DialogueCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        # P0 后模型创建收进 model/client.py，假模型贴在新的缝上。
        self._llm = mock.patch.object(client_mod, "ChatOpenAI", ScriptedLLM)
        self._llm.start()

    def tearDown(self):
        self._llm.stop()
        self._ctx.__exit__(None, None, None)


class TestReminderDialogue(_DialogueCase):
    """原对话 5 步（VERIFY.md 修复一节“仍需服务器实测”）。"""

    def test_720_asks_am_pm(self):
        reply = chat("demo", "七点二十分提醒我喝水")
        self.assertIn("早上", reply)
        self.assertIn("晚上", reply)
        self.assertEqual(reminder_rows("demo"), [])  # 歧义不落盘

    def test_evening_answer_sets_1920(self):
        chat("demo", "七点二十分提醒我喝水")
        reply = chat("demo", "晚上")
        self.assertIn("19:20", reply)
        rows = reminder_rows("demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "喝水")
        self.assertEqual(rows[0][2], _expected_due("晚上7点20分"))

    def test_reset_to_1920_echoes(self):
        tools = {t.name: t for t in make_tools("demo")}
        tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点"})
        reply = chat("demo", "把喝水的提醒重设成晚上七点二十分")
        self.assertIn("19:20", reply)
        rows = [r for r in reminder_rows("demo") if r[1] == "喝水"]
        self.assertEqual(len(rows), 1)  # 是改，不是新设一条
        self.assertEqual(rows[0][2], _expected_due("晚上七点二十分"))

    def test_set_8pm_echoes_time(self):
        reply = chat("demo", "晚上8点提醒我开会")
        self.assertIn("20:00", reply)
        rows = [r for r in reminder_rows("demo") if r[1] == "开会"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], _expected_due("晚上8点"))

    def test_bare_8_asks_once_then_sets(self):
        reply = chat("demo", "8点提醒我开会")
        self.assertIn("早上", reply)
        self.assertEqual(reminder_rows("demo"), [])  # 问完之前不落盘
        reply = chat("demo", "晚上")
        self.assertIn("20:00", reply)
        rows = [r for r in reminder_rows("demo") if r[1] == "开会"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], _expected_due("晚上8点"))

    def test_bare_evening_without_pending_goes_to_model(self):
        # 澄清捷径是 fail-closed 的：没有待澄清的提醒请求时，
        # 裸“晚上”不设提醒、不猜，走正常模型路径。
        reply = chat("demo", "晚上")
        self.assertEqual(reply, "好的。")
        self.assertEqual(reminder_rows("demo"), [])
