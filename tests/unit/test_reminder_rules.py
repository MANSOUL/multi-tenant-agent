"""顺延/核对规则 + 提示词语义（unit）。

用例来源：VERIFY.md 修复一节“顺延/核对规则 5 项 + 例子/记忆范围 3 项”
（其中“系统提示含核对规则与顺延规则”拆成 2 个断言，“空租户 load_context
为空”和“只列三张表内容”合在 1 个测试里），外加 TOOLS_DOC 含
update_reminder（VERIFY.md 修复改动里明确写了 TOOLS_DOC 增加
update_reminder）。
"""
import unittest
from datetime import datetime

from tests.helpers import fresh_tenant_data
from agent.config import SYSTEM_PROMPT, TOOLS_DOC
from agent.memory import due_day_label, extract_explicit, load_context, parse_reminder_time

NOW = datetime(2026, 11, 26, 18, 0)


class TestDeferralRules(unittest.TestCase):
    """顺延/核对规则（VERIFY.md 修复一节：顺延/核对规则 5 项，拆成 6 个断言）。"""

    def test_evening_8_at_1800_is_today(self):
        r = parse_reminder_time("晚上8点", now=NOW)
        self.assertEqual(r["due_at"], "2026-11-26T20:00:00+08:00")

    def test_evening_8_at_2100_is_tomorrow(self):
        r = parse_reminder_time("晚上8点", now=datetime(2026, 11, 26, 21, 0))
        self.assertEqual(r["due_at"], "2026-11-27T20:00:00+08:00")

    def test_due_day_label_today(self):
        self.assertEqual(
            due_day_label("2026-11-26T20:00:00+08:00", now=NOW), "今天")

    def test_due_day_label_tomorrow(self):
        self.assertEqual(
            due_day_label("2026-11-27T20:00:00+08:00", now=NOW), "明天")

    def test_system_prompt_has_verify_rule(self):
        # 核对规则：改完把工具返回的时间跟用户要的时间核对一遍。
        self.assertIn("核对一遍", SYSTEM_PROMPT)
        self.assertIn("绝不许编", SYSTEM_PROMPT)

    def test_system_prompt_has_deferral_rule(self):
        # 顺延规则：只有时间已过才顺延到明天，并在回显里说清楚。
        self.assertIn("顺延", SYSTEM_PROMPT)
        self.assertIn("已经过去了", SYSTEM_PROMPT)


class TestPromptExamples(unittest.TestCase):
    """例子/记忆范围（VERIFY.md 修复一节：例子/记忆范围 3 项）。"""

    def test_no_drinking_water_in_system_prompt(self):
        # 例子里的“喝水”曾被模型当成用户说过的话；提示词里不再出现这二字。
        self.assertNotIn("喝水", SYSTEM_PROMPT)

    def test_tools_doc_marks_examples(self):
        self.assertIn("只是格式示例", TOOLS_DOC)

    def test_tools_doc_lists_update_reminder(self):
        self.assertIn("update_reminder", TOOLS_DOC)

    def test_load_context_only_lists_three_tables(self):
        with fresh_tenant_data():
            # 空租户：四项全空。
            ctx = load_context("nobody")
            self.assertEqual(
                (ctx["facts"], ctx["summary"], ctx["history_text"],
                 ctx["todos"], ctx["reminders"], ctx["due_notifications"]),
                ("", "", "", "", "", []))
            # 有内容的租户：只列三张表里的内容，不多不少。
            extract_explicit("记住，我在上海", "demo", now=NOW)
            extract_explicit("待办事项：买牛奶", "demo", now=NOW)
            extract_explicit("晚上8点提醒我喝水", "demo", now=NOW)
            ctx = load_context("demo")
            self.assertIn("上海", ctx["facts"])
            self.assertIn("买牛奶", ctx["todos"])
            self.assertIn("喝水", ctx["reminders"])
            self.assertNotIn("不存在的内容", ctx["facts"] + ctx["todos"]
                             + ctx["reminders"])
