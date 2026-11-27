"""工具层入口：注册表 + 执行器 + 权限管线。

make_tools(tenant) 是给一个租户建一套 LangChain 工具的入口
（见 tools/registry.py）；工具定义在 tools/definitions/*.py，
业务逻辑在 app/*_service.py，权限在 tools/permissions.py，
执行检查点在 tools/executor.py。
"""
try:
    from .registry import ToolRegistry, make_tools
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.registry import ToolRegistry, make_tools

__all__ = ["ToolRegistry", "make_tools"]
