"""循环配置（循环控制层）：AgentLoopConfig。

照 pi-agent 的做法：循环本身不知道任何具体工具和业务，只认这一个
config 对象。原来埋在 agent/graph.py 里的 MAX_TOOL_ROUNDS、
recursion_limit 30 全部搬到这里；超时和重试策略原来根本没有，
P3 新加（默认值见 from_env）。
"""
import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass
class AgentLoopConfig:
    """一轮对话循环的控制参数。

    max_tool_rounds：工具轮数上限，到上限强制收尾（不再带工具调模型）。
    recursion_limit：LangGraph 图的递归上限（原来写死 30）。
    timeout_seconds：整轮超时，超时不挂死，直接报 TimeoutError。
    retry_attempts：模型调用失败时的重试次数（只重试模型调用本身，
      不重试整张图——整图重跑会把已执行的工具再跑一遍）。
    retry_backoff_seconds：重试前等几秒。
    """
    max_tool_rounds: int = 3
    recursion_limit: int = 30
    timeout_seconds: float = 120.0
    retry_attempts: int = 0
    retry_backoff_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "AgentLoopConfig":
        """从环境变量读配置（没配就用默认值；测试可直传数值）。"""
        return cls(
            max_tool_rounds=_int_env("MAX_TOOL_ROUNDS", 3),
            recursion_limit=_int_env("LOOP_RECURSION_LIMIT", 30),
            timeout_seconds=_float_env("LOOP_TIMEOUT_SECONDS", 120.0),
            retry_attempts=_int_env("LOOP_RETRY_ATTEMPTS", 0),
            retry_backoff_seconds=_float_env("LOOP_RETRY_BACKOFF_SECONDS",
                                             1.0),
        )
