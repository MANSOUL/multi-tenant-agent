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

from . import event_log, turns as turns_mod

try:
    from model import metering as metering_mod
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from model import metering as metering_mod

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
    # P3 计量：每次模型调用一笔（model/metering.py，best-effort 估算，
    # cost_estimate 先留 0.0，不承诺计费级精度）。
    "CREATE TABLE IF NOT EXISTS metering"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, model TEXT,"
    " ts TEXT, tokens_input INTEGER, tokens_output INTEGER,"
    " tokens_total INTEGER, cost_estimate REAL)",
    # P3 Turn 记录：每轮一条（store/turns.py）。
    "CREATE TABLE IF NOT EXISTS turns"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, tenant TEXT,"
    " working_dir TEXT, approval_policy TEXT, sandbox_mode TEXT,"
    " model TEXT, tokens INTEGER, started_at TEXT, ended_at TEXT)",
    # P3 会话元数据（session/store.py）。
    "CREATE TABLE IF NOT EXISTS sessions"
    "(id TEXT PRIMARY KEY, tenant TEXT, status TEXT, summary TEXT,"
    " created_at TEXT, updated_at TEXT)",
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

    def remember_fact(self, key: str, value: str) -> None:
        """facts 直写（按 key 覆盖）：给 remember_fact 工具用。

        和 remember_fact_once 的区别：这个不按 value 去重，同一 key
        反复记就反复覆盖——工具的语义就是“记住了：k = v”。
        """
        from datetime import datetime, timezone

        self.conn.execute(
            "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
            (key, value, datetime.now(timezone.utc).isoformat()))

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
    def find_pending_reminder_near(self, text: str, due_at: str | None,
                                   window_minutes: int = 5):
        """找同内容、同时间的 pending 提醒：due_at 在 ±window 分钟内的
        算同一条（P3 去重 bug 修复）。返回 (id, text, due_at, repeat,
        status)，没有返回 None。due_at 为空时不匹配（歧义提醒不落盘，
        本来就没有行）。"""
        from datetime import datetime

        text = (text or "").strip(" ，,。；;、")
        if not text or not due_at:
            return None
        try:
            target = datetime.fromisoformat(due_at)
        except ValueError:
            return None
        rows = self.conn.execute(
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " WHERE text = ? AND status = 'pending' AND due_at IS NOT NULL",
            (text,)).fetchall()
        for row in rows:
            try:
                due = datetime.fromisoformat(row[2])
            except ValueError:
                continue
            if abs((due - target).total_seconds()) <= window_minutes * 60:
                return row
        return None

    def add_reminder(self, text: str, parsed: dict) -> bool:
        from datetime import datetime, timezone

        text = (text or "").strip(" ，,。；;、")
        if not text:
            return False
        # P3 去重：同内容、同时间（±5 分钟）已有一条 pending 就不新建。
        if self.find_pending_reminder_near(text, parsed.get("due_at")):
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

    def invalidate_notifications(self, tenant: str, text: str) -> None:
        """把某条提醒之前到点产生的未送达通知作废，避免旧时间再冒出来。

        原 app/reminder_service.py 里直接拼的 UPDATE，2026-11-27 接线
        到仓储（改提醒时间时调这个）。"""
        self.conn.execute(
            "UPDATE pending_notifications SET delivered = 1"
            " WHERE tenant = ? AND text = ? AND delivered = 0",
            (tenant, text))

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

    # -- metering（P3，模型调用计量：实现在 model/metering.py，
    #    这里只委托，和 event_log 的委托方式一样）-----------------------
    def record_metering(self, tenant: str, model: str, input_text: str = "",
                        output_text: str = "", cost_estimate: float = 0.0
                        ) -> int:
        """记一次模型调用（token 按文本粗估，best-effort；成本先留 0.0）。"""
        return metering_mod.record_call(
            self.conn, tenant, model, input_text, output_text,
            cost_estimate=cost_estimate)

    def metering_summary(self, tenant: str) -> dict:
        """按租户汇总计量：调用次数、token 合计、成本估算合计。"""
        return metering_mod.summarize(self.conn, tenant)

    # -- turns（P3，每轮 Turn 记录：实现在 store/turns.py）------------
    def record_turn(self, session_id: str, tenant: str, working_dir: str = "",
                    approval_policy: str = "", sandbox_mode: str = "",
                    model: str = "", tokens: int = 0,
                    started_at: str | None = None,
                    ended_at: str | None = None) -> int:
        """记一轮 Turn，返回行 id。"""
        return turns_mod.record_turn(
            self.conn, session_id, tenant, working_dir, approval_policy,
            sandbox_mode, model, tokens, started_at, ended_at)

    def list_turns(self, tenant: str, limit: int = 20) -> list:
        """某租户最近的 Turn，按 id 倒序。"""
        return turns_mod.list_turns(self.conn, tenant, limit)

    # -- sessions（P3 会话元数据：原 session/store.py 的 5 条 SQL，
    #    2026-11-27 接线到仓储，方言留在这里，调用方不再拼 SQL）----
    def create_session(self, session_id: str, tenant: str,
                       summary: str = "") -> dict:
        """新建会话（已存在时不覆盖，返回已有的那条）。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (id, tenant, status, summary, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, tenant, "active", summary, now, now))
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        """按 id 取会话，没有返回 None。"""
        row = self.conn.execute(
            "SELECT id, tenant, status, summary, created_at, updated_at"
            " FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "tenant": row[1], "status": row[2],
                "summary": row[3], "created_at": row[4],
                "updated_at": row[5]}

    def list_sessions(self, tenant: str) -> list:
        """某租户的全部会话，按创建时间升序。"""
        rows = self.conn.execute(
            "SELECT id, tenant, status, summary, created_at, updated_at"
            " FROM sessions WHERE tenant = ? ORDER BY created_at ASC",
            (tenant,)).fetchall()
        return [{"id": r[0], "tenant": r[1], "status": r[2],
                 "summary": r[3], "created_at": r[4],
                 "updated_at": r[5]} for r in rows]

    def update_session_summary(self, session_id: str, summary: str) -> None:
        """更新会话的历史摘要（fork 时复制摘要就调这个）。"""
        from datetime import datetime, timezone

        self.conn.execute(
            "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
            (summary, datetime.now(timezone.utc).isoformat(), session_id))

    def archive_session(self, session_id: str) -> bool:
        """归档会话：状态翻成 archived。返回是否真的改了一条。"""
        from datetime import datetime, timezone

        cur = self.conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ?"
            " WHERE id = ? AND status = ?",
            ("archived", datetime.now(timezone.utc).isoformat(),
             session_id, "active"))
        return cur.rowcount > 0
