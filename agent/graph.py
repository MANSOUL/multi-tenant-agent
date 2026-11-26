"""LangGraph 图：recall_memory → llm → tools 循环 → save_memory。

每次请求现建一张图，tenant 只进 state 和工具闭包，不会出现在别的租户的图里。
图本身无状态（没有 checkpointer）；跨轮次的记忆靠每租户 SQLite 的
facts / chat_log / summaries / todos / reminders /
pending_notifications 表（见 memory.py）。
接地确认（2026-11-26）：凡是用户说记住/保存/提醒我/待办，必须调相应工具，
只有工具返回成功后才能说“已记下/已设好”并回显内容和时间；没调工具不许认。
此外 save_memory 里还有一道确定性自动落盘（extract_explicit），就算模型
没调工具，显式指令也会被写进 facts/todos/reminders 表。
"""
from datetime import datetime, timezone
from typing import Annotated, TypedDict

try:
    from .config import (
        API_KEY,
        BASE_URL,
        MAX_TOOL_ROUNDS,
        MODEL,
        SYSTEM_PROMPT,
        TENANT_ID,
        TOOLS_DOC,
    )
    from .memory import (
        extract_explicit,
        load_context,
        mark_notifications_delivered,
    )
    from .tenant import is_own_tenant, tenant_db
    from .tools import make_tools
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from config import (
        API_KEY,
        BASE_URL,
        MAX_TOOL_ROUNDS,
        MODEL,
        SYSTEM_PROMPT,
        TENANT_ID,
        TOOLS_DOC,
    )
    from memory import (
        extract_explicit,
        load_context,
        mark_notifications_delivered,
    )
    from tenant import is_own_tenant, tenant_db
    from tools import make_tools

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
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
    """拼系统提示：待送达提醒 → 摘要 → 长期事实 → 最近对话 → 待办/提醒 → 工具说明。
    待送达提醒放最前面，模型先看到、先当面提醒用户。
    系统提示和最新用户消息永远不截断，只截旧历史/旧事实。"""
    system = SYSTEM_PROMPT + "\n" + TOOLS_DOC
    for n in state.get("due_notifications") or []:
        system += (f"\n\n【待提醒】{n['text']}"
                   f"（应提醒时间 {n['due_at']}）：先把这条当面告诉用户。")
    if state.get("summary"):
        system += f"\n\n较早对话的摘要：\n{state['summary']}"
    if state.get("facts"):
        system += f"\n\n你记得的关于这个用户的事：\n{state['facts']}"
    if state.get("history_text"):
        system += f"\n\n最近的对话：\n{state['history_text']}"
    if state.get("todos"):
        system += f"\n\n未完成的待办：\n{state['todos']}"
    if state.get("reminders"):
        system += f"\n\n提醒：\n{state['reminders']}"
    return system


def build_graph(tenant: str):
    """给这个租户建图。tenant 只进 state 和工具闭包，不会出现在别的租户的图里。"""
    tools = make_tools(tenant)
    llm = ChatOpenAI(
        model=MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0.7
    )
    llm_with_tools = llm.bind_tools(tools)

    def recall_memory(state: AgentState) -> dict:
        """读记忆节点：摘要 + 最近对话 + 长期事实 + 待办/提醒 + 待送达提醒，
        一次读出来供 llm 节点用。轮数超阈值时在这里做摘要/截断（见 load_context），
        只读本租户的库。"""
        ctx = load_context(tenant, llm=llm)
        return {"facts": ctx["facts"], "summary": ctx["summary"],
                "history_text": ctx["history_text"],
                "todos": ctx.get("todos", ""),
                "reminders": ctx.get("reminders", ""),
                "due_notifications": ctx.get("due_notifications", [])}

    def call_llm(state: AgentState) -> dict:
        """调模型节点：带上系统提示（含摘要/记忆/最近对话）+ 对话，返回一条 AIMessage。"""
        reply = llm_with_tools.invoke(
            [SystemMessage(content=_system_prompt(state))]
            + list(state["messages"])
        )
        return {"messages": [reply], "steps": state["steps"] + 1}

    def final_llm(state: AgentState) -> dict:
        """最后一轮：工具轮数已到上限，这次不带工具，强制模型直接回答。"""
        reply = llm.invoke(
            [SystemMessage(content=_system_prompt(state))]
            + list(state["messages"])
        )
        return {"messages": [reply], "steps": state["steps"] + 1}

    def save_memory(state: AgentState) -> dict:
        """存记忆节点：把本轮的一问一答记到该租户的 chat_log 表里；同时
        对用户消息跑确定性自动落盘（extract_explicit），把显式指令写进
        facts/todos/reminders 表——就算模型没调工具，指令也不会丢。
        抽取本身静默失败（不报错、不阻塞）；最后把本轮拼进系统提示的
        待送达提醒标记为已送达（delivered=1），下次不再重复提醒。"""
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
                now = datetime.now(timezone.utc).isoformat()
                if user_text:
                    conn.execute("INSERT INTO chat_log VALUES (?, ?, ?)",
                                 (now, "user", user_text))
                if ai_text:
                    conn.execute("INSERT INTO chat_log VALUES (?, ?, ?)",
                                 (now, "assistant", ai_text))
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


def chat(tenant: str, user_text: str) -> str:
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
