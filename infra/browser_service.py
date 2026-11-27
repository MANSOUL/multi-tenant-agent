"""基础设施层：浏览器服务按租户路由（P4 浏览器隔离）。

职责：只做“该调哪个 browser 服务”的地址解析 + HTTP 调用，
不认识聊天、提醒、记忆。租户隔离规则：
- 每租户一个 browser 容器 mt-browser-<租户>（网关在登录时按需
  docker run 起，见 gateway/infra/runtime.py 的
  ensure_browser_container；容器把租户卷 mt-data-<租户> 挂到
  /data，所以截图写 /data/<租户>/workspace 时正好落在租户卷
  的 workspace 里，/files/ 能直接serve）。
- 共享 browser（BROWSER_URL，默认 http://browser:8001）只做
  fallback：租户容器起不来 / 调不通时走共享，并记日志；截图
  此时落在共享卷，不在租户卷里，调用方要在返回里说清楚。
- 本模块只用标准库：容器存活判断由调用方注入（网关传 docker
  状态，agent 侧直接试调 + 失败回退），不 import docker。

browser/browser.py 本身不用改逻辑：它本来就按请求里的 tenant
写 /data/<tenant>/workspace；换了挂载点（租户卷）后，同一份
代码写出来的截图自然进了租户卷。
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BROWSER_PORT = 8001
DEFAULT_SHARED_URL = "http://browser:8001"


def browser_container_name(tenant: str) -> str:
    """租户 browser 容器的名字（和网关侧同名函数保持一致）。"""
    return f"mt-browser-{tenant}"


def browser_url_for_tenant(tenant: str) -> str:
    """租户 browser 容器的内网地址：容器名就是 Docker 网络里的 DNS 名。"""
    return f"http://{browser_container_name(tenant)}:{BROWSER_PORT}"


def shared_browser_url() -> str:
    """共享 browser 的地址（BROWSER_URL 环境变量，默认指向 compose
    里常驻的 browser 服务）。"""
    return os.environ.get("BROWSER_URL", DEFAULT_SHARED_URL).rstrip("/")


def resolve_browser_url(tenant: str, container_running=None):
    """解析该租户该调哪个 browser 服务。

    container_running 是调用方注入的存活判断（传容器名返回
    True/False；不传表示“不知道”，默认先认租户容器，让调用方
    试调、失败再回退）。返回 (url, via_tenant)：
    - 租户容器在跑 → (租户地址, True)；
    - 租户容器明确不在跑 → (共享地址, False)，并记一条 warning；
    - 不知道 → (租户地址, True)，试调失败时调用方再回退。
    """
    if container_running is not None and not container_running(
            browser_container_name(tenant)):
        logger.warning("租户浏览器容器不在跑，走共享 browser fallback："
                       "tenant=%s", tenant)
        return shared_browser_url(), False
    return browser_url_for_tenant(tenant), True


def post_browse(base_url: str, tenant: str, url: str, screenshot: bool,
                urlopen=None) -> dict:
    """往一个 browser 服务 POST /browse，返回解析后的 dict。

    urlopen 可注入（测试用 fake）；不传就用 urllib.request.urlopen。
    HTTPError / URLError 原样抛给调用方，由它决定要不要回退。
    body 里带 error 时也原样返回（调用方按“打开页面失败”处理）。
    """
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/browse",
        data=json.dumps({
            "url": url,
            "screenshot": bool(screenshot),
            "tenant": tenant,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(req, timeout=60) as resp:
        return json.loads(resp.read())


def browse_page(tenant: str, url: str, screenshot: bool,
                urlopen=None, container_running=None):
    """调 browser 服务打开页面：先试租户容器，失败回退共享。

    返回 (result_dict, via_tenant)：via_tenant 表示最后是哪边
    成功的。两边都失败时抛 RuntimeError，消息里写清楚最后一次
    失败的原因（调用方原样返回给用户）。
    """
    primary_url, via_tenant = resolve_browser_url(
        tenant, container_running=container_running)
    last_error = None
    for base_url, is_tenant in ((primary_url, via_tenant),
                                (shared_browser_url(), False)):
        if not is_tenant and base_url == primary_url:
            continue  # 主地址本身就是共享地址时不重复试
        try:
            result = post_browse(base_url, tenant, url, screenshot,
                                 urlopen=urlopen)
        except urllib.error.HTTPError as e:
            last_error = (f"浏览器服务返回 {e.code}："
                          f"{e.read().decode()[:300]}")
            logger.warning("browser 请求失败（%s），尝试回退：tenant=%s "
                           "err=%s", base_url, tenant, last_error)
            continue
        except urllib.error.URLError as e:
            last_error = (f"连不上浏览器服务（{base_url}）：{e.reason}")
            logger.warning("browser 请求失败（%s），尝试回退：tenant=%s "
                           "err=%s", base_url, tenant, last_error)
            continue
        if result.get("error"):
            last_error = f"打开页面失败：{result['error']}"
            logger.warning("browser 返回错误（%s），尝试回退：tenant=%s "
                           "err=%s", base_url, tenant, last_error)
            continue
        if not is_tenant:
            logger.warning("租户浏览器不可用，已回退到共享 browser："
                           "tenant=%s url=%s", tenant, url)
        return result, is_tenant
    raise RuntimeError(last_error or "浏览器服务不可用")
