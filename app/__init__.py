"""应用层入口：ChatService / ReminderService / TodoService。

编排一次请求（读上下文 → 调模型/工具 → 落盘），不认识 HTTP、
SQL、LangGraph 图细节。

注意：本文件故意不做 eager import——tools/definitions/* 要
import app.reminder_service / app.todo_service，而 app.chat_service
又要 import infra.graph（它 import tools.registry），__init__
里一旦先 import chat_service 就会成环。调用方直接
`from app.chat_service import ChatService` 这样按子模块 import。
"""
