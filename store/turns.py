"""Turn 记录（数据层）：每轮记一条 Turn 记录。

字段按 ARCHITECTURE.md 第二节数据层：会话 id、工作目录、审批/沙箱
策略、模型、本轮 token、开始/结束时间。只追加，不改写——查问题时
不用翻日志猜是哪一轮、用的什么模型和策略。

本模块不 import sqlite3：只对调用方传进来的 DB-API 连接做 SQL，
建表 SQL 也在 store/sqlite_store.py 的 SCHEMA 里。
"""
from datetime import datetime, timezone

TURNS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS turns"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, tenant TEXT,"
    " working_dir TEXT, approval_policy TEXT, sandbox_mode TEXT,"
    " model TEXT, tokens INTEGER, started_at TEXT, ended_at TEXT)"
)


def ensure_turns_table(conn) -> None:
    """建 turns 表（不存在才建）。"""
    conn.execute(TURNS_TABLE_SQL)


def record_turn(conn, session_id: str, tenant: str, working_dir: str = "",
                approval_policy: str = "", sandbox_mode: str = "",
                model: str = "", tokens: int = 0,
                started_at: str | None = None,
                ended_at: str | None = None) -> int:
    """记一轮 Turn，返回行 id。时间没给就用现在（UTC ISO）。"""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO turns (session_id, tenant, working_dir,"
        " approval_policy, sandbox_mode, model, tokens, started_at,"
        " ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, tenant, working_dir, approval_policy, sandbox_mode,
         model, tokens, started_at or now, ended_at or now))
    return cur.lastrowid


def list_turns(conn, tenant: str, limit: int = 20) -> list:
    """某租户最近的 Turn：(id, session_id, working_dir, approval_policy,
    sandbox_mode, model, tokens, started_at, ended_at)，按 id 倒序。"""
    return conn.execute(
        "SELECT id, session_id, working_dir, approval_policy,"
        " sandbox_mode, model, tokens, started_at, ended_at FROM turns"
        " WHERE tenant = ? ORDER BY id DESC LIMIT ?",
        (tenant, limit)).fetchall()
