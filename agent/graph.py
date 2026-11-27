"""LangGraph 图（兼容薄层，P3）：原来 220 行的 agent/graph.py 已拆走。

- 图接线（节点 + 边）：infra/graph.py 的 build_graph
- 循环控制（路由、轮数上限、递归上限、超时）：loop/loop.py +
  loop/config.py，路由判断是 loop.decide_next
- 编排（会话 → 注入检查 → 调图 → 记 Turn）：app/chat_service.py
  的 ChatService；调用方向是 chat() 调 ChatService，ChatService
  调图，不是反过来
- 上下文拼装：context/assembler.py

本文件只剩兼容导出：tests 和旧调用方 import 的 chat /
chat_with_events / build_graph / collect_events / _system_prompt /
_system_prompt 用的 _extract_reply 还在这里找得到，行为和拆
之前一致。图实例按会话复用（session/session.py），不再每次
现建图——复用逻辑在 ChatService 里，不在这里。
"""
try:
    from app.chat_service import chat, chat_with_events
    from infra.graph import (
        AgentState,
        _extract_reply,
        _system_prompt,
        build_graph,
        collect_events,
    )
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.chat_service import chat, chat_with_events
    from infra.graph import (
        AgentState,
        _extract_reply,
        _system_prompt,
        build_graph,
        collect_events,
    )

__all__ = ["AgentState", "_extract_reply", "_system_prompt", "build_graph",
           "chat", "chat_with_events", "collect_events"]
