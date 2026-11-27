"""可观测性（横切层）：结构化日志 + 健康检查 + 指标。

- logging.py：JSON 日志，每请求一个 id、每条带租户 id
- health.py：/api/health 的响应体（内容和原来 server.py 里的一致）
- metrics.py：三个内存计数（请求数 / 失败数 / 模型调用数）+
  Prometheus 文本格式的只读输出
本包只依赖标准库，不 import agent、gateway、graph、tools。
"""
from . import health, logging, metrics

__all__ = ["health", "logging", "metrics"]
