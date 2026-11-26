"""多租户 Agent API 包。

入口见 app.py（薄入口）。各模块分工：
config（配置）、tenant（租户隔离）、memory（记忆与上下文）、
tools（10 个工具）、graph（LangGraph 图）、server（HTTP 服务）、
sandbox（每会话沙箱）。
"""
