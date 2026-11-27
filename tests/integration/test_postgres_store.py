"""P4 存储换 Postgres（integration，fake 连接，不用真 Postgres）。

覆盖：建表语句（11 张表，含 metering / turns / sessions，
SERIAL 自增、没有 AUTOINCREMENT）、全部 SQL 用 %s 占位符（一个
? 都没有）、参数原样传进 cursor、Postgres 特有写法（ON CONFLICT、
RETURNING id、due_at 为空时 IS NULL 而不是 IS %s）、STORE_BACKEND
切换（sqlite 默认、postgres 可选、配错直接报错）、connect() 在
没装 psycopg2 / 没配 POSTGRES_DSN 时报清楚的错。
"""
import os
import sqlite3
import unittest
from unittest import mock

from store import backend_name, create_store
from store.postgres_store import (
    SCHEMA_STATEMENTS,
    PostgresStore,
    connect,
    ensure_schema,
)


class _FakeCursor:
    """录下每条 SQL + 参数，按剧本返回 fetchone/fetchall。"""

    def __init__(self, fetchone=None, fetchall=None):
        self.executed = []  # [(sql, params)]
        self._fetchone = fetchone
        self._fetchone_queue = None
        self._fetchall = fetchall if fetchall is not None else []
        self.rowcount = 1

    def with_fetchone_queue(self, queue):
        self._fetchone_queue = list(queue)
        return self

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return self

    def fetchone(self):
        if self._fetchone_queue is not None:
            return self._fetchone_queue.pop(0)
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class _FakeConn:
    """cursor() 永远返回同一个 fake cursor（PostgresStore 只调它）。"""

    def __init__(self, cursor=None):
        self.cursor_obj = cursor or _FakeCursor()

    def cursor(self):
        return self.cursor_obj


def _sqls(cursor):
    return [sql for sql, _ in cursor.executed]


def _assert_no_question_mark(testcase, cursor):
    for sql in _sqls(cursor):
        testcase.assertNotIn(
            "?", sql, f"Postgres SQL 里不许出现 ? 占位符：{sql}")


