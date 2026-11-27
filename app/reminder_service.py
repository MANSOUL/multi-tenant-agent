"""提醒用例（应用层）：设/改/查/到点。

编排提醒这一个用例，不认识 HTTP、SQL、LangGraph 图细节：解析走
domain/reminder，落盘走 store/sqlite_store.py（SQLiteStore），
时间词剥离的正则和 agent/memory.py 的 extract_explicit 共用同一份
写法（见 _strip_time_words）。

两个 P3 遗留 bug 修在这里：
1. 澄清后直接设：find_pending_clarification() 从最近对话里找回
   那条没说上下午的提醒请求，ChatService 在用户只答“早上/晚上”
   时直接调 set_reminder，不再经过模型问确认。
2. 去重：set_reminder 先查同租户、同内容、同时间 ±5 分钟内已有的
   pending 提醒，有就直接返回已有的那条，不新建（store 层也有一道
   同样的防线，见 SQLiteStore.add_reminder）。
"""
import re
from datetime import datetime, timedelta, timezone

try:
    from agent.tenant import tenant_db
    from domain.reminder import (
        _clean_reminder_text,
        _now_in_tz,
        _parse_hour,
        due_day_label,
        parse_reminder_time,
        reminder_tz,
    )
    from store.sqlite_store import SQLiteStore
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.tenant import tenant_db
    from domain.reminder import (
        _clean_reminder_text,
        _now_in_tz,
        _parse_hour,
        due_day_label,
        parse_reminder_time,
        reminder_tz,
    )
    from store.sqlite_store import SQLiteStore

# 裸上下午回答：只有这几个词才算澄清（不含时间、不含其它内容），
# 多一个字都不接——接错了会把一句正常聊天当成设提醒。
PERIOD_WORDS = ("凌晨", "早上", "上午", "中午", "下午", "晚上")

# 从提醒内容里剥时间词：和 agent/memory.py 的 extract_explicit 同一份
# 正则（“每天/明天 + 上下午 + 几点几分”），两处改要一起改。
_TIME_WORD_RE = (
    r"^(每天|每日|明天|明日)?(凌晨|早上|上午|中午|下午|晚上)?"
    r"[零一二三四五六七八九十0-9]{1,3}\s*[点時时]"
    r"(半|一刻|三刻|二刻|[零一二三四五六七八九十]{1,3}分"
    r"|[0-9]{1,2}分?|[:：][0-9]{1,2})?"
    r"[，, ]*"
)


def _strip_time_words(text: str) -> str:
    """去掉开头的提醒时间词，只留要做的事。"""
    return _clean_reminder_text(re.sub(_TIME_WORD_RE, "", (text or "").strip()))


def extract_clarification_target(text: str) -> dict | None:
    """从一条歧义提醒请求里抽出澄清要用的目标。

    只接 parse_reminder_time 判成 ambiguous 的（裸“8点”没说上下午）：
    返回 {"what": 要做的事, "hour": 时, "minute": 分}；不是歧义请求、
    或抽不出“做什么”时返回 None。
    时间可能在“提醒我”前面（“七点二十分提醒我喝水”），也可能在
    后面（“8点提醒我开会”），两种都认。
    """
    text = (text or "").strip()
    if "提醒" not in text:
        return None
    parsed = parse_reminder_time(text)
    if not parsed.get("ambiguous"):
        return None
    hm = _parse_hour(text)
    if hm is None:
        return None
    hour, minute = hm
    what = ""
    m = re.search(r"提醒我(.+)$", text)
    if m:
        before = text[:m.start()].strip()
        body = m.group(1).strip()
        if before and _parse_hour(before):
            what = _clean_reminder_text(body)
        else:
            what = _strip_time_words(body)
    else:
        what = _strip_time_words(text)
    if not what:
        return None
    return {"what": what, "hour": hour, "minute": minute}


