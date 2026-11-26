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
    from .config import DATA_DIR, TOKEN_SECRET, TRUST_TENANT_HEADER
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from config import DATA_DIR, TOKEN_SECRET, TRUST_TENANT_HEADER

# 租户 id 只允许字母数字 _ - ，防止路径穿越和奇怪的租户名
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def tenant_dirs(tenant: str) -> tuple[Path, Path, Path]:
    """返回 (工作区目录, 记忆库路径, 浏览器 profile 目录)，不存在就建出来。"""
    base = DATA_DIR / tenant
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (base / "browser-profile").mkdir(parents=True, exist_ok=True)
    return workspace, base / "memory.db", base / "browser-profile"


def tenant_db(tenant: str):
    _, db_path, _ = tenant_dirs(tenant)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS facts"
        "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_log"
        "(ts TEXT, role TEXT, content TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS summaries"
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, summary TEXT,"
        " up_to_ts TEXT)"
    )
    return conn


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
        if not TENANT_RE.match(tenant):
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
    """从请求头拿租户身份：优先校验 token，其次信任网关转发的 X-Tenant-Id。"""
    token = handler.headers.get("X-Tenant-Token", "")
    if token:
        tenant = verify_token(token)
        if tenant:
            return tenant
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        tenant = verify_token(auth[7:].strip())
        if tenant:
            return tenant
    # gateway 在容器内网转发时会带这个头；agent-api 端口不对外发布，
    # 所以只有可信的网关能走到这里。本地调试时用 TRUST_TENANT_HEADER=1 开启。
    forwarded = handler.headers.get("X-Tenant-Id", "")
    if forwarded and TENANT_RE.match(forwarded):
        return forwarded if TRUST_TENANT_HEADER else None
    return None
