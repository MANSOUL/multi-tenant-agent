"""LangGraph 图接线（基础设施层）：只剩节点接线，不含控制逻辑。

节点：recall_memory → llm → tools → llm … → final_llm → save_memory。
控制逻辑（轮数上限、递归上限、超时、路由判断）全在 loop/ 里：
路由判断是 loop/loop.py 的 decide_next，这里只把它挂到
add_conditional_edges 上；轮数上限和递归上限在 loop/config.py
的 AgentLoopConfig 里；超时在 loop/loop.py 的 run_graph 里。
模型调用走 model/client.py（不直接 new ChatOpenAI），工具执行
走 tools/executor.py（参数校验 → 权限 → 执行 → 审计日志），
钩子（beforeLLM / afterLLM / beforeToolCall / afterToolCall）
从 loop/hooks.py 传进来，默认空实现。
"""
from datetime import datetime, timezone
from typing import Annotated, TypedDict

try:
    from agent.config import MODEL, TENANT_ID
    from agent.memory import extract_explicit, mark_notifications_delivered
    from agent.tenant import checkpoint_db_path, tenant_db
    from context.assembler import (
        assemble_context, build_system_prompt,
        messages_cover_history)
    from infra import checkpoints as checkpoints_mod
    from loop.config import AgentLoopConfig
    from loop.hooks import LoopHooks
    from loop.loop import decide_next
    from model.client import create_client, invoke
    from model.metering import extract_usage
    from model.metering import extract_usage
    from observability import metrics as obs_metrics
    from store import create_store
    from tools.permissions import DENY, log_audit
    from tools.registry import make_tools
except ImportError:  # 以脚本方式跑时项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import MODEL, TENANT_ID
    from agent.memory import extract_explicit, mark_notifications_delivered
    from agent.tenant import checkpoint_db_path, tenant_db
    from context.assembler import (
        assemble_context, build_system_prompt,
        messages_cover_history)
    from infra import checkpoints as checkpoints_mod
    from loop.config import AgentLoopConfig
    from loop.hooks import LoopHooks
    from loop.loop import decide_next
    from model.client import create_client, invoke
    from model.metering import extract_usage
    from model.metering import extract_usage
    from observability import metrics as obs_metrics
    from store import create_store
    from tools.permissions import DENY, log_audit
    from tools.registry import make_tools

