"""待办用例（应用层）：加/列/完成。

编排待办这一个用例，不认识 HTTP、SQL、LangGraph 图细节：落盘走
store/sqlite_store.py（SQLiteStore）。返回字符串和原来待办工具
的一字不改，测试断言认它。
"""
try:
    from agent.tenant import tenant_db
    from store.sqlite_store import SQLiteStore
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.tenant import tenant_db
    from store.sqlite_store import SQLiteStore


class TodoService:
    """待办用例：不持有状态，每次调用都现开本租户的库。"""

    def add_todo(self, tenant: str, text: str) -> str:
        """加一条待办（去重：同一内容的 pending 待办不重复写）。"""
        with tenant_db(tenant) as conn:
            ok = SQLiteStore(conn).add_todo(text)
        return (f"已加入待办：{text.strip()}" if ok
                else f"待办里已经有：{text.strip()}")

    def list_todos(self, tenant: str) -> str:
        """列待办（未完成在前，已完成在后）。"""
        with tenant_db(tenant) as conn:
            rows = SQLiteStore(conn).list_all_todos()
        if not rows:
            return "待办清单还是空的"
        return "\n".join(
            f"[{'待办' if s == 'pending' else '已完成'} {i}] {t}"
            for i, t, s in rows)

    def complete_todo(self, tenant: str, todo_id: int) -> str:
        """把一条待办标为已完成；找不到时不报错，只说没找到。"""
        with tenant_db(tenant) as conn:
            text = SQLiteStore(conn).complete_todo(todo_id)
        if text is None:
            return f"没有找到编号为 {todo_id} 的未完成待办"
        return f"已完成待办：{text}"
