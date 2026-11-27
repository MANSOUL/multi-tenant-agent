"""待办工具定义（工具层）：add_todo / list_todos / complete_todo。

只做参数校验 + 调 app/todo_service.py，不再自己写业务逻辑；
执行前过 tools/executor.py 的权限检查（见 tools/registry.py
拼出来的 LangChain 工具）。
"""
import inspect

try:
    from app.todo_service import TodoService
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.todo_service import TodoService


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""
    service = TodoService()
    return [
        {"name": "add_todo",
         "description": "往本租户的待办清单里加一条。成功时返回写进去的内容，供回显。",
         "params": [("text", inspect.Parameter.empty, str)],
         "required": ("text",),
         "handler": lambda args: service.add_todo(tenant, args["text"])},
        {"name": "list_todos",
         "description": "列出本租户的待办清单（未完成在前，已完成在后）。",
         "params": [],
         "required": (),
         "handler": lambda args: service.list_todos(tenant)},
        {"name": "complete_todo",
         "description": "把本租户待办清单里的一条标为已完成（传 list_todos 里看到的 id）。",
         "params": [("todo_id", inspect.Parameter.empty, int)],
         "required": ("todo_id",),
         "handler": lambda args: service.complete_todo(
             tenant, args["todo_id"])},
    ]
