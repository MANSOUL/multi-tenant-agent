"""模型调用计量（模型接入层）：每次模型调用记一笔，每租户可查。

两档口径：
- 有真实用量（reply.usage_metadata，P6 起 ChatDeepSeek +
  stream_usage 保证流式和非流式都有）就记真实数：输入/输出/
  思考 token、缓存命中数，成本按 model/pricing.py 的 DeepSeek
  价目表算——这是账单口径的估算，不是 DeepSeek 的实际扣费
  （缓存命中、高峰时段都按调用那一刻算，节假日没接进来）。
- 没用量（假模型、老版本客户端）就退回老路：token 数按
  domain/memory.estimate_tokens 粗估（约 4 字符 = 1 token），
  成本记 0.0，不编数字。

本模块不直接碰数据库驱动或模型 SDK：它只对调用方传进来的
DB-API 连接做 SQL，建表 SQL 也在 store/sqlite_store.py 的
SCHEMA 里，这里的 ensure_metering_table 只是给不走
ensure_schema 的调用方（测试、网关控制面直读）用的兜底，
同时给已建好的老表补列（P6 新增 tokens_reasoning /
tokens_cached）。
"""
from datetime import datetime, timezone

try:
    from domain.memory import estimate_tokens
    from model.pricing import cost_usd
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from domain.memory import estimate_tokens
    from model.pricing import cost_usd

METERING_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS metering"
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, model TEXT,"
    " ts TEXT, tokens_input INTEGER, tokens_output INTEGER,"
    " tokens_total INTEGER, cost_estimate REAL,"
    " tokens_reasoning INTEGER DEFAULT 0,"
    " tokens_cached INTEGER DEFAULT 0)"
)

# 老表补列（CREATE TABLE IF NOT EXISTS 不会给已建好的表加列）。
_MIGRATION_COLUMNS = (
    ("tokens_reasoning", "INTEGER DEFAULT 0"),
    ("tokens_cached", "INTEGER DEFAULT 0"),
)


def extract_usage(reply) -> dict | None:
    """从一次模型回复里取真实用量，没有就返回 None。

    reply 是 AIMessage / AIMessageChunk：usage_metadata 是
    LangChain 的标准字段（ChatDeepSeek 从 DeepSeek 的 usage 里
    换算过来：input_tokens / output_tokens / total_tokens，
    input_token_details.cache_read，output_token_details.reasoning）。
    输出 token 已含思考 token（DeepSeek 按输出计费也是这么算的），
    所以 total = input + output，不再把 reasoning 另加一次。
    """
    meta = getattr(reply, "usage_metadata", None) or {}
    if not meta.get("total_tokens") and not meta.get("input_tokens") \
            and not meta.get("output_tokens"):
        return None
    input_details = meta.get("input_token_details") or {}
    output_details = meta.get("output_token_details") or {}
    tokens_input = int(meta.get("input_tokens") or 0)
    tokens_output = int(meta.get("output_tokens") or 0)
    return {
        "input_tokens": tokens_input,
        "output_tokens": tokens_output,
        "total_tokens": int(meta.get("total_tokens")
                            or tokens_input + tokens_output),
        "reasoning_tokens": int(output_details.get("reasoning") or 0),
        "cached_tokens": int(input_details.get("cache_read") or 0),
    }


def ensure_metering_table(conn) -> None:
    """建 metering 表（不存在才建）；已建好的老表补上 P6 的两列。"""
    conn.execute(METERING_TABLE_SQL)
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(metering)").fetchall()}
    except Exception:
        return
    for name, ddl in _MIGRATION_COLUMNS:
        if name not in cols:
            conn.execute(
                f"ALTER TABLE metering ADD COLUMN {name} {ddl}")


def record_call(conn, tenant: str, model: str, input_text: str = "",
                output_text: str = "", tokens_input: int | None = None,
                tokens_output: int | None = None,
                cost_estimate: float | None = None,
                usage: dict | None = None) -> int:
    """记一次模型调用，返回行 id。

    usage 给了就记真实数（input/output/reasoning/cached + 按
    价目表算成本）；没给就按老路：token 没给的按文本粗估，
    成本默认 0.0。
    """
    if usage is not None:
        tokens_input = usage.get("input_tokens", 0)
        tokens_output = usage.get("output_tokens", 0)
        reasoning = usage.get("reasoning_tokens", 0)
        cached = usage.get("cached_tokens", 0)
        if cost_estimate is None:
            cost_estimate = cost_usd(
                model, tokens_input, cached, tokens_output)
    else:
        if tokens_input is None:
            tokens_input = estimate_tokens(input_text)
        if tokens_output is None:
            tokens_output = estimate_tokens(output_text)
        reasoning, cached = 0, 0
        if cost_estimate is None:
            cost_estimate = 0.0
    ensure_metering_table(conn)
    cur = conn.execute(
        "INSERT INTO metering (tenant, model, ts, tokens_input,"
        " tokens_output, tokens_total, cost_estimate,"
        " tokens_reasoning, tokens_cached)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant, model, datetime.now(timezone.utc).isoformat(),
         tokens_input, tokens_output, tokens_input + tokens_output,
         cost_estimate, reasoning, cached))
    return cur.lastrowid


def summarize(conn, tenant: str) -> dict:
    """按租户汇总计量：调用次数、token 合计（含思考/缓存）、成本估算合计。

    metering="available" 表示这张表里已有计量口径的数据（哪怕
    0 行也算 available：0 行就是“还没调过模型”，不是“没计量”）。
    """
    ensure_metering_table(conn)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(tokens_input), 0),"
        " COALESCE(SUM(tokens_output), 0), COALESCE(SUM(tokens_total), 0),"
        " COALESCE(SUM(cost_estimate), 0.0),"
        " COALESCE(SUM(tokens_reasoning), 0),"
        " COALESCE(SUM(tokens_cached), 0) FROM metering WHERE tenant = ?",
        (tenant,)).fetchone()
    calls, tok_in, tok_out, tok_total, cost, tok_reason, tok_cached = row
    return {"tenant": tenant, "model_calls": calls,
            "tokens_input": tok_in, "tokens_output": tok_out,
            "tokens_total": tok_total, "cost_estimate": cost,
            "tokens_reasoning": tok_reason, "tokens_cached": tok_cached,
            "metering": "available"}
