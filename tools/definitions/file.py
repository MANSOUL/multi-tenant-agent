"""文件工具定义（工具层）：read_file / write_file / list_workspace /
search_file。

只做参数校验 + 调 agent/tenant.py 的路径隔离函数，不再自己写业务
逻辑；执行前过 tools/executor.py 的权限检查——其中 write_file
默认是 ask（没有审批通道时拒绝执行并记审计，见
tools/permissions.py），所以模型通过工具写文件时会被挡下来，
这是 P3 权限管线有意的行为（见汇报“取舍”）。
"""
import inspect
import os
from pathlib import Path

try:
    from agent.tenant import safe_path, tenant_dirs
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
    from agent.tenant import safe_path, tenant_dirs


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""

    def _read(args):
        p = safe_path(tenant, args["path"])
        return p.read_text(encoding="utf-8")[:4000] if p.exists() else "文件不存在"

    def _write(args):
        p = safe_path(tenant, args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return f"已写入 {args['path']}"

    def _list(args):
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

    def _search(args):
        workspace, _, _ = tenant_dirs(tenant)
        keyword = args["keyword"]
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

    return [
        {"name": "read_file",
         "description": "读本租户工作区里的一个文件，返回内容。只能读自己的目录。",
         "params": [("path", inspect.Parameter.empty, str)],
         "required": ("path",),
         "handler": _read},
        {"name": "write_file",
         "description": "往本租户工作区里写一个文件。只能写自己的目录。",
         "params": [("path", inspect.Parameter.empty, str),
                    ("content", inspect.Parameter.empty, str)],
         "required": ("path", "content"),
         "handler": _write},
        {"name": "list_workspace",
         "description": "列出本租户工作区里的文件和子目录（相对路径，最多 100 条）。",
         "params": [],
         "required": (),
         "handler": _list},
        {"name": "search_file",
         "description": "在本租户工作区的文件内容里搜关键词（只读文本文件，单个文件限 200KB，"
                        "最多返回 20 条匹配，含文件名、行号和片段）。",
         "params": [("keyword", inspect.Parameter.empty, str)],
         "required": ("keyword",),
         "handler": _search},
    ]
