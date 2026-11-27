"""上下文层：一次请求的上下文组装。

- assembler.py：assemble_context()（读库组装：摘要+事实+最近对话+待办/
  提醒+待送达提醒，按预算截断，触发压缩）+ build_system_prompt()
  （把组装好的上下文拼成系统提示）

原来这两处各在一半：load_context 在 agent/memory.py 里读库，
_system_prompt 在 agent/graph.py 里拼串；P0 把它们合成一处，行为不变。
"""
