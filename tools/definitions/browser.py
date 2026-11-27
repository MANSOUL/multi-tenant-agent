"""浏览器工具定义（工具层）：browse_page。

只做参数校验 + 调 browser 服务，不再自己写业务逻辑；执行前过
tools/executor.py 的权限检查（见 tools/registry.py 拼出来的
LangChain 工具）。

P4 浏览器隔离：先调本租户的 browser 容器（mt-browser-<租户>，
地址解析在 infra/browser_service.py），调不通再回退共享
browser（BROWSER_URL）；截图只存到本租户工作区——租户容器把
租户卷挂到 /data，截图写 /data/<租户>/workspace，正好是租户卷
里的 workspace；走共享 fallback 时截图落在共享卷，不在租户卷里，
返回里会注明。
"""
import inspect

try:
    from infra import browser_service as browser_svc
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from infra import browser_service as browser_svc


def browse_page_impl(tenant: str, url: str, screenshot: bool,
                     urlopen=None) -> str:
    """调 browser 服务打开页面；截图存到本租户工作区。

    urlopen 可注入（测试用 fake；不传就用 urllib.request.urlopen）。
    先试租户 browser 容器，失败回退共享；两边都失败时返回最后一次
    失败的原因（和 P3 之前单地址时的报错格式一致）。
    """
    if not url.startswith(("http://", "https://")):
        return "browse_page 只支持 http:// 或 https:// 开头的地址"
    try:
        d, via_tenant = browser_svc.browse_page(
            tenant, url, bool(screenshot), urlopen=urlopen)
    except RuntimeError as e:
        return str(e)
    out = f"标题：{d.get('title', '')}\n网址：{url}\n正文摘要：\n{d.get('text', '')[:2000]}"
    shot = d.get("screenshot_path", "")
    if shot:
        # browser 服务把截图写到本租户工作区，只返回相对路径；
        # 租户容器下它就在租户卷里，/files/ 能直接 serve。
        out += f"\n截图已保存：{shot}"
        if not via_tenant:
            out += "\n（注：租户浏览器不可用，本次走共享浏览器，截图不在租户卷里）"
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
