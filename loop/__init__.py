"""循环控制层入口：AgentLoopConfig / LoopHooks / AgentLoop /
decide_next / run_graph。

循环本身不知道任何具体工具和业务，只认 config 和钩子。
"""
try:
    from .config import AgentLoopConfig
    from .hooks import LoopHooks
    from .loop import AgentLoop, decide_next, run_graph
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from loop.config import AgentLoopConfig
    from loop.hooks import LoopHooks
    from loop.loop import AgentLoop, decide_next, run_graph

__all__ = ["AgentLoop", "AgentLoopConfig", "LoopHooks", "decide_next",
           "run_graph"]
