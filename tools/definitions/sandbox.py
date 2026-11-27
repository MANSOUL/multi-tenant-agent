"""沙箱工具定义（工具层）：run_command。

只做参数校验 + 调 agent/sandbox.py 的 run_command，不再自己写业务
逻辑；执行前过 tools/executor.py 的权限检查——run_command 默认
是 ask（没有审批通道时拒绝执行并记审计，见 tools/permissions.py），
所以模型通过工具跑命令时会被挡下来，这是 P3 权限管线有意的
行为（见汇报“取舍”）。
"""
import inspect

try:
    from agent.sandbox import run_command as sandbox_run
    from agent.tenant import tenant_dirs
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.sandbox import run_command as sandbox_run
    from agent.tenant import tenant_dirs


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""

    def _run(args):
        workspace, _, _ = tenant_dirs(tenant)
        out, mode = sandbox_run(args["command"], str(workspace))
        if mode == "local":
            return f"[本地回退：未隔离，仅开发用]\n{out}"
        if mode == "docker":
            return f"[沙箱容器]\n{out}"
        return out

    return [
        {"name": "run_command",
         "description": "在沙箱里跑一条白名单只读命令。可用：ls、pwd、date、df、free、uptime、whoami、git。",
         "params": [("command", inspect.Parameter.empty, str)],
         "required": ("command",),
         "handler": _run},
    ]
