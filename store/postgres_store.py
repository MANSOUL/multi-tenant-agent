"""Postgres 仓储（数据层，P4 存储换 Postgres）：实现
domain/ports.py 里的 ReminderStore / MemoryStore。

和 store/sqlite_store.py 同一个接口、同一套方法签名，只是方言不同：
- 参数占位符全部是 %s（一个 ? 都没有）；
- 自增主键用 SERIAL，不用 AUTOINCREMENT；
- INSERT OR REPLACE / INSERT OR IGNORE 换成 ON CONFLICT；
- chat_log 没有 SQLite 的 rowid：改成显式的 id SERIAL PRIMARY KEY，
  list_active_chat_rows / archive_turns 里的“行号”就是这个 id
  （调用方只把它当不透明行号传回 archive_turns，不做算术）；
- 执行统一走 conn.cursor()（psycopg2 的连接没有 .execute，只有
  cursor；sqlite3 的连接也有 .cursor()，但本类只接 Postgres 连接）。

租户隔离：Postgres 里不再是一租户一个库文件，而是一租户一个
schema（CREATE SCHEMA IF NOT EXISTS "<租户>" + SET search_path，
见 ensure_schema）：表名、字段名和 SQLite 版一字不差，隔离靠
schema 名，租户 id 只允许字母数字 _ -（不合规直接 ValueError，
绝不拼进 SQL）。

建表覆盖：facts / chat_log / summaries / todos / reminders /
pending_notifications / chat_archived / compaction_events +
P3 的三张新表 metering / turns / sessions（和 SQLite 版同字段，
只换自增写法）。

接线状态（诚实说明，见文末“未接线”）：
- STORE_BACKEND 环境变量切换在 store/__init__.py 的 create_store：
  sqlite（默认）→ SQLiteStore，postgres → PostgresStore；
  传错连接（postgres 后端配了个 SQLite 连接）直接 ValueError。
- connect() 只连库：psycopg2 是懒 import，没装时报清楚的
  RuntimeError（本地 mtvenv 没装 psycopg2，真库验证在服务器上
  用 mt-postgres 容器做）；DSN 从 POSTGRES_DSN 环境变量读。
- 未接线（服务器验证前要补，见汇报）：agent/tenant.py 的
  tenant_db() 现在还是只开 SQLite——它一旦按 STORE_BACKEND
  切到 Postgres，调用方里直接写 SQLite 方言的 7 处 SQL 也要改
  （session/store.py 5 处、app/reminder_service.py 1 处、
  tools/definitions/memory.py 的 INSERT OR REPLACE 1 处），
  还有 gateway/control/tenants.py 的用量汇总是直接读 SQLite
  文件的。create_store 的调用点已经全换过去了（agent/memory.py、
  app/*、context/assembler.py、tools/definitions/memory.py、
  infra/graph.py），sqlite 默认下行为一字不改。
"""
import os
import re

try:
    from domain.memory import estimate_tokens
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from domain.memory import estimate_tokens

TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 建表 SQL：和 store/sqlite_store.py 的 SCHEMA_STATEMENTS 同表同字段，
# 只把 AUTOINCREMENT 换成 SERIAL、chat_log 加显式 id 主键。
SCHEMA_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS facts"
    "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS chat_log"
    "(id SERIAL PRIMARY KEY, ts TEXT, role TEXT, content TEXT)",
    "CREATE TABLE IF NOT EXISTS summaries"
    "(id SERIAL PRIMARY KEY, ts TEXT, summary TEXT, up_to_ts TEXT)",
    "CREATE TABLE IF NOT EXISTS todos"
    "(id SERIAL PRIMARY KEY, text TEXT, status TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS reminders"
    "(id SERIAL PRIMARY KEY, text TEXT, due_at TEXT, repeat TEXT,"
    " status TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS pending_notifications"
    "(id SERIAL PRIMARY KEY, tenant TEXT, text TEXT, due_at TEXT,"
    " created_at TEXT, delivered INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS chat_archived"
    "(rowid INTEGER PRIMARY KEY, archived_at TEXT)",
    "CREATE TABLE IF NOT EXISTS compaction_events"
    "(id SERIAL PRIMARY KEY, ts TEXT, archived_count INTEGER,"
    " up_to_ts TEXT)",
    # P3 的三张新表：metering / turns / sessions（字段和 SQLite 版一致）。
    "CREATE TABLE IF NOT EXISTS metering"
    "(id SERIAL PRIMARY KEY, tenant TEXT, model TEXT, ts TEXT,"
    " tokens_input INTEGER, tokens_output INTEGER, tokens_total INTEGER,"
    " cost_estimate DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS turns"
    "(id SERIAL PRIMARY KEY, session_id TEXT, tenant TEXT,"
    " working_dir TEXT, approval_policy TEXT, sandbox_mode TEXT,"
    " model TEXT, tokens INTEGER, started_at TEXT, ended_at TEXT)",
    "CREATE TABLE IF NOT EXISTS sessions"
    "(id TEXT PRIMARY KEY, tenant TEXT, status TEXT, summary TEXT,"
    " created_at TEXT, updated_at TEXT)",
]


