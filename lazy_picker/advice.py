"""Conditional observation plans adapted from DSA's trend discipline."""

from .engine.normalize import safe_float, safe_text


# DSA's default MA5 chase threshold in stock_analyzer.py and agent/skills/defaults.py.
CHASE_BIAS_PCT = 5.0
BAD_BAR_FLAGS = {"stale_cache", "fetch_failed", "non_positive_price", "invalid_ohlc"}


def candidate_advice(row: dict, completed_trade_date: str, *, stale: bool) -> dict:
    """Use completed, same-source bars for price comparisons and reference levels."""
    date = safe_text(row.get("daily_trade_date"))
    flags = set(safe_text(row.get("daily_quality_flags")).split(";"))
    prices = [safe_float(row.get(key)) for key in ("daily_close", "ma5", "ma10", "ma20")]
    if stale or date != completed_trade_date or flags & BAD_BAR_FLAGS or any(
        price is None or price <= 0 for price in prices
    ):
        return {
            "stance": "等待数据确认",
            "summary": "缺少与本轮对应的完整收盘日线，先确认数据再判断价位。",
            "no_position": "等有效收盘日线补齐后再判断趋势和价格位置。",
            "has_position": "先核对最新收盘价、持仓成本和原定风控条件。",
            "technical": None,
        }

    close, ma5, ma10, ma20 = prices
    bias = round((close / ma5 - 1) * 100, 2)
    bullish = ma5 > ma10 > ma20
    has_full_lookback = (safe_float(row.get("daily_data_points")) or 0) >= 21
    if close < ma20:
        stance = "复查趋势"
        summary = "收盘已低于 20 日均线，优先确认趋势是否走弱。"
        no_position = "等收盘重新站上 20 日均线，再观察量价能否企稳。"
        has_position = "重点复核跌破 20 日均线是否持续，以及成交量是否扩大。"
    elif bias > CHASE_BIAS_PCT:
        stance = "观望，避免追高"
        summary = "收盘高于 5 日均线超过 5%，留意回撤和追高风险。"
        no_position = "等价格回到 5 日或 10 日均线附近，再看回踩是否站稳。"
        has_position = "留意价格回落时能否守住 5 日及 10 日均线。"
    elif not bullish or close < ma5:
        stance = "等待趋势确认"
        summary = "短期均线与收盘位置还没有同时满足趋势观察条件。"
        no_position = "等收盘站上 5 日均线，观察 5、10、20 日均线能否顺序向上。"
        has_position = "观察 20 日均线附近能否稳住，以及短期均线是否继续走弱。"
    else:
        stance = "观察回踩"
        summary = "5、10、20 日均线多头排列，收盘距 5 日均线在 5% 以内。"
        no_position = "观察缩量回踩 5 日均线能否站稳，10 日均线是下一处参考。"
        has_position = "留意回踩量能和 10 日均线，收盘跌破 20 日均线时重新评估。"

    return {
        "stance": stance,
        "summary": summary,
        "no_position": no_position,
        "has_position": has_position,
        "technical": {
            "trade_date": date,
            "source": safe_text(row.get("daily_source")),
            "close": round(close, 4),
            "ma5": round(ma5, 4),
            "ma10": round(ma10, 4),
            "ma20": round(ma20, 4),
            "bias_ma5_pct": bias,
            "bullish_alignment": bullish,
            "previous_high_20d": safe_float(row.get("prev_high_20d")) if has_full_lookback else None,
            "volume_ratio_20d": safe_float(row.get("volume_ratio_20d")) if has_full_lookback else None,
        },
    }
