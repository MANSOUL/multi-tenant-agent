"""记忆与上下文的纯规则：预算、摘要策略。

从 agent/memory.py 和 agent/config.py 原样搬过来（P0 搬移，值一字不改）：
- 上下文预算：CONTEXT_MAX_TURNS / CONTEXT_MAX_CHARS / SUMMARY_THRESHOLD_TURNS
  的默认值（环境变量每次调用 load_context 时重读，见 context/assembler.py）
- token 粗估：约 4 个字符 = 1 个 token（中英文混排的经验值）

本模块不碰数据库、不碰网络，是纯函数和常量。
"""
# 上下文管理默认值（与 .env.example 一致；生产默认值见 .env.example）
DEFAULT_CONTEXT_MAX_TURNS = 10
DEFAULT_CONTEXT_MAX_CHARS = 12000
DEFAULT_SUMMARY_THRESHOLD_TURNS = 20


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：约 4 个字符 = 1 个 token（中英文混排的经验值）。"""
    return max(0, len(text or "") // 4)
