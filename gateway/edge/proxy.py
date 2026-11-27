"""Edge 层（网关数据面）：反向代理——把请求透传到租户容器，原样透传上游 Content-Type。

只做转发，不认识聊天、提醒、记忆、文件内容。调用方（gateway/main.py
的 Handler._proxy）先校验 token 再调这里；这里只负责 ensure 租户容器
在运行、把请求发过去、把响应（状态码 + body + Content-Type）原样带回来。
"""
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

try:
    from .auth import token_from
    from ..infra.runtime import (
        agent_url,
        container_name,
        ensure_tenant_container,
        touch,
    )
    from errors.codes import upstream_unavailable
except ImportError:  # 以脚本方式直接跑某个网关模块时，包相对导入不可用
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from gateway.edge.auth import token_from
    from gateway.infra.runtime import (
        agent_url,
        container_name,
        ensure_tenant_container,
        touch,
    )
    from errors.codes import upstream_unavailable


def proxy_request(handler, tenant: str, ensure_fn=None) -> None:
    """校验通过后的代理：ensure 租户容器 → 透传到该租户的容器，带上租户身份。

    handler 是 BaseHTTPRequestHandler（或测试用的同形 fake），要提供
    .path / .command / .headers / .rfile / ._send() / ._send_error()。
    ensure_fn 可注入（默认 infra/runtime.ensure_tenant_container），
    方便测试时换成 fake，不用真起容器。
    上游的 Content-Type 原样透传：/files/ 的 html 按 text/html、图片按
    image/*，绝不能一律套上 json 头（以前是 get_content_type() +
    "; charset=utf-8"，图片也会被标成带 charset 的类型，浏览器可能不认）。
    """
    ensure = ensure_fn or ensure_tenant_container
    try:
        ensure(tenant)
    except RuntimeError as e:
        return handler._send_error(upstream_unavailable(
            f"租户容器不可用：{e}"))
    length = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(length) if length else None
    headers = {
        "Content-Type": handler.headers.get("Content-Type",
                                            "application/json"),
        "X-Tenant-Id": tenant,
        "X-Tenant-Token": token_from(handler),
    }
    req = urllib.request.Request(
        agent_url(tenant) + handler.path, data=body, headers=headers,
        method=handler.command)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
            touch(tenant)
            ctype = resp.headers.get("Content-Type") or \
                "application/octet-stream"
            return handler._send(resp.status, data, ctype)
    except HTTPError as e:
        touch(tenant)
        ctype = e.headers.get("Content-Type") or \
            "application/octet-stream"
        return handler._send(e.code, e.read(), ctype)
    except Exception as e:  # noqa: BLE001 - 代理失败时把原因告诉前端
        return handler._send_error(upstream_unavailable(
            f"连不上租户容器 {container_name(tenant)}：{e}"))


__all__ = ["proxy_request"]
