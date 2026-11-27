"""结构化 JSON 日志（横切层：可观测性，不占业务层）。

每条日志一行 JSON，字段：ts（UTC ISO 时间）、level、event、
request_id（每请求一个，缺失时为空）、tenant（每条带租户 id，
未知时为空），再加调用方传的其它字段。只依赖标准库 logging，
不 import agent、gateway、graph、tools。
"""
import json
import logging
import uuid
from datetime import datetime, timezone

LOGGER_NAME = "mt-agent"


def new_request_id() -> str:
    """给一个请求发一个 id（16 位 hex，够在日志里把一次请求串起来）。"""
    return uuid.uuid4().hex[:16]


def format_event(event: str, level: str = "info", request_id: str | None = None,
                 tenant: str | None = None, **fields) -> dict:
    """拼一条结构化日志记录（纯函数，可单测）：固定字段 + 调用方字段。"""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "request_id": request_id or "",
        "tenant": tenant or "",
    }
    record.update(fields)
    return record


def emit(event: str, level: str = "info", request_id: str | None = None,
         tenant: str | None = None, **fields) -> None:
    """发一条 JSON 日志（一行一个 JSON 对象，写到标准 logging 输出）。"""
    logger = logging.getLogger(LOGGER_NAME)
    level_no = getattr(logging, level.upper(), logging.INFO)
    logger.log(level_no, json.dumps(
        format_event(event, level, request_id, tenant, **fields),
        ensure_ascii=False))


def setup_logging(level: str = "INFO") -> None:
    """配一次根 logger：没有 handler 时加一个写 stderr 的 StreamHandler。

    BaseHTTPRequestHandler 的默认刷屏日志仍由各 Handler 自己关掉
    （log_message 空实现），这里只管我们自己的 JSON 行。
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger(LOGGER_NAME).setLevel(
        getattr(logging, level.upper(), logging.INFO))
