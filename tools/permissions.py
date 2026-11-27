"""工具权限管线（工具层）：deny > ask > allow，默认拒绝。

沙箱（容器隔离）和审批（要不要问用户）是两个独立的轴（见
ARCHITECTURE.md 第五节）：沙箱管“命令在哪里跑”，这里只管
“这个工具调用让不让跑”。
- deny：未知工具、或明确禁用的工具，一律拒绝。
- ask：写到工作区之外的路径、以及将来需要人工确认的工具——
  本阶段没有审批通道，所以 ask 的行为 = 记一条审计日志 + 拒绝
  执行并返回“需要确认”的错误，绝不偷偷放行。
- allow：读类工具（读文件、列目录、搜文件、读记忆、列待办/提醒）
  和租户内数据库写操作（记事实、加待办、设提醒——这是产品的
  核心功能，且只写本租户的库；等有了审批通道再考虑要不要收紧）；
  以及**工作区内的写文件**和**沙箱内执行命令**：这两者的破坏半径
  都是本租户自己的工作区——写文件有 safe_path 二次 confinement，
  执行命令跑在 --read-only、--network none、--cap-drop ALL 的沙箱
  容器里，只能动工作区文件。P3 曾把它们一律划为 ask（无审批通道
  = 拒绝），结果做网页（/files/）功能整个不可用，是回归；
  2026-11-27 修正为按路径/沙箱放行，越出工作区的写操作仍走 ask。

每个工具调用执行前都过 check_permission（检查点在
tools/executor.py），默认策略是 deny——注册表里没有的工具名
直接拒绝。
"""
import logging
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# 默认策略：没写进来的工具名一律 deny（未知工具拒绝）。
DEFAULT_POLICIES = {
    # 读类：allow。
    "read_file": ALLOW,
    "list_workspace": ALLOW,
    "search_file": ALLOW,
    "remember_fact": ALLOW,
    "recall_fact": ALLOW,
    "search_facts": ALLOW,
    "list_facts": ALLOW,
    "list_todos": ALLOW,
    "list_reminders": ALLOW,
    "browse_page": ALLOW,
    # 租户内数据库写：allow（核心功能，只写本租户的库）。
    "add_todo": ALLOW,
    "complete_todo": ALLOW,
    "add_reminder": ALLOW,
    "update_reminder": ALLOW,
    # 写文件 / 执行命令：路径在工作区内、或沙箱内执行时 allow；
    # 写到工作区之外时 ask（无审批通道 = 审计 + 拒绝），见 check_permission。
    "write_file": ASK,
    "run_command": ALLOW,
}

# 审计日志：进程内列表（测试可读；生产上同时记 logging，见 log_audit）。
# ask/deny 的拒绝必须留痕——“拒绝了但没人知道”和“放行了”一样糟。
AUDIT_LOG: list = []


def _is_workspace_path(path: str) -> bool:
    """判断一个写文件路径是否在租户工作区内（相对路径、不含 ..）。

    这是权限层的第一道检查；真正执行时 safe_path 还会再拦一次
    （defense in depth）。绝对路径、含 .. 的路径一律不算工作区内。
    """
    if not path or not isinstance(path, str):
        return False
    if os.path.isabs(path):
        return False
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return ".." not in parts


def check_permission(tool_name: str, tenant: str = "",
                     args: dict | None = None) -> tuple[str, str]:
    """查一个工具调用的权限决定。返回 (决定, 原因)。

    决定是 allow / ask / deny 之一；未知工具名直接 deny。
    本函数只做决定，不记日志、不执行——记日志是 executor 的事。

    write_file 是唯一的“按参数定”的工具：路径在工作区内 allow，
    越出工作区 ask（无审批通道 = 拒绝）；run_command 在沙箱内跑，
    破坏半径与写文件相同，直接 allow。
    """
    policy = DEFAULT_POLICIES.get(tool_name)
    if policy is None:
        return DENY, f"未知工具：{tool_name}（默认拒绝）"
    if tool_name == "write_file":
        if _is_workspace_path((args or {}).get("path", "")):
            return ALLOW, ""
        return ASK, (f"工具 {tool_name} 要写到工作区之外，"
                     "需要用户确认，本阶段没有审批通道，已拒绝执行")
    if policy == ASK:
        return ASK, (f"工具 {tool_name} 需要用户确认，"
                     "本阶段没有审批通道，已拒绝执行")
    return ALLOW, ""


def log_audit(tenant: str, tool_name: str, decision: str,
              reason: str = "", args: dict | None = None) -> dict:
    """记一条审计日志（进程内列表 + logging），返回这条记录。"""
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "tenant": tenant, "tool": tool_name, "decision": decision,
             "reason": reason,
             "args": {k: str(v)[:200] for k, v in (args or {}).items()}}
    AUDIT_LOG.append(entry)
    logger.info("tool_audit tenant=%s tool=%s decision=%s reason=%s",
                tenant, tool_name, decision, reason)
    return entry


def audit_log() -> list:
    """返回审计日志的副本（测试读这个）。"""
    return list(AUDIT_LOG)


def clear_audit_log() -> None:
    """清空审计日志（测试 setup 用）。"""
    AUDIT_LOG.clear()
