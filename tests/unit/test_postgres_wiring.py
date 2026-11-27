"""Postgres 接线回归测试（2026-11-27）。

原来直接写 SQLite 方言的 10 处 SQL 已全部改成走仓储接口：
session/store.py 5 处、app/reminder_service.py 1 处、
tools/definitions/memory.py 1 处（model/metering.py 的 3 处本就
由 SQLiteStore 委托、PostgresStore 自有方言实现，不算旁路）。
本文件测三件事：
1. 新仓储方法在真 SQLite 上的行为（会话 CRUD、通知作废、facts 直写）；
2. 新方法在 PostgresStore 上只发 %s 方言 SQL（fake 连接录 SQL）；
3. session/store.py 转调仓储、tenant_db() 按 STORE_BACKEND 切后端。
"""
import os
import sqlite3
import unittest
from unittest import mock

from tests.helpers import fresh_tenant_data
from tests.integration.test_postgres_store import _FakeConn, _FakeCursor
from agent.tenant import tenant_db
from session import store as session_store
from store import backend_name, create_store
from store.postgres_store import PostgresStore
from store.sqlite_store import SQLiteStore, ensure_schema


class _SqliteCase(unittest.TestCase):
    def setUp(self):
        self._ctx = fresh_tenant_data()
        self._ctx.__enter__()
        self.conn = tenant_db("demo")
        self.store = SQLiteStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self._ctx.__exit__(None, None, None)


class TestRememberFact(_SqliteCase):
    def test_upsert_overwrites_same_key(self):
        self.store.remember_fact("城市", "上海")
        self.store.remember_fact("城市", "北京")
        self.assertEqual(self.store.recall_fact("城市"), "北京")

    def test_remember_fact_once_still_dedups_by_value(self):
        self.assertTrue(self.store.remember_fact_once("a", "x"))
        self.assertFalse(self.store.remember_fact_once("b", "x"))


class TestSessionsSqlite(_SqliteCase):
    def test_create_get_list_update_archive(self):
        created = self.store.create_session("s1", "demo", "摘要一")
        self.assertEqual(created["id"], "s1")
        self.assertEqual(created["status"], "active")
        again = self.store.create_session("s1", "demo", "不该覆盖")
        self.assertEqual(again["summary"], "摘要一")
        self.store.create_session("s2", "demo")
        self.assertEqual([s["id"] for s in self.store.list_sessions("demo")],
                         ["s1", "s2"])
        self.store.update_session_summary("s1", "新摘要")
        self.assertEqual(self.store.get_session("s1")["summary"], "新摘要")
        self.assertTrue(self.store.archive_session("s1"))
        self.assertFalse(self.store.archive_session("s1"))
        self.assertEqual(self.store.get_session("s1")["status"], "archived")
        self.assertIsNone(self.store.get_session("不存在"))


class TestInvalidateNotifications(_SqliteCase):
    def test_invalidate_only_undelivered_with_same_text(self):
        self.store.add_notification_if_absent(
            "demo", "喝水", "2026-11-27T21:20:00+08:00")
        self.store.add_notification_if_absent(
            "demo", "开会", "2026-11-27T21:20:00+08:00")
        self.assertEqual(len(self.store.list_undelivered_notifications()), 2)
        self.store.invalidate_notifications("demo", "喝水")
        left = self.store.list_undelivered_notifications()
        self.assertEqual([r[1] for r in left], ["开会"])


class TestSessionStoreDelegates(_SqliteCase):
    def test_functions_take_store_not_conn(self):
        created = session_store.create_session(self.store, "s9", "demo")
        self.assertEqual(created["id"], "s9")
        self.assertEqual(
            session_store.get_session(self.store, "s9")["tenant"], "demo")
        session_store.update_summary(self.store, "s9", "摘要")
        self.assertEqual(
            session_store.get_session(self.store, "s9")["summary"], "摘要")
        self.assertTrue(session_store.archive_session(self.store, "s9"))


class TestTenantDbBackendSwitch(unittest.TestCase):
    def test_sqlite_default_still_sqlite_conn(self):
        with fresh_tenant_data():
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STORE_BACKEND", None)
                self.assertEqual(backend_name(), "sqlite")
                conn = tenant_db("demo")
                self.assertIsInstance(conn, sqlite3.Connection)
                conn.close()

    def test_postgres_without_dsn_raises_clearly(self):
        with fresh_tenant_data():
            with mock.patch.dict(os.environ, {"STORE_BACKEND": "postgres"}):
                os.environ.pop("POSTGRES_DSN", None)
                with self.assertRaises(RuntimeError) as cm:
                    tenant_db("demo")
                # 本地没装 psycopg2 时先报缺包，装了则报缺 DSN——两种
                # 都是明确的错，不静默连错地方。
                self.assertTrue(
                    "psycopg2" in str(cm.exception)
                    or "POSTGRES_DSN" in str(cm.exception))


class TestPostgresDialect(unittest.TestCase):
    """新方法在 PostgresStore 上只发 %s SQL（fake 连接录下来断言）。"""

    def _store(self, cursor=None):
        return PostgresStore(_FakeConn(cursor or _FakeCursor()))

    def test_session_sql_is_postgres_dialect(self):
        cur = _FakeCursor(fetchone=("s1", "demo", "active", "", "t", "t"))
        store = self._store(cur)
        store.create_session("s1", "demo")
        store.get_session("s1")
        store.list_sessions("demo")
        store.update_session_summary("s1", "摘要")
        store.archive_session("s1")
        joined = "\n".join(sql for sql, _ in cur.executed)
        self.assertNotIn("?", joined)
        self.assertIn("ON CONFLICT (id) DO NOTHING", joined)
        self.assertIn("WHERE id = %s", joined)
        self.assertIn("WHERE tenant = %s", joined)

    def test_archive_returns_rowcount(self):
        cur = _FakeCursor()
        cur.rowcount = 0
        self.assertFalse(self._store(cur).archive_session("s1"))
        cur.rowcount = 1
        self.assertTrue(self._store(cur).archive_session("s1"))

    def test_remember_fact_and_invalidate_are_postgres_dialect(self):
        cur = _FakeCursor()
        store = self._store(cur)
        store.remember_fact("城市", "北京")
        store.invalidate_notifications("demo", "喝水")
        joined = "\n".join(sql for sql, _ in cur.executed)
        self.assertNotIn("?", joined)
        self.assertIn("ON CONFLICT (key) DO UPDATE", joined)
        self.assertIn("WHERE tenant = %s AND text = %s", joined)


if __name__ == "__main__":
    unittest.main()
