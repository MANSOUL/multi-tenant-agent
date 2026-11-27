"""工具权限管线（工具层）：deny > ask > allow，默认拒绝。

沙箱（容器隔离）和审批（要不要问用户）是两个独立的轴（见
ARCHITECTURE.md 第五节）：沙箱管“命令在哪里跑”，这里只管
“这个工具调用让不让跑”。
- deny：未知工具、或明确禁用的工具，一律拒绝。
- ask：写文件 / 执行命令类工具（write_file、run_command）——
  本阶段没有审批通道，所以 ask 的行为 = 记一条审计日志 + 拒绝
  执行并返回“需要确认”的错误，绝不偷偷放行。
- allow：读类工具（读文件、列目录、搜文件、读记忆、列待办/提醒）
  和租户内数据库写操作（记事实、加待办、设提醒——这是产品的
  核心功能，且只写本租户的库；等有了审批通道再考虑要不要收紧）。

每个工具调用执行前都过 check_permission（检查点在
tools/executor.py），默认策略是 deny——注册表里没有的工具名
直接拒绝。
"""
import logging
from datetime import datetime, timezone

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
    # 写文件 / 执行命令：ask——本阶段没有审批通道，ask = 审计 + 拒绝。
    "write_file": ASK,
    "run_command": ASK,
}

# 审计日志：进程内列表（测试可读；生产上同时记 logging，见 log_audit）。
# ask/deny 的拒绝必须留痕——“拒绝了但没人知道”和“放行了”一样糟。
AUDIT_LOG: list = []


def check_permission(tool_name: str, tenant: str = "",
                     args: dict | None = None) -> tuple[str, str]:
    """查一个工具调用的权限决定。返回 (决定, 原因)。

    决定是 allow / ask / deny 之一；未知工具名直接 deny。
    本函数只做决定，不记日志、不执行——记日志是 executor 的事。
    """
    policy = DEFAULT_POLICIES.get(tool_name)
    if policy is None:
        return DENY, f"未知工具：{tool_name}（默认拒绝）"
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
