"""DeepSeek 计价表（模型接入层）：把真实用量换算成美元成本估算。

价目来源：DeepSeek 官方 API 文档 Models & Pricing 页
（api-docs.deepseek.com/quick_start/pricing，2026-11-27 查到）：
deepseek-chat / deepseek-reasoner 两个老别名已经退役，请求由
DeepSeek-V4.1-Flash-1017 承接，按 Flash 价目计费；deepseek-v4-pro
按 Pro 价目。单位：美元 / 1M tokens。

- Flash：输入缓存命中 $0.003（高峰 $0.006）/ 未命中 $0.15（高峰
  $0.30），输出 $0.60（高峰 $1.20）。思考 token 按输出计费。
- Pro：输入缓存命中 $0.022（高峰 $0.044）/ 未命中 $0.66（高峰
  $1.32），输出 $1.98（高峰 $3.96）。
- 高峰：周一到周五 01:00–04:00 和 06:00–10:00 UTC（北京时间
  09:00–12:00 和 14:00–18:00），中国法定节假日全天按非高峰——
  节假日表没接进来，所以节假日的高峰会按高峰价多算，成本只会
  高估不会低估；DeepSeek 调价时这里要跟着改，数字只是估算，
  不是账单。

不在表里的模型不编价格：cost_estimate 记 0.0，token 数照记。
"""
from datetime import datetime, timezone

# 模型名 → （缓存命中输入价， 未命中输入价， 输出价），美元 / 1M tokens，
# 都是非高峰价；高峰价是非高峰的 2 倍（官方价目表就是这么标的）。
_RATES = {
    # 两个老别名 + 新名都按 Flash 价目。
    "deepseek-chat": (0.003, 0.15, 0.60),
    "deepseek-reasoner": (0.003, 0.15, 0.60),
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}

# 高峰窗口（UTC 小时区间，左闭右开），只在周一到周五生效。
_PEAK_WINDOWS = ((1, 4), (6, 10))


def is_peak(now: datetime | None = None) -> bool:
    """现在是不是 DeepSeek 的高峰时段（UTC 周一到周五两个窗口）。"""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:  # 周六周日全天非高峰
        return False
    hour = now.hour + now.minute / 60
    return any(start <= hour < end for start, end in _PEAK_WINDOWS)


def cost_usd(model: str, tokens_input: int = 0, tokens_cached: int = 0,
             tokens_output: int = 0, now: datetime | None = None) -> float:
    """一次调用的成本估算（美元）。tokens_cached 是输入里命中缓存
    的部分（没命中 = input - cached）；不在表里的模型返回 0.0。"""
    rates = _RATES.get(model)
    if rates is None:
        return 0.0
    hit_rate, miss_rate, out_rate = rates
    if is_peak(now):
        hit_rate, miss_rate, out_rate = (
            hit_rate * 2, miss_rate * 2, out_rate * 2)
    missed = max(tokens_input - tokens_cached, 0)
    return (tokens_cached * hit_rate + missed * miss_rate
            + tokens_output * out_rate) / 1_000_000
