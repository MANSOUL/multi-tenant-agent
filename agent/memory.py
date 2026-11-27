"""记忆与上下文：每租户 SQLite 的 facts / chat_log / summaries 三张表，
外加提醒/记忆改进（2026-11-26）新增的 todos / reminders /
pending_notifications 三张表。

顺序：待送达提醒 → 摘要 → 最近若干轮原文 → 长期事实 → 待办/提醒 → 模型。
提醒行状态：pending（未到点）→ due（已到点，等送达）→ notified（已当面
提醒过，不再出现）；每天重复的到点后另起一行次日的 pending。
自动落盘：save_memory 每轮对用户消息跑 extract_explicit()，把显式指令
直接写进 facts / todos / reminders，不依赖模型调工具；失败时静默跳过。
load_context 只读本租户的库，绝不跨租户；轮数超阈值时把最旧的轮
用模型总结进 summaries 表（没 key / 失败时直接截断），再按字符预算
从最旧一轮开始截，永远保留至少最后一轮；系统提示和最新用户消息不截。
"""
import re

try:
    from .tenant import tenant_db
    from context.assembler import assemble_context
    from domain.reminder import (
        _clean_reminder_text,
        due_day_label,
        parse_reminder_time,
    )
    from store.sqlite_store import SQLiteStore
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tenant import tenant_db
    from context.assembler import assemble_context
    from domain.reminder import (
        _clean_reminder_text,
        due_day_label,
        parse_reminder_time,
    )
    from store.sqlite_store import SQLiteStore



def _remember_fact_once(conn, key: str, value: str) -> bool:
    """facts 去重写：同一 value 已存在就不重复写。返回是否真的写了。
    （P0：SQL 在 store/sqlite_store.py，这里只调 store；tools.py 还调这个函数，签名不变。）"""
    return SQLiteStore(conn).remember_fact_once(key, value)


def _add_todo_row(conn, text: str) -> bool:
    """写一条待办（去重）。P0：SQL 在 store，这里只调 store；tools.py 还调这个函数，签名不变。"""
    return SQLiteStore(conn).add_todo(text)


def _add_reminder_row(conn, text: str, parsed: dict) -> bool:
    """写一条提醒（去重）。P0：SQL 在 store，这里只调 store；tools.py 还调这个函数，签名不变。"""
    return SQLiteStore(conn).add_reminder(text, parsed)


