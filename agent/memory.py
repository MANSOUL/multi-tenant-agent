"""记忆与上下文：每租户 SQLite 的 facts / chat_log / summaries 三张表。

顺序：摘要 → 最近若干轮原文 → 长期事实 → 模型。
load_context 只读本租户的库，绝不跨租户；轮数超阈值时把最旧的轮
用模型总结进 summaries 表（没 key / 失败时直接截断），再按字符预算
从最旧一轮开始截，永远保留至少最后一轮；系统提示和最新用户消息不截。
"""
import os
from datetime import datetime, timezone

try:
    from .config import (
        API_KEY,
        DEFAULT_CONTEXT_MAX_CHARS,
        DEFAULT_CONTEXT_MAX_TURNS,
        DEFAULT_SUMMARY_THRESHOLD_TURNS,
    )
    from .tenant import tenant_db
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from config import (
        API_KEY,
        DEFAULT_CONTEXT_MAX_CHARS,
        DEFAULT_CONTEXT_MAX_TURNS,
        DEFAULT_SUMMARY_THRESHOLD_TURNS,
    )
    from tenant import tenant_db

from langchain_core.messages import HumanMessage, SystemMessage


def _int_env(name: str, default: int) -> int:
    """读整数环境变量，读不到或非法时用默认值（每次调用都重读，方便测试调小阈值）。"""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：约 4 个字符 = 1 个 token（中英文混排的经验值）。"""
    return max(0, len(text or "") // 4)


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
    if llm is None or not API_KEY:
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


def load_context(tenant: str, llm=None, max_turns=None, max_chars=None,
                 summary_threshold_turns=None) -> dict:
    """加载本租户的上下文（只读本租户的库，绝不跨租户）。

    1) 读 facts 和最新一条 summary；
    2) chat_log 按 rowid 分组为若干轮（每轮以一条 user 消息开头）；
       轮数超过阈值时，把最旧的、超出最近 max_turns 的那些轮先尝试用模型
       总结成摘要存进 summaries 表，总结失败/没 key 时直接截断删掉，
       然后删掉这些旧行（只删本租户的）；
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
        facts = "\n".join(
            f"- {k}: {v}" for k, v in
            conn.execute("SELECT key, value FROM facts").fetchall())
        row = conn.execute(
            "SELECT summary FROM summaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        summary = row[0] if row else ""
        rows = conn.execute(
            "SELECT rowid, ts, role, content FROM chat_log ORDER BY rowid ASC"
        ).fetchall()
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
                summary = (summary + "\n" + new_summary).strip() if summary \
                    else new_summary
                conn.execute(
                    "INSERT INTO summaries (ts, summary, up_to_ts)"
                    " VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), summary,
                     old_turns[-1]["ts"]))
                summarized = True
            else:
                truncated = True
            old_ids = [r for t in old_turns for r in t["rowids"]]
            conn.execute(
                f"DELETE FROM chat_log WHERE rowid IN "
                f"({','.join('?' * len(old_ids))})", old_ids)
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
            "estimated_tokens": estimate_tokens(
                facts + summary + history_text),
            "summarized": summarized, "truncated": truncated}