class TestPostgresSchema(unittest.TestCase):
    """建表：11 张表全有，SERIAL 自增，没有 SQLite 写法。"""

    def test_all_tables_created(self):
        conn = _FakeConn()
        ensure_schema(conn)
        sqls = _sqls(conn.cursor_obj)
        self.assertEqual(len(sqls), len(SCHEMA_STATEMENTS))
        joined = "\n".join(sqls)
        for table in ("facts", "chat_log", "summaries", "todos",
                      "reminders", "pending_notifications", "chat_archived",
                      "compaction_events", "metering", "turns", "sessions"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", joined)
        self.assertNotIn("AUTOINCREMENT", joined)
        _assert_no_question_mark(self, conn.cursor_obj)

    def test_chat_log_uses_serial_id_not_rowid(self):
        conn = _FakeConn()
        ensure_schema(conn)
        chat_log = [s for s in _sqls(conn.cursor_obj)
                    if "chat_log" in s][0]
        self.assertIn("id SERIAL PRIMARY KEY", chat_log)

    def test_schema_scoped_to_tenant(self):
        conn = _FakeConn()
        ensure_schema(conn, schema="demo")
        sqls = _sqls(conn.cursor_obj)
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "demo"', sqls[0])
        self.assertIn('SET search_path TO "demo"', sqls[1])
        self.assertEqual(len(sqls), len(SCHEMA_STATEMENTS) + 2)

    def test_invalid_schema_name_rejected(self):
        conn = _FakeConn()
        with self.assertRaises(ValueError):
            ensure_schema(conn, schema="bad/name")
        with self.assertRaises(ValueError):
            ensure_schema(conn, schema='demo"; DROP TABLE facts; --')
        self.assertEqual(conn.cursor_obj.executed, [])


class TestPostgresFactsAndChat(unittest.TestCase):
    """facts / chat_log：占位符、ON CONFLICT、归档只标记。"""

    def test_remember_fact_once_inserts_with_on_conflict(self):
        cur = _FakeCursor(fetchone=None)
        store = PostgresStore(_FakeConn(cur))
        self.assertTrue(store.remember_fact_once("城市", "上海"))
        self.assertEqual(len(cur.executed), 2)
        select_sql, select_params = cur.executed[0]
        self.assertIn("SELECT 1 FROM facts WHERE value = %s", select_sql)
        self.assertEqual(select_params, ("上海",))
        insert_sql, insert_params = cur.executed[1]
        self.assertIn("INSERT INTO facts", insert_sql)
        self.assertIn("ON CONFLICT (key) DO UPDATE", insert_sql)
        self.assertEqual(insert_params[:2], ("城市", "上海"))
        _assert_no_question_mark(self, cur)

    def test_remember_fact_once_dedups_by_value(self):
        cur = _FakeCursor(fetchone=(1,))
        store = PostgresStore(_FakeConn(cur))
        self.assertFalse(store.remember_fact_once("城市", "上海"))
        self.assertEqual(len(cur.executed), 1)  # 查到已存在就不写

    def test_search_facts_escapes_like_wildcards(self):
        cur = _FakeCursor(fetchall=[])
        store = PostgresStore(_FakeConn(cur))
        store.search_facts("a%b_c\\d")
        sql, params = cur.executed[0]
        self.assertIn("LIKE %s ESCAPE", sql)
        self.assertEqual(params, ("%a\\%b\\_c\\\\d%", "%a\\%b\\_c\\\\d%"))
        _assert_no_question_mark(self, cur)

    def test_archive_turns_marks_and_logs_event(self):
        cur = _FakeCursor()
        store = PostgresStore(_FakeConn(cur))
        self.assertEqual(store.archive_turns([3, 4], "2026-11-27T00:00:00"),
                         2)
        sqls = _sqls(cur)
        self.assertEqual(len(sqls), 3)
        self.assertIn("INSERT INTO chat_archived", sqls[0])
        self.assertIn("ON CONFLICT (rowid) DO NOTHING", sqls[0])
        self.assertIn("INSERT INTO compaction_events", sqls[2])
        _assert_no_question_mark(self, cur)

    def test_archive_turns_empty_does_nothing(self):
        cur = _FakeCursor()
        store = PostgresStore(_FakeConn(cur))
        self.assertEqual(store.archive_turns([], "2026-11-27T00:00:00"), 0)
        self.assertEqual(cur.executed, [])

    def test_list_active_chat_rows_excludes_archived(self):
        cur = _FakeCursor(fetchall=[])
        store = PostgresStore(_FakeConn(cur))
        store.list_active_chat_rows()
        sql, _ = cur.executed[0]
        self.assertIn("NOT IN (SELECT rowid FROM chat_archived)", sql)
        self.assertIn("ORDER BY id ASC", sql)


class TestPostgresReminders(unittest.TestCase):
    """reminders：due_at 空/非空两条 SQL、±5 分钟去重、IN 列表。"""

    def test_add_reminder_none_due_at_uses_is_null(self):
        cur = _FakeCursor().with_fetchone_queue([None, None])
        store = PostgresStore(_FakeConn(cur))
        # find_pending_reminder_near 不匹配（due_at 为空直接返回 None，
        # 不查库）→ 精确去重查一次 → INSERT。
        self.assertTrue(store.add_reminder(
            "喝水", {"due_at": None, "repeat": "once"}))
        sqls = _sqls(cur)
        self.assertEqual(len(sqls), 2)
        self.assertIn("due_at IS NULL", sqls[0])
        self.assertNotIn("due_at = %s", sqls[0])
        self.assertIn("INSERT INTO reminders", sqls[1])
        self.assertEqual(sqls[1] and cur.executed[1][1][:2],
                         ("喝水", None))
        _assert_no_question_mark(self, cur)

    def test_add_reminder_with_due_at_uses_equals(self):
        cur = _FakeCursor().with_fetchone_queue([None, None])
        store = PostgresStore(_FakeConn(cur))
        self.assertTrue(store.add_reminder(
            "喝水", {"due_at": "2026-11-27T19:20:00", "repeat": "once"}))
        sqls = _sqls(cur)
        # find_pending_reminder_near 先查一次（±5 分钟），再精确去重。
        self.assertIn("due_at IS NOT NULL", sqls[0])
        self.assertIn("due_at = %s", sqls[1])
        self.assertEqual(cur.executed[1][1],
                         ("喝水", "2026-11-27T19:20:00"))
        _assert_no_question_mark(self, cur)

    def test_find_pending_reminder_near_matches_within_5_minutes(self):
        rows = [(7, "喝水", "2026-11-27T19:22:00", "once", "pending")]
        cur = _FakeCursor(fetchall=rows)
        store = PostgresStore(_FakeConn(cur))
        found = store.find_pending_reminder_near(
            "喝水", "2026-11-27T19:20:00")
        self.assertEqual(found, rows[0])

    def test_mark_notifications_delivered_builds_in_list(self):
        cur = _FakeCursor(fetchall=[("喝水", "2026-11-27T19:20:00")])
        store = PostgresStore(_FakeConn(cur))
        rows = store.mark_notifications_delivered("demo", [3, 5])
        self.assertEqual(rows, [("喝水", "2026-11-27T19:20:00")])
        select_sql, select_params = cur.executed[0]
        self.assertIn("id IN (%s,%s)", select_sql)
        self.assertEqual(list(select_params), ["demo", 3, 5])
        update_sql, update_params = cur.executed[1]
        self.assertIn("id IN (%s,%s)", update_sql)
        self.assertEqual(list(update_params), ["demo", 3, 5])
        _assert_no_question_mark(self, cur)

    def test_mark_notifications_delivered_empty_ids_no_sql(self):
        cur = _FakeCursor()
        store = PostgresStore(_FakeConn(cur))
        self.assertEqual(store.mark_notifications_delivered("demo", []), [])
        self.assertEqual(cur.executed, [])

    def test_update_reminder_resets_to_pending(self):
        cur = _FakeCursor()
        store = PostgresStore(_FakeConn(cur))
        store.update_reminder(9, "2026-11-27T19:40:00", "daily")
        sql, params = cur.executed[0]
        self.assertIn("status = 'pending'", sql)
        self.assertEqual(params, ("2026-11-27T19:40:00", "daily", 9))
        _assert_no_question_mark(self, cur)


class TestPostgresMeteringTurns(unittest.TestCase):
    """metering / turns：RETURNING id、汇总口径和 SQLite 版一致。"""

    def test_record_metering_returns_id(self):
        cur = _FakeCursor(fetchone=(42,))
        store = PostgresStore(_FakeConn(cur))
        rid = store.record_metering("demo", "deepseek-chat",
                                    "abcd", "efgh")
        self.assertEqual(rid, 42)
        sql, params = cur.executed[0]
        self.assertIn("INSERT INTO metering", sql)
        self.assertIn("RETURNING id", sql)
        # token 粗估：输入/输出各 1 token，总计 2（和 SQLite 版同口径）。
        self.assertEqual(params[3:6], (1, 1, 2))
        _assert_no_question_mark(self, cur)

    def test_metering_summary_aggregates(self):
        cur = _FakeCursor(fetchone=(3, 10, 20, 30, 0.0))
        store = PostgresStore(_FakeConn(cur))
        summary = store.metering_summary("demo")
        self.assertEqual(summary, {
            "tenant": "demo", "model_calls": 3, "tokens_input": 10,
            "tokens_output": 20, "tokens_total": 30,
            "cost_estimate": 0.0, "metering": "available"})
        sql, params = cur.executed[0]
        self.assertIn("WHERE tenant = %s", sql)
        self.assertEqual(params, ("demo",))

    def test_record_turn_returns_id_and_list_turns_limits(self):
        cur = _FakeCursor(fetchone=(7,))
        store = PostgresStore(_FakeConn(cur))
        self.assertEqual(store.record_turn("sess-1", "demo"), 7)
        sql, _ = cur.executed[0]
        self.assertIn("INSERT INTO turns", sql)
        self.assertIn("RETURNING id", sql)
        cur2 = _FakeCursor(fetchall=[])
        store2 = PostgresStore(_FakeConn(cur2))
        store2.list_turns("demo", limit=5)
        sql, params = cur2.executed[0]
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params, ("demo", 5))
        _assert_no_question_mark(self, cur)
        _assert_no_question_mark(self, cur2)


