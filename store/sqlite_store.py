"""SQLite 仓储：实现 domain/ports.py 里的 ReminderStore / MemoryStore。

全部 SQL 从 agent/memory.py、agent/tenant.py、agent/tools.py 原样搬过来
（P0 搬移，表结构、字段名、SQL 文本一字不改），只挪位置。
新增的两张表（chat_archived、compaction_events）是 P0 事件日志机制的，
见 store/event_log.py 的说明。

包名说明：ARCHITECTURE.md 写的是 data/，但本仓库根目录的 data/ 是
gitignored 的运行时目录（浏览器卷挂载点），且容器里 /app/data 是租户
数据卷的挂载点——代码放进 data/ 既进不了 git，又会在容器里被数据卷
遮住。所以实现放在 store/，接口（domain/ports.py）不变。
"""
import sqlite3

from . import event_log

# 建表 SQL：从 agent/tenant.py 的 tenant_db() 原样搬过来，一字不改。
SCHEMA_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS facts"
    "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS chat_log"
    "(ts TEXT, role TEXT, content TEXT)",
    "CREATE TABLE IF NOT EXISTS summaries"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, summary TEXT,"
    " up_to_ts TEXT)",
    # 待办与提醒（2026-11-26 提醒/记忆改进）：每租户独立的表，和 facts 一样
    # 只属于本租户。reminders.status：pending（未到点）/ due（已到点，等送达）/
    # notified（已当面提醒过，不再出现）。歧义时间（裸“8点”没说上下午）
    # 不落盘，due_at 为空的行只可能是历史遗留，调度器会跳过它。
    "CREATE TABLE IF NOT EXISTS todos"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, status TEXT,"
    " created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS reminders"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, due_at TEXT,"
    " repeat TEXT, status TEXT, created_at TEXT)",
    # 调度器到点后写的待送达提醒：delivered=0 表示还没在聊天里当面告诉用户。
    "CREATE TABLE IF NOT EXISTS pending_notifications"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, text TEXT,"
    " due_at TEXT, created_at TEXT, delivered INTEGER DEFAULT 0)",
    # P0 事件日志新增：归档标记 + 压缩事件（见 store/event_log.py）。
    # chat_log 行本身不删除，归档只是在这里记 rowid。
    "CREATE TABLE IF NOT EXISTS chat_archived"
    "(rowid INTEGER PRIMARY KEY, archived_at TEXT)",
    "CREATE TABLE IF NOT EXISTS compaction_events"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, archived_count INTEGER,"
    " up_to_ts TEXT)",
]


def ensure_schema(conn) -> None:
    """建全部表（不存在才建）。"""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)


def connect(db_path):
    """打开一个租户库并建表，返回连接（调用方负责关闭/提交）。"""
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    return conn


