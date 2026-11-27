"""提醒去重回归（integration，P3 bug 2）。

规则：设提醒时同租户、同内容、同时间（±5 分钟内）已有一条
pending 提醒，就直接返回已有的那条，不新建。
覆盖：同一条重复设、±5 分钟内算同一条、10 分钟外是两条、
不同内容不算重复、不同租户不算重复、确定性抽取路径也去重。
"""
import unittest

from tests.helpers import fresh_tenant_data, reminder_rows
from agent.memory import extract_explicit
from agent.tools import make_tools


def _tools(tenant):
    return {t.name: t for t in make_tools(tenant)}


class _DedupCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)


class TestReminderDedup(_DedupCase):
    """P3 bug 2：重复提醒去重（同租户、同内容、同时间 ±5 分钟）。"""

    def test_same_reminder_twice_returns_existing(self):
        tools = _tools("demo")
        out1 = tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        self.assertIn("已设好提醒", out1)
        out2 = tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        self.assertIn("已经有一条相同", out2)
        self.assertEqual(len(reminder_rows("demo")), 1)

    def test_within_5_minutes_counts_as_duplicate(self):
        tools = _tools("demo")
        tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        out = tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点23分"})
        self.assertIn("已经有一条相同", out)
        self.assertEqual(len(reminder_rows("demo")), 1)

    def test_10_minutes_apart_is_two_reminders(self):
        tools = _tools("demo")
        tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        out = tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点30分"})
        self.assertIn("已设好提醒", out)
        self.assertEqual(len(reminder_rows("demo")), 2)

    def test_different_text_same_time_is_not_duplicate(self):
        tools = _tools("demo")
        tools["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        tools["add_reminder"].invoke(
            {"text": "开会", "time_text": "晚上7点20分"})
        self.assertEqual(len(reminder_rows("demo")), 2)

    def test_different_tenant_not_duplicate(self):
        _tools("tenant-a")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        _tools("tenant-b")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        self.assertEqual(len(reminder_rows("tenant-a")), 1)
        self.assertEqual(len(reminder_rows("tenant-b")), 1)

    def test_extract_explicit_path_also_dedups(self):
        # 确定性抽取路径（save_memory 调的 extract_explicit）也走
        # 同一份 store 去重：同一句话抽两次只落一条。
        first = extract_explicit("每天早上8点提醒我喝水", "demo")
        second = extract_explicit("每天早上8点提醒我喝水", "demo")
        self.assertEqual(first["reminders"], 1)
        self.assertEqual(second["reminders"], 0)
        self.assertEqual(len(reminder_rows("demo")), 1)
