"""会话元数据仓储（数据层）：sessions 表的操作。

会话是 Codex Thread 原语的轻量版：一个会话有 id、所属租户、状态
（active/archived）、一份历史摘要。聊天全文不在这里——全文仍在
租户级的 chat_log 事件日志里（P0 只追加），fork 时只复制摘要 +
事实引用（事实本身是租户级的 facts 表，fork 后新会话天然引用同一
份事实，不复制全文）。

2026-11-27 接线：本模块不再直接拼 SQL——5 条 SQL 搬进了
store/sqlite_store.py 和 store/postgres_store.py（方言各在各家），
这里只转发给调用方传进来的仓储对象。函数签名从 conn 改成 store，
调用方见 session/session.py。
"""
from datetime import datetime, timezone

SESSION_ACTIVE = "active"
SESSION_ARCHIVED = "archived"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(store, session_id: str, tenant: str,
                   summary: str = "") -> dict:
    """新建会话（已存在时不覆盖，返回已有的那条）。"""
    return store.create_session(session_id, tenant, summary)


def get_session(store, session_id: str) -> dict | None:
    """按 id 取会话，没有返回 None。"""
    return store.get_session(session_id)


def list_sessions(store, tenant: str) -> list:
    """某租户的全部会话，按创建时间升序。"""
    return store.list_sessions(tenant)


def update_summary(store, session_id: str, summary: str) -> None:
    """更新会话的历史摘要（fork 时复制摘要就调这个）。"""
    store.update_session_summary(session_id, summary)


def archive_session(store, session_id: str) -> bool:
    """归档会话：状态翻成 archived。返回是否真的改了一条
    （不存在的会话返回 False）。归档后该会话只读，见 session/session.py。"""
    return store.archive_session(session_id)
