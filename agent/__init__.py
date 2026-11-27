"""多租户 Agent API 包。

入口见 app.py（薄入口）。各模块分工：
config（配置）、tenant（租户隔离）、memory（记忆与提醒，薄层）、
tools（16 个工具）、graph（LangGraph 图）、server（HTTP 服务）、
sandbox（每会话沙箱）。

P0 分层后的新顶层包（项目根目录）：
domain/（纯规则 + ports 接口）、store/（SQLite 仓储 + 事件日志）、
model/（模型客户端）、context/（上下文组装）。
"""