from langchain_core.messages import (
    AIMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tenant_id: str
    steps: int  # 已调了几轮模型，用来封顶工具轮数（判断在 loop 里）
    facts: str  # recall_memory 节点读出来的本租户长期记忆
    summary: str  # 较早对话的压缩摘要（summaries 表最新一条）
    history_text: str  # 最近若干轮对话原文（chat_log），已按预算截断
    todos: str  # 未完成的待办（todos 表）
    reminders: str  # 未到点/已到点的提醒（reminders 表）
    due_notifications: list  # 到点未送达的提醒（pending_notifications 表）
    guardrail_note: str  # 注入检查命中时系统提示里的标注（没命中为空串）


def _system_prompt(state: AgentState) -> str:
    """拼系统提示（实现在 context/assembler.build_system_prompt，
    这里只把 state 转成 ctx 传过去；注入标注有就追加在最后）。

    去重：消息列表里已经带了往轮历史时（checkpointer 下的常规
    情况），系统提示里不再拼同一段历史文本——同一段历史发两遍
    是 2026-11-27 实测 36K 输入 token 的大头之一。新会话、没开
    checkpointer 时消息列表里只有本轮，历史仍走系统提示，不丢。
    """
    ctx = {
        "due_notifications": state.get("due_notifications"),
        "summary": state.get("summary"),
        "facts": state.get("facts"),
        "history_text": state.get("history_text"),
        "todos": state.get("todos"),
        "reminders": state.get("reminders"),
    }
    prompt = build_system_prompt(
        ctx, include_history=not messages_cover_history(
            state.get("messages")) )
    note = state.get("guardrail_note") or ""
    if note:
        prompt += "\n\n" + note
    return prompt


def _record_metering(tenant: str, input_text: str, reply) -> None:
    """记一笔模型调用计量（失败不阻塞聊天）。

    有真实用量（reply.usage_metadata，P6 起 ChatDeepSeek +
    stream_usage 保证有）就记真实数 + 按价目表算成本；没有就
    退回按文本粗估（假模型、老客户端）。
    """
    try:
        usage = extract_usage(reply)
        output_text = reply.content if isinstance(reply.content, str) \
            else "".join(p.get("text", "") for p in reply.content
                         if isinstance(p, dict) and p.get("type") == "text")
        with tenant_db(tenant) as conn:
            create_store(conn).record_metering(
                tenant, MODEL, input_text, output_text, usage=usage)
    except Exception:
        pass


def build_graph(tenant: str, loop_config: AgentLoopConfig | None = None,
                hooks: LoopHooks | None = None, checkpointer=None):
    """给这个租户建图。tenant 只进 state 和工具闭包，不会出现在别的租户的图里。

    loop_config 不传就用环境变量默认值（AgentLoopConfig.from_env）；
    hooks 不传就是空实现。
    checkpointer 不传就按租户取默认的 SqliteSaver（每租户一个
    checkpoint.db，见 infra/checkpoints.py；CHECKPOINTER_ENABLED=0
    或依赖缺失时返回 None，图回到无 checkpointer 的老行为）。
    有 checkpointer 时，调用方必须在 invoke 的 config 里传
    configurable.thread_id（会话 id，见 loop/loop.py 的 run_graph），
    否则 invoke 直接报 ValueError（langgraph 的要求：带 checkpointer
    的图必须带 thread_id / checkpoint_ns / checkpoint_id 之一）；
    目前唯一的生产调用方 app/chat_service.py 每次都传 sid。
    """
    config = loop_config or AgentLoopConfig.from_env()
    hooks = hooks or LoopHooks()
    tools = make_tools(tenant)
    tools_by_name = {t.name: t for t in tools}
    # 客户端默认就是 streaming + stream_usage（见 model/client.py）：
    # invoke() 仍返回完整消息，同时 graph.stream (stream_mode=
    # "messages") 能拿到逐 token 增量和最后一个 chunk 的真实用量；
    # 流式和非流式两条路共用这张图。
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
        system = _system_prompt(state)
        messages = [SystemMessage(content=system)] + list(state["messages"])
        hooks.fire_before_llm(messages)
        obs_metrics.inc(obs_metrics.MODEL_CALLS)
        reply = invoke(llm_with_tools, messages)
        reply = hooks.fire_after_llm(reply)
        _record_metering(tenant, system + "\n" + "\n".join(
            str(getattr(m, "content", "")) for m in state["messages"]), reply)
        return {"messages": [reply], "steps": state["steps"] + 1}

    def final_llm(state: AgentState) -> dict:
        """最后一轮：工具轮数已到上限，这次不带工具，强制模型直接回答。"""
        system = _system_prompt(state)
        messages = [SystemMessage(content=system)] + list(state["messages"])
        hooks.fire_before_llm(messages)
        obs_metrics.inc(obs_metrics.MODEL_CALLS)
        reply = invoke(llm, messages)
        reply = hooks.fire_after_llm(reply)
        _record_metering(tenant, system + "\n" + "\n".join(
            str(getattr(m, "content", "")) for m in state["messages"]), reply)
        return {"messages": [reply], "steps": state["steps"] + 1}

    def call_tools(state: AgentState) -> dict:
        """工具节点：逐个执行最后一条 AIMessage 里的工具调用。

        原来是 langchain 的 ToolNode；P3 改成自己的节点，只为了让
        钩子（beforeToolCall 可拦截、afterToolCall 可改写）和权限
        检查点（tools/executor.py）真正生效：执行走各工具底层的
        func（里面已经包了一层 executor：参数校验 → 权限 → 执行 →
        审计），这里不再重复调一次 executor，避免审计记两遍。
        未知工具名直接返回权限拒绝（默认拒绝），不执行。
        """
        last = state["messages"][-1]
        out_messages = []
        for tc in (last.tool_calls or []):
            name = tc.get("name", "")
            args = tc.get("args") or {}
            intercepted = hooks.fire_before_tool_call(name, args)
            if intercepted is not None:
                result = intercepted
            else:
                tool_obj = tools_by_name.get(name)
                if tool_obj is None:
                    reason = f"未知工具：{name}（默认拒绝）"
                    log_audit(tenant, name, DENY, reason, args)
                    result = f"权限拒绝：{reason}"
                else:
                    try:
                        func = getattr(tool_obj, "func", None)
                        if func is not None:
                            result = func(**args)
                        else:
                            result = tool_obj.invoke(args)
                    except Exception as e:  # noqa: BLE001 - 工具异常变文本
                        result = f"调用 {name} 失败：{e}"
            result = hooks.fire_after_tool_call(name, args, result)
            out_messages.append(ToolMessage(
                content=result if isinstance(result, str) else str(result),
                tool_call_id=tc.get("id", "")))
        return {"messages": out_messages}

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
                store = create_store(conn)
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
        """路由判断在 loop/loop.py 的 decide_next 里，这里只转发
        （图里不留任何控制逻辑）。"""
        return decide_next(state, config)

    graph = StateGraph(AgentState)
    graph.add_node("recall_memory", recall_memory)
    graph.add_node("llm", call_llm)
    graph.add_node("tools", call_tools)
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
    # P4：图挂上 checkpointer（默认按租户取 SqliteSaver，见上面
    # build_graph 的说明）。跨轮次的记忆仍有两份：图内多轮消息状态
    # 在 checkpoint.db 里，摘要 + 长期事实等派生上下文还在每租户
    # SQLite 的 facts / chat_log / summaries / todos / reminders /
    # pending_notifications 表里（P0 的 event_log 不动）。
    # P3：图实例按会话复用，复用逻辑在 session/session.py，
    # 这里只负责建图。
    if checkpointer is None:
        try:
            checkpointer = checkpoints_mod.get_checkpointer(
                checkpoint_db_path(tenant))
        except Exception:  # noqa: BLE001 - checkpointer 建不出来就无状态跑
            checkpointer = None
    return graph.compile(checkpointer=checkpointer)


def collect_events(result: dict, since_index: int | None = None) -> list:
    """从一次图执行结果里收集前端事件（纯函数，见 docs/FRONTEND_PROTOCOL.md）。

    顺序：先是本轮送达的提醒（recall_memory 时读到的待送达提醒，
    save_memory 已把它们标为已送达），再是本轮的工具调用（按消息顺序）。
    P1 只产这两种事件；"chat" 类型已在协议里占位，本阶段不发。
    result 是 graph.invoke 的返回：messages 里带 tool_calls 的 AIMessage、
    due_notifications 是 recall_memory 读到的待送达提醒列表。
    since_index：有 checkpointer 时，result["messages"] 里带了本会话
    全部历史消息，只取下标 >= since_index 的那一段（本轮新产生的），
    不然第二轮会把第一轮的工具调用事件再推一遍；不传就是全量（老行为）。
    """
    events = []
    for n in result.get("due_notifications") or []:
        events.append({
            "type": "reminder_delivered",
            "notification_id": n.get("id"),
            "text": n.get("text"),
            "due_at": n.get("due_at"),
        })
    messages = result.get("messages") or []
    if since_index is not None:
        messages = messages[since_index:]
    for msg in messages:
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
