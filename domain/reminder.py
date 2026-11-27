"""提醒的纯规则：时间解析、状态机、去重判定。

从 agent/memory.py 原样搬过来（P0 搬移，逻辑一字不改），只挪位置：
- 中文数字解析、时间词解析（七点二十分 / 7点20分 / 7:20 / 19:20 /
  八点半 / 八点一刻 / 三刻）
- 提醒状态机：pending（未到点）→ due（已到点，等送达）→ notified
  （已当面提醒过，不再出现）；每天重复的到点后另起一行次日的 pending
- 去重规则：同一内容 + 同一 due_at 的 pending 提醒不重复写
  （实现在 store/sqlite_store.py 的 SQL 里，规则本身记在这里）

本模块不碰数据库、不碰网络，只读 REMINDER_TZ 环境变量定提醒时区。
"""
import os
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 才走这条，本项目是 3.12，不会用到
    ZoneInfo = None

# 提醒行状态（reminders.status）
REMINDER_PENDING = "pending"
REMINDER_DUE = "due"
REMINDER_NOTIFIED = "notified"
REMINDER_STATUSES = (REMINDER_PENDING, REMINDER_DUE, REMINDER_NOTIFIED)


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
