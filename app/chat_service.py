"""聊天编排（应用层）：ChatService 编排一次请求。

编排顺序：解析会话 → 注入检查 →（澄清捷径）→ 读上下文 →
调模型/工具（图）→ 落盘（图里的 save_memory）→ 记 Turn。
不认识 HTTP、SQL、LangGraph 图细节：图是 infra/graph.py 建的，
循环控制在 loop/ 里，会话在 session/ 里，落盘走 store/。

调用方向（P3 定下来的）：agent/graph.py 的 chat() 调这里，
这里调 infra/graph.py 的图——不是反过来。图只负责“怎么跑”，
“跑哪一张图、跑之前先做什么、跑完记什么”全在这里。

澄清捷径（P3 bug 1）：用户只答“早上/晚上”时，不经过模型，
直接按最近那条没说上下午的提醒请求设好——模型在这种地方
容易再问一遍“确认一下吗”，捷径把它绕过去了。找不回待澄清
的请求时走正常模型路径，绝不猜。
"""
import os
from datetime import datetime, timezone

try:
    from agent.config import MODEL, TENANT_ID
    from agent.tenant import is_own_tenant, tenant_db, tenant_dirs
    from domain.memory import estimate_tokens
    from infra.graph import (
        _extract_reply,
        build_graph,
        collect_events,
    )
    from loop.config import AgentLoopConfig
    from loop.loop import run_graph
    from model import client as client_mod
    from model.client import has_api_key
    from security.guardrails import check_injection, guardrail_note, log_injection
    from session.session import DEFAULT_SESSION_ID, SessionManager
    from store.sqlite_store import SQLiteStore
    from .reminder_service import PERIOD_WORDS, ReminderService
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import MODEL, TENANT_ID
    from agent.tenant import is_own_tenant, tenant_db, tenant_dirs
    from domain.memory import estimate_tokens
    from infra.graph import (
        _extract_reply,
        build_graph,
        collect_events,
    )
    from loop.config import AgentLoopConfig
    from loop.loop import run_graph
    from model import client as client_mod
    from model.client import has_api_key
    from security.guardrails import check_injection, guardrail_note, log_injection
    from session.session import DEFAULT_SESSION_ID, SessionManager
    from store.sqlite_store import SQLiteStore
    from app.reminder_service import PERIOD_WORDS, ReminderService

from langchain_core.messages import HumanMessage

# Turn 记录里的审批策略：权限管线 deny > ask > allow；本阶段没有
# 审批通道，ask = 记审计 + 拒绝（见 tools/permissions.py）。
APPROVAL_POLICY = "deny>ask>allow; ask=deny+审计（本阶段无审批通道）"


