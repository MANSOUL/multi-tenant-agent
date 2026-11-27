"""时间解析 + 确定性抽取（unit，零 I/O 网络，只调纯函数 + 临时库）。

用例来源：VERIFY.md“提醒功能 5 个实测 bug 修复”一节（解析 11 项 + 抽取 6 项，
新行为为准）+“提醒与记忆改进”一节（时间解析 10 项 + 确定性抽取 8 项，
其中 2 项已被新行为取代，见 tests/README.md）。
注入时间固定在 2026-11-26（周四）18:00：晚上 7/8 点还没过、早上 7/8 点已过，
所以“早上 7 点 20 分”算明天、“晚上 7 点 20 分”算今天。
"""
import unittest
from datetime import datetime

from tests.helpers import fresh_tenant_data, reminder_rows
from agent.memory import extract_explicit, parse_reminder_time
from agent.tenant import tenant_db

NOW = datetime(2026, 11, 26, 18, 0)  # 周四 18:00
TODAY = NOW.date()
TOMORROW = datetime(2026, 11, 27).date()


def _due(text, now=NOW):
    return parse_reminder_time(text, now=now)


class TestParseReminderTimeFixed(unittest.TestCase):
    """Bug 1 修复后的解析（VERIFY.md 修复一节：解析 11 项）。"""

    def test_evening_720_chinese(self):
        r = _due("晚上七点二十分")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-26T19:20:00+08:00")

    def test_morning_720_digits(self):
        r = _due("早上7点20分")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-27T07:20:00+08:00")

    def test_morning_720_colon(self):
        r = _due("早上7:20")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-27T07:20:00+08:00")

    def test_morning_720_no_fen(self):
        # _parse_hour 文档里支持的写法“7点20”（不带“分”）。
        r = _due("早上7点20")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-27T07:20:00+08:00")

    def test_1920_colon_unambiguous(self):
        r = _due("19:20")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-26T19:20:00+08:00")

    def test_morning_half(self):
        r = _due("早上八点半")
        self.assertEqual(r["due_at"], "2026-11-27T08:30:00+08:00")

    def test_morning_quarter(self):
        r = _due("早上八点一刻")
        self.assertEqual(r["due_at"], "2026-11-27T08:15:00+08:00")

    def test_afternoon_three_quarters(self):
        r = _due("下午三点三刻")
        self.assertEqual(r["due_at"], "2026-11-27T15:45:00+08:00")

    def test_morning_805(self):
        r = _due("早上8点05分")
        self.assertEqual(r["due_at"], "2026-11-27T08:05:00+08:00")

    def test_bare_720_chinese_ambiguous(self):
        r = _due("七点二十分")
        self.assertTrue(r["ambiguous"])
        self.assertIsNone(r["due_at"])

    def test_bare_720_digits_ambiguous(self):
        r = _due("7点20分")
        self.assertTrue(r["ambiguous"])
        self.assertIsNone(r["due_at"])


class TestParseReminderTimeLegacy(unittest.TestCase):
    """旧套件的时间解析 10 项：新解析器下行为没变，全部保留。"""

    def test_morning_8(self):
        self.assertEqual(_due("早上8点")["due_at"], "2026-11-27T08:00:00+08:00")

    def test_forenoon_8(self):
        self.assertEqual(_due("上午8点")["due_at"], "2026-11-27T08:00:00+08:00")

    def test_evening_8(self):
        self.assertEqual(_due("晚上8点")["due_at"], "2026-11-26T20:00:00+08:00")

    def test_afternoon_3(self):
        self.assertEqual(_due("下午3点")["due_at"], "2026-11-27T15:00:00+08:00")

    def test_bare_8_ambiguous(self):
        r = _due("8点")
        self.assertTrue(r["ambiguous"])
        self.assertIsNone(r["due_at"])

    def test_tomorrow_morning_8(self):
        self.assertEqual(_due("明天早上8点")["due_at"],
                         "2026-11-27T08:00:00+08:00")

    def test_daily_morning_8(self):
        # 每天重复：repeat=daily；当天 08:00 已过也不顺延（顺延只针对 once）。
        r = _due("每天早上8点")
        self.assertEqual(r["repeat"], "daily")
        self.assertIn("T08:00:00", r["due_at"])

    def test_morning_half_8(self):
        self.assertEqual(_due("早上8点半")["due_at"],
                         "2026-11-27T08:30:00+08:00")

    def test_20_oclock_unambiguous(self):
        r = _due("20点")
        self.assertFalse(r["ambiguous"])
        self.assertEqual(r["due_at"], "2026-11-26T20:00:00+08:00")

    def test_past_time_defers_to_tomorrow(self):
        r = _due("晚上8点", now=datetime(2026, 11, 26, 21, 0))
        self.assertEqual(r["due_at"], "2026-11-27T20:00:00+08:00")


