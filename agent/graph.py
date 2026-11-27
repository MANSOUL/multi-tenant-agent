"""LangGraph 图：recall_memory → llm → tools 循环 → save_memory。

每次请求现建一张图，tenant 只进 state 和工具闭包，不会出现在别的租户的图里。
图本身无状态（没有 checkpointer）；跨轮次的记忆靠每租户 SQLite 的
facts / chat_log / summaries / todos / reminders /
pending_notifications 表（见 memory.py）。
接地确认（2026-11-26）：凡是用户说记住/保存/提醒我/待办，必须调相应工具，
只有工具返回成功后才能说“已记下/已设好”并回显内容和时间；没调工具不许认。
改提醒时间必须调 update_reminder，改完把工具返回的时间跟用户要的时间核对
一遍，对不上换写法再试一次，两次都对不上才如实说没改成功，绝不编解释。
此外 save_memory 里还有一道确定性自动落盘（extract_explicit），就算模型
没调工具，显式指令也会被写进 facts/todos/reminders 表（歧义时间的不写）。
"""
from datetime import datetime, timezone
from typing import Annotated, TypedDict

try:
    from .config import API_KEY, MAX_TOOL_ROUNDS, TENANT_ID
    from .memory import extract_explicit, mark_notifications_delivered
    from .tenant import is_own_tenant, tenant_db
    from .tools import make_tools
    from context.assembler import assemble_context, build_system_prompt
    from model.client import create_client, invoke
    from observability import metrics as obs_metrics
    from store.sqlite_store import SQLiteStore
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import API_KEY, MAX_TOOL_ROUNDS, TENANT_ID
    from memory import extract_explicit, mark_notifications_delivered
    from tenant import is_own_tenant, tenant_db
    from tools import make_tools
    from context.assembler import assemble_context, build_system_prompt
    from model.client import create_client, invoke
    from observability import metrics as obs_metrics
    from store.sqlite_store import SQLiteStore

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tenant_id: str
    steps: int  # 已调了几轮模型，用来封顶工具轮数
    facts: str  # recall_memory 节点读出来的本租户长期记忆
    summary: str  # 较早对话的压缩摘要（summaries 表最新一条）
    history_text: str  # 最近若干轮对话原文（chat_log），已按预算截断
    todos: str  # 未完成的待办（todos 表）
    reminders: str  # 未到点/已到点的提醒（reminders 表）
    due_notifications: list  # 到点未送达的提醒（pending_notifications 表）


def _system_prompt(state: AgentState) -> str:
    """拼系统提示（P0：实现在 context/assembler.build_system_prompt，
    这里只把 state 转成 ctx 传过去；tests 还调这个函数，签名不变）。"""
    ctx = {
        "due_notifications": state.get("due_notifications"),
        "summary": state.get("summary"),
        "facts": state.get("facts"),
        "history_text": state.get("history_text"),
        "todos": state.get("todos"),
        "reminders": state.get("reminders"),
    }
    return build_system_prompt(ctx)


def build_graph(tenant: str):
    """给这个租户建图。tenant 只进 state 和工具闭包，不会出现在别的租户的图里。"""
    tools = make_tools(tenant)
    llm = create_client()
    llm_with_tools = llm.bind_tools(tools)

    def recall_memory(state: AgentState) -> dict:
        """读记忆节点：摘要 + 最近对话 + 长期事实 + 待办/提醒 + 待送达提醒，
        一次读出来供 llm 节点用。轮数超阈值时在这里做摘要/截断（见 context/assembler.assemble_context），
        只读本租户的库。"""
        ctx = assemble_context(tenant, llm=llm)
        return {"facts": ctx["facts"], "summary": ctx["summary"],
                "history_text": ctx["history_text"],
                "todos": ctx.get("todos", ""),
                "reminders": ctx.get("reminders", ""),
                "due_notifications": ctx.get("due_notifications", [])}

    def call_llm(state: AgentState) -> dict:
        """调模型节点：带上系统提示（含摘要/记忆/最近对话）+ 对话，返回一条 AIMessage。"""
        obs_metrics.inc(obs_metrics.MODEL_CALLS)
        reply = invoke(
            llm_with_tools,
            [SystemMessage(content=_system_prompt(state))]
            + list(state["messages"]),
        )
        return {"messages": [reply], "steps": state["steps"] + 1}

    def final_llm(state: AgentState) -> dict:
        """最后一轮：工具轮数已到上限，这次不带工具，强制模型直接回答。"""
        obs_metrics.inc(obs_metrics.MODEL_CALLS)
        reply = invoke(
            llm,
            [SystemMessage(content=_system_prompt(state))]
            + list(state["messages"]),
        )
        return {"messages": [reply], "steps": state["steps"] + 1}

    def save_memory(state: AgentState) -> dict:
        """存记忆节点：把本轮的一问一答记到该租户的 chat_log 表里；同时
        对用户消息跑确定性自动落盘（extract_explicit），把显式指令写进
        facts/todos/reminders 表——就算模型没调工具，指令也不会丢。
        抽取本身静默失败（不报错、不阻塞）；最后把本轮拼进系统提示的
        待送达提醒标记为已送达（delivered=1），同时把对应的提醒行从 due
        翻成 notified——不翻的话，提醒行会一直留在提醒列表里，模型每轮
        都看到、每轮都再说一遍（Bug 4 实测：第二轮聊天又出现一次）。"""
        user_text, ai_text = "", ""
        for msg in state["messages"]:
            if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
                user_text = msg.content
            if isinstance(msg, AIMessage) and isinstance(msg.content, str) \
                    and msg.content.strip() and not msg.tool_calls:
                ai_text = msg.content
        if user_text:
            try:
                extract_explicit(user_text, tenant)
            except Exception:
                pass  # 自动落盘绝不阻塞聊天
        if user_text or ai_text:
            with tenant_db(tenant) as conn:
                store = SQLiteStore(conn)
                now = datetime.now(timezone.utc).isoformat()
                if user_text:
                    store.append_chat(now, "user", user_text)
                if ai_text:
                    store.append_chat(now, "assistant", ai_text)
        try:
            mark_notifications_delivered(
                tenant, [n["id"] for n in state.get("due_notifications") or []])
        except Exception:
            pass
        return {}

    def route(state: AgentState) -> str:
        """llm 节点之后的分支：有工具调用且没到上限 → tools；
        有工具调用但已到上限 → final_llm；否则 → save_memory。"""
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools" if state["steps"] < MAX_TOOL_ROUNDS else "final_llm"
        return "save_memory"

    graph = StateGraph(AgentState)
    graph.add_node("recall_memory", recall_memory)
    graph.add_node("llm", call_llm)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("final_llm", final_llm)
    graph.add_node("save_memory", save_memory)
    graph.add_edge(START, "recall_memory")
    graph.add_edge("recall_memory", "llm")
    graph.add_conditional_edges("llm", route,
                                {"tools": "tools",
                                 "final_llm": "final_llm",
                                 "save_memory": "save_memory"})
    graph.add_edge("tools", "llm")
    graph.add_edge("final_llm", "save_memory")
    graph.add_edge("save_memory", END)
    # 图本身无状态（没有 checkpointer），每次请求现建图；
    # 跨轮次的记忆靠每租户 SQLite 的 facts / chat_log 表。
    return graph.compile()