class ChatService:
    """一次聊天请求的编排入口。"""

    def __init__(self, session_manager=None, loop_config=None,
                 graph_builder=None):
        self.sessions = session_manager or SessionManager()
        self.loop_config = loop_config or AgentLoopConfig.from_env()
        self.reminders = ReminderService()
        self._graph_builder = graph_builder or build_graph

    # -- 主入口 ----------------------------------------------------------
    def chat(self, tenant: str, user_text: str,
             session_id: str | None = None) -> dict:
        """聊一句，返回 {"reply": 回复文本, "events": [...],
        "session_id": 会话 id}。异常行为和原来 chat() 一样：租户不对、
        没 key、模型失败都抛 RuntimeError。"""
        # 单租户容器只服务自己的租户：网关路由错了也在这里挡掉，不读别人的库。
        if TENANT_ID and not is_own_tenant(tenant):
            raise RuntimeError(
                f"这个容器只服务租户 {TENANT_ID}，不处理租户 {tenant} 的请求")
        if not has_api_key():
            raise RuntimeError("还没填模型密钥：复制 .env.example 成 .env 再填")
        started_at = datetime.now(timezone.utc).isoformat()
        session = self.sessions.ensure_session(tenant, session_id)
        sid = session["id"]
        if session["status"] == "archived":
            raise RuntimeError(f"会话 {sid} 已归档，只读，不能再发消息")

        # 注入检查：命中只记审计 + 在系统提示里标注，不直接拒绝用户消息。
        hits = check_injection(user_text)
        if hits:
            log_injection(tenant, user_text, hits)
        note = guardrail_note(hits)

        # 澄清捷径：用户只答上下午，直接设，不再问确认。
        stripped = (user_text or "").strip()
        if stripped in PERIOD_WORDS:
            reply_text = self.reminders.resolve_clarification(
                tenant, stripped)
            if reply_text is not None:
                self._append_turn_text(tenant, user_text, reply_text)
                self._record_turn(tenant, sid, user_text, reply_text,
                                  started_at)
                return {"reply": reply_text, "events": [],
                        "session_id": sid}

        graph = self.sessions.get_graph(
            tenant, sid, self._graph_builder,
            # 注意：必须每次从 model.client 模块上现读 ChatOpenAI 再取
            # id——from-import 抓到的名字是旧引用，测试换假模型时盐值
            # 不变，旧图会被误复用（已实测踩过这个坑）。
            salt=id(client_mod.ChatOpenAI))
        try:
            result = run_graph(
                graph,
                {"messages": [HumanMessage(content=user_text)],
                 "tenant_id": tenant, "steps": 0, "facts": "", "summary": "",
                 "history_text": "", "todos": "", "reminders": "",
                 "due_notifications": [], "guardrail_note": note},
                self.loop_config)
        except TimeoutError as e:
            raise RuntimeError(f"模型调用超时：{e}")
        except Exception as e:  # noqa: BLE001 - 模型接口报错时原样告诉用户
            raise RuntimeError(f"模型调用失败：{e}")
        reply = _extract_reply(result)
        self._record_turn(tenant, sid, user_text, reply, started_at)
        return {"reply": reply, "events": collect_events(result),
                "session_id": sid}

    def chat_text(self, tenant: str, user_text: str,
                  session_id: str | None = None) -> str:
        """聊一句，只返回回复文本（兼容旧 chat() 签名）。"""
        return self.chat(tenant, user_text, session_id)["reply"]

    # -- 内部：落盘 ------------------------------------------------------
    def _append_turn_text(self, tenant: str, user_text: str,
                          reply_text: str) -> None:
        """澄清捷径不走图，所以这一问一答要自己写进 chat_log，
        不然历史里会少一轮。失败不阻塞（主回复已经有了）。"""
        try:
            with tenant_db(tenant) as conn:
                store = SQLiteStore(conn)
                now = datetime.now(timezone.utc).isoformat()
                store.append_chat(now, "user", user_text)
                store.append_chat(now, "assistant", reply_text)
        except Exception:
            pass

    def _record_turn(self, tenant: str, session_id: str, user_text: str,
                     reply_text: str, started_at: str) -> None:
        """记一轮 Turn（会话 id、工作目录、审批/沙箱策略、模型、
        本轮 token、开始/结束时间）。失败不阻塞聊天。"""
        try:
            workspace, _, _ = tenant_dirs(tenant)
            tokens = estimate_tokens((user_text or "") + (reply_text or ""))
            with tenant_db(tenant) as conn:
                SQLiteStore(conn).record_turn(
                    session_id, tenant, str(workspace), APPROVAL_POLICY,
                    os.environ.get("SANDBOX_MODE", "docker"), MODEL,
                    tokens, started_at,
                    datetime.now(timezone.utc).isoformat())
        except Exception:
            pass


# -- 模块级默认实例：agent/graph.py 的 chat() 调这里 ----------------------
_DEFAULT_SERVICE: ChatService | None = None


def _default_service() -> ChatService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = ChatService()
    return _DEFAULT_SERVICE


def chat(tenant: str, user_text: str,
         session_id: str | None = None) -> str:
    """聊一句，返回回复文本（兼容旧签名；要事件时调 chat_with_events）。"""
    return chat_with_events(tenant, user_text, session_id)["reply"]


def chat_with_events(tenant: str, user_text: str,
                     session_id: str | None = None) -> dict:
    """聊一句，返回 {"reply": 回复文本, "events": [...],
    "session_id": 会话 id}（前端协议 v1.0，events 始终出现）。"""
    return _default_service().chat(tenant, user_text, session_id)
