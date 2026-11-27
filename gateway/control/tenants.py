"""控制面（Control Plane）：租户开通/删除/改密码/配额/用量汇总 + 只对内的管理 API。

不在聊天请求路径上，只对内、不对外暴露：管理 API 只监听 127.0.0.1
（与对外 8080 分开的内网端口，默认 8082），需要管理 token
（环境变量 GATEWAY_ADMIN_TOKEN）；缺省则管理接口拒绝服务（不启动
监听，只记日志），不是“无鉴权放行”。

Docker 操作不直接写 subprocess：走注入的 runtime 接口（默认
infra/runtime.DockerRuntime，薄包装 subprocess docker CLI；测试时
换 fake）。租户 id 规则、容器/卷命名复用 infra/runtime，不自己再
拼一份。

用量汇总：按 ARCHITECTURE.md 应读 model/metering 的统计，但计量是
P3 才有的模块（model/metering.py），P2 先留接口——usage_summary()
返回空统计（调用 0 次、token 0），并在返回值里注明 P3 补齐，不要
假装有真实用量数据。

P3 补齐：usage_summary() 接上真实数据——通过注入的 metering_reader
（callable，tenant -> model/metering.summarize 形状的 dict）或
metering_dir（租户数据目录，<dir>/<tenant>/memory.db 里读 metering
表，只读打开）读真实汇总；两个都没配时仍返回 P2 的空统计
（metering="not_available"），数字 0 不代表实际用量。
注意：标准 compose 部署里网关容器只挂了 docker.sock，没有挂租户
卷，所以网关进程默认读不到租户库——要让管理接口看到真实用量，
得把租户数据目录共享给网关（MT_METERING_DIR）或由 agent 侧上报，
见 gateway/main.py 的说明；没配时不要假装有数。
"""
import hmac
import json
import logging
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from ..infra.runtime import (
        DockerRuntime,
        container_name,
        is_valid_tenant,
        volume_name,
    )
    from errors.codes import (
        ApiError,
        bad_request,
        internal_error,
        not_found,
        unauthenticated,
        upstream_unavailable,
    )
except ImportError:  # 以脚本方式直接跑某个网关模块时，包相对导入不可用
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from gateway.infra.runtime import (
        DockerRuntime,
        container_name,
        is_valid_tenant,
        volume_name,
    )
    from errors.codes import (
        ApiError,
        bad_request,
        internal_error,
        not_found,
        unauthenticated,
        upstream_unavailable,
    )

logger = logging.getLogger(__name__)

# 密码里不许出现的字符：GATEWAY_USERS 的格式是 user:pass,user:pass，
# 冒号/逗号/空白会把解析切碎；换行更不许进 .env。
_FORBIDDEN_PASSWORD_CHARS = set(":, \t\r\n")


def _validate_password(password: str) -> str:
    password = password or ""
    if not password:
        raise ValueError("密码不能为空")
    if any(c in _FORBIDDEN_PASSWORD_CHARS for c in password):
        raise ValueError("密码里不能含冒号、逗号、空白或换行"
                         "（GATEWAY_USERS 格式要求）")
    return password


def generate_password(length_bytes: int = 12) -> str:
    """生成初始密码：secrets.token_urlsafe，只含字母数字 - _，能直接写进
    GATEWAY_USERS（不含冒号/逗号）。只在开通时返回一次，调用方自己保管。"""
    return secrets.token_urlsafe(length_bytes)


