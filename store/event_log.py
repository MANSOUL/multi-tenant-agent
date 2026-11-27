"""事件日志：chat_log 只追加，归档只标记，不删除。

设计选择（P0 唯一的行为变化）：
- chat_log 是只追加的事件日志：只 INSERT，从不 DELETE、不 UPDATE。
- 摘要时旧行不再 DELETE，而是记两条新记录：
  1. `chat_archived`：被归档的 chat_log rowid 标记（行本身还留在 chat_log 里，
     只是不再参与上下文组装）；
  2. `compaction_events`：一次压缩事件（时间、归档行数、摘要覆盖到的时间点）。
- 之所以不用“迁入 chat_archive 复制表再 DELETE”的做法：一是任务的 grep
  检查要求删除 chat_log 的 SQL 彻底消失；二是复制表会让同一行存两份，
  标记法只有一份事实来源，归档状态就是一张标记表。

读上下文时只读未归档的行（见 list_active_chat_rows），归档后的行永远
可查（count_all_chat_rows / 直接查 chat_log 全量）。
"""
from datetime import datetime, timezone


def append_chat(conn, ts: str, role: str, content: str) -> None:
    """往 chat_log 只追加一行。"""
    conn.execute(
        "INSERT INTO chat_log (ts, role, content) VALUES (?, ?, ?)",
        (ts, role, content))


def list_active_chat_rows(conn) -> list:
    """未归档的 chat_log 行：(rowid, ts, role, content)，按 rowid 升序。"""
    return conn.execute(
        "SELECT rowid, ts, role, content FROM chat_log"
        " WHERE rowid NOT IN (SELECT rowid FROM chat_archived)"
        " ORDER BY rowid ASC").fetchall()


def list_all_chat_rows(conn) -> list:
    """chat_log 全量行（含已归档的）：(rowid, ts, role, content)。"""
    return conn.execute(
        "SELECT rowid, ts, role, content FROM chat_log"
        " ORDER BY rowid ASC").fetchall()


def count_all_chat_rows(conn) -> int:
    """chat_log 总行数（含已归档的）——归档不删除，所以总数不变。"""
    return conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]


def archive_turns(conn, rowids, up_to_ts: str) -> int:
    """把指定的 chat_log 行归档：记 chat_archived 标记 + 一条
    compaction_events 事件；返回归档的行数。行本身不删除。"""
    rowids = list(rowids or [])
    if not rowids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    for rid in rowids:
        conn.execute(
            "INSERT OR IGNORE INTO chat_archived (rowid, archived_at)"
            " VALUES (?, ?)", (rid, now))
    conn.execute(
        "INSERT INTO compaction_events (ts, archived_count, up_to_ts)"
        " VALUES (?, ?, ?)", (now, len(rowids), up_to_ts))
    return len(rowids)


def compaction_event_count(conn) -> int:
    """compaction_events 表里的事件条数。"""
    return conn.execute(
        "SELECT COUNT(*) FROM compaction_events").fetchone()[0]
