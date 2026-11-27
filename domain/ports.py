"""领域层定义的接口（端口）：仓储、模型客户端。

只定义接口，不实现。实现在 store/（SQLite）和 model/（模型客户端），
业务代码只认这些接口，不认具体实现——换存储、换模型时只改实现层。

方法签名按现有调用方式来（agent/memory.py、agent/tools.py、
agent/graph.py 里实际用的操作），P0 不改签名。
"""
from typing import Any, Protocol


class ReminderStore(Protocol):
    """提醒仓储：reminders / pending_notifications 两张表的操作。"""

    def add_reminder(self, text: str, parsed: dict) -> bool:
        """写一条提醒（去重：同一内容 + 同一 due_at 的 pending 行不重复写）。"""
        ...

    def list_pending_reminders(
            self) -> list[tuple[int, str, str | None, str, str]]:
        """未到点的提醒：(id, text, due_at, repeat, status)，按 id 升序。"""
        ...

    def list_all_reminders(
            self) -> list[tuple[int, str, str | None, str, str]]:
        """全部提醒（含已到点、已提醒）：同上。"""
        ...

    def update_reminder(self, rid: int, due_at: str, repeat: str) -> None:
        """改一条提醒的时间和 repeat，状态回到 pending。"""
        ...

    def find_reminder_for_update(
            self, rid: int, text: str) -> tuple[int, str] | None:
        """找要改的那条提醒：优先按 id，没有时按 text 找最近一条
        pending/due 的；找不到返回 None。"""
        ...

    def list_due_candidates(self) -> list[tuple[int, str, str, str]]:
        """调度器要扫的行：status=pending 且 due_at 非空，
        (id, text, due_at, repeat)。"""
        ...

    def mark_reminder_due(self, rid: int) -> None:
        """把一条提醒置为 due（已到点，等送达）。"""
        ...

    def mark_reminder_notified(self, text: str, due_at: str) -> None:
        """把已到点又已送达的提醒行从 due 翻成 notified。"""
        ...

    def add_notification_if_absent(
            self, tenant: str, text: str, due_at: str) -> bool:
        """写一条未送达记录（同 text+due_at 已有未送达记录时不重复写）。"""
        ...

    def list_undelivered_notifications(self) -> list[tuple[int, str, str]]:
        """未送达的提醒：(id, text, due_at)，按 id 升序。"""
        ...

    def mark_notifications_delivered(
            self, tenant: str, ids: list[int]) -> list[tuple[str, str]]:
        """把指定租户、指定 id 的待提醒标为已送达，返回对应的 (text, due_at)
        列表，供调用方把提醒行翻成 notified。"""
        ...


class MemoryStore(Protocol):
    """记忆仓储：facts / chat_log / summaries / todos 的操作。"""

    def remember_fact_once(self, key: str, value: str) -> bool:
        """facts 去重写：同一 value 已存在就不重复写。返回是否真的写了。"""
        ...

    def get_facts(self) -> list[tuple[str, str]]:
        """全部 facts：(key, value)。"""
        ...

    def search_facts(self, keyword: str) -> list[tuple[str, str]]:
        """按关键词搜 facts（key 和 value 都查，LIKE 转义后查）。"""
        ...

    def recall_fact(self, key: str) -> str | None:
        """按 key 查一条 fact，查不到返回 None。"""
        ...

    def append_chat(self, ts: str, role: str, content: str) -> None:
        """往 chat_log 只追加一行（事件日志只追加，不删除、不改写）。"""
        ...

    def list_active_chat_rows(self) -> list[tuple[int, str, str, str]]:
        """未归档的 chat_log 行：(rowid, ts, role, content)，按 rowid 升序。
        已归档的行不在这里，只在 chat_log 全量里。"""
        ...

    def count_all_chat_rows(self) -> int:
        """chat_log 总行数（含已归档的）——归档只标记，不删除，所以总数不变。"""
        ...

    def latest_summary(self) -> str:
        """最新一条摘要，没有返回空串。"""
        ...

    def add_summary(self, ts: str, summary: str, up_to_ts: str) -> None:
        """写一条摘要。"""
        ...

    def list_pending_todos(self) -> list[tuple[int, str]]:
        """未完成的待办：(id, text)，按 id 升序。"""
        ...

    def add_todo(self, text: str) -> bool:
        """写一条待办（同一内容的 pending 待办不重复写）。"""
        ...

    def archive_turns(self, rowids: list[int], up_to_ts: str) -> int:
        """把指定的 chat_log 行归档：记 chat_archived 标记 + 一条
        compaction_events 事件；返回归档的行数。行本身不删除。"""
        ...

    def compaction_event_count(self) -> int:
        """compaction_events 表里的事件条数。"""
        ...


class LLMClient(Protocol):
    """模型客户端：按 graph.py 里 ChatOpenAI 的实际调用方式。"""

    def invoke(self, messages: list) -> Any:
        """调一次模型，返回带 .content 的消息对象。"""
        ...

    def bind_tools(self, tools: list) -> "LLMClient":
        """绑上工具后返回一个能调工具的客户端。"""
        ...