def extract_explicit(text: str, tenant: str, now=None) -> dict:
    """确定性抽取：对一条用户消息跑一遍显式指令匹配，写进 facts/todos/
    reminders 表，不依赖模型调 remember_fact。

    匹配的指令：
    - “记住…/记住：…”→ facts；“别忘了…”→ facts
    - “提醒我…/…点提醒我…/每天…点提醒我…”→ reminders（只有时间解析出
      due_at 才写；裸“8点”歧义或根本没时间时不落盘，记到
      result["ambiguous_reminders"] 里，由模型当场追问——Bug 5 的根因就是
      旧版把歧义提醒以 due_at=None 写进表里，模型回头就当成已设好）
    - “待办…/待办事项：…”→ todos（按 、，；分多条）
    返回 {"facts": n, "todos": n, "reminders": n,
          "ambiguous_reminders": [提醒内容...]}；任何异常都静默吞掉，
    返回全 0，绝不阻塞聊天。调用方（graph.save_memory）也要包 try。
    """
    result = {"facts": 0, "todos": 0, "reminders": 0,
              "ambiguous_reminders": []}
    try:
        text = (text or "").strip()
        if not text:
            return result
        with tenant_db(tenant) as conn:
            m = re.search(r"(?:记住|记一下|请记住)[：:，, ]*(.+)$", text)
            if m and _remember_fact_once(
                    conn, "记住:" + m.group(1).strip()[:30],
                    m.group(1).strip()):
                result["facts"] += 1
            m = re.search(r"(?:别忘了|不要忘了|别忘记)[：:，, ]*(.+)$", text)
            if m and _remember_fact_once(
                    conn, "别忘了:" + m.group(1).strip()[:30],
                    m.group(1).strip()):
                result["facts"] += 1
            m = re.search(r"(?:待办事项|待办|代办事项|代办)[：:，, ]*(.+)$", text)
            if m:
                for item in re.split(r"[、，,；;]", m.group(1)):
                    if _add_todo_row(conn, item):
                        result["todos"] += 1
            # 提醒：时间可能在“提醒我”前面（“七点二十分提醒我喝水”、
            # “每天早上8点提醒我喝水”），也可能在后面（“提醒我明天早上8点喝水”）。
            # 两种都走同一份 parse_reminder_time，不各写一份。
            # 旧版 Bug 1 在这里也有一份：第一条正则只认“点”后跟“半”，
            # “七点二十分提醒我…”根本匹配不上，时间就丢了。
            m = re.search(r"提醒我(.+)$", text)
            if m:
                before = text[:m.start()].strip()
                body = m.group(1).strip()
                what = body
                parsed = (parse_reminder_time(before, now=now) if before
                          else {"due_at": None, "repeat": "once",
                                "ambiguous": False, "text": before})
                if not parsed.get("due_at") and not parsed.get("ambiguous"):
                    # “提醒我”前面没时间，时间在后面：解析 body 并去掉时间词，
                    # 如“早上8点喝水”→“喝水”、“明天晚上7点20分开会”→“开会”。
                    parsed = parse_reminder_time(body, now=now)
                    what = re.sub(
                        r"^(每天|每日|明天|明日)?(凌晨|早上|上午|中午|下午|晚上)?"
                        r"[零一二三四五六七八九十0-9]{1,3}\s*[点時时]"
                        r"(半|一刻|三刻|二刻|[零一二三四五六七八九十]{1,3}分"
                        r"|[0-9]{1,2}分?|[:：][0-9]{1,2})?"
                        r"[，, ]*", "", body)
                what = _clean_reminder_text(what)
                if parsed.get("ambiguous"):
                    result["ambiguous_reminders"].append(what)
                elif parsed.get("due_at"):
                    if _add_reminder_row(conn, what, parsed):
                        result["reminders"] += 1
    except Exception:
        return {"facts": 0, "todos": 0, "reminders": 0,
                "ambiguous_reminders": []}
    return result


def check_due_reminders(tenant: str, now=None) -> list:
    """调度器单次扫描：把到点的 reminders（due_at <= 现在，status=pending）
    置为 due，并往 pending_notifications 写一条未送达记录（同 text+due_at
    已有未送达记录时不重复写）。返回本轮变 due 的提醒文本列表。
    （P3：实现搬进 app/reminder_service.py 的 ReminderService.scan_due，
    这里只做薄层转发；调度器和 tests 还调这个函数，签名不变。）"""
    from app.reminder_service import ReminderService

    return ReminderService().scan_due(tenant, now=now)


def mark_notifications_delivered(tenant: str, notification_ids) -> None:
    """把指定 id 的待提醒标记为已送达（delivered=1），下次不再出现。
    同时把对应的提醒行从 due 翻成 notified——Bug 4 的真正根因：
    旧版只标了通知，已送达的提醒行还留在 status='due'，而 load_context
    的提醒列表会把 status='due' 的行一直列出来，模型每轮看到就再说一遍，
    所以第二轮聊天又出现一次。翻成 notified 后它就从提醒列表里消失，
    list_reminders 工具里仍能看到（算历史）。"""
    ids = [i for i in (notification_ids or []) if isinstance(i, int)]
    if not ids:
        return
    try:
        with tenant_db(tenant) as conn:
            store = SQLiteStore(conn)
            rows = store.mark_notifications_delivered(tenant, ids)
            for rtext, due_at in rows:
                store.mark_reminder_notified(rtext, due_at)
    except Exception:
        pass


def load_context(tenant: str, llm=None, max_turns=None, max_chars=None,
                 summary_threshold_turns=None) -> dict:
    """加载本租户的上下文（P0：实现在 context/assembler.assemble_context，
    这里只做薄层转发；tests 和 graph 还调这个函数，签名和返回不变）。"""
    return assemble_context(tenant, llm=llm, max_turns=max_turns,
                            max_chars=max_chars,
                            summary_threshold_turns=summary_threshold_turns)
