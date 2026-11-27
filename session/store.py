"""会话元数据仓储（数据层）：sessions 表的操作。

会话是 Codex Thread 原语的轻量版：一个会话有 id、所属租户、状态
（active/archived）、一份历史摘要。聊天全文不在这里——全文仍在
租户级的 chat_log 事件日志里（P0 只追加），fork 时只复制摘要 +
事实引用（事实本身是租户级的 facts 表，fork 后新会话天然引用同一
份事实，不复制全文）。

本模块不 import sqlite3：只对调用方传进来的 DB-API 连接做 SQL，
建表 SQL 在 store/sqlite_store.py 的 SCHEMA 里。
"""
from datetime import datetime, timezone

SESSION_ACTIVE = "active"
SESSION_ARCHIVED = "archived"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(conn, session_id: str, tenant: str,
                   summary: str = "") -> dict:
    """新建会话（已存在时不覆盖，返回已有的那条）。"""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO sessions"
        " (id, tenant, status, summary, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, tenant, SESSION_ACTIVE, summary, now, now))
    return get_session(conn, session_id)


def get_session(conn, session_id: str) -> dict | None:
    """按 id 取会话，没有返回 None。"""
    row = conn.execute(
        "SELECT id, tenant, status, summary, created_at, updated_at"
        " FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "tenant": row[1], "status": row[2],
            "summary": row[3], "created_at": row[4], "updated_at": row[5]}


def list_sessions(conn, tenant: str) -> list:
    """某租户的全部会话，按创建时间升序。"""
    rows = conn.execute(
        "SELECT id, tenant, status, summary, created_at, updated_at"
        " FROM sessions WHERE tenant = ? ORDER BY created_at ASC",
        (tenant,)).fetchall()
    return [{"id": r[0], "tenant": r[1], "status": r[2], "summary": r[3],
             "created_at": r[4], "updated_at": r[5]} for r in rows]


def update_summary(conn, session_id: str, summary: str) -> None:
    """更新会话的历史摘要（fork 时复制摘要就调这个）。"""
    conn.execute(
        "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
        (summary, _now(), session_id))


def archive_session(conn, session_id: str) -> bool:
    """归档会话：状态翻成 archived。返回是否真的改了一条
    （不存在的会话返回 False）。归档后该会话只读，见 session/session.py。"""
    cur = conn.execute(
        "UPDATE sessions SET status = ?, updated_at = ?"
        " WHERE id = ? AND status = ?",
        (SESSION_ARCHIVED, _now(), session_id, SESSION_ACTIVE))
    return cur.rowcount > 0
