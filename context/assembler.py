"""上下文组装：读库 + 截断/压缩 + 拼系统提示。

从 agent/memory.py 的 load_context 和 agent/graph.py 的 _system_prompt
原样搬过来（P0 搬移），只做两处改变：
1. 读库走 store（create_store 按 STORE_BACKEND 选实现），不再在 memory.py 里拼 SQL；
2. 摘要后旧行不再 DELETE，改成归档（store/archive_turns）——见
   store/event_log.py，chat_log 只追加。

其余行为不变：阈值、预算、截断顺序、返回的 dict 结构、系统提示的
拼接顺序（待送达提醒 → 摘要 → 长期事实 → 最近对话 → 待办/提醒）。
"""
import os
from datetime import datetime, timezone

try:
    from agent.config import SYSTEM_PROMPT, TOOLS_DOC
    from agent.tenant import tenant_db
    from domain.memory import (
        DEFAULT_CONTEXT_MAX_CHARS,
        DEFAULT_CONTEXT_MAX_TURNS,
        DEFAULT_SUMMARY_THRESHOLD_TURNS,
        estimate_tokens,
    )
    from model.client import has_api_key
    from store import create_store
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import SYSTEM_PROMPT, TOOLS_DOC
    from agent.tenant import tenant_db
    from domain.memory import (
        DEFAULT_CONTEXT_MAX_CHARS,
        DEFAULT_CONTEXT_MAX_TURNS,
        DEFAULT_SUMMARY_THRESHOLD_TURNS,
        estimate_tokens,
    )
    from model.client import has_api_key
    from store import create_store

from langchain_core.messages import HumanMessage, SystemMessage


def _int_env(name: str, default: int) -> int:
    """读整数环境变量，读不到或非法时用默认值（每次调用都重读，方便测试调小阈值）。"""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _format_turns(turns) -> str:
    """把若干轮对话拼成给模型看的文本：每轮“用户：… / 助手：…”。"""
    lines = []
    for turn in turns:
        for role, content in turn["msgs"]:
            lines.append(f"{'用户' if role == 'user' else '助手'}：{content}")
    return "\n".join(lines)


def _summarize_with_llm(llm, turns_text: str) -> str | None:
    """用模型把旧对话浓缩成摘要。没 key / 没模型 / 调用失败都返回 None，
    调用方会改用直接截断旧对话，不会因此报错。"""
    if llm is None or not has_api_key():
        return None
    try:
        msg = llm.invoke([
            SystemMessage(content="请把以下对话浓缩成不超过200字的中文要点，"
                                  "只保留事实、偏好和待办，不要编造。"),
            HumanMessage(content=turns_text),
        ])
        content = msg.content
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text")
        content = (content or "").strip()
        return content[:1000] if content else None
    except Exception:
        return None


