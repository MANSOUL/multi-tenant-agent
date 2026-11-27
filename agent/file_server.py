"""文件服务（传输层：/files/<路径>，只发本租户 workspace 里的静态文件）。

P1 从 agent/server.py 拆出来：server.py 只留路由分发，这里只管
“路径 + 后缀 + 读文件发出去”。鉴权在调用方（先过 _need_tenant 拿租户）；
错误响应走 errors/ 的统一结构（403 forbidden / 404 not_found，
JSON 体，不再是 plain text 的 "forbidden"/"not found"）。
"""
import json
from urllib.parse import unquote

try:
    from .tenant import safe_path
    from errors.codes import forbidden, not_found
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tenant import safe_path
    from errors.codes import forbidden, not_found

ALLOWED = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _send_error(handler, err) -> None:
    """按统一错误结构发一个 JSON 错误响应（code/message + 状态码）。"""
    handler._send(err.status, json.dumps(err.body(), ensure_ascii=False),
                  "application/json")


def serve_file(handler, tenant: str, rel: str) -> None:
    """GET /files/<相对路径>：只发本租户 workspace 里的网页/静态文件。

    safe_path 会把 `..`、绝对路径、符号链接逃逸都挡在 workspace 内，
    逃不出去的返回 403；点开头文件、.db/.sqlite/.env 和不在白名单的
    后缀一律 403；不存在或是个目录就 404（目录不列表）。
    二进制按字节读，Content-Type 按后缀显式映射，不猜错图片。
    """
    rel = unquote(rel)
    if rel.startswith("/"):
        # /files//etc/passwd、/files/%2Fetc%2Fpasswd 这种绝对路径一律挡掉
        return _send_error(handler, forbidden())
    if not rel or rel.endswith("/"):
        return _send_error(handler, not_found())
    try:
        p = safe_path(tenant, rel)
    except ValueError:
        return _send_error(handler, forbidden())
    # safe_path 之后再按解析后的真实路径检查：workspace 里的符号链接
    # 指到 memory.db 也会被后缀挡住；点开头文件（.env 等）一律不发。
    if p.name.startswith(".") or p.suffix.lower() not in ALLOWED:
        return _send_error(handler, forbidden())
    if not p.is_file():
        return _send_error(handler, not_found())
    try:
        data = p.read_bytes()
    except OSError:
        return _send_error(handler, not_found())
    handler._send(200, data, ALLOWED[p.suffix.lower()])
