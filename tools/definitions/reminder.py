"""提醒工具定义（工具层）：add_reminder / update_reminder / list_reminders。

只做参数校验 + 调 app/reminder_service.py，不再自己写业务逻辑；
执行前过 tools/executor.py 的权限检查（见 tools/registry.py
拼出来的 LangChain 工具）。提醒去重（±5 分钟）和澄清后直接设
的修法都在 service 层，这里只透传参数。
"""
import inspect

try:
    from app.reminder_service import ReminderService
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.reminder_service import ReminderService


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""
    service = ReminderService()
    return [
        {"name": "add_reminder",
         "description": "给本租户设一个提醒。text 是要做的事（如“开会”，只是格式示例），time_text 是时间"
                        "（如“早上8点”“晚上7点20分”“19:20”“每天早上8点”）；repeat 为 \"daily\" 时每天重复。"
                        "成功返回里含内容和完整时间（今天/明天），供模型回显和核对。"
                        "裸“8点”没说上下午时不落盘，只返回追问；没解析出时间时也不落盘。"
                        "（Bug 5 的根因：旧版先写了 due_at 为空的行再追问，模型回头就当成已设好。）",
         "params": [("text", inspect.Parameter.empty, str),
                    ("time_text", "", str),
                    ("repeat", "once", str)],
         "required": ("text",),
         "handler": lambda args: service.set_reminder(
             tenant, args["text"], args.get("time_text", ""),
             args.get("repeat", "once"))},
        {"name": "update_reminder",
         "description": "把本租户的一条提醒改到新时间。优先按 id（list_reminders 里看到的编号）；"
                        "id 为 0 时按 text 匹配最近一条未完成/未到点的提醒。"
                        "改提醒直接执行，不许先问“确认一下吗”。返回里含改完的新时间，"
                        "调用方必须拿它跟用户要的时间核对一遍（见系统提示的核对规则）。"
                        "裸“8点”没说上下午时不改，只返回追问。",
         "params": [("reminder_id", 0, int), ("text", "", str),
                    ("new_time_text", "", str)],
         "required": (),
         "handler": lambda args: service.update_reminder(
             tenant, args.get("reminder_id", 0), args.get("text", ""),
             args.get("new_time_text", ""))},
        {"name": "list_reminders",
         "description": "列出本租户的提醒（未到点的、已到点的、已提醒过的都在内）。",
         "params": [],
         "required": (),
         "handler": lambda args: service.list_reminders(tenant)},
    ]
