"""统一错误码（横切层）：错误分类 + 线上统一的 {"error": {...}} 结构。

入口见 errors/codes.py：ApiError、ErrorCategory、error_body、
unauthenticated / forbidden / tenant_isolation_violation /
not_found / bad_request / model_failure / upstream_unavailable /
internal_error。本包只依赖标准库，不 import agent、gateway、graph、
tools（横切层不许反向依赖业务层）。
"""
from .codes import (
    ApiError,
    ErrorCategory,
    bad_request,
    error_body,
    error_status,
    forbidden,
    internal_error,
    model_failure,
    not_found,
    tenant_isolation_violation,
    unauthenticated,
    upstream_unavailable,
)

__all__ = [
    "ApiError",
    "ErrorCategory",
    "bad_request",
    "error_body",
    "error_status",
    "forbidden",
    "internal_error",
    "model_failure",
    "not_found",
    "tenant_isolation_violation",
    "unauthenticated",
    "upstream_unavailable",
]
