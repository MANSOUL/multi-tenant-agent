"""Edge 层（网关数据面）：登录、token 签发/校验、cookie 读取。

只认“用户名/密码对不对”和“token 是不是网关签的”，不认识聊天、提醒、
记忆、文件内容。登录成功后调 infra/runtime.ensure_fresh() 比对一次
镜像 digest（P2：镜像变了自动重建租户容器），再调 ensure 起容器；
digest 比对失败时 fail-open，不拦登录。
"""
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from ..infra.runtime import (
        container_name,
        ensure_browser_container,
        ensure_fresh,
        ensure_tenant_container,
        is_valid_tenant,
    )
    from errors.codes import (
        bad_request,
        unauthenticated,
        upstream_unavailable,
    )
except ImportError:  # 以脚本方式直接跑某个网关模块时，包相对导入不可用
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from gateway.infra.runtime import (
        container_name,
        ensure_browser_container,
        ensure_fresh,
        ensure_tenant_container,
        is_valid_tenant,
    )
    from errors.codes import (
        bad_request,
        unauthenticated,
        upstream_unavailable,
    )

logger = logging.getLogger(__name__)

SECRET = os.environ.get("GATEWAY_SECRET", "change-me-to-a-long-random-string")

# token 有效期 12 小时（和原来 gateway.py 一致）。
TOKEN_TTL_HOURS = 12


def load_users() -> dict:
    """GATEWAY_USERS=demo:demo123,tenant-a:changeme-a"""
    users = {}
    for pair in os.environ.get("GATEWAY_USERS", "demo:demo123").split(","):
        if ":" in pair:
            name, pw = pair.split(":", 1)
            users[name.strip()] = pw.strip()
    return users


def make_token(tenant: str) -> str:
    exp = int((datetime.now(timezone.utc)
               + timedelta(hours=TOKEN_TTL_HOURS)).timestamp())
    sig = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{tenant}.{exp}.{sig}"


def verify_token(token: str) -> str | None:
    try:
        tenant, exp, sig = token.split(".")
        if not is_valid_tenant(tenant):
            return None
        if int(exp) < int(datetime.now(timezone.utc).timestamp()):
            return None
        expect = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                          hashlib.sha256).hexdigest()
        return tenant if hmac.compare_digest(expect, sig) else None
    except (ValueError, TypeError):
        return None


def token_from(handler) -> str:
    """从请求里拿 token：优先 Authorization: Bearer，其次 mt_token cookie。

    /files/* 是浏览器直接打开的，不带 Authorization 头，靠登录时种的
    cookie 认身份，所以两条路都要认。
    """
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("mt_token="):
            return part[len("mt_token="):]
    return ""


def _load_login_page() -> str:
    """读登录页静态文件（gateway/web/login.html）；后端只读文件返回。"""
    return (Path(__file__).resolve().parent.parent / "web" / "login.html"
            ).read_text(encoding="utf-8")


LOGIN_HTML = _load_login_page()


def attempt_login(username: str, password: str, users: dict | None = None,
                  ensure_fresh_fn=None, ensure_container_fn=None,
                  ensure_browser_fn=None) -> str:
    """登录路径（edge/auth.py 是调用 runtime.ensure_fresh 的入口）。

    密码对上 → 先 ensure_fresh() 比对镜像 digest（变了就自动重建租户
    容器；docker 不可用时 fail-open，只记日志，不拦登录）→ 再
    ensure_tenant_container() 起容器 → 再 ensure_browser_container()
    起该租户的 browser 容器（P4：起不来只记日志、走共享 fallback，
    不拦登录）→ 返回 HMAC 签名 token。
    失败时抛 ApiError（调用方直接 _send_error）：密码不对 401、租户 id
    不合法 400、容器起不来 502。ensure_* 可注入，方便测试。
    """
    users = users if users is not None else load_users()
    name = (username or "").strip()
    if name not in users or users[name] != (password or ""):
        raise unauthenticated("用户名或密码不对")
    if not is_valid_tenant(name):
        raise bad_request(f"租户 id 不合法：{name!r}，只允许字母数字 _ -")
    fresh_fn = ensure_fresh_fn or ensure_fresh
    try:
        fresh_fn(name)
    except Exception:  # noqa: BLE001 - digest 比对绝不能拦登录
        logger.warning("登录时 digest 比对异常，fail-open 继续登录："
                       "tenant=%s", name, exc_info=True)
    ensure_fn = ensure_container_fn or ensure_tenant_container
    try:
        ensure_fn(name)
    except RuntimeError as e:
        raise upstream_unavailable(f"租户容器启动失败：{e}")
    browser_fn = ensure_browser_fn or ensure_browser_container
    try:
        browser_fn(name)
    except Exception:  # noqa: BLE001 - 浏览器起不来走共享，不拦登录
        logger.warning("登录时保障租户浏览器容器异常，走共享 fallback："
                       "tenant=%s", name, exc_info=True)
    return make_token(name)


__all__ = [
    "LOGIN_HTML",
    "SECRET",
    "attempt_login",
    "container_name",
    "load_users",
    "make_token",
    "token_from",
    "verify_token",
]