def assemble_context(tenant: str, llm=None, max_turns=None, max_chars=None,
                     summary_threshold_turns=None) -> dict:
    """加载本租户的上下文（只读本租户的库，绝不跨租户）。

    1) 读 facts 和最新一条 summary；
    2) chat_log 按 rowid 分组为若干轮（每轮以一条 user 消息开头）；
       轮数超过阈值时，把最旧的、超出最近 max_turns 的那些轮先尝试用模型
       总结成摘要存进 summaries 表，总结失败/没 key 时直接截断，
       然后把这些旧行归档（记 chat_archived 标记 + compaction_events 事件，
       行本身留在 chat_log 里不删除——P0 事件日志机制）；
    3) 按 CONTEXT_MAX_CHARS 预算从最旧一轮开始截断，只保留最近的轮，
       永远保留至少最后一轮；最新一条用户消息还没进 chat_log，不会被截掉。
    返回 facts / summary / history_text 等；调用方只把这些拼进系统提示，
    不把历史消息混进 state["messages"]，避免顺序错乱。
    """
    max_turns = max_turns if max_turns is not None else _int_env(
        "CONTEXT_MAX_TURNS", DEFAULT_CONTEXT_MAX_TURNS)
    max_chars = max_chars if max_chars is not None else _int_env(
        "CONTEXT_MAX_CHARS", DEFAULT_CONTEXT_MAX_CHARS)
    threshold = (summary_threshold_turns if summary_threshold_turns is not None
                 else _int_env("SUMMARY_THRESHOLD_TURNS",
                               DEFAULT_SUMMARY_THRESHOLD_TURNS))
    max_turns = max(1, max_turns)
    max_chars = max(10, max_chars)  # 下限只防 0/负数，不挡小预算测试
    threshold = max(max_turns, threshold)
    summarized = False
    truncated = False
    with tenant_db(tenant) as conn:
        store = create_store(conn)
        facts = "\n".join(
            f"- {k}: {v}" for k, v in store.get_facts())
        summary = store.latest_summary()
        # 待办与提醒：只读 pending 的；待送达的 due 提醒只读 delivered=0 的，
        # 拼进 system prompt 后由 save_memory 标记已送达。
        # 提醒列表只列 pending 的——已到点的提醒只走【待提醒】这一条路，
        # 送达后行状态翻成 notified，不再列出（Bug 4：旧版把 due 的行也列在
        # 这里，模型每轮都看到、每轮都再说一遍）。
        todos = "\n".join(
            f"- [{tid}] {t}" for tid, t in store.list_pending_todos())
        reminders = "\n".join(
            f"- [{rid}] {t}（{d or '时间未定，需确认上下午'}，"
            f"{'每天' if r == 'daily' else '一次'}，{s}）"
            for rid, t, d, r, s in store.list_pending_reminders())
        due_notifications = [
            {"id": nid, "text": t, "due_at": d}
            for nid, t, d in store.list_undelivered_notifications()]
        rows = store.list_active_chat_rows()
        turns = []
        for rowid, ts, role, content in rows:
            if role == "user" or not turns:
                turns.append({"rowids": [rowid], "ts": ts,
                              "msgs": [(role, content)]})
            else:
                turns[-1]["rowids"].append(rowid)
                turns[-1]["msgs"].append((role, content))
        if len(turns) > threshold:
            old_turns = turns[:len(turns) - max_turns]
            old_text = _format_turns(old_turns)
            new_summary = _summarize_with_llm(llm, old_text)
            if new_summary:
                # P3 计量：摘要也是一次模型调用，记一笔（失败不阻塞组装）。
                try:
                    from agent.config import MODEL
                    store.record_metering(
                        tenant, MODEL, old_text, new_summary)
                except Exception:
                    pass
                summary = (summary + "\n" + new_summary).strip() if summary \
                    else new_summary
                store.add_summary(
                    datetime.now(timezone.utc).isoformat(), summary,
                    old_turns[-1]["ts"])
                summarized = True
            else:
                truncated = True
            old_ids = [r for t in old_turns for r in t["rowids"]]
            store.archive_turns(old_ids, old_turns[-1]["ts"])
            turns = turns[len(turns) - max_turns:]
        history_text = _format_turns(turns)
        # 按字符预算从最旧一轮开始截，至少保留最后一轮。
        while (len(turns) > 1 and
               len(facts) + len(summary) + len(history_text) > max_chars):
            turns = turns[1:]
            history_text = _format_turns(turns)
            truncated = True
        if len(facts) + len(summary) + len(history_text) > max_chars:
            # 事实实在太多时只截事实文本，历史和摘要不动。
            budget = max(0, max_chars - len(summary) - len(history_text))
            facts = facts[:budget]
            truncated = True
    return {"facts": facts, "summary": summary,
            "history_text": history_text, "history_turns": len(turns),
            "todos": todos, "reminders": reminders,
            "due_notifications": due_notifications,
            "estimated_tokens": estimate_tokens(
                facts + summary + history_text + todos + reminders),
            "summarized": summarized, "truncated": truncated}


def build_system_prompt(ctx: dict) -> str:
    """拼系统提示：待送达提醒 → 摘要 → 长期事实 → 最近对话 → 待办/提醒 → 工具说明。
    待送达提醒放最前面，模型先看到、先当面提醒用户。
    系统提示和最新用户消息永远不截断，只截旧历史/旧事实。"""
    system = SYSTEM_PROMPT + "\n" + TOOLS_DOC
    for n in ctx.get("due_notifications") or []:
        system += (f"\n\n【待提醒】{n['text']}"
                   f"（应提醒时间 {n['due_at']}）：先把这条当面告诉用户。")
    if ctx.get("summary"):
        system += f"\n\n较早对话的摘要：\n{ctx['summary']}"
    if ctx.get("facts"):
        system += f"\n\n你记得的关于这个用户的事：\n{ctx['facts']}"
    if ctx.get("history_text"):
        system += f"\n\n最近的对话：\n{ctx['history_text']}"
    if ctx.get("todos"):
        system += f"\n\n未完成的待办：\n{ctx['todos']}"
    if ctx.get("reminders"):
        system += f"\n\n提醒：\n{ctx['reminders']}"
    return system
