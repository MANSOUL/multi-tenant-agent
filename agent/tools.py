"""工具（兼容薄层，P3）：原来 306 行的 agent/tools.py 已拆走。

- 注册表 + LangChain 工具拼装：tools/registry.py
- 每类工具定义：tools/definitions/*.py（todo/reminder/memory/
  file/browser/sandbox），工具只做参数校验 + 调 service
- 权限管线：tools/permissions.py（deny > ask > allow）
- 执行检查点：tools/executor.py（参数校验 → 权限 → 执行 → 审计）
- 业务逻辑：app/reminder_service.py、app/todo_service.py

本文件只剩兼容导出：tests 和旧调用方 import 的 make_tools /
browse_page_impl 还在这里找得到，行为和拆之前一致。
"""
try:
    from tools.definitions.browser import browse_page_impl
    from tools.registry import ToolRegistry, make_tools
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.definitions.browser import browse_page_impl
    from tools.registry import ToolRegistry, make_tools

__all__ = ["ToolRegistry", "browse_page_impl", "make_tools"]