class SQLiteStore:
    """SQLite 实现的仓储：包一个连接，所有方法都是原 SQL 的直译。

    约定和原来一样：调用方用 `with tenant_db(tenant) as conn:` 打开连接，
    with 退出时提交；本类不自己提交、不自己关闭。
    """

    def __init__(self, conn):
        self.conn = conn

    # -- facts ---------------------------------------------------------
    def remember_fact_once(self, key: str, value: str) -> bool:
        """facts 去重写：同一 value 已存在就不重复写。返回是否真的写了。"""
        from datetime import datetime, timezone

        value = (value or "").strip()
        if not value:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM facts WHERE value = ?", (value,)).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
            (key, value, datetime.now(timezone.utc).isoformat()))
        return True

    def get_facts(self):
        return self.conn.execute(
            "SELECT key, value FROM facts").fetchall()

    def search_facts(self, keyword: str):
        kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace(
            "_", "\\_")
        return self.conn.execute(
            "SELECT key, value FROM facts WHERE key LIKE ? ESCAPE '\\'"
            " OR value LIKE ? ESCAPE '\\' LIMIT 20",
            (f"%{kw}%", f"%{kw}%")).fetchall()

    def recall_fact(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # -- chat_log / summaries（事件日志）-------------------------------
    def append_chat(self, ts: str, role: str, content: str) -> None:
        event_log.append_chat(self.conn, ts, role, content)

    def list_active_chat_rows(self):
        return event_log.list_active_chat_rows(self.conn)

    def list_all_chat_rows(self):
        return event_log.list_all_chat_rows(self.conn)

    def count_all_chat_rows(self) -> int:
        return event_log.count_all_chat_rows(self.conn)

    def latest_summary(self) -> str:
        row = self.conn.execute(
            "SELECT summary FROM summaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def add_summary(self, ts: str, summary: str, up_to_ts: str) -> None:
        self.conn.execute(
            "INSERT INTO summaries (ts, summary, up_to_ts)"
            " VALUES (?, ?, ?)", (ts, summary, up_to_ts))

    def archive_turns(self, rowids, up_to_ts: str) -> int:
        return event_log.archive_turns(self.conn, rowids, up_to_ts)

    def compaction_event_count(self) -> int:
        return event_log.compaction_event_count(self.conn)

    # -- todos ---------------------------------------------------------
    def add_todo(self, text: str) -> bool:
        from datetime import datetime, timezone

        text = (text or "").strip(" ，,。；;、")
        if not text:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM todos WHERE text = ? AND status = 'pending'",
            (text,)).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT INTO todos (text, status, created_at)"
            " VALUES (?, 'pending', ?)",
            (text, datetime.now(timezone.utc).isoformat()))
        return True

    def list_pending_todos(self):
        return self.conn.execute(
            "SELECT id, text FROM todos WHERE status = 'pending'"
            " ORDER BY id ASC").fetchall()

    def list_all_todos(self):
        return self.conn.execute(
            "SELECT id, text, status FROM todos ORDER BY"
            " CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id ASC"
        ).fetchall()

    def complete_todo(self, todo_id: int):
        row = self.conn.execute(
            "SELECT text FROM todos WHERE id = ? AND status = 'pending'",
            (todo_id,)).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE todos SET status = 'done' WHERE id = ?", (todo_id,))
        return row[0]

    # -- reminders -----------------------------------------------------
    def add_reminder(self, text: str, parsed: dict) -> bool:
        from datetime import datetime, timezone

        text = (text or "").strip(" ，,。；;、")
        if not text:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM reminders WHERE text = ? AND status = 'pending'"
            " AND due_at IS ?",
            (text, parsed.get("due_at"))).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT INTO reminders (text, due_at, repeat, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (text, parsed.get("due_at"), parsed.get("repeat", "once"),
             datetime.now(timezone.utc).isoformat()))
        return True

    def list_pending_reminders(self):
        return self.conn.execute(
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()

    def list_all_reminders(self):
        return self.conn.execute(
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " ORDER BY id ASC").fetchall()

    def update_reminder(self, rid: int, due_at: str, repeat: str) -> None:
        self.conn.execute(
            "UPDATE reminders SET due_at = ?, repeat = ?,"
            " status = 'pending' WHERE id = ?",
            (due_at, repeat, rid))

    def find_reminder_for_update(self, rid: int, text: str):
        row = None
        if rid:
            row = self.conn.execute(
                "SELECT id, text FROM reminders WHERE id = ?"
                " AND status IN ('pending', 'due')",
                (rid,)).fetchone()
        if row is None and (text or "").strip():
            row = self.conn.execute(
                "SELECT id, text FROM reminders WHERE text = ?"
                " AND status IN ('pending', 'due')"
                " ORDER BY id DESC LIMIT 1",
                (text.strip(),)).fetchone()
        return row

    def list_due_candidates(self):
        return self.conn.execute(
            "SELECT id, text, due_at, repeat FROM reminders"
            " WHERE status = 'pending' AND due_at IS NOT NULL"
        ).fetchall()

    def mark_reminder_due(self, rid: int) -> None:
        self.conn.execute(
            "UPDATE reminders SET status = 'due' WHERE id = ?", (rid,))

    def mark_reminder_notified(self, text: str, due_at: str) -> None:
        self.conn.execute(
            "UPDATE reminders SET status = 'notified'"
            " WHERE text = ? AND due_at = ? AND status = 'due'",
            (text, due_at))

    # -- pending_notifications -----------------------------------------
    def add_notification_if_absent(
            self, tenant: str, text: str, due_at: str) -> bool:
        from datetime import datetime, timezone

        exists = self.conn.execute(
            "SELECT 1 FROM pending_notifications WHERE text = ?"
            " AND due_at = ? AND delivered = 0",
            (text, due_at)).fetchone()
        if exists:
            return False
        self.conn.execute(
            "INSERT INTO pending_notifications"
            " (tenant, text, due_at, created_at, delivered)"
            " VALUES (?, ?, ?, ?, 0)",
            (tenant, text, due_at,
             datetime.now(timezone.utc).isoformat()))
        return True

    def list_undelivered_notifications(self):
        return self.conn.execute(
            "SELECT id, text, due_at FROM pending_notifications"
            " WHERE delivered = 0 ORDER BY id ASC").fetchall()

    def mark_notifications_delivered(self, tenant: str, ids):
        ids = [i for i in (ids or []) if isinstance(i, int)]
        if not ids:
            return []
        rows = self.conn.execute(
            f"SELECT text, due_at FROM pending_notifications WHERE"
            f" tenant = ? AND id IN ({','.join('?' * len(ids))})",
            [tenant, *ids]).fetchall()
        self.conn.execute(
            f"UPDATE pending_notifications SET delivered = 1 WHERE"
            f" tenant = ? AND id IN ({','.join('?' * len(ids))})",
            [tenant, *ids])
        return rows
