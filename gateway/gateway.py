"""网关：演示登录 + 租户容器管理 + 反向代理（只用 Python 标准库）。

一租户一容器（2026-11-26 重构）：不再调一个共享 agent-api，
每个租户单独一个 agent 容器（mt-tenant-<租户>），数据在该租户的
专属 Docker volume（mt-data-<租户>）里。

流程：
  1. 用户打开 http://<宿主机>:8080 ，看到登录页。
  2. POST /api/login，账号密码对上 GATEWAY_USERS 里的演示账号，
     就发一个 HMAC 签名 token（tenant.过期时间.签名），有效期 12 小时，
     存在 cookie 里，同时返回给前端。登录时会把该租户的容器 ensure 起来
     （不存在就 docker run 创建，停了就 docker start，起来后等
     /api/health 通了再返回）。
  3. 之后所有 /api/* 和 /files/* 请求，网关先校验 token，再 ensure 该租户的容器，
     校验通过才反向代理到 http://mt-tenant-<租户>:8081，并在转发时带上
     X-Tenant-Id 和 X-Tenant-Token。校验不过直接 401，不会透传。
     /files/* 是租户工作区里的网页/静态文件（agent 的 /files/ 接口），
     浏览器直接打开时不带 Authorization 头，靠登录时种的 mt_token cookie
     认身份（token_from() 会读 cookie）；上游的 Content-Type 原样透传，
     不许给 html/图片套 json 头。
  4. 闲置回收：后台线程每 60 秒检查一次，租户容器最后活跃超过
     TENANT_IDLE_MINUTES（默认 30 分钟）就 docker stop（不停删，
     volume 保留，下次登录/聊天时再 docker start）。

只用标准库 + subprocess 调 docker CLI，不引入新依赖。网关容器需要
挂载 /var/run/docker.sock 才能调宿主机 Docker（见 docker-compose.yml
和 README 安全提醒：拿到 docker.sock 就等于拿到宿主机 root，只能在
自己可信的单机上用）。

⚠️ 这只是演示登录：密码明文写在 .env 的 GATEWAY_USERS 里，没有防爆破、
没有改密码、没有多因素。生产请换成 Keycloak / Authentik（OIDC），
把 GATEWAY_USERS 和这套 token 逻辑整个替换掉，agent 容器不用改——
它只认 X-Tenant-Token / X-Tenant-Id，见 agent/tenant.py 的 get_tenant。

P1（2026-11-27）：登录页已从内嵌字符串拆成 gateway/web/login.html
（后端只读文件返回）；错误响应统一走 errors/（{"error": {"code": ...,
"message": ...}}），状态码不变。构建时注意：gateway/Dockerfile 要把
gateway/web/ 和顶层 errors/ 一起拷进镜像（见 Dockerfile 注释）。
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

try:
    from errors.codes import (
        ApiError, bad_request, not_found, unauthenticated,
        upstream_unavailable,
    )
except ImportError:  # 以脚本方式运行 python3 gateway/gateway.py 时
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from errors.codes import (
        ApiError, bad_request, not_found, unauthenticated,
        upstream_unavailable,
    )

PORT = int(os.environ.get("PORT", "8080"))
SECRET = os.environ.get("GATEWAY_SECRET", "change-me-to-a-long-random-string")

# --- 租户容器配置（见 .env.example）---
# compose 默认建的网络名就是 <项目目录名>_default；用 -p 改过项目名时改这里。
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "multi-tenant-agent_default")
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "multi-tenant-agent-agent-api:latest")
TENANT_CPU = os.environ.get("TENANT_CPU", "0.5")
TENANT_MEMORY = os.environ.get("TENANT_MEMORY", "512m")
IDLE_MINUTES = int(os.environ.get("TENANT_IDLE_MINUTES", "30") or 30)
# 容器启动后等 /api/health 通的最长秒数（首次 docker run 拉镜像不算，
# 镜像必须事先 build 好，见 README 快速开始）。
HEALTH_WAIT_SECONDS = int(os.environ.get("TENANT_HEALTH_WAIT", "60") or 60)

# 租户 id 只允许字母数字 _ - ，防止路径穿越、容器名注入和奇怪的租户名。
# 容器名/卷名/环境变量都由它拼出来，所以必须先校验再拼命令；docker 调用
# 全部用 argv 列表（不用 shell=True），租户 id 进不了 shell。
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 转发给租户容器的环境变量：LLM_* 等从网关自己的环境继承（compose 里
# gateway 用了 env_file: .env，所以这些值都在）。只转白名单里的，
# GATEWAY_SECRET 也转过去，agent 容器靠它校验 token（TOKEN_SECRET 为空时
# 回退读 GATEWAY_SECRET，见 agent/config.py）。
FORWARD_ENVS = [
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "MAX_TOOL_ROUNDS", "CONTEXT_MAX_TURNS", "CONTEXT_MAX_CHARS",
    "SUMMARY_THRESHOLD_TURNS", "GATEWAY_SECRET", "TOKEN_SECRET",
    "SANDBOX_MODE", "SANDBOX_IMAGE", "BROWSER_URL",
]


def is_valid_tenant(tenant: str) -> bool:
    return bool(tenant and TENANT_RE.fullmatch(tenant))


def container_name(tenant: str) -> str:
    return f"mt-tenant-{tenant}"


def volume_name(tenant: str) -> str:
    return f"mt-data-{tenant}"


def agent_url(tenant: str) -> str:
    """租户容器的内网地址：容器名就是 Docker 网络里的 DNS 名。"""
    return f"http://{container_name(tenant)}:8081"


def load_users() -> dict:
    """GATEWAY_USERS=demo:demo123,tenant-a:changeme-a"""
    users = {}
    for pair in os.environ.get("GATEWAY_USERS", "demo:demo123").split(","):
        if ":" in pair:
            name, pw = pair.split(":", 1)
            users[name.strip()] = pw.strip()
    return users


def make_token(tenant: str) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())
    sig = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{tenant}.{exp}.{sig}"


def verify_token(token: str) -> str | None:
    try:
        tenant, exp, sig = token.split(".")
        if not TENANT_RE.fullmatch(tenant):
            return None
        if int(exp) < int(datetime.now(timezone.utc).timestamp()):
            return None
        expect = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                          hashlib.sha256).hexdigest()
        return tenant if hmac.compare_digest(expect, sig) else None
    except (ValueError, TypeError):
        return None


def token_from(handler) -> str:
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("mt_token="):
            return part[len("mt_token="):]
    return ""


# ---------------------------------------------------------------------------
# 租户容器管理：docker CLI 全部走 subprocess argv 列表，不用 shell。
# _docker() 是唯一调 docker 的地方，测试时 monkeypatch 它就能验证
# ensure 的调用序列，不用真的起容器。
# ---------------------------------------------------------------------------

def _docker(argv, timeout=120):
    """跑一条 docker 命令。返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(["docker"] + argv, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", f"docker 命令执行失败：{e}"


def _http_ok(url: str, timeout=2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 健康检查不通就是没就绪，不报错
        return False


def docker_run_argv(tenant: str) -> list:
    """创建租户容器的 docker run 参数（纯函数，方便测试）。

    - 专属 volume mt-data-<租户> 挂到 /app/data：SQLite 和 workspace 都在里面，
      容器删了数据还在；volume 不存在时 docker 会自动建。
    - 资源上限 --cpus / --memory（TENANT_CPU / TENANT_MEMORY 可配）。
    - 沙箱还要起每会话容器，所以也挂 /var/run/docker.sock（只读），
      见 README 安全提醒；SANDBOX_VOLUME 告诉沙箱直接挂卷而不是挂宿主机路径。
    """
    argv = [
        "run", "-d",
        "--name", container_name(tenant),
        "--network", DOCKER_NETWORK,
        "--restart", "unless-stopped",
        "--cpus", TENANT_CPU,
        "--memory", TENANT_MEMORY,
        "-v", f"{volume_name(tenant)}:/app/data",
        "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
        "-e", "PORT=8081",
        "-e", "DATA_DIR=/app/data",
        "-e", f"TENANT_ID={tenant}",
        "-e", f"SANDBOX_VOLUME={volume_name(tenant)}",
    ]
    for key in FORWARD_ENVS:
        if key in os.environ:
            argv += ["-e", f"{key}={os.environ[key]}"]
    argv.append(AGENT_IMAGE)
    return argv


def container_state(tenant: str) -> str:
    """租户容器状态：running / stopped / missing。"""
    rc, out, _ = _docker(
        ["inspect", "-f", "{{.State.Running}}", container_name(tenant)],
        timeout=30)
    if rc != 0:
        return "missing"
    return "running" if out.strip() == "true" else "stopped"


# 最后活跃时间：{租户: 时间戳}，每次代理成功后更新；回收线程只看这里。
_last_active: dict = {}
_last_active_lock = threading.Lock()


def touch(tenant: str, now: float | None = None) -> None:
    with _last_active_lock:
        _last_active[tenant] = now if now is not None else time.time()


def should_stop(last_active_ts: float, idle_minutes: int,
                now: float | None = None) -> bool:
    """闲置判断（纯函数，方便测试）：超过 idle 分钟没活跃就该停。"""
    now = now if now is not None else time.time()
    return (now - last_active_ts) > idle_minutes * 60


def ensure_tenant_container(tenant: str) -> None:
    """保证租户容器在运行且 /api/health 已通，否则抛 RuntimeError。

    不存在 → docker run 创建；已停止 → docker start；已在跑 →
    直接等健康检查。启动后轮询 /api/health 直到就绪或超时。
    """
    if not is_valid_tenant(tenant):
        raise RuntimeError(f"租户 id 不合法：{tenant!r}")
    state = container_state(tenant)
    if state == "missing":
        rc, _out, err = _docker(docker_run_argv(tenant), timeout=180)
        if rc != 0:
            raise RuntimeError(f"创建租户容器失败：{err or 'docker run 失败'}")
    elif state == "stopped":
        rc, _out, err = _docker(["start", container_name(tenant)], timeout=60)
        if rc != 0:
            raise RuntimeError(f"启动租户容器失败：{err or 'docker start 失败'}")
    deadline = time.time() + HEALTH_WAIT_SECONDS
    while time.time() < deadline:
        if _http_ok(agent_url(tenant) + "/api/health"):
            touch(tenant)
            return
        time.sleep(1)
    raise RuntimeError(
        f"租户容器 {container_name(tenant)} 启动超时，"
        f"/api/health 在 {HEALTH_WAIT_SECONDS} 秒内一直不通")


def reap_idle(now: float | None = None) -> list:
    """停掉所有超过闲置时长的租户容器（只 stop 不 rm，volume 保留）。

    返回这次停掉的租户列表。只停 _last_active 里记过的租户——
    也就是网关经手过的；网关重启后丢了记录的容器不会被误停，
    下次该租户登录/聊天时 ensure 会重新接管。
    """
    now = now if now is not None else time.time()
    stopped = []
    with _last_active_lock:
        candidates = list(_last_active.items())
    for tenant, ts in candidates:
        if should_stop(ts, IDLE_MINUTES, now):
            _docker(["stop", container_name(tenant)], timeout=60)
            with _last_active_lock:
                _last_active.pop(tenant, None)
            stopped.append(tenant)
    return stopped


def _reaper_loop() -> None:
    while True:
        time.sleep(60)
        try:
            reap_idle()
        except Exception:  # noqa: BLE001 - 回收失败不能把网关拖垮
            pass


def _load_login_page() -> str:
    """读登录页静态文件（gateway/web/login.html）；后端只读文件返回。"""
    return (Path(__file__).resolve().parent / "web" / "login.html").read_text(
        encoding="utf-8")


LOGIN_HTML = _load_login_page()


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
            users = load_users()
            name = (payload.get("username") or "").strip()
            if name in users and users[name] == (payload.get("password") or ""):
                if not is_valid_tenant(name):
                    return self._send_error(bad_request(
                        f"租户 id 不合法：{name!r}，只允许字母数字 _ -"))
                try:
                    ensure_tenant_container(name)
                except RuntimeError as e:
                    return self._send_error(upstream_unavailable(
                        f"租户容器启动失败：{e}"))
                token = make_token(name)
                return self._send(200, json.dumps(
                    {"token": token, "tenant": name,
                     "container": container_name(name)},
                    ensure_ascii=False),
                    "application/json",
                    {"Set-Cookie": f"mt_token={token}; Path=/; HttpOnly; SameSite=Lax"})
            return self._send_error(unauthenticated("用户名或密码不对"))
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send_error(not_found())

    def _proxy(self):
        """校验 token → ensure 租户容器 → 透传到该租户的容器，带上租户身份。"""
        tenant = verify_token(token_from(self))
        if not tenant:
            return self._send_error(unauthenticated("未登录：请先登录"))
        try:
            ensure_tenant_container(tenant)
        except RuntimeError as e:
            return self._send_error(upstream_unavailable(
                f"租户容器不可用：{e}"))
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "X-Tenant-Id": tenant,
            "X-Tenant-Token": token_from(self),
        }
        req = urllib.request.Request(
            agent_url(tenant) + self.path, data=body, headers=headers,
            method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
                touch(tenant)
                # 上游的 Content-Type 原样透传：/files/ 的 html 按 text/html、
                # 图片按 image/*，绝不能像以前那样一律套上 json 头
                # （以前是 get_content_type() + "; charset=utf-8"，图片也会被
                # 标成带 charset 的类型，浏览器可能不认）。
                ctype = resp.headers.get("Content-Type") or \
                    "application/octet-stream"
                return self._send(resp.status, data, ctype)
        except HTTPError as e:
            touch(tenant)
            ctype = e.headers.get("Content-Type") or \
                "application/octet-stream"
            return self._send(e.code, e.read(), ctype)
        except Exception as e:  # noqa: BLE001 - 代理失败时把原因告诉前端
            return self._send_error(upstream_unavailable(
                f"连不上租户容器 {container_name(tenant)}：{e}"))


if __name__ == "__main__":
    threading.Thread(target=_reaper_loop, daemon=True).start()
    print(f"网关跑起来了：http://localhost:{PORT}  → 每租户一个容器 "
          f"（{AGENT_IMAGE}，网络 {DOCKER_NETWORK}，"
          f"闲置 {IDLE_MINUTES} 分钟回收）")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