class ReminderService:
    """提醒用例：不持有状态，每次调用都现开本租户的库。"""

    # -- 设 / 改 / 查 ---------------------------------------------------
    def set_reminder(self, tenant: str, text: str, time_text: str = "",
                     repeat: str = "once") -> str:
        """设提醒。返回给模型回显的字符串（和原来 add_reminder 工具
        的返回一字不改，测试断言 19:20 / 今天明天标签都认它）。

        裸时间没说上下午、或没解析出时间时不落盘，只返回追问；
        同内容同时间 ±5 分钟内已有 pending 提醒时返回已有的那条。
        """
        parsed = parse_reminder_time(time_text or text)
        if repeat == "daily":
            parsed["repeat"] = "daily"
        if parsed.get("ambiguous"):
            return (f"“{time_text or text}”没说早上还是晚上，这条提醒还没存。"
                    f"请只问用户一次：早上还是晚上？问到答案后立刻用完整时间"
                    f"（如“晚上8点”）再调一次 add_reminder。不要说已经设好了。")
        if not parsed.get("due_at"):
            return (f"“{time_text or text}”里没解析出时间，这条提醒还没存。"
                    f"请问用户具体几点（比如“早上8点”或“19:20”）。")
        with tenant_db(tenant) as conn:
            store = SQLiteStore(conn)
            existing = store.find_pending_reminder_near(
                text.strip(), parsed["due_at"])
            if existing:
                when = (existing[2] or "").replace("T", " ").split("+")[0]
                return (f"已经有一条相同的提醒：{text.strip()}，时间 {when}"
                        f"（{due_day_label(existing[2])}，"
                        f"{'每天' if existing[3] == 'daily' else '一次'}），"
                        f"不用再设了。")
            store.add_reminder(text.strip(), parsed)
        when = parsed["due_at"].replace("T", " ").split("+")[0]
        return (f"已设好提醒：{text.strip()}，时间 {when}"
                f"（{due_day_label(parsed['due_at'])}，"
                f"{'每天' if parsed['repeat'] == 'daily' else '一次'}）")

    def update_reminder(self, tenant: str, reminder_id: int = 0,
                        text: str = "", new_time_text: str = "") -> str:
        """改提醒时间。直接执行，不许先问“确认一下吗”（和原来
        update_reminder 工具的返回一字不改）。"""
        parsed = parse_reminder_time(new_time_text or "")
        if parsed.get("ambiguous"):
            return (f"“{new_time_text}”没说早上还是晚上，这条提醒还没改。"
                    f"请只问用户一次：早上还是晚上？问到答案后立刻用完整时间再调一次。")
        if not parsed.get("due_at"):
            return (f"“{new_time_text}”里没解析出时间，这条提醒还没改。"
                    f"请问用户具体几点（比如“晚上7点20分”或“19:20”）。")
        with tenant_db(tenant) as conn:
            store = SQLiteStore(conn)
            row = store.find_reminder_for_update(reminder_id, text)
            if row is None:
                return "没有找到要改的那条提醒，请先用 list_reminders 查一下编号。"
            rid, old_text = row
            store.update_reminder(rid, parsed["due_at"],
                                  parsed.get("repeat", "once"))
            # 这条提醒之前到点产生的未送达通知作废，避免旧时间再冒出来。
            conn.execute(
                "UPDATE pending_notifications SET delivered = 1"
                " WHERE tenant = ? AND text = ? AND delivered = 0",
                (tenant, old_text))
        when = parsed["due_at"].replace("T", " ").split("+")[0]
        return (f"已把提醒「{old_text}」改成 {when}"
                f"（{due_day_label(parsed['due_at'])}，"
                f"{'每天' if parsed['repeat'] == 'daily' else '一次'}）")

    def list_reminders(self, tenant: str) -> str:
        """列提醒（中文状态，和原来 list_reminders 工具的返回一致）。"""
        status_label = {"pending": "待提醒", "due": "已到点",
                        "notified": "已提醒", "done": "已完成"}
        with tenant_db(tenant) as conn:
            store = SQLiteStore(conn)
            rows = store.list_all_reminders()
        if not rows:
            return "提醒还是空的"
        lines = []
        for i, t, d, r, s in rows:
            when = (d or "时间未定，需确认上下午").replace("T", " ").split("+")[0]
            lines.append(
                f"[{status_label.get(s, s)} {i}] {t}，{when}，"
                f"{'每天' if r == 'daily' else '一次'}")
        return "\n".join(lines)

    # -- 澄清：找回那条没说上下午的提醒请求 ------------------------------
    def find_pending_clarification(self, tenant: str) -> dict | None:
        """找回最近一次没说完上下午的提醒请求。

        只认这个形状：最近一条助手消息在问上下午（同时含“早上”和
        “晚上”），而它前面紧挨着的那条用户消息是一条歧义提醒请求
        （extract_clarification_target 能抽出目标）。形状对不上
        （用户中间插了别的话、助手问的不是上下午）就返回 None，
        调用方走正常模型路径，绝不猜。
        """
        with tenant_db(tenant) as conn:
            store = SQLiteStore(conn)
            rows = store.list_all_chat_rows()
        rows = rows[-8:]  # 只看最近几行，旧对话里的追问不算
        for idx in range(len(rows) - 1, 0, -1):
            _, _, role, content = rows[idx]
            if role != "assistant" or not content:
                continue
            if "早上" not in content or "晚上" not in content:
                return None  # 最近一条助手消息不是在问上下午
            prev_role, prev_content = rows[idx - 1][2], rows[idx - 1][3]
            if prev_role != "user":
                return None
            return extract_clarification_target(prev_content or "")
        return None

    def resolve_clarification(self, tenant: str, period: str) -> str | None:
        """用户只答了上下午（period 是“早上/晚上”之一）：找回待澄清
        的提醒请求，直接设好，返回设好的回显字符串；找不回时返回 None
        （调用方走正常模型路径）。"""
        if period not in PERIOD_WORDS:
            return None
        target = self.find_pending_clarification(tenant)
        if target is None:
            return None
        time_text = f"{period}{target['hour']}点"
        if target["minute"]:
            time_text += f"{target['minute']}分"
        return self.set_reminder(tenant, target["what"], time_text)

    # -- 到点扫描（调度器调；原来 agent/memory.check_due_reminders
    #    的实现，原样搬过来，只挪位置）-----------------------------------
    def scan_due(self, tenant: str, now=None) -> list:
        """把到点的 reminders（due_at <= 现在，status=pending）置为 due，
        并往 pending_notifications 写一条未送达记录（同 text+due_at
        已有未送达记录时不重复写）。返回本轮变 due 的提醒文本列表。"""
        due_texts = []
        try:
            now_dt = _now_in_tz(now)
            with tenant_db(tenant) as conn:
                store = SQLiteStore(conn)
                for rid, rtext, due_at, repeat in store.list_due_candidates():
                    try:
                        due = datetime.fromisoformat(due_at)
                    except ValueError:
                        continue
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=reminder_tz())
                    if due > now_dt:
                        continue
                    store.mark_reminder_due(rid)
                    store.add_notification_if_absent(tenant, rtext, due_at)
                    due_texts.append(rtext)
                    if repeat == "daily":
                        # 每天重复：下一天同一时间再提醒一次。
                        nxt = due + timedelta(days=1)
                        store.add_reminder(
                            rtext, {"due_at": nxt.isoformat(),
                                    "repeat": "daily"})
        except Exception:
            return due_texts
        return due_texts
