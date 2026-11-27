"""网关 thin 入口：只组装 + 启动，不含业务逻辑。

组装：读 env → 建各模块（edge/auth 登录、edge/proxy 代理、
infra/runtime 容器编排、control/tenants 管理面）→ 起 HTTP server。
业务逻辑都在各模块里：登录/token 在 edge/auth.py，代理在
edge/proxy.py，Docker 编排/闲置回收/digest 比对在 infra/runtime.py，
租户开通/删除/配额在 control/tenants.py。

对外：8080（登录 + 代理）。对内：管理 API 单独监听 127.0.0.1:8082，
需要 GATEWAY_ADMIN_TOKEN，缺省则不启动（见 control/tenants.py 的
start_admin_server），只能 docker exec 进容器用 curl 调。

运行：python3 -m gateway.main（仓库根目录 / 容器 /app 下）。
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .control.tenants import TenantControl, start_admin_server
    from .edge.auth import (
        LOGIN_HTML,
        attempt_login,
        load_users,
        token_from,
        verify_token,
    )
    from .edge.proxy import proxy_request
    from .infra.runtime import (
        _reaper_loop,
        container_name,
        ensure_browser_container,
        ensure_fresh,
        ensure_tenant_container,
        reap_idle,
    )
    from errors.codes import ApiError, not_found, unauthenticated
except ImportError:  # 以脚本方式跑 python3 gateway/main.py 时
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gateway.control.tenants import TenantControl, start_admin_server
    from gateway.edge.auth import (
        LOGIN_HTML,
        attempt_login,
        load_users,
        token_from,
        verify_token,
    )
    from gateway.edge.proxy import proxy_request
    from gateway.infra.runtime import (
        _reaper_loop,
        container_name,
        ensure_browser_container,
        ensure_fresh,
        ensure_tenant_container,
        reap_idle,
    )
    from errors.codes import ApiError, not_found, unauthenticated

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "8080"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8082"))
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")

# 登录共用的用户表：control/tenants.py 开通/改密码改的就是这个 dict
#（同一对象），新租户不用重启网关就能登录；.env 持久化见 TenantControl。
USERS = load_users()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, err: ApiError):
        """按统一错误结构发 JSON 错误响应（状态码由 ApiError 自带）。"""
        self._send(err.status, json.dumps(err.body(), ensure_ascii=False),
                   "application/json")

    def _read_json(self) -> dict:
        try:
            return json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, LOGIN_HTML)
        if self.path.startswith("/api/") or self.path.startswith("/files/"):
            return self._proxy()
        return self._send_error(not_found())

    def do_POST(self):
        if self.path == "/api/login":
            payload = self._read_json()
            name = (payload.get("username") or "").strip()
            try:
                token = attempt_login(
                    name, payload.get("password") or "", users=USERS,
                    ensure_fresh_fn=ensure_fresh,
                    ensure_container_fn=ensure_tenant_container,
                    ensure_browser_fn=ensure_browser_container)
            except ApiError as e:
                return self._send_error(e)
            return self._send(200, json.dumps(
                {"token": token, "tenant": name,
                 "container": container_name(name)},
                ensure_ascii=False),
                "application/json",
                {"Set-Cookie": f"mt_token={token}; Path=/; HttpOnly; SameSite=Lax"})
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send_error(not_found())

    def _proxy(self):
        """校验 token → 透传到该租户的容器（ensure + 转发在 edge/proxy.py）。"""
        tenant = verify_token(token_from(self))
        if not tenant:
            return self._send_error(unauthenticated("未登录：请先登录"))
        return proxy_request(self, tenant,
                             ensure_fn=ensure_tenant_container)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # 管理面：只监听回环；没配 GATEWAY_ADMIN_TOKEN 时拒绝服务（返回 None）。
    # .env 持久化只在 .env 文件存在时写（TenantControl._persist_users），
    # 不存在不新建；部署时 .env 由 compose 的 env_file 提供。
    env_file = Path(".env")
    control = TenantControl(
        users=USERS,
        users_file=env_file if env_file.exists() else None)
    admin_server = start_admin_server(
        control, host="127.0.0.1", port=ADMIN_PORT,
        admin_token=ADMIN_TOKEN or None)
    if admin_server is not None:
        threading.Thread(target=admin_server.serve_forever,
                         daemon=True, name="admin-server").start()
    threading.Thread(target=_reaper_loop, daemon=True,
                     name="tenant-reaper").start()
    print(f"网关跑起来了：http://localhost:{PORT}  → 每租户一个容器 "
          f"（闲置回收每 60 秒扫一次，登录时比对镜像 digest 自动重建）")
    if admin_server is None:
        print("管理接口未启动：未配置 GATEWAY_ADMIN_TOKEN（只限回环的管理口）")
    else:
        print(f"管理接口：http://127.0.0.1:{ADMIN_PORT}（只限回环，"
              "需 X-Admin-Token，可 docker exec 进容器调）")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()


__all__ = [
    "ADMIN_PORT",
    "ADMIN_TOKEN",
    "PORT",
    "USERS",
    "Handler",
    "LOGIN_HTML",
    "attempt_login",
    "container_name",
    "ensure_browser_container",
    "ensure_fresh",
    "ensure_tenant_container",
    "load_users",
    "main",
    "proxy_request",
    "reap_idle",
    "token_from",
    "verify_token",
]
