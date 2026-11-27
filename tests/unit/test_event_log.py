"""事件日志：chat_log 只追加，摘要后旧行归档不删除（P0 唯一的行为变化）。

旧行为（load_context 里 DELETE FROM chat_log）已改成标记式归档：
- 旧行留在 chat_log 里（总数不变），只在 chat_archived 记 rowid 标记；
- 每次压缩记一条 compaction_events 事件；
- 再读上下文时已归档的行不再出现（history 里没有），也不会重复归档。
"""
import unittest

from tests.helpers import fresh_tenant_data
from agent.memory import load_context
from agent.tenant import tenant_db
from store.sqlite_store import SQLiteStore


class _FakeLLM:
    """假模型：invoke 永远返回固定摘要。"""

    def __init__(self, summary):
        self._summary = summary

    def invoke(self, messages):
        class _Msg:
            content = self._summary
        return _Msg()


def _seed_turns(tenant, n):
    """往 chat_log 写 n 轮对话（每轮一问一答，共 2n 行）。"""
    with tenant_db(tenant) as conn:
        store = SQLiteStore(conn)
        for i in range(n):
            ts = f"2026-11-27T10:{i:02d}:00+08:00"
            store.append_chat(ts, "user", f"第{i}轮用户")
            store.append_chat(ts, "assistant", f"第{i}轮助手")


def _chat_stats(tenant):
    """(chat_log 总行数, 已归档行数, 压缩事件数)。"""
    with tenant_db(tenant) as conn:
        store = SQLiteStore(conn)
        total = store.count_all_chat_rows()
        archived = conn.execute(
            "SELECT COUNT(*) FROM chat_archived").fetchone()[0]
        events = store.compaction_event_count()
    return total, archived, events


class TestEventLog(unittest.TestCase):
    def test_truncation_archives_without_deleting(self):
        with fresh_tenant_data():
            _seed_turns("demo", 6)  # 6 轮，12 行
            ctx = load_context("demo", llm=None, max_turns=2,
                               summary_threshold_turns=3)
            self.assertEqual(ctx["history_turns"], 2)
            self.assertTrue(ctx["truncated"])
            self.assertFalse(ctx["summarized"])  # 没模型，不写摘要
            self.assertIn("第5轮用户", ctx["history_text"])
            self.assertNotIn("第0轮用户", ctx["history_text"])
            total, archived, events = _chat_stats("demo")
            self.assertEqual(total, 12)      # 行还在，一行没删
            self.assertEqual(archived, 8)    # 前 4 轮（8 行）已归档
            self.assertEqual(events, 1)      # 记了一条压缩事件

    def test_second_load_does_not_rearchive(self):
        with fresh_tenant_data():
            _seed_turns("demo", 6)
            load_context("demo", llm=None, max_turns=2,
                         summary_threshold_turns=3)
            ctx = load_context("demo", llm=None, max_turns=2,
                               summary_threshold_turns=3)
            self.assertEqual(ctx["history_turns"], 2)
            total, archived, events = _chat_stats("demo")
            self.assertEqual((total, archived, events), (12, 8, 1))

    def test_summary_path_writes_summary_and_archives(self):
        with fresh_tenant_data():
            _seed_turns("demo", 6)
            fake = _FakeLLM("测试摘要：用户在上海，喜欢喝美式。")
            ctx = load_context("demo", llm=fake, max_turns=2,
                               summary_threshold_turns=3)
            self.assertTrue(ctx["summarized"])
            self.assertIn("测试摘要", ctx["summary"])
            self.assertEqual(ctx["history_turns"], 2)
            total, archived, events = _chat_stats("demo")
            self.assertEqual((total, archived, events), (12, 8, 1))

    def test_load_context_return_shape_unchanged(self):
        with fresh_tenant_data():
            _seed_turns("demo", 1)
            ctx = load_context("demo")
            self.assertEqual(
                set(ctx.keys()),
                {"facts", "summary", "history_text", "history_turns",
                 "todos", "reminders", "due_notifications",
                 "estimated_tokens", "summarized", "truncated"})

    def test_archived_rows_still_readable(self):
        with fresh_tenant_data():
            _seed_turns("demo", 6)
            load_context("demo", llm=None, max_turns=2,
                         summary_threshold_turns=3)
            with tenant_db("demo") as conn:
                store = SQLiteStore(conn)
                rows = store.list_all_chat_rows()
            self.assertEqual(len(rows), 12)
            self.assertEqual(rows[0][3], "第0轮用户")  # 最旧的行还在全量里


if __name__ == "__main__":
    unittest.main()
