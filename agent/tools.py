"""工具：给一个租户建一套 LangChain 工具（共 15 个）。

每个工具函数都闭包了 tenant，内部只走 tenant.py 的隔离函数
（tenant_dirs / tenant_db / safe_path）和 sandbox.py 的 run_command，
这里不重复实现任何隔离逻辑。浏览器工具调 browser 服务。
待办/提醒工具（2026-11-26 新增）走 memory.py 的解析和写表函数，
同样只读写本租户的库。
"""
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

try:
    from .config import BROWSER_URL
    from .memory import _add_reminder_row, _add_todo_row, parse_reminder_time
    from .tenant import safe_path, tenant_db, tenant_dirs
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from config import BROWSER_URL
    from memory import _add_reminder_row, _add_todo_row, parse_reminder_time
    from tenant import safe_path, tenant_db, tenant_dirs

from langchain_core.tools import tool

try:
    from .sandbox import run_command as sandbox_run
except ImportError:
    from sandbox import run_command as sandbox_run


def browse_page_impl(tenant: str, url: str, screenshot: bool) -> str:
    """调 browser 服务打开页面；截图存到本租户工作区。"""
    if not url.startswith(("http://", "https://")):
        return "browse_page 只支持 http:// 或 https:// 开头的地址"
    workspace, _, _ = tenant_dirs(tenant)
    req = urllib.request.Request(
        f"{BROWSER_URL}/browse",
        data=json.dumps({
            "url": url,
            "screenshot": bool(screenshot),
            "tenant": tenant,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read())
    except HTTPError as e:
        return f"浏览器服务返回 {e.code}：{e.read().decode()[:300]}"
    except URLError as e:
        return f"连不上浏览器服务（{BROWSER_URL}）：{e.reason}"
    if d.get("error"):
        return f"打开页面失败：{d['error']}"
    out = f"标题：{d.get('title', '')}\n网址：{url}\n正文摘要：\n{d.get('text', '')[:2000]}"
    shot = d.get("screenshot_path", "")
    if shot:
        # browser 服务把截图写到共享卷的本租户工作区，只返回相对路径
        out += f"\n截图已保存：{shot}"
    return out


def make_tools(tenant: str):
    """给这个租户建一套工具。工具名和上一版一致，内部走原来的隔离逻辑。"""

    @tool
    def run_command(command: str) -> str:
        """在沙箱里跑一条白名单只读命令。可用：ls、pwd、date、df、free、uptime、whoami、git。"""
        workspace, _, _ = tenant_dirs(tenant)
        out, mode = sandbox_run(command, str(workspace))
        if mode == "local":
            return f"[本地回退：未隔离，仅开发用]\n{out}"
        if mode == "docker":
            return f"[沙箱容器]\n{out}"
        return out

    @tool
    def read_file(path: str) -> str:
        """读本租户工作区里的一个文件，返回内容。只能读自己的目录。"""
        p = safe_path(tenant, path)
        return p.read_text(encoding="utf-8")[:4000] if p.exists() else "文件不存在"

    @tool
    def write_file(path: str, content: str) -> str:
        """往本租户工作区里写一个文件。只能写自己的目录。"""
        p = safe_path(tenant, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {path}"

    @tool
    def remember_fact(key: str, value: str) -> str:
        """把一件事记到本租户的长期记忆里。"""
        with tenant_db(tenant) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO facts VALUES (?, ?, ?)",
                (key, value, datetime.now(timezone.utc).isoformat()),
            )
        return f"记住了：{key} = {value}"

    @tool
    def recall_fact(key: str) -> str:
        """从本租户的长期记忆里查一件事。"""
        with tenant_db(tenant) as conn:
            row = conn.execute(
                "SELECT value FROM facts WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else "没记过这件事"

    @tool
    def list_facts() -> str:
        """列出本租户长期记忆里的所有事。"""
        with tenant_db(tenant) as conn:
            rows = conn.execute("SELECT key, value FROM facts").fetchall()
        return "\n".join(f"{k}: {v}" for k, v in rows) or "记忆还是空的"

    @tool
    def search_facts(keyword: str) -> str:
        """按关键词在本租户的长期记忆里搜索（key 和 value 都查）。"""
        kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with tenant_db(tenant) as conn:
            rows = conn.execute(
                "SELECT key, value FROM facts WHERE key LIKE ? ESCAPE '\\'"
                " OR value LIKE ? ESCAPE '\\' LIMIT 20",
                (f"%{kw}%", f"%{kw}%")).fetchall()
        return "\n".join(f"{k}: {v}" for k, v in rows) or "记忆里没找到相关的事"

    @tool
    def list_workspace() -> str:
        """列出本租户工作区里的文件和子目录（相对路径，最多 100 条）。"""
        workspace, _, _ = tenant_dirs(tenant)
        out = []
        for root, dirs, files in os.walk(workspace):
            dirs.sort()
            for d in sorted(dirs):
                out.append(str((Path(root) / d).relative_to(workspace)) + "/")
            for f in sorted(files):
                out.append(str((Path(root) / f).relative_to(workspace)))
            if len(out) >= 100:
                break
        return "\n".join(out[:100]) or "工作区还是空的"

    @tool
    def search_file(keyword: str) -> str:
        """在本租户工作区的文件内容里搜关键词（只读文本文件，单个文件限 200KB，
        最多返回 20 条匹配，含文件名、行号和片段）。"""
        workspace, _, _ = tenant_dirs(tenant)
        matches, scanned = [], 0
        for root, dirs, files in os.walk(workspace):
            dirs.sort()
            for name in sorted(files):
                if len(matches) >= 20 or scanned >= 200:
                    break
                p = Path(root) / name
                try:
                    if p.stat().st_size > 200 * 1024:
                        continue
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except (OSError, UnicodeError):
                    continue
                scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if keyword.lower() in line.lower():
                        rel = p.relative_to(workspace)
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= 20:
                            break
        return "\n".join(matches) or "工作区里没找到相关内容"

    @tool
    def browse_page(url: str, screenshot: bool = False) -> str:
        """打开一个网页，返回标题和正文摘要；screenshot 为真时还截图保存到本租户工作区。"""
        return browse_page_impl(tenant, url, screenshot)

    @tool
    def add_todo(text: str) -> str:
        """往本租户的待办清单里加一条。成功时返回写进去的内容，供回显。"""
        with tenant_db(tenant) as conn:
            ok = _add_todo_row(conn, text)
        return f"已加入待办：{text.strip()}" if ok else f"待办里已经有：{text.strip()}"

    @tool
    def list_todos() -> str:
        """列出本租户的待办清单（未完成在前，已完成在后）。"""
        with tenant_db(tenant) as conn:
            rows = conn.execute(
                "SELECT id, text, status FROM todos ORDER BY"
                " CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id ASC"
            ).fetchall()
        if not rows:
            return "待办清单还是空的"
        return "\n".join(
            f"[{'待办' if s == 'pending' else '已完成'} {i}] {t}"
            for i, t, s in rows)

    @tool
    def complete_todo(todo_id: int) -> str:
        """把本租户待办清单里的一条标为已完成（传 list_todos 里看到的 id）。"""
        with tenant_db(tenant) as conn:
            row = conn.execute(
                "SELECT text FROM todos WHERE id = ? AND status = 'pending'",
                (todo_id,)).fetchone()
            if not row:
                return f"没有找到编号为 {todo_id} 的未完成待办"
            conn.execute(
                "UPDATE todos SET status = 'done' WHERE id = ?", (todo_id,))
        return f"已完成待办：{row[0]}"

    @tool
    def add_reminder(text: str, time_text: str = "", repeat: str = "once") -> str:
        """给本租户设一个提醒。text 是要做的事（如“喝水”），time_text 是时间
        （如“早上8点”“明天晚上8点”“每天早上8点”）；repeat 为 "daily" 时每天重复。
        成功返回里含内容和时间，供模型回显；裸“8点”没说上下午时返回追问，
        提醒先记下（时间未定），等用户补齐时间再算。"""
        parsed = parse_reminder_time(time_text or text)
        if repeat == "daily":
            parsed["repeat"] = "daily"
        with tenant_db(tenant) as conn:
            _add_reminder_row(conn, text.strip(), parsed)
        if parsed.get("ambiguous"):
            return (f"已记下提醒：{text.strip()}，但“{time_text or text}”没说"
                    f"是早上还是晚上，请告诉我是早上还是晚上，我再把时间定下来。")
        if parsed.get("due_at"):
            when = parsed["due_at"].replace("T", " ").split("+")[0]
            return (f"已设好提醒：{text.strip()}，时间 {when}"
                    f"（{'每天' if parsed['repeat'] == 'daily' else '一次'}）")
        return (f"已记下提醒：{text.strip()}，但没解析出时间，"
                f"请告诉我具体几点（比如“早上8点”）。")

    @tool
    def list_reminders() -> str:
        """列出本租户的提醒（未到点的和已到点的都在内）。"""
        with tenant_db(tenant) as conn:
            rows = conn.execute(
                "SELECT id, text, due_at, repeat, status FROM reminders"
                " ORDER BY id ASC").fetchall()
        if not rows:
            return "提醒还是空的"
        lines = []
        for i, t, d, r, s in rows:
            when = (d or "时间未定，需确认上下午").replace("T", " ").split("+")[0]
            lines.append(
                f"[{s} {i}] {t}，{when}，{'每天' if r == 'daily' else '一次'}")
        return "\n".join(lines)

    return [run_command, read_file, write_file, list_workspace, search_file,
            remember_fact, recall_fact, search_facts, list_facts, browse_page,
            add_todo, list_todos, complete_todo, add_reminder, list_reminders]
