"""多租户 Agent API 包。

入口见 app.py（薄入口）。各模块分工：
config（配置）、tenant（租户隔离）、memory（记忆与提醒，薄层）、
tools（16 个工具）、graph（LangGraph 图）、server（HTTP 服务）、
sandbox（每会话沙箱）。

P0 分层后的新顶层包（项目根目录）：
domain/（纯规则 + ports 接口）、store/（SQLite 仓储 + 事件日志）、
model/（模型客户端）、context/（上下文组装）。

P1 新增的顶层包：errors/（统一错误码）、observability/（结构化日志 +
健康检查 + 指标）。agent/ 内新增 file_server.py（/files/ 服务）和
web/chat.html（聊天页静态文件，server.py 只剩路由分发）；graph.chat()
之上加了 chat_with_events()（返回 reply + events，见
docs/FRONTEND_PROTOCOL.md）。
"""
