"""健康检查（横切层：可观测性，不占业务层）。

/api/health 的返回内容不变，只是实现从 agent/server.py 挪到这里：
{"ok": True, "model": ..., "engine": "langgraph", "tenant": ...,
 "single_tenant": ..., "sandbox_mode": ...}。本模块不 import agent
的配置——调用方把值当参数传进来，健康检查本身不知道模型名从哪来。
"""
from __future__ import annotations


def health_payload(model: str, tenant: str | None, single_tenant: bool,
                   sandbox_mode: str) -> dict:
    """拼 /api/health 的响应体（纯函数；字段和原来 server.py 里的一字不改）。"""
    return {
        "ok": True,
        "model": model,
        "engine": "langgraph",
        "tenant": tenant,
        "single_tenant": single_tenant,
        "sandbox_mode": sandbox_mode,
    }