def collect_events(result: dict) -> list:
    """从一次图执行结果里收集前端事件（纯函数，见 docs/FRONTEND_PROTOCOL.md）。

    顺序：先是本轮送达的提醒（recall_memory 时读到的待送达提醒，
    save_memory 已把它们标为已送达），再是本轮的工具调用（按消息顺序）。
    P1 只产这两种事件；"chat" 类型已在协议里占位，本阶段不发。
    result 是 graph.invoke 的返回：messages 里带 tool_calls 的 AIMessage、
    due_notifications 是 recall_memory 读到的待送达提醒列表。
    """
    events = []
    for n in result.get("due_notifications") or []:
        events.append({
            "type": "reminder_delivered",
            "notification_id": n.get("id"),
            "text": n.get("text"),
            "due_at": n.get("due_at"),
        })
    for msg in result.get("messages") or []:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                events.append({
                    "type": "tool_call",
                    "name": tc.get("name"),
                    "args": tc.get("args", {}),
                })
    return events


def _extract_reply(result: dict) -> str:
    """从图执行结果里取最后一条有文字的 AI 回复（原来 chat() 里的逻辑）。"""
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) \
                and msg.content.strip():
            return msg.content
        if isinstance(msg, AIMessage) and isinstance(msg.content, list):
            text = "".join(
                part.get("text", "") for part in msg.content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            if text:
                return text
    return "（模型没有返回文字回答）"


def chat(tenant: str, user_text: str) -> str:
    """聊一句，返回回复文本（兼容旧签名；要事件时调 chat_with_events）。"""
    return chat_with_events(tenant, user_text)["reply"]


def chat_with_events(tenant: str, user_text: str) -> dict:
    """聊一句，返回 {"reply": 回复文本, "events": [...]}（前端协议 v1.0）。

    events 始终出现，没有事件时是空数组。事件由 collect_events() 从
    图执行结果里收集：本轮的工具调用（tool_call）+ 本轮送达的提醒
    （reminder_delivered）。异常行为和 chat() 原来一样：租户不对、
    没 key、模型失败都抛 RuntimeError。
    """
    # 单租户容器只服务自己的租户：网关路由错了也在这里挡掉，不读别人的库。
    if TENANT_ID and not is_own_tenant(tenant):
        raise RuntimeError(
            f"这个容器只服务租户 {TENANT_ID}，不处理租户 {tenant} 的请求")
    if not API_KEY:
        raise RuntimeError("还没填 LLM_API_KEY：复制 .env.example 成 .env 再填")
    graph = build_graph(tenant)
    try:
        result = graph.invoke(
            {"messages": [HumanMessage(content=user_text)],
             "tenant_id": tenant, "steps": 0, "facts": "", "summary": "",
             "history_text": "", "todos": "", "reminders": "",
             "due_notifications": []},
            config={"recursion_limit": 30},
        )
    except Exception as e:  # noqa: BLE001 - 模型接口报错时原样告诉用户
        raise RuntimeError(f"模型调用失败：{e}")
    return {"reply": _extract_reply(result),
            "events": collect_events(result)}
