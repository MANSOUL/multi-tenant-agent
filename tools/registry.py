"""工具注册表（工具层）：发现、参数 schema、租户开关。

发现：make_tools(tenant) 时把 tools/definitions/*.py 各模块的
spec(tenant) 聚到一起，一个租户一套工具，工具名和 P3 之前一致
（共 16 个，见 test_tool_count_is_16）。
参数 schema：每个 spec 的 params 拼成函数签名（__signature__），
LangChain 的 @tool 按签名派生 args schema；schemas() 直接返回
各工具的 args schema 供外部查。
租户开关：make_tools(tenant, disabled={...}) 可以按租户关掉个别
工具（关掉的工具不在返回列表里，模型看不到也调不到；权限层
的未知工具 deny 是第二道防线）。
工具本身只做参数校验 + 调 service（见 definitions/*），执行前
统一过 tools/executor.py（参数校验 → 权限 → 执行 → 审计日志）。
"""
import inspect

try:
    from langchain_core.tools import tool as _lc_tool
except ImportError:  # pragma: no cover - langchain 是硬依赖
    _lc_tool = None

try:
    from . import executor as executor_mod
    from .definitions import (
        browser as browser_defs,
        file as file_defs,
        memory as memory_defs,
        reminder as reminder_defs,
        sandbox as sandbox_defs,
        todo as todo_defs,
    )
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import executor as executor_mod
    from tools.definitions import (
        browser as browser_defs,
        file as file_defs,
        memory as memory_defs,
        reminder as reminder_defs,
        sandbox as sandbox_defs,
        todo as todo_defs,
    )

# 发现顺序：决定 make_tools 返回列表的顺序（测试只按名字取，不认顺序，
# 但保持固定顺序方便排障）。
_DEFINITION_MODULES = (sandbox_defs, file_defs, memory_defs, browser_defs,
                       todo_defs, reminder_defs)


def _build_langchain_tool(tenant: str, spec: dict):
    """按一个 spec 拼一个 LangChain 工具。

    spec 的 params 是 [(参数名, 默认值, 类型)]，默认值用
    inspect.Parameter.empty 表示必填；拼出来的函数签名让 @tool
    派生出和原来手写 @tool 一样的 args schema。
    函数体只有一行：调 executor（参数校验 → 权限 → 执行 → 审计）。
    """
    name = spec["name"]
    params = spec.get("params", [])
    required = spec.get("required", ())
    handler = spec["handler"]

    def _impl(**kwargs):
        return executor_mod.execute_tool(
            tenant, name, kwargs, handler, required)

    _impl.__name__ = name
    _impl.__doc__ = spec.get("description", "")
    _impl.__signature__ = inspect.Signature(parameters=[
        inspect.Parameter(p, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=d, annotation=t)
        for p, d, t in params])
    _impl.__annotations__ = {p: t for p, d, t in params}
    return _lc_tool(_impl)


class ToolRegistry:
    """一个租户的工具注册表：spec 聚合 + 租户开关 + schema 查询。"""

    def __init__(self, tenant: str, disabled=None):
        self.tenant = tenant
        self.disabled = set(disabled or [])
        self._specs = {}
        for mod in _DEFINITION_MODULES:
            for spec in mod.specs(tenant):
                self._specs[spec["name"]] = spec

    def names(self) -> list:
        """已开启的工具名（按发现顺序）。"""
        return [n for n in self._specs if n not in self.disabled]

    def get_spec(self, name: str) -> dict | None:
        """取一个工具的 spec（关掉的也查得到，供排障；执行时不启用）"""
        return self._specs.get(name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """租户开关：关掉 / 打开一个工具。"""
        if enabled:
            self.disabled.discard(name)
        else:
            self.disabled.add(name)

    def schemas(self) -> dict:
        """各已开启工具的参数 schema：{工具名: {"description": ...,
        "parameters": {参数名: {...}}}}（从拼出来的 LangChain 工具上取，
        和模型实际看到的 schema 同一份）。"""
        return {t.name: {"description": t.description, "parameters": t.args}
                for t in self.tools()}

    def tools(self) -> list:
        """拼出已开启的 LangChain 工具列表（给图和测试用）。"""
        return [_build_langchain_tool(self.tenant, self._specs[name])
                for name in self.names()]


def make_tools(tenant: str, disabled=None) -> list:
    """给这个租户建一套工具（16 个，名字和 P3 之前一致）。

    disabled 是按租户关掉的工具名集合（租户开关）；不传就是全开。
    """
    return ToolRegistry(tenant, disabled=disabled).tools()
