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
import os
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 才走这条，本项目是 3.12，不会用到
    ZoneInfo = None

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


def reminder_tz():
    """提醒用的时区：环境变量 REMINDER_TZ，默认 Asia/Shanghai。"""
    name = os.environ.get("REMINDER_TZ", "Asia/Shanghai") or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


def _now_in_tz(now=None):
    """当前时间（提醒时区，带时区信息）。测试可传 now 注入时间。"""
    tz = reminder_tz()
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn_int(s: str):
    """中文数字转 int，支持 0–59：“五”→5、“十”→10、“十三”→13、
    “二十五”→25、“零五”→5、“三十”→30。不是数字返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        if tens is None:
            return None
        total = tens * 10
        if tail:
            ones = _cn_int(tail)
            if ones is None:
                return None
            total += ones
        return total
    total = 0
    for ch in s:
        if ch not in _CN_NUM:
            return None
        total = total * 10 + _CN_NUM[ch]
    return total


def _parse_hour(text: str):
    """从时间词里取 (小时, 分钟)，取不到返回 None。
    支持：“8点”“八点”“8点半”(=30分)、“八点一刻”(=15分)、
    “三刻”(=45分)、“七点二十分”(=20分)、“7点20分”“7点20”、
    “8:30”“19:20”。
    Bug 1 的根因（2026-11-26 实测）：旧版只认“半”一种分钟写法，
    “二十分/一刻/20分”全被丢掉，所以“七点二十分”存成了 19:00。
    """
    text = (text or "").strip()
    m = re.search(r"(\d{1,2})[:：](\d{1,2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    m = re.search(r"([零一二三四五六七八九十]{1,3}|[0-9]{1,2})\s*[点時时]", text)
    if not m:
        return None
    hour = _cn_int(m.group(1))
    if hour is None or not 0 <= hour <= 23:
        return None
    rest = text[m.end():]
    minute = 0
    mm = re.match(r"\s*(半|一刻|三刻|二刻)", rest)
    if mm:
        minute = {"半": 30, "一刻": 15, "三刻": 45, "二刻": 30}[mm.group(1)]
    else:
        mm = re.match(
            r"\s*([零一二三四五六七八九十]{1,3}|[0-9]{1,2})\s*分?", rest)
        if mm:
            minute = _cn_int(mm.group(1))
            if minute is None or minute > 59:
                return None
    return hour, minute


def due_day_label(due_at_iso, now=None) -> str:
    """due_at 是今天/明天/哪天：返回“今天”/“明天”/“11月28日”，解析失败返回“”。"""
    try:
        due = datetime.fromisoformat(due_at_iso)
    except (TypeError, ValueError):
        return ""
    if due.tzinfo is None:
        due = due.replace(tzinfo=reminder_tz())
    delta = (due.date() - _now_in_tz(now).date()).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    return due.strftime("%m月%d日")


def parse_reminder_time(text: str, now=None) -> dict:
    """解析提醒时间。返回 dict：
    {"due_at": ISO 字符串或 None, "repeat": "once"/"daily",
     "ambiguous": True/False, "text": 提醒内容（去掉时间词之后）}。

    支持：早上/上午 8 点→08:00，晚上/下午 8 点→20:00，中午 12 点→12:00，
    “明天早上 8 点”，“每天早上 8 点”→repeat=daily，“8点半”“8:30”。
    分钟（2026-11-26 修 Bug 1）：七点二十分→:20、7点20分/7点20→:20、
    8:30→:30、八点半→:30、八点一刻→:15、三刻→:45，解析不到分钟时分钟为 0。
    裸“8点”（没有早上/上午/中午/下午/晚上/凌晨）不猜上下午，
    返回 ambiguous=True、due_at=None。
    没解析出时间时 due_at=None、ambiguous=False。
    """
    text = (text or "").strip()
    repeat = "daily" if "每天" in text or "每日" in text else "once"
    is_tomorrow = "明天" in text or "明日" in text
    hm = _parse_hour(text)
    if hm is None:
        return {"due_at": None, "repeat": repeat, "ambiguous": False,
                "text": text}
    hour, minute = hm
    period = None
    for key in ("凌晨", "早上", "上午", "中午", "下午", "晚上"):
        if key in text:
            period = key
            break
    ambiguous = False
    if period is None:
        if hour > 12:
            pass  # 20点这种本身就明确，不用上下午
        else:
            ambiguous = True
    elif period in ("下午", "晚上"):
        if hour <= 12:
            hour += 12
    if ambiguous:
        return {"due_at": None, "repeat": repeat, "ambiguous": True,
                "text": text}
    base = _now_in_tz(now)
    due = base.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if is_tomorrow:
        due += timedelta(days=1)
    elif repeat == "once" and due <= base:
        due += timedelta(days=1)  # 今天这个点已经过了，算明天
    return {"due_at": due.isoformat(), "repeat": repeat, "ambiguous": False,
            "text": text}


def _clean_reminder_text(text: str) -> str:
    """把提醒内容里的时间词去掉，只留要做的事，如“8点提醒我喝水”→“喝水”。"""
    text = (text or "").strip()
    text = re.sub(r"[，,。；;、\s]*$", "", text)
    return text


def _remember_fact_once(conn, key: str, value: str) -> bool:
    """facts 去重写：同一 value 已存在就不重复写。返回是否真的写了。"""
    value = (value or "").strip()
    if not value:
        return False
    row = conn.execute(
        "SELECT 1 FROM facts WHERE value = ?", (value,)).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
        (key, value, datetime.now(timezone.utc).isoformat()))
    return True


def _add_todo_row(conn, text: str) -> bool:
    text = (text or "").strip(" ，,。；;、")
    if not text:
        return False
    row = conn.execute(
        "SELECT 1 FROM todos WHERE text = ? AND status = 'pending'",
        (text,)).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO todos (text, status, created_at) VALUES (?, 'pending', ?)",
        (text, datetime.now(timezone.utc).isoformat()))
    return True


def _add_reminder_row(conn, text: str, parsed: dict) -> bool:
    text = (text or "").strip(" ，,。；;、")
    if not text:
        return False
    row = conn.execute(
        "SELECT 1 FROM reminders WHERE text = ? AND status = 'pending'"
        " AND due_at IS ?",
        (text, parsed.get("due_at"))).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO reminders (text, due_at, repeat, status, created_at)"
        " VALUES (?, ?, ?, 'pending', ?)",
        (text, parsed.get("due_at"), parsed.get("repeat", "once"),
         datetime.now(timezone.utc).isoformat()))
    return True


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
    已有未送达记录时不重复写）。返回本轮变 due 的提醒文本列表。"""
    due_texts = []
    try:
        now_dt = _now_in_tz(now)
        with tenant_db(tenant) as conn:
            rows = conn.execute(
                "SELECT id, text, due_at, repeat FROM reminders"
                " WHERE status = 'pending' AND due_at IS NOT NULL"
            ).fetchall()
            for rid, rtext, due_at, repeat in rows:
                try:
                    due = datetime.fromisoformat(due_at)
                except ValueError:
                    continue
                if due.tzinfo is None:
                    due = due.replace(tzinfo=reminder_tz())
                if due > now_dt:
                    continue
                conn.execute(
                    "UPDATE reminders SET status = 'due' WHERE id = ?", (rid,))
                exists = conn.execute(
                    "SELECT 1 FROM pending_notifications WHERE text = ?"
                    " AND due_at = ? AND delivered = 0",
                    (rtext, due_at)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO pending_notifications"
                        " (tenant, text, due_at, created_at, delivered)"
                        " VALUES (?, ?, ?, ?, 0)",
                        (tenant, rtext, due_at,
                         datetime.now(timezone.utc).isoformat()))
                due_texts.append(rtext)
                if repeat == "daily":
                    # 每天重复：下一天同一时间再提醒一次。
                    nxt = due + timedelta(days=1)
                    conn.execute(
                        "INSERT INTO reminders (text, due_at, repeat,"
                        " status, created_at) VALUES (?, ?, 'daily',"
                        " 'pending', ?)",
                        (rtext, nxt.isoformat(),
                         datetime.now(timezone.utc).isoformat()))
    except Exception:
        return due_texts
    return due_texts


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
            rows = conn.execute(
                f"SELECT text, due_at FROM pending_notifications WHERE"
                f" tenant = ? AND id IN ({','.join('?' * len(ids))})",
                [tenant, *ids]).fetchall()
            conn.execute(
                f"UPDATE pending_notifications SET delivered = 1 WHERE"
                f" tenant = ? AND id IN ({','.join('?' * len(ids))})",
                [tenant, *ids])
            for rtext, due_at in rows:
                conn.execute(
                    "UPDATE reminders SET status = 'notified'"
                    " WHERE text = ? AND due_at = ? AND status = 'due'",
                    (rtext, due_at))
    except Exception:
        pass


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
        # 待办与提醒（2026-11-26）：只读 pending 的；待送达的 due 提醒
        # 只读 delivered=0 的，拼进 system prompt 后由 save_memory 标记已送达。
        # 提醒列表只列 pending 的——已到点的提醒只走【待提醒】这一条路，
        # 送达后行状态翻成 notified，不再列出（Bug 4：旧版把 due 的行也列在
        # 这里，模型每轮都看到、每轮都再说一遍）。
        todos = "\n".join(
            f"- [{tid}] {t}" for tid, t in conn.execute(
                "SELECT id, text FROM todos WHERE status = 'pending'"
                " ORDER BY id ASC").fetchall())
        reminders = "\n".join(
            f"- [{rid}] {t}（{d or '时间未定，需确认上下午'}，"
            f"{'每天' if r == 'daily' else '一次'}，{s}）"
            for rid, t, d, r, s in conn.execute(
                "SELECT id, text, due_at, repeat, status FROM reminders"
                " WHERE status = 'pending' ORDER BY id ASC"
            ).fetchall())
        due_notifications = [
            {"id": nid, "text": t, "due_at": d}
            for nid, t, d in conn.execute(
                "SELECT id, text, due_at FROM pending_notifications"
                " WHERE delivered = 0 ORDER BY id ASC").fetchall()]
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
            "todos": todos, "reminders": reminders,
            "due_notifications": due_notifications,
            "estimated_tokens": estimate_tokens(
                facts + summary + history_text + todos + reminders),
            "summarized": summarized, "truncated": truncated}