class _ExtractCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)


class TestExtractExplicitFixed(_ExtractCase):
    """Bug 1/5 修复后的抽取（VERIFY.md 修复一节：抽取 6 项）。"""

    def test_evening_720(self):
        r = extract_explicit("晚上七点二十分提醒我喝水", "demo", now=NOW)
        self.assertEqual((r["reminders"], r["ambiguous_reminders"]), (1, []))
        rows = reminder_rows("demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "喝水")
        self.assertEqual(rows[0][2], "2026-11-26T19:20:00+08:00")

    def test_bare_720_not_saved(self):
        r = extract_explicit("七点二十分提醒我喝水", "demo", now=NOW)
        self.assertEqual(r["reminders"], 0)
        self.assertEqual(r["ambiguous_reminders"], ["喝水"])
        self.assertEqual(reminder_rows("demo"), [])

    def test_bare_8_not_saved(self):
        r = extract_explicit("8点提醒我开会", "demo", now=NOW)
        self.assertEqual(r["reminders"], 0)
        self.assertEqual(r["ambiguous_reminders"], ["开会"])
        self.assertEqual(reminder_rows("demo"), [])

    def test_morning_8(self):
        r = extract_explicit("提醒我早上8点喝水", "demo", now=NOW)
        self.assertEqual(r["reminders"], 1)
        rows = reminder_rows("demo")
        self.assertEqual(rows[0][1], "喝水")
        self.assertEqual(rows[0][2], "2026-11-27T08:00:00+08:00")

    def test_tomorrow_evening(self):
        r = extract_explicit("提醒我明天晚上7点20分开会", "demo", now=NOW)
        self.assertEqual(r["reminders"], 1)
        rows = reminder_rows("demo")
        self.assertEqual(rows[0][1], "开会")
        self.assertEqual(rows[0][2], "2026-11-27T19:20:00+08:00")

    def test_no_time_not_saved(self):
        r = extract_explicit("提醒我喝水", "demo", now=NOW)
        self.assertEqual(r, {"facts": 0, "todos": 0, "reminders": 0,
                             "ambiguous_reminders": []})
        self.assertEqual(reminder_rows("demo"), [])


class TestExtractExplicitLegacy(_ExtractCase):
    """旧套件的抽取：除被 Bug 5 取代的 1 项外全部保留。

    注意：“提醒我早上 8 点喝水”（带空格）抽取后内容是“早上 8 点喝水”
    而不是“喝水”（时间词剥离正则不认“早上”和数字之间的空格），
    所以这里只断言时间和条数，不对内容断言；这是现行行为，不是本测试的错。
    """

    def test_remember_fact(self):
        r = extract_explicit("记住，我在上海", "demo", now=NOW)
        self.assertEqual(r["facts"], 1)
        with tenant_db("demo") as conn:
            row = conn.execute(
                "SELECT value FROM facts WHERE value LIKE '%上海%'").fetchone()
        self.assertIsNotNone(row)

    def test_dont_forget_fact(self):
        r = extract_explicit("别忘了带钥匙", "demo", now=NOW)
        self.assertEqual(r["facts"], 1)

    def test_remember_dedup(self):
        extract_explicit("记住，我在上海", "demo", now=NOW)
        r = extract_explicit("记住，我在上海", "demo", now=NOW)
        self.assertEqual(r["facts"], 0)
        with tenant_db("demo") as conn:
            n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_todo_split_three(self):
        r = extract_explicit("待办事项：买牛奶、跑步、看书", "demo", now=NOW)
        self.assertEqual(r["todos"], 3)
        with tenant_db("demo") as conn:
            rows = conn.execute(
                "SELECT text FROM todos ORDER BY id").fetchall()
        self.assertEqual([t for (t,) in rows], ["买牛奶", "跑步", "看书"])

    def test_evening_8_before_reminder(self):
        r = extract_explicit("晚上 8 点提醒我喝水", "demo", now=NOW)
        self.assertEqual(r["reminders"], 1)
        rows = reminder_rows("demo")
        self.assertEqual(rows[0][1], "喝水")
        self.assertEqual(rows[0][2], "2026-11-26T20:00:00+08:00")

    def test_no_instruction_silent(self):
        self.assertEqual(
            extract_explicit("今天天气不错", "demo", now=NOW),
            {"facts": 0, "todos": 0, "reminders": 0, "ambiguous_reminders": []})
        self.assertEqual(
            extract_explicit("", "demo", now=NOW),
            {"facts": 0, "todos": 0, "reminders": 0, "ambiguous_reminders": []})
