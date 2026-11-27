"""统一错误码（横切层，不占业务层，可被 agent/、gateway/ import）。

分类（ARCHITECTURE.md 第二节横切“错误 errors/”）：
- 可重试 RETRYABLE：上游暂时不可用、模型调用失败这类过一会儿重试可能好的错；
- 不可重试 NON_RETRYABLE：鉴权、参数、不存在这类重试也没用的错；
- 租户隔离违规 TENANT_ISOLATION：跨租户访问——一律按拒绝处理，绝不重试；
- 模型失败 MODEL_FAILURE：模型调用本身失败（单列一类，方便以后接重试/降级）。

线上的错误响应统一是 {"error": {"code": ..., "message": ...}}，只含这两
个键；分类和 retryable 只存在 ApiError 对象上（给服务端记日志/做决策
用），不进线上传输。HTTP 状态码语义不变：401/403/404/400/500/502
各归各的码，见下面各 code 的 status。
"""
from enum import Enum


class ErrorCategory(str, Enum):
    """错误分类：可重试 / 不可重试 / 租户隔离违规 / 模型失败。"""
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    TENANT_ISOLATION = "tenant_isolation"
    MODEL_FAILURE = "model_failure"


# code -> (http_status, category, retryable)
_CODES = {
    "unauthenticated": (401, ErrorCategory.NON_RETRYABLE, False),
    "forbidden": (403, ErrorCategory.NON_RETRYABLE, False),
    "tenant_isolation": (403, ErrorCategory.TENANT_ISOLATION, False),
    "not_found": (404, ErrorCategory.NON_RETRYABLE, False),
    "bad_request": (400, ErrorCategory.NON_RETRYABLE, False),
    "model_error": (500, ErrorCategory.MODEL_FAILURE, True),
    "upstream_unavailable": (502, ErrorCategory.RETRYABLE, True),
    "internal_error": (500, ErrorCategory.NON_RETRYABLE, False),
}


class ApiError(Exception):
    """一个可直接翻译成 HTTP 错误响应的业务异常。

    code 必须是上面 _CODES 里的值；status / category / retryable 都由
    code 派生，调用方只传 code + message，不许各处自造状态码。
    """

    def __init__(self, code: str, message: str):
        if code not in _CODES:
            raise ValueError(f"未知的错误码：{code!r}")
        status, category, retryable = _CODES[code]
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.category = category
        self.retryable = retryable

    def body(self) -> dict:
        """线上统一结构：{"error": {"code": ..., "message": ...}}。"""
        return {"error": {"code": self.code, "message": self.message}}


def error_body(code: str, message: str) -> dict:
    """不抛异常、只拼响应体：{"error": {"code": ..., "message": ...}}。"""
    return ApiError(code, message).body()


def error_status(code: str) -> int:
    """错误码对应的 HTTP 状态码。"""
    return _CODES[code][0]


# -- 工厂函数：各处只调这些，不直接拼 {"error": ...} -------------------
def unauthenticated(message: str = "未登录") -> ApiError:
    return ApiError("unauthenticated", message)


def forbidden(message: str = "forbidden") -> ApiError:
    return ApiError("forbidden", message)


def tenant_isolation_violation(message: str = "租户隔离违规") -> ApiError:
    return ApiError("tenant_isolation", message)


def not_found(message: str = "not found") -> ApiError:
    return ApiError("not_found", message)


def bad_request(message: str = "请求不合法") -> ApiError:
    return ApiError("bad_request", message)


def model_failure(message: str) -> ApiError:
    return ApiError("model_error", message)


def upstream_unavailable(message: str) -> ApiError:
    return ApiError("upstream_unavailable", message)


def internal_error(message: str = "服务器内部错误") -> ApiError:
    return ApiError("internal_error", message)
