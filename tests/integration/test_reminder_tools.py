"""提醒/待办工具 + 调度 + Bug 4 只出现一次（integration，真工具 + 临时库）。

用例来源：VERIFY.md 修复一节（工具 8 项 + Bug 4 8 项 + 顺延/核对里
update_reminder 作废旧通知、list_reminders 中文状态、add_reminder 回显含
今天/明天 3 项）+ 旧套件（工具与隔离 7 项 + 调度与送达 6 项 + 调度器线程 1 项）。
工具层调用 parse_reminder_time 用的是真实当前时间，所以凡是涉及时
间的断言，都以同一份 parse_reminder_time 在断言时刻算出的期望为准，
不硬编码日期（VERIFY.md 修复一节也注明测试曾因此修过断言）。
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import fresh_tenant_data, reminder_rows
from agent.graph import _system_prompt
from agent.memory import (
    check_due_reminders,
    load_context,
    mark_notifications_delivered,
    parse_reminder_time,
)
from agent.tenant import tenant_db
from agent.tools import make_tools

TZ8 = timezone(timedelta(hours=8))
DUE_1900 = "2026-11-26T19:00:00+08:00"
DUE_NOW = datetime(2026, 11, 26, 19, 5, tzinfo=TZ8)  # 19:00 刚过 5 分钟


def _tools(tenant):
    return {t.name: t for t in make_tools(tenant)}


def _insert_reminder(tenant, text, due_at, repeat="once"):
    with tenant_db(tenant) as conn:
        conn.execute(
            "INSERT INTO reminders (text, due_at, repeat, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (text, due_at, repeat, "2026-11-26T10:00:00+00:00"))


def _notifications(tenant):
    with tenant_db(tenant) as conn:
        return conn.execute(
            "SELECT id, text, due_at, delivered FROM pending_notifications"
            " WHERE tenant = ? ORDER BY id", (tenant,)).fetchall()


class _ToolsCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)


class TestAddUpdateReminder(_ToolsCase):
    """修复一节：工具 8 项 + 3 项修复改动里写明的附加行为。"""

    def test_add_reminder_evening_720(self):
        out = _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        self.assertIn("19:20", out)
        rows = reminder_rows("demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "喝水")
        expect = parse_reminder_time("晚上7点20分")
        self.assertEqual(rows[0][2], expect["due_at"])

    def test_add_reminder_bare_8_asks_and_not_saved(self):
        out = _tools("demo")["add_reminder"].invoke(
            {"text": "开会", "time_text": "8点"})
        self.assertIn("早上还是晚上", out)
        self.assertEqual(reminder_rows("demo"), [])

    def test_list_reminders_has_no_ambiguous_row(self):
        _tools("demo")["add_reminder"].invoke(
            {"text": "开会", "time_text": "8点"})
        out = _tools("demo")["list_reminders"].invoke({})
        self.assertEqual(out, "提醒还是空的")

    def test_update_reminder_by_text(self):
        _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点"})
        out = _tools("demo")["update_reminder"].invoke(
            {"text": "喝水", "new_time_text": "晚上7点20分"})
        self.assertIn("19:20", out)
        rows = [r for r in reminder_rows("demo") if r[1] == "喝水"]
        self.assertEqual(len(rows), 1)  # 是改，不是新设一条
        expect = parse_reminder_time("晚上7点20分")
        self.assertEqual(rows[0][2], expect["due_at"])

    def test_update_reminder_by_id(self):
        _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点"})
        rid = reminder_rows("demo")[0][0]
        out = _tools("demo")["update_reminder"].invoke(
            {"reminder_id": rid, "new_time_text": "晚上8点"})
        self.assertIn("20:00", out)
        expect = parse_reminder_time("晚上8点")
        self.assertEqual(reminder_rows("demo")[0][2], expect["due_at"])

    def test_update_reminder_ambiguous_keeps_row(self):
        _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点"})
        before = reminder_rows("demo")[0][2]
        out = _tools("demo")["update_reminder"].invoke(
            {"text": "喝水", "new_time_text": "8点"})
        self.assertIn("早上还是晚上", out)
        self.assertEqual(reminder_rows("demo")[0][2], before)

    def test_update_reminder_missing_is_not_error(self):
        out = _tools("demo")["update_reminder"].invoke(
            {"text": "不存在的提醒xyz", "new_time_text": "晚上8点"})
        self.assertIn("没有找到", out)

    def test_tool_count_is_16(self):
        self.assertEqual(len(make_tools("demo")), 16)

    def test_update_invalidates_old_notification(self):
        # Bug 2 修复改动：改时间后，旧时间产生的未送达通知作废。
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        self.assertEqual(len(_notifications("demo")), 1)
        self.assertEqual(_notifications("demo")[0][3], 0)
        _tools("demo")["update_reminder"].invoke(
            {"text": "喝水", "new_time_text": "晚上9点"})
        self.assertEqual(_notifications("demo")[0][3], 1)

    def test_list_reminders_chinese_status(self):
        # 修复改动：list_reminders 状态显示中文（待提醒/已到点/已提醒）。
        _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上8点"})
        _insert_reminder("demo", "开会", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        out = _tools("demo")["list_reminders"].invoke({})
        self.assertIn("待提醒", out)
        self.assertIn("已提醒", out)

    def test_add_reminder_reply_has_day_label(self):
        # 修复改动：add_reminder 返回含今天/明天（Bug 3 的回显依据）。
        from agent.memory import due_day_label
        out = _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上7点20分"})
        expect = parse_reminder_time("晚上7点20分")
        self.assertIn(due_day_label(expect["due_at"]), out)


class TestTodoToolsLegacy(_ToolsCase):
    """旧套件：工具与隔离 7 项。"""

    def test_add_todo_returns_content(self):
        out = _tools("demo")["add_todo"].invoke({"text": "买牛奶"})
        self.assertIn("买牛奶", out)

    def test_list_todos(self):
        _tools("demo")["add_todo"].invoke({"text": "买牛奶"})
        self.assertIn("买牛奶", _tools("demo")["list_todos"].invoke({}))

    def test_todos_isolated_between_tenants(self):
        _tools("tenant-a")["add_todo"].invoke({"text": "买牛奶"})
        self.assertEqual(_tools("tenant-b")["list_todos"].invoke({}),
                         "待办清单还是空的")

    def test_complete_todo_success(self):
        _tools("demo")["add_todo"].invoke({"text": "买牛奶"})
        with tenant_db("demo") as conn:
            tid = conn.execute(
                "SELECT id FROM todos WHERE text = '买牛奶'").fetchone()[0]
        out = _tools("demo")["complete_todo"].invoke({"todo_id": tid})
        self.assertIn("已完成", out)
        self.assertIn("已完成", _tools("demo")["list_todos"].invoke({}))

    def test_complete_todo_missing_no_error(self):
        out = _tools("demo")["complete_todo"].invoke({"todo_id": 9999})
        self.assertIn("没有找到", out)

    def test_add_reminder_morning_8(self):
        out = _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "早上8点"})
        self.assertIn("08:00", out)

    def test_list_reminders_lists_rows(self):
        _tools("demo")["add_reminder"].invoke(
            {"text": "喝水", "time_text": "晚上8点"})
        _tools("demo")["add_reminder"].invoke(
            {"text": "开会", "time_text": "早上9点"})
        out = _tools("demo")["list_reminders"].invoke({})
        self.assertIn("喝水", out)
        self.assertIn("开会", out)


class TestSchedulingDelivery(_ToolsCase):
    """旧套件：调度与送达 6 项。"""

    def test_due_reminder_flips_to_due(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        self.assertEqual(check_due_reminders("demo", now=DUE_NOW), ["喝水"])
        self.assertEqual(reminder_rows("demo")[0][4], "due")

    def test_due_writes_pending_notification(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        notes = _notifications("demo")
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0][1], notes[0][2], notes[0][3]),
                         ("喝水", DUE_1900, 0))

    def test_due_appears_first_in_system_prompt(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        ctx = load_context("demo")
        self.assertEqual(len(ctx["due_notifications"]), 1)
        state = {"due_notifications": ctx["due_notifications"],
                 "summary": "摘要内容", "facts": "", "history_text": "",
                 "todos": "", "reminders": ""}
        prompt = _system_prompt(state)
        self.assertLess(prompt.index("【待提醒】"), prompt.index("摘要内容"))
        self.assertIn("喝水", prompt)

    def test_marked_delivered_gone_next_round(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        self.assertEqual(load_context("demo")["due_notifications"], [])

    def test_repeat_scheduling_no_duplicate_notification(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)
        check_due_reminders("demo", now=DUE_NOW)
        self.assertEqual(len(_notifications("demo")), 1)

    def test_daily_spawns_next_day(self):
        _insert_reminder("demo", "喝水", DUE_1900, repeat="daily")
        check_due_reminders("demo", now=DUE_NOW)
        rows = reminder_rows("demo")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2], "2026-11-27T19:00:00+08:00")
        self.assertEqual((rows[1][3], rows[1][4]), ("daily", "pending"))


class TestBug4OnlyOnce(_ToolsCase):
    """Bug 4：到点提醒只出现一次（VERIFY.md 修复一节：Bug 4 8 项）。

    每条都是同一场景的不同检查点：插一条 19:00 到点的提醒，19:05 调度。
    """

    def _setup_due(self):
        _insert_reminder("demo", "喝水", DUE_1900)
        check_due_reminders("demo", now=DUE_NOW)

    def test_1_due_flips(self):
        self._setup_due()
        self.assertEqual(reminder_rows("demo")[0][4], "due")

    def test_2_first_round_shows_notification(self):
        self._setup_due()
        notes = load_context("demo")["due_notifications"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "喝水")

    def test_3_first_round_reminder_list_excludes_due(self):
        self._setup_due()
        # 已到点的行只走【待提醒】，不再列在提醒列表里。
        self.assertEqual(load_context("demo")["reminders"], "")

    def test_4_mark_delivered_flips_row(self):
        self._setup_due()
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        self.assertEqual(reminder_rows("demo")[0][4], "notified")

    def test_5_second_round_no_notification(self):
        self._setup_due()
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        self.assertEqual(load_context("demo")["due_notifications"], [])

    def test_6_second_round_reminder_list_empty(self):
        self._setup_due()
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        self.assertEqual(load_context("demo")["reminders"], "")

    def test_7_list_reminders_still_shows_notified(self):
        self._setup_due()
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        out = _tools("demo")["list_reminders"].invoke({})
        self.assertIn("已提醒", out)
        self.assertIn("喝水", out)

    def test_8_repeat_check_no_new_notification_third_round_empty(self):
        self._setup_due()
        nid = load_context("demo")["due_notifications"][0]["id"]
        mark_notifications_delivered("demo", [nid])
        check_due_reminders("demo", now=DUE_NOW)
        self.assertEqual(len(_notifications("demo")), 1)
        self.assertEqual(load_context("demo")["due_notifications"], [])


class TestSchedulerThread(unittest.TestCase):
    """旧套件：调度器线程 1 项。

    说明：start_reminder_scheduler 起的是停不掉的守护线程，所以本测试
    把它要扫的租户固定成一个别的测试都不会用的 "sched-tenant"
    （patch agent.server.TENANT_ID，测试结束也不还原——还原后线程会去扫
    demo，别的测试的临时目录里万一有到点提醒会被它翻掉），并且用包默认
    的临时 DATA_DIR，不用 fresh_tenant_data（线程在测试返回后还在跑，
    临时目录删掉后它再扫会把目录重建出来）。
    """

    def test_scheduler_thread_picks_up_due(self):
        import agent.server as server_mod
        from agent.server import start_reminder_scheduler
        mock.patch.object(server_mod, "TENANT_ID", "sched-tenant").start()
        due_at = (datetime.now(TZ8) - timedelta(minutes=5)).isoformat()
        _insert_reminder("sched-tenant", "喝水", due_at)
        start_reminder_scheduler(interval_seconds=1)
        deadline = datetime.now() + timedelta(seconds=10)
        found = []
        while datetime.now() < deadline:
            found = _notifications("sched-tenant")
            if found:
                break
            import time as _time
            _time.sleep(0.2)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0][1], found[0][3]), ("喝水", 0))
