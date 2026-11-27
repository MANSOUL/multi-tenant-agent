"""工具执行器（工具层）：参数校验 → 权限 → 执行 → 审计日志。

这是工具调用的唯一检查点：LangGraph 的 tools 节点（infra/graph.py）
和直接调工具的测试都走这里，不存在绕过权限的第二条路。
- 参数校验：按注册表里的 schema 查必填参数，缺了直接返回错误
  字符串（不抛异常——工具结果是给模型看的文本，不是异常）。
- 权限：deny → 记审计 + 返回“权限拒绝”；ask → 记审计 + 返回
  “需要确认”，本阶段没有审批通道，绝不放行；allow → 执行。
- 执行：handler 抛异常时返回错误字符串，同样记审计。
- 审计：每一次调用（放行、拒绝、失败）都记一笔。
"""
try:
    from .permissions import (
        ALLOW,
        ASK,
        DENY,
        check_permission,
        log_audit,
    )
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.permissions import (
        ALLOW,
        ASK,
        DENY,
        check_permission,
        log_audit,
    )


def execute_tool(tenant: str, tool_name: str, args: dict,
                 handler, required_params: tuple = ()) -> str:
    """执行一次工具调用（参数校验 → 权限 → 执行 → 审计日志）。

    handler 是 (args: dict) -> str 的可调用对象；required_params 是
    必填参数名。返回给模型看的字符串结果（拒绝/失败也是字符串，
    不抛异常）。
    """
    args = dict(args or {})
    missing = [p for p in required_params if p not in args
               or args[p] is None]
    if missing:
        reason = f"参数缺失：{', '.join(missing)}"
        log_audit(tenant, tool_name, "validation_error", reason, args)
        return f"调用 {tool_name} 失败：{reason}"
    decision, reason = check_permission(tool_name, tenant, args)
    if decision == DENY:
        log_audit(tenant, tool_name, DENY, reason, args)
        return f"权限拒绝：{reason}"
    if decision == ASK:
        log_audit(tenant, tool_name, "ask_denied", reason, args)
        return (f"需要确认：{reason}。"
                "请先向用户说明要做什么，等用户明确同意后再试。")
    try:
        result = handler(args)
    except Exception as e:  # noqa: BLE001 - 工具异常变成文本，不炸掉整轮
        log_audit(tenant, tool_name, "error", str(e), args)
        return f"调用 {tool_name} 失败：{e}"
    log_audit(tenant, tool_name, ALLOW, "", args)
    return result if isinstance(result, str) else str(result)
