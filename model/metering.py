"""模型调用计量（模型接入层）：每次模型调用记一笔，每租户可查。

只做 best-effort 估算，不承诺计费级精度（见 ARCHITECTURE.md 第五节
pi-agent 的提醒）：token 数按 domain/memory.estimate_tokens 粗估
（约 4 字符 = 1 token），cost_estimate 字段先留 0.0——没有真实计价
表，任何成本数字都是编的，所以不编，只记 token。

本模块不直接碰数据库驱动或模型 SDK：它只对调用方传进来的 DB-API
连接做 SQL，建表 SQL 也在 store/sqlite_store.py 的 SCHEMA 里，
这里的 ensure_metering_table 只是给不走 ensure_schema 的调用方
（测试、网关控制面直读）用的兜底。
"""
from datetime import datetime, timezone

try:
    from domain.memory import estimate_tokens
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from domain.memory import estimate_tokens

METERING_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS metering"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, model TEXT,"
    " ts TEXT, tokens_input INTEGER, tokens_output INTEGER,"
    " tokens_total INTEGER, cost_estimate REAL)"
)


def ensure_metering_table(conn) -> None:
    """建 metering 表（不存在才建）。"""
    conn.execute(METERING_TABLE_SQL)


def record_call(conn, tenant: str, model: str, input_text: str = "",
                output_text: str = "", tokens_input: int | None = None,
                tokens_output: int | None = None,
                cost_estimate: float = 0.0) -> int:
    """记一次模型调用，返回行 id。

    token 数没给就按输入/输出文本粗估（best-effort）；cost_estimate
    默认 0.0——没有计价表，不编成本数字。
    """
    if tokens_input is None:
        tokens_input = estimate_tokens(input_text)
    if tokens_output is None:
        tokens_output = estimate_tokens(output_text)
    cur = conn.execute(
        "INSERT INTO metering (tenant, model, ts, tokens_input,"
        " tokens_output, tokens_total, cost_estimate)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant, model, datetime.now(timezone.utc).isoformat(),
         tokens_input, tokens_output, tokens_input + tokens_output,
         cost_estimate))
    return cur.lastrowid


def summarize(conn, tenant: str) -> dict:
    """按租户汇总计量：调用次数、token 合计、成本估算合计。

    metering="available" 表示这张表里已有计量口径的数据（哪怕
    0 行也算 available：0 行就是“还没调过模型”，不是“没计量”）。
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(tokens_input), 0),"
        " COALESCE(SUM(tokens_output), 0), COALESCE(SUM(tokens_total), 0),"
        " COALESCE(SUM(cost_estimate), 0.0) FROM metering WHERE tenant = ?",
        (tenant,)).fetchone()
    calls, tok_in, tok_out, tok_total, cost = row
    return {"tenant": tenant, "model_calls": calls,
            "tokens_input": tok_in, "tokens_output": tok_out,
            "tokens_total": tok_total, "cost_estimate": cost,
            "metering": "available"}