class TenantControl:
    """租户管理操作：开通/删除/改密码/配额/用量汇总。

    users 是与网关登录共用的 dict（同一对象，main.py 里建好传进来，
    开通/改密码后新租户不用重启网关就能登录）。users_file 是可选的
    .env 文件路径：配了就把 GATEWAY_USERS 那一行写回去（其它行原样
    保留），没配（文件不存在）就只改内存并记日志——这时重启后新租户
    会丢，需要运维把 GATEWAY_USERS 同步进部署环境，见 _persist_users
    的注释。绝不新建 .env 文件。
    """

    def __init__(self, users: dict | None = None, users_file=None,
                 runtime=None, cpu: str | None = None,
                 memory: str | None = None, metering_reader=None,
                 metering_dir=None):
        self.users = users if users is not None else {}
        self.users_file = Path(users_file) if users_file else None
        self.runtime = runtime if runtime is not None else DockerRuntime()
        self.cpu = cpu or os.environ.get("TENANT_CPU", "0.5")
        self.memory = memory or os.environ.get("TENANT_MEMORY", "512m")
        # P3 计量来源（二选一，不配就是 P2 的空统计）：
        # metering_reader：callable，tenant -> summarize 形状的 dict；
        # metering_dir：租户数据目录（<dir>/<tenant>/memory.db，只读）。
        self.metering_reader = metering_reader
        self.metering_dir = (Path(metering_dir) if metering_dir
                             else (Path(os.environ.get("MT_METERING_DIR", ""))
                                   if os.environ.get("MT_METERING_DIR")
                                   else None))

    # -- 持久化：只改 .env 里 GATEWAY_USERS 这一行 ----------------------
    def _persist_users(self) -> bool:
        """把当前 users 写回 users_file 的 GATEWAY_USERS 行。

        文件不存在或没配 users_file 时返回 False（只改内存，记日志，
        不新建文件、不报错——开通本身已成功，丢的是“重启后还认得”）。
        写失败（权限等）同样返回 False 并记日志，不让持久化失败把
        开通/改密码整个推翻。
        """
        if self.users_file is None or not self.users_file.exists():
            logger.warning("租户用户表未持久化：%s 不存在或未配置，"
                           "重启后新租户/新密码会丢，请同步 GATEWAY_USERS",
                           self.users_file)
            return False
        try:
            value = ",".join(f"{k}:{v}" for k, v in sorted(self.users.items()))
            text = self.users_file.read_text(encoding="utf-8")
            lines = text.splitlines()
            replaced = False
            for i, line in enumerate(lines):
                if line.startswith("GATEWAY_USERS="):
                    lines[i] = f"GATEWAY_USERS={value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"GATEWAY_USERS={value}")
            self.users_file.write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")
            return True
        except OSError:
            logger.warning("写回 %s 失败，租户用户表只留在内存",
                           self.users_file, exc_info=True)
            return False

    # -- 租户操作 --------------------------------------------------------
    def list_tenants(self) -> list:
        return sorted(self.users)

    def create_tenant(self, tenant: str, password: str | None = None) -> dict:
        """开通租户：建卷 + 写用户条目 + 返回初始密码。

        password 不给就生成一个随机初始密码（只返回这一次）。
        租户已存在、id 不合法、密码含禁用字符时抛 ValueError；
        建卷失败抛 RuntimeError（docker 侧的原因原样带上）。
        """
        if not is_valid_tenant(tenant):
            raise ValueError(f"租户 id 不合法：{tenant!r}，"
                             "只允许字母数字 _ -")
        if tenant in self.users:
            raise ValueError(f"租户已存在：{tenant!r}")
        password = _validate_password(
            password if password is not None else generate_password())
        self.runtime.create_volume(tenant)
        self.users[tenant] = password
        self._persist_users()
        return {"tenant": tenant, "password": password,
                "volume": volume_name(tenant),
                "container": container_name(tenant)}

    def delete_tenant(self, tenant: str, confirm: bool = False) -> dict:
        """删除租户：停容器 + 删容器 + 删卷 + 删用户条目。

        必须显式 confirm=True，否则抛 ValueError（删卷 = 删该租户全部
        数据，不许一次误调用就删掉）。租户不存在抛 KeyError。
        容器已不存在时 stop/remove 失败不致命：跳过，继续删卷。
        """
        if not confirm:
            raise ValueError("删除租户需要显式确认：confirm=true"
                             "（删卷会删掉该租户全部数据）")
        if not is_valid_tenant(tenant) or tenant not in self.users:
            raise KeyError(tenant)
        state = "missing"
        try:
            state = self.runtime.container_state(tenant)
        except Exception:  # noqa: BLE001 - 查不到状态也继续往下删
            logger.warning("删除租户时查不到容器状态，继续删除：tenant=%s",
                           tenant, exc_info=True)
        if state != "missing":
            for op in ("stop_container", "remove_container"):
                try:
                    getattr(self.runtime, op)(tenant)
                except Exception:  # noqa: BLE001 - 容器已停/已删时跳过
                    logger.warning("删除租户时 %s 失败，继续删除：tenant=%s",
                                   op, tenant, exc_info=True)
        self.runtime.remove_volume(tenant)
        self.users.pop(tenant, None)
        self._persist_users()
        return {"tenant": tenant, "deleted": True}

    def change_password(self, tenant: str, new_password: str) -> dict:
        """改密码：只改用户条目（内存 + .env），不碰容器和卷。

        租户不存在抛 KeyError；新密码为空或含禁用字符抛 ValueError。
        返回里不带密码。
        """
        if not is_valid_tenant(tenant) or tenant not in self.users:
            raise KeyError(tenant)
        self.users[tenant] = _validate_password(new_password)
        self._persist_users()
        return {"tenant": tenant, "changed": True}

    def get_quota(self, tenant: str) -> dict:
        """配额查询：CPU/内存上限（网关配置）+ 当前容器状态。"""
        if not is_valid_tenant(tenant):
            raise ValueError(f"租户 id 不合法：{tenant!r}")
        try:
            state = self.runtime.container_state(tenant)
        except Exception:  # noqa: BLE001 - 查不到就报 unknown，不抛错
            state = "unknown"
        return {"tenant": tenant, "cpu": self.cpu, "memory": self.memory,
                "container_state": state}

    def _read_metering_from_dir(self, tenant: str) -> dict | None:
        """从 metering_dir/<tenant>/memory.db 只读汇总计量。

        没配 metering_dir、库文件不存在、或里面没有 metering 表时
        返回 None（调用方回落到空统计）。只读打开（mode=ro），
        绝不建库、不写库。汇总口径和 model/metering.summarize
        同一份 SQL（COUNT + SUM），不在这里另发明算法。
        """
        if self.metering_dir is None:
            return None
        db_path = self.metering_dir / tenant / "memory.db"
        if not db_path.is_file():
            return None
        try:
            import sqlite3
            from model import metering as metering_mod
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'metering'").fetchone()
                if not has_table:
                    return None
                return metering_mod.summarize(conn, tenant)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 计量读不到就回落空统计
            logger.warning("读租户 %s 的计量失败，回落空统计",
                           tenant, exc_info=True)
            return None

    def usage_summary(self, tenant: str, metering=None) -> dict:
        """用量汇总：每租户模型调用/token 用量统计。

        数据来源按优先级：本次调用传的 metering（dict 或 callable）
        > 构造时注入的 metering_reader > metering_dir 只读汇总 >
        都没有时返回 P2 的空统计（metering="not_available"，数字 0
        不代表实际用量）。真实数据的 cost_estimate 是 best-effort
        估算（先留 0.0），不承诺计费级精度。
        """
        if not is_valid_tenant(tenant):
            raise ValueError(f"租户 id 不合法：{tenant!r}")
        summary = None
        if metering is not None:
            summary = metering(tenant) if callable(metering) else metering
        elif self.metering_reader is not None:
            try:
                summary = self.metering_reader(tenant)
            except Exception:  # noqa: BLE001 - 读不到就回落空统计
                logger.warning("metering_reader 读租户 %s 用量失败",
                               tenant, exc_info=True)
                summary = None
        if summary is None:
            summary = self._read_metering_from_dir(tenant)
        if summary is None:
            return {"tenant": tenant, "model_calls": 0, "tokens_total": 0,
                    "tokens_input": 0, "tokens_output": 0,
                    "cost_estimate": None, "metering": "not_available",
                    "note": "用量计量是 P3（model/metering.py）才有的模块，"
                            "P2 先返回空统计，数字 0 不代表实际用量"}
        return {"tenant": tenant,
                "model_calls": summary.get("model_calls", 0),
                "tokens_total": summary.get("tokens_total", 0),
                "tokens_input": summary.get("tokens_input", 0),
                "tokens_output": summary.get("tokens_output", 0),
                "cost_estimate": summary.get("cost_estimate"),
                "metering": "available",
                "note": "best-effort 估算（token 按文本粗估，成本先留 0.0），"
                        "不承诺计费级精度"}