class TestStoreBackendSwitch(unittest.TestCase):
    """STORE_BACKEND 切换：sqlite 默认，postgres 可选，配错就报错。"""

    def test_default_backend_is_sqlite(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STORE_BACKEND", None)
            self.assertEqual(backend_name(), "sqlite")

    def test_create_store_sqlite(self):
        with mock.patch.dict(os.environ, {"STORE_BACKEND": "sqlite"}):
            conn = sqlite3.connect(":memory:")
            try:
                from store.sqlite_store import SQLiteStore

                self.assertIsInstance(create_store(conn), SQLiteStore)
            finally:
                conn.close()

    def test_create_store_postgres(self):
        with mock.patch.dict(os.environ, {"STORE_BACKEND": "postgres"}):
            self.assertIsInstance(create_store(_FakeConn()), PostgresStore)

    def test_create_store_postgres_with_sqlite_conn_rejected(self):
        with mock.patch.dict(os.environ, {"STORE_BACKEND": "postgres"}):
            conn = sqlite3.connect(":memory:")
            try:
                with self.assertRaises(ValueError) as cm:
                    create_store(conn)
                self.assertIn("SQLite 连接", str(cm.exception))
            finally:
                conn.close()

    def test_create_store_unknown_backend_rejected(self):
        with mock.patch.dict(os.environ, {"STORE_BACKEND": "mysql"}):
            with self.assertRaises(ValueError):
                create_store(_FakeConn())

    def test_connect_without_psycopg2_raises_clear_error(self):
        # 本地 mtvenv 没装 psycopg2：connect() 必须报清楚的错，不报
        # ImportError 原样，更不该静默返回一个假连接。
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTGRES_DSN", None)
            with self.assertRaises(RuntimeError) as cm:
                connect()
            self.assertIn("psycopg2", str(cm.exception))


class TestNoQuestionMarkSweep(unittest.TestCase):
    """全方法扫一遍：PostgresStore 发出的每条 SQL 都不含 ?。"""

    def test_all_methods_use_percent_s_placeholders(self):
        # fetchone 给一个通用行 (5,)，让 count / recall / complete_todo
        # 这类“取 [0]”的方法都能走下去；断言只关心 SQL 文本。
        cur = _FakeCursor(fetchone=(5,), fetchall=[])
        store = PostgresStore(_FakeConn(cur))
        store.get_facts()
        store.recall_fact("k")
        store.append_chat("ts", "user", "hi")
        store.list_all_chat_rows()
        store.count_all_chat_rows()
        store.latest_summary()
        store.add_summary("ts", "s", "ts")
        store.compaction_event_count()
        store.list_pending_todos()
        store.list_all_todos()
        store.list_pending_reminders()
        store.list_all_reminders()
        store.list_due_candidates()
        store.mark_reminder_due(1)
        store.mark_reminder_notified("t", "d")
        store.add_notification_if_absent("demo", "t", "d")
        store.list_undelivered_notifications()
        store.complete_todo(1)
        store.find_reminder_for_update(1, "t")
        _assert_no_question_mark(self, cur)
        self.assertTrue(len(cur.executed) >= 15)


if __name__ == "__main__":
    unittest.main()