def _execute(conn, sql, params=()):
    """在连接上跑一条 SQL，返回 cursor（fetchall / fetchone / rowcount
    都从它上面取）。psycopg2 和 sqlite3 的连接都有 .cursor()，
    但占位符方言不同——本模块的 SQL 只写 %s，别混进 ?。"""
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def ensure_schema(conn, schema: str | None = None) -> None:
    """建全部表（不存在才建）。schema 给了就先建 schema 并切
    search_path（租户隔离靠它）；不给就在连接当前的 search_path
    下建表。建表本身不提交，调用方按原来的约定提交。"""
    if schema is not None:
        if not TENANT_RE.fullmatch(schema):
            raise ValueError(f"schema 名不合法：{schema!r}")
        _execute(conn, f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        _execute(conn, f'SET search_path TO "{schema}"')
    for stmt in SCHEMA_STATEMENTS:
        _execute(conn, stmt)


def connect(dsn: str | None = None):
    """连 Postgres，返回连接（调用方负责建表 + 提交 + 关闭）。

    psycopg2 是懒 import：没装时报 RuntimeError（本地开发机没装，
    真库验证在服务器上做）；DSN 不给就读 POSTGRES_DSN 环境变量，
    没配也报清楚，不猜默认值（猜一个连不上的 host 只会浪费时间）。
    """
    try:
        import psycopg2
    except ImportError as e:
        raise RuntimeError(
            "没装 psycopg2，连不上 Postgres：先 pip install "
            "psycopg2-binary（服务器部署时见 agent/requirements.txt）"
        ) from e
    dsn = dsn or os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        raise RuntimeError(
            "没配 POSTGRES_DSN，连不上 Postgres：例如 "
            "POSTGRES_DSN=postgresql://mt_agent:<redacted>"
            "@mt-postgres:5432/mt_agent")
    return psycopg2.connect(dsn)


class PostgresStore:
    """Postgres 实现的仓储：包一个连接，所有方法都是 SQLiteStore
    的 Postgres 方言直译（同一接口、同一返回形状）。

    约定和 SQLiteStore 一样：调用方负责提交/关闭；本类不自己
    提交、不自己关闭。会话（sessions 表）只有建表，没有方法——
    session/store.py 现在还只会 SQLite 方言，见模块头“未接线”。
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
        row = _execute(
            self.conn, "SELECT 1 FROM facts WHERE value = %s",
            (value,)).fetchone()
        if row:
            return False
        _execute(
            self.conn,
            "INSERT INTO facts (key, value, updated_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = EXCLUDED.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()))
        return True

    def get_facts(self):
        return _execute(
            self.conn, "SELECT key, value FROM facts").fetchall()

    def search_facts(self, keyword: str):
        kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace(
            "_", "\\_")
        return _execute(
            self.conn,
            "SELECT key, value FROM facts WHERE key LIKE %s ESCAPE '\\'"
            " OR value LIKE %s ESCAPE '\\' LIMIT 20",
            (f"%{kw}%", f"%{kw}%")).fetchall()

    def recall_fact(self, key: str):
        row = _execute(
            self.conn, "SELECT value FROM facts WHERE key = %s",
            (key,)).fetchone()
        return row[0] if row else None

    # -- chat_log / summaries（事件日志：只追加，归档只标记）-----------
    def append_chat(self, ts: str, role: str, content: str) -> None:
        _execute(
            self.conn,
            "INSERT INTO chat_log (ts, role, content) VALUES (%s, %s, %s)",
            (ts, role, content))

    def list_active_chat_rows(self):
        return _execute(
            self.conn,
            "SELECT id, ts, role, content FROM chat_log"
            " WHERE id NOT IN (SELECT rowid FROM chat_archived)"
            " ORDER BY id ASC").fetchall()

    def list_all_chat_rows(self):
        return _execute(
            self.conn,
            "SELECT id, ts, role, content FROM chat_log"
            " ORDER BY id ASC").fetchall()

    def count_all_chat_rows(self) -> int:
        return _execute(
            self.conn, "SELECT COUNT(*) FROM chat_log").fetchone()[0]

    def latest_summary(self) -> str:
        row = _execute(
            self.conn,
            "SELECT summary FROM summaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def add_summary(self, ts: str, summary: str, up_to_ts: str) -> None:
        _execute(
            self.conn,
            "INSERT INTO summaries (ts, summary, up_to_ts)"
            " VALUES (%s, %s, %s)", (ts, summary, up_to_ts))

    def archive_turns(self, rowids, up_to_ts: str) -> int:
        """把指定的 chat_log 行归档：记 chat_archived 标记 + 一条
        compaction_events 事件；返回归档的行数。行本身不删除。"""
        from datetime import datetime, timezone

        rowids = list(rowids or [])
        if not rowids:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        for rid in rowids:
            _execute(
                self.conn,
                "INSERT INTO chat_archived (rowid, archived_at)"
                " VALUES (%s, %s) ON CONFLICT (rowid) DO NOTHING",
                (rid, now))
        _execute(
            self.conn,
            "INSERT INTO compaction_events (ts, archived_count, up_to_ts)"
            " VALUES (%s, %s, %s)", (now, len(rowids), up_to_ts))
        return len(rowids)

    def compaction_event_count(self) -> int:
        return _execute(
            self.conn,
            "SELECT COUNT(*) FROM compaction_events").fetchone()[0]

    # -- todos ---------------------------------------------------------
    def add_todo(self, text: str) -> bool:
        from datetime import datetime, timezone

        text = (text or "").strip(" ，,。；;、")
        if not text:
            return False
        row = _execute(
            self.conn,
            "SELECT 1 FROM todos WHERE text = %s AND status = 'pending'",
            (text,)).fetchone()
        if row:
            return False
        _execute(
            self.conn,
            "INSERT INTO todos (text, status, created_at)"
            " VALUES (%s, 'pending', %s)",
            (text, datetime.now(timezone.utc).isoformat()))
        return True

    def list_pending_todos(self):
        return _execute(
            self.conn,
            "SELECT id, text FROM todos WHERE status = 'pending'"
            " ORDER BY id ASC").fetchall()

    def list_all_todos(self):
        return _execute(
            self.conn,
            "SELECT id, text, status FROM todos ORDER BY"
            " CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id ASC"
        ).fetchall()

    def complete_todo(self, todo_id: int):
        row = _execute(
            self.conn,
            "SELECT text FROM todos WHERE id = %s AND status = 'pending'",
            (todo_id,)).fetchone()
        if not row:
            return None
        _execute(
            self.conn,
            "UPDATE todos SET status = 'done' WHERE id = %s",
            (todo_id,))
        return row[0]

    # -- reminders -----------------------------------------------------
    def find_pending_reminder_near(self, text: str, due_at: str | None,
                                   window_minutes: int = 5):
        """找同内容、同时间的 pending 提醒：due_at 在 ±window 分钟内的
        算同一条（P3 去重 bug 修复）。返回 (id, text, due_at, repeat,
        status)，没有返回 None。"""
        from datetime import datetime

        text = (text or "").strip(" ，,。；;、")
        if not text or not due_at:
            return None
        try:
            target = datetime.fromisoformat(due_at)
        except ValueError:
            return None
        rows = _execute(
            self.conn,
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " WHERE text = %s AND status = 'pending'"
            " AND due_at IS NOT NULL",
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
        due_at = parsed.get("due_at")
        # Postgres 不支持 `due_at IS %s`（IS 后面只接受 NULL 字面量），
        # 所以空和非空分两条 SQL 写，绝不在这里拼字符串。
        if due_at is None:
            row = _execute(
                self.conn,
                "SELECT 1 FROM reminders WHERE text = %s"
                " AND status = 'pending' AND due_at IS NULL",
                (text,)).fetchone()
        else:
            row = _execute(
                self.conn,
                "SELECT 1 FROM reminders WHERE text = %s"
                " AND status = 'pending' AND due_at = %s",
                (text, due_at)).fetchone()
        if row:
            return False
        _execute(
            self.conn,
            "INSERT INTO reminders (text, due_at, repeat, status,"
            " created_at) VALUES (%s, %s, %s, 'pending', %s)",
            (text, due_at, parsed.get("repeat", "once"),
             datetime.now(timezone.utc).isoformat()))
        return True

    def list_pending_reminders(self):
        return _execute(
            self.conn,
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()

    def list_all_reminders(self):
        return _execute(
            self.conn,
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " ORDER BY id ASC").fetchall()

    def update_reminder(self, rid: int, due_at: str, repeat: str) -> None:
        _execute(
            self.conn,
            "UPDATE reminders SET due_at = %s, repeat = %s,"
            " status = 'pending' WHERE id = %s",
            (due_at, repeat, rid))

    def find_reminder_for_update(self, rid: int, text: str):
        row = None
        if rid:
            row = _execute(
                self.conn,
                "SELECT id, text FROM reminders WHERE id = %s"
                " AND status IN ('pending', 'due')",
                (rid,)).fetchone()
        if row is None and (text or "").strip():
            row = _execute(
                self.conn,
                "SELECT id, text FROM reminders WHERE text = %s"
                " AND status IN ('pending', 'due')"
                " ORDER BY id DESC LIMIT 1",
                (text.strip(),)).fetchone()
        return row

    def list_due_candidates(self):
        return _execute(
            self.conn,
            "SELECT id, text, due_at, repeat FROM reminders"
            " WHERE status = 'pending' AND due_at IS NOT NULL"
        ).fetchall()

    def mark_reminder_due(self, rid: int) -> None:
        _execute(
            self.conn,
            "UPDATE reminders SET status = 'due' WHERE id = %s", (rid,))

    def mark_reminder_notified(self, text: str, due_at: str) -> None:
        _execute(
            self.conn,
            "UPDATE reminders SET status = 'notified'"
            " WHERE text = %s AND due_at = %s AND status = 'due'",
            (text, due_at))

    # -- pending_notifications -----------------------------------------
    def add_notification_if_absent(
            self, tenant: str, text: str, due_at: str) -> bool:
        from datetime import datetime, timezone

        exists = _execute(
            self.conn,
            "SELECT 1 FROM pending_notifications WHERE text = %s"
            " AND due_at = %s AND delivered = 0",
            (text, due_at)).fetchone()
        if exists:
            return False
        _execute(
            self.conn,
            "INSERT INTO pending_notifications"
            " (tenant, text, due_at, created_at, delivered)"
            " VALUES (%s, %s, %s, %s, 0)",
            (tenant, text, due_at,
             datetime.now(timezone.utc).isoformat()))
        return True

    def list_undelivered_notifications(self):
        return _execute(
            self.conn,
            "SELECT id, text, due_at FROM pending_notifications"
            " WHERE delivered = 0 ORDER BY id ASC").fetchall()

    def mark_notifications_delivered(self, tenant: str, ids):
        ids = [i for i in (ids or []) if isinstance(i, int)]
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        rows = _execute(
            self.conn,
            f"SELECT text, due_at FROM pending_notifications WHERE"
            f" tenant = %s AND id IN ({placeholders})",
            [tenant, *ids]).fetchall()
        _execute(
            self.conn,
            f"UPDATE pending_notifications SET delivered = 1 WHERE"
            f" tenant = %s AND id IN ({placeholders})",
            [tenant, *ids])
        return rows

    # -- metering（P3，模型调用计量：和 model/metering.py 同口径，
    #    token 粗估、cost_estimate 留 0.0；这里是 Postgres 方言直译，
    #    不复用 metering.py——它只会 SQLite 的 ? 占位符）----------------
    def record_metering(self, tenant: str, model: str, input_text: str = "",
                        output_text: str = "", cost_estimate: float = 0.0
                        ) -> int:
        """记一次模型调用，返回行 id（INSERT ... RETURNING id；
        psycopg2 的 cursor 没有 lastrowid）。"""
        from datetime import datetime, timezone

        tokens_input = estimate_tokens(input_text)
        tokens_output = estimate_tokens(output_text)
        row = _execute(
            self.conn,
            "INSERT INTO metering (tenant, model, ts, tokens_input,"
            " tokens_output, tokens_total, cost_estimate)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant, model, datetime.now(timezone.utc).isoformat(),
             tokens_input, tokens_output, tokens_input + tokens_output,
             cost_estimate)).fetchone()
        return row[0]

    def metering_summary(self, tenant: str) -> dict:
        """按租户汇总计量：调用次数、token 合计、成本估算合计。"""
        row = _execute(
            self.conn,
            "SELECT COUNT(*), COALESCE(SUM(tokens_input), 0),"
            " COALESCE(SUM(tokens_output), 0), COALESCE(SUM(tokens_total), 0),"
            " COALESCE(SUM(cost_estimate), 0.0) FROM metering"
            " WHERE tenant = %s",
            (tenant,)).fetchone()
        calls, tok_in, tok_out, tok_total, cost = row
        return {"tenant": tenant, "model_calls": calls,
                "tokens_input": tok_in, "tokens_output": tok_out,
                "tokens_total": tok_total, "cost_estimate": cost,
                "metering": "available"}

    # -- turns（P3，每轮 Turn 记录：和 store/turns.py 同字段，
    #    Postgres 方言直译，不复用 turns.py）-----------------------------
    def record_turn(self, session_id: str, tenant: str, working_dir: str = "",
                    approval_policy: str = "", sandbox_mode: str = "",
                    model: str = "", tokens: int = 0,
                    started_at: str | None = None,
                    ended_at: str | None = None) -> int:
        """记一轮 Turn，返回行 id。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        row = _execute(
            self.conn,
            "INSERT INTO turns (session_id, tenant, working_dir,"
            " approval_policy, sandbox_mode, model, tokens, started_at,"
            " ended_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (session_id, tenant, working_dir, approval_policy,
             sandbox_mode, model, tokens, started_at or now,
             ended_at or now)).fetchone()
        return row[0]

    def list_turns(self, tenant: str, limit: int = 20) -> list:
        """某租户最近的 Turn，按 id 倒序。"""
        return _execute(
            self.conn,
            "SELECT id, session_id, working_dir, approval_policy,"
            " sandbox_mode, model, tokens, started_at, ended_at FROM turns"
            " WHERE tenant = %s ORDER BY id DESC LIMIT %s",
            (tenant, limit)).fetchall()
