"""长期记忆工具定义（工具层）：remember_fact / recall_fact /
list_facts / search_facts。

只做参数校验 + 调 store/sqlite_store.py（SQLiteStore），不再自己
写业务逻辑；执行前过 tools/executor.py 的权限检查（见
tools/registry.py 拼出来的 LangChain 工具）。
"""
import inspect
from datetime import datetime, timezone

try:
    from agent.tenant import tenant_db
    from store.sqlite_store import SQLiteStore
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.tenant import tenant_db
    from store.sqlite_store import SQLiteStore


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""

    def _remember(args):
        with tenant_db(tenant) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
                (args["key"], args["value"],
                 datetime.now(timezone.utc).isoformat()))
        return f"记住了：{args['key']} = {args['value']}"

    def _recall(args):
        with tenant_db(tenant) as conn:
            value = SQLiteStore(conn).recall_fact(args["key"])
        return value if value else "没记过这件事"

    def _list(args):
        with tenant_db(tenant) as conn:
            rows = SQLiteStore(conn).get_facts()
        return "\n".join(f"{k}: {v}" for k, v in rows) or "记忆还是空的"

    def _search(args):
        with tenant_db(tenant) as conn:
            rows = SQLiteStore(conn).search_facts(args["keyword"])
        return "\n".join(f"{k}: {v}" for k, v in rows) or "记忆里没找到相关的事"

    return [
        {"name": "remember_fact",
         "description": "把一件事记到本租户的长期记忆里。",
         "params": [("key", inspect.Parameter.empty, str),
                    ("value", inspect.Parameter.empty, str)],
         "required": ("key", "value"),
         "handler": _remember},
        {"name": "recall_fact",
         "description": "从本租户的长期记忆里查一件事。",
         "params": [("key", inspect.Parameter.empty, str)],
         "required": ("key",),
         "handler": _recall},
        {"name": "list_facts",
         "description": "列出本租户长期记忆里的所有事。",
         "params": [],
         "required": (),
         "handler": _list},
        {"name": "search_facts",
         "description": "按关键词在本租户的长期记忆里搜索（key 和 value 都查）。",
         "params": [("keyword", inspect.Parameter.empty, str)],
         "required": ("keyword",),
         "handler": _search},
    ]
