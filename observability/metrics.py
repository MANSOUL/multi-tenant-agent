"""指标（横切层：可观测性，不占业务层）。

P1 只记三个内存计数：请求数、失败数、模型调用数。不接 Prometheus
客户端、不落盘、不跨进程汇总——网关进程和租户容器进程各记各的，
/api/metrics 只读本进程的计数。只依赖标准库，不 import agent、
gateway、graph、tools。计数名：
- mt_requests_total：收到的 HTTP 请求数（agent 进程）
- mt_request_failures_total：返回 4xx/5xx 的请求数
- mt_model_calls_total：调模型的次数（graph.py 的调模型节点里记）
"""
import threading

REQUESTS = "requests"
FAILURES = "failures"
MODEL_CALLS = "model_calls"

_PROM_NAMES = {
    REQUESTS: "mt_requests_total",
    FAILURES: "mt_request_failures_total",
    MODEL_CALLS: "mt_model_calls_total",
}

_counters: dict = {REQUESTS: 0, FAILURES: 0, MODEL_CALLS: 0}
_lock = threading.Lock()


def inc(name: str, amount: int = 1) -> None:
    """计数 +amount（name 必须是上面三个之一，错名直接报错，别静默丢数）。"""
    if name not in _counters:
        raise ValueError(f"未知的指标：{name!r}")
    with _lock:
        _counters[name] += amount


def snapshot() -> dict:
    """当前三个计数的快照（key 是 requests / failures / model_calls）。"""
    with _lock:
        return dict(_counters)


def reset() -> None:
    """三个计数清零（只给测试用，生产代码不许调）。"""
    with _lock:
        for key in _counters:
            _counters[key] = 0


def render_prometheus() -> str:
    """Prometheus 文本风格：每计数两行注释 + 一行数值，共三行计数。"""
    snap = snapshot()
    lines = []
    for key in (REQUESTS, FAILURES, MODEL_CALLS):
        name = _PROM_NAMES[key]
        lines.append(f"# HELP {name} Total count.")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {snap[key]}")
    return "\n".join(lines) + "\n"