# ---------------------------------------------------------------------------
# 管理 API：只监听回环（127.0.0.1:8082，见 start_admin_server），与对外
# 8080 分开；每个请求都要过 X-Admin-Token 校验。
# ---------------------------------------------------------------------------

def make_admin_handler(control: TenantControl, admin_token: str | None):
    """造一个管理 API 的 Handler 类（BaseHTTPRequestHandler 子类）。

    路由（全部 JSON，错误走统一 {"error": {"code", "message"}} 结构）：
      GET    /admin/tenants                  列出全部租户
      GET    /admin/tenants/<t>/quota        配额查询
      GET    /admin/tenants/<t>/usage        用量汇总（P2 为空统计）
      POST   /admin/tenants                  开通 {"tenant", "password"?}
      POST   /admin/tenants/<t>/password     改密码 {"password"}
      DELETE /admin/tenants/<t>              删除，body 或 query 须带
                                             confirm=true，否则 400
    鉴权：请求头 X-Admin-Token 必须等于 admin_token，否则 401；
    admin_token 未配置时 start_admin_server 根本不启动监听（见下），
    这里再挡一道：返回 503 service_unavailable（errors/codes.py 里
    没有 503 码，这串是管理接口本地的，不进线上错误码表）。
    """

    class AdminHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, err: ApiError):
            self._send(err.status, err.body())

        def _read_json(self) -> dict:
            try:
                return json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            except ValueError:
                return {}

        def _check_admin(self) -> bool:
            if not admin_token:
                self._send(503, {"error": {
                    "code": "service_unavailable",
                    "message": "管理接口未配置 GATEWAY_ADMIN_TOKEN，拒绝服务"}})
                return False
            token = self.headers.get("X-Admin-Token", "")
            if not hmac.compare_digest(token, admin_token):
                self._send_error(unauthenticated("管理 token 不对"))
                return False
            return True

        def _route(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] != ["admin", "tenants"]:
                return None, None, None
            tenant = parts[2] if len(parts) > 2 else None
            action = parts[3] if len(parts) > 3 else None
            return parts, tenant, action

        def _handle(self):
            if not self._check_admin():
                return
            _, tenant, action = self._route()
            if tenant is None and self.command == "GET":
                return self._send(200, {"tenants": control.list_tenants()})
            if tenant is None and self.command == "POST":
                payload = self._read_json()
                try:
                    result = control.create_tenant(
                        (payload.get("tenant") or "").strip(),
                        payload.get("password"))
                except ValueError as e:
                    return self._send_error(bad_request(str(e)))
                except RuntimeError as e:
                    return self._send_error(upstream_unavailable(str(e)))
                return self._send(201, result)
            if tenant is not None and action == "quota" \
                    and self.command == "GET":
                try:
                    return self._send(200, control.get_quota(tenant))
                except ValueError as e:
                    return self._send_error(bad_request(str(e)))
            if tenant is not None and action == "usage" \
                    and self.command == "GET":
                try:
                    return self._send(200, control.usage_summary(tenant))
                except ValueError as e:
                    return self._send_error(bad_request(str(e)))
            if tenant is not None and action == "password" \
                    and self.command == "POST":
                payload = self._read_json()
                try:
                    return self._send(
                        200, control.change_password(
                            tenant, payload.get("password") or ""))
                except KeyError:
                    return self._send_error(not_found(f"租户不存在：{tenant!r}"))
                except ValueError as e:
                    return self._send_error(bad_request(str(e)))
            if tenant is not None and action is None \
                    and self.command == "DELETE":
                payload = self._read_json()
                query = urlparse(self.path).query
                confirm = payload.get("confirm") is True \
                    or "confirm=true" in query
                try:
                    return self._send(
                        200, control.delete_tenant(tenant, confirm=confirm))
                except KeyError:
                    return self._send_error(not_found(f"租户不存在：{tenant!r}"))
                except ValueError as e:
                    return self._send_error(bad_request(str(e)))
                except RuntimeError as e:
                    return self._send_error(upstream_unavailable(str(e)))
            return self._send_error(not_found())

        def do_GET(self):
            try:
                self._handle()
            except Exception as e:  # noqa: BLE001 - 管理接口异常不拖垮进程
                logger.warning("管理接口异常", exc_info=True)
                self._send_error(internal_error(str(e)))

        def do_POST(self):
            self.do_GET()

        def do_DELETE(self):
            self.do_GET()

    return AdminHandler


def start_admin_server(control: TenantControl, host: str = "127.0.0.1",
                       port: int = 8082, admin_token: str | None = None):
    """起管理 API 服务器（只监听回环，不对外）。

    admin_token 缺省（空）时拒绝服务：不启动监听、只记一条错误日志、
    返回 None——调用方（gateway/main.py）据此跳过管理接口，而不是
    起一个无鉴权的管理口。返回 server 对象时调用方自己起线程 serve_forever。
    """
    if not admin_token:
        logger.error("管理接口拒绝服务：未配置 GATEWAY_ADMIN_TOKEN，"
                     "不启动 127.0.0.1:%s 管理监听", port)
        return None
    server = ThreadingHTTPServer(
        (host, port), make_admin_handler(control, admin_token))
    logger.info("管理接口监听 %s:%s（只限回环，需 X-Admin-Token）",
                host, port)
    return server


__all__ = [
    "TenantControl",
    "generate_password",
    "make_admin_handler",
    "start_admin_server",
]
