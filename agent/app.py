"""多租户 Agent API 入口（薄入口，只做转发）。

真正的代码在同目录的分模块里：
  config.py — 环境变量和常量
  tenant.py — 租户隔离（路径/数据库/token）
  memory.py — 记忆与上下文（facts/chat_log/summaries）
  tools.py  — 10 个工具
  graph.py  — LangGraph 图与 chat()
  server.py — HTTP 服务
  sandbox.py — 每会话沙箱

启动方式不变：python3 agent/app.py （从项目根目录）
"""
try:
    from .server import main
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from server import main

if __name__ == "__main__":
    main()
