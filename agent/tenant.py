"""租户隔离：路径、数据库、token 校验。

所有租户隔离逻辑只在这里一份：工作区目录、每租户 SQLite、路径穿越检查、
网关 token（HMAC）校验。工具、记忆、图都调这里的函数，不各自重复实现。
"""
import hashlib
import hmac
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from .config import DATA_DIR, TENANT_ID, TOKEN_SECRET, TRUST_TENANT_HEADER
    from store.sqlite_store import ensure_schema
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import DATA_DIR, TENANT_ID, TOKEN_SECRET, TRUST_TENANT_HEADER
    from store.sqlite_store import ensure_schema

# 租户 id 只允许字母数字 _ - ，防止路径穿越和奇怪的租户名
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_own_tenant(tenant: str) -> bool:
    """单租户模式（TENANT_ID 已设）下，容器只认自己的租户。

    一租户一容器后，进程级隔离已经由容器保证，这里是第二道防线：
    万一网关把别的租户的请求错发到这个容器，请求会被挡掉而不是
    读到别人的数据。TENANT_ID 为空时是老的多租户进程模式，不挡。
    """
    if not TENANT_ID:
        return True
    return tenant == TENANT_ID


def tenant_dirs(tenant: str) -> tuple[Path, Path, Path]:
    """返回 (工作区目录, 记忆库路径, 浏览器 profile 目录)，不存在就建出来。"""
    base = DATA_DIR / tenant
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (base / "browser-profile").mkdir(parents=True, exist_ok=True)
    return workspace, base / "memory.db", base / "browser-profile"


def tenant_db(tenant: str):
    """打开本租户的库并建表。建表 SQL 在 store/，这里只调它，
    不再自己拼 CREATE TABLE。

    后端按 STORE_BACKEND 切（2026-11-27 接线）：sqlite（默认）还是
    老路子——每租户一个 memory.db 文件；postgres 时连 POSTGRES_DSN
    的库，在 "tenant" 同名的 schema 里建表（租户隔离靠 schema），
    每次调用都是新连接、search_path 只对这个连接生效。两种连接
    都支持 `with ... as conn:`（退出时提交），调用方不用区分。
    """
    _, db_path, _ = tenant_dirs(tenant)
    from store import backend_name

    if backend_name() == "postgres":
        from store.postgres_store import connect as pg_connect
        from store.postgres_store import ensure_schema as pg_ensure_schema

        conn = pg_connect()
        pg_ensure_schema(conn, schema=tenant)
        return conn
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    return conn


def checkpoint_db_path(tenant: str) -> Path:
    """本租户 checkpointer 的 SQLite 文件（P4）。

    和 memory.db 分开的文件 checkpoint.db：图内多轮消息状态只写
    这里，不碰记忆库，避免互相干扰。父目录不存在就建出来。
    """
    base = DATA_DIR / tenant
    base.mkdir(parents=True, exist_ok=True)
    return base / "checkpoint.db"


def safe_path(tenant: str, rel: str) -> Path:
    workspace, _, _ = tenant_dirs(tenant)
    p = (workspace / rel).resolve()
    if not str(p).startswith(str(workspace.resolve())):
        raise ValueError("只能读写本租户 workspace 目录内的文件")
    return p


def verify_token(token: str) -> str | None:
    """校验网关发的 token，格式：tenant.exp.hmac。返回 tenant 或 None。"""
    if not token or not TOKEN_SECRET:
        return None
    try:
        tenant, exp, sig = token.split(".")
        if not TENANT_RE.fullmatch(tenant):
            return None
        if int(exp) < int(datetime.now(timezone.utc).timestamp()):
            return None
        expect = hmac.new(
            TOKEN_SECRET.encode(), f"{tenant}.{exp}".encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expect, sig):
            return tenant
    except (ValueError, TypeError):
        return None
    return None


def get_tenant(handler) -> str | None:
    """从请求头拿租户身份：优先校验 token，其次信任网关转发的 X-Tenant-Id。

    单租户模式下再过一道 is_own_tenant()：token 是别的租户的也返回 None，
    容器只服务自己的租户。
    """
    token = handler.headers.get("X-Tenant-Token", "")
    if token:
        tenant = verify_token(token)
        if tenant and is_own_tenant(tenant):
            return tenant
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        tenant = verify_token(auth[7:].strip())
        if tenant:
            return tenant
    # 浏览器直接打开租户容器页面（agent/web/chat.html）时没有
    # Authorization 头，靠网关登录时种的 mt_token cookie（HttpOnly，
    # JS 读不到，但浏览器每次请求都会自动带上）认身份；和网关
    # token_from 的 cookie 分支同口径。
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("mt_token="):
            tenant = verify_token(part[len("mt_token="):].strip())
            if tenant and is_own_tenant(tenant):
                return tenant
    # gateway 在容器内网转发时会带这个头；agent-api 端口不对外发布，
    # 所以只有可信的网关能走到这里。本地调试时用 TRUST_TENANT_HEADER=1 开启。
    forwarded = handler.headers.get("X-Tenant-Id", "")
    if forwarded and TENANT_RE.fullmatch(forwarded) and is_own_tenant(forwarded):
        return forwarded if TRUST_TENANT_HEADER else None
    return None
