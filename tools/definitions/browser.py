"""浏览器工具定义（工具层）：browse_page。

只做参数校验 + 调 browser 服务，不再自己写业务逻辑；执行前过
tools/executor.py 的权限检查（见 tools/registry.py 拼出来的
LangChain 工具）。截图只存到本租户工作区。
"""
import inspect
import json
import urllib.request
from urllib.error import HTTPError, URLError

try:
    from agent.config import BROWSER_URL
    from agent.tenant import tenant_dirs
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import BROWSER_URL
    from agent.tenant import tenant_dirs


def browse_page_impl(tenant: str, url: str, screenshot: bool) -> str:
    """调 browser 服务打开页面；截图存到本租户工作区。
    （原来 agent/tools.py 的同名函数，原样搬过来。）"""
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


def specs(tenant: str) -> list:
    """本类工具的 spec 列表：registry 按它拼 LangChain 工具。"""
    return [
        {"name": "browse_page",
         "description": "打开一个网页，返回标题和正文摘要；screenshot 为真时还截图保存到本租户工作区。",
         "params": [("url", inspect.Parameter.empty, str),
                    ("screenshot", False, bool)],
         "required": ("url",),
         "handler": lambda args: browse_page_impl(
             tenant, args["url"], args.get("screenshot", False))},
    ]
