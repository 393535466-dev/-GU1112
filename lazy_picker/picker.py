"""A-share selection using the screening primitives adapted from DSA."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Callable

import pandas as pd
import requests
import yaml

from .engine.daily import enrich_daily_features
from .engine.filter import apply_hard_filters, requires_daily_features, without_daily_filters
from .engine.models import HardFilterConfig, Pick, ScreeningConfig
from .engine.normalize import safe_float, safe_text
from .engine.risk import apply_portfolio_overlay, apply_risk_overlay
from .engine.scorer import compute_screen_scores, factor_score_columns, normalized_factor_weights
from .engine.snapshot import fetch_snapshot_with_fallback

logger = logging.getLogger(__name__)
STRATEGY_DIR = Path(__file__).parent / "engine" / "strategies"
SHORTLIST_LIMIT = 48
CANDIDATE_LIMIT = 5
SNAPSHOT_SOURCES = ["sina", "efinance", "akshare_em", "em_datacenter"]
SOURCE_NAMES = {
    "sina": "新浪财经",
    "efinance": "东方财富（eFinance）",
    "akshare_em": "东方财富（AkShare）",
    "em_datacenter": "东方财富数据中心",
    "last_good_cache": "本地行情缓存",
}
FACTOR_NAMES = {
    "momentum": "趋势", "value": "估值", "liquidity": "流动性",
    "activity": "成交活跃度", "stability": "稳定性", "reversal": "反转",
    "theme_heat": "板块热度", "size": "市值", "topic_alignment": "题材匹配",
}
RISK_NAMES = {
    "single_day_chase_risk": "单日涨幅偏大，存在追高回撤风险。",
    "single_day_breakdown_risk": "单日跌幅较大，需要观察是否继续走弱。",
    "abnormal_volume_ratio": "成交量异常放大，价格波动可能加剧。",
    "high_turnover": "换手率偏高，短期波动可能加剧。",
    "negative_or_invalid_pe": "市盈率为负或无法有效判断。",
    "high_pb": "市净率偏高，需要关注估值回落风险。",
    "weak_daily_signal": "日线趋势信号偏弱。",
    "macd_bearish": "MACD 指标偏弱。",
    "rsi_overbought": "RSI 已进入超买区间。",
    "low_daily_quality": "日线数据质量不足，技术指标可能失真。",
    "daily_fetch_failed": "本次未获取到日线行情。",
    "daily_stale_cache": "日线行情有滞后。",
    "daily_source_fallback_errors": "部分日线行情源未能返回数据。",
    "bad_daily_quality_flags": "日线行情存在缺失或异常。",
}


@dataclass(frozen=True)
class Direction:
    title: str
    strategy: str
    description: str
    holding: str
    review: str
    metric: str
    metric_format: str


DIRECTIONS = {
    "short": Direction(
        "短线动量", "capital_heat", "看成交活跃度和近期价格动量。",
        "1–5 个交易日", "放量后能否维持强势，缩量跌回整理区间时重新评估。",
        "volume_ratio", "量比 {:.1f} 倍",
    ),
    "swing": Direction(
        "趋势波段", "momentum_quality", "看趋势延续，同时考虑估值和流动性。",
        "5–20 个交易日", "趋势是否持续，跌破近期均线时重新评估。",
        "change_60d", "近 60 个交易日涨跌幅 {:+.1f}%",
    ),
    "defensive": Direction(
        "低波稳健", "low_volatility_quality", "看低波动、较浅回撤和估值。",
        "10–30 个交易日", "回撤或波动显著扩大时重新评估。",
        "volatility_20d_pct", "近 20 个交易日波动率 {:.1f}%",
    ),
    "long": Direction(
        "长线观察", "quality_value", "看估值、流动性和走势稳定性。",
        "3–6 个月", "下一份财报发布后复核盈利、经营现金流和估值。",
        "pe_ratio", "当前市盈率 {:.1f} 倍",
    ),
}


class ScreeningDataError(RuntimeError):
    """Market data needed for the selected direction is unavailable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def directions() -> list[dict]:
    return [
        {"id": key, "title": style.title, "description": style.description, "holding": style.holding}
        for key, style in DIRECTIONS.items()
    ]


def load_strategy(style: Direction) -> ScreeningConfig:
    raw = yaml.safe_load((STRATEGY_DIR / f"{style.strategy}.yaml").read_text(encoding="utf-8"))
    config = raw["screening"].copy()
    filters = HardFilterConfig(**config.pop("hard_filters"))
    return ScreeningConfig(hard_filters=filters, **config)


def snapshot_columns(filters: HardFilterConfig) -> list[str]:
    rules = {
        "amount": (filters.amount_min,),
        "price": (filters.price_min, filters.price_max),
        "total_mv": (filters.market_cap_min, filters.market_cap_max),
        "pe_ratio": (filters.pe_ttm_min, filters.pe_ttm_max),
        "pb_ratio": (filters.pb_min, filters.pb_max),
        "volume_ratio": (filters.volume_ratio_min,),
        "turnover_rate": (filters.turnover_rate_min,),
        "change_pct": (filters.change_pct_min, filters.change_pct_max),
    }
    return ["code", "name", "price", "change_pct", "amount"] + [
        name for name, thresholds in rules.items()
        if name not in {"price", "change_pct", "amount"} and any(value is not None for value in thresholds)
    ]


def _candidate(row: dict, rank: int) -> Pick:
    score = float(row["screen_score"])
    return Pick(
        rank=rank,
        code=row["code"],
        name=row["name"],
        screen_score=score,
        final_score=score,
        price=float(row["price"]),
        change_pct=float(row["change_pct"]),
        amount=float(row["amount"]),
        industry=safe_text(row.get("industry")),
        pe_ratio=safe_float(row.get("pe_ratio")),
        pb_ratio=safe_float(row.get("pb_ratio")),
        volume_ratio=safe_float(row.get("volume_ratio")),
        turnover_rate=safe_float(row.get("turnover_rate")),
        signal_score=safe_float(row.get("signal_score")),
        macd_status=safe_text(row.get("macd_status")),
        rsi_status=safe_text(row.get("rsi_status")),
        daily_quality_score=safe_float(row.get("daily_quality_score")),
        daily_quality_flags=safe_text(row.get("daily_quality_flags")),
        factor_scores={
            name: value for name, column in factor_score_columns().items()
            if (value := safe_float(row.get(column))) is not None
        },
    )


def _explain(pick: Pick, row: dict, style: Direction, weights: dict[str, float]) -> dict:
    ordered_factors = sorted(
        (factor for factor in pick.factor_scores if factor in FACTOR_NAMES and weights.get(factor, 0) > 0),
        key=lambda factor: pick.factor_scores[factor] * weights[factor],
        reverse=True,
    )[:2]
    factors = "、".join(FACTOR_NAMES[factor] for factor in ordered_factors)
    metric = safe_float(row.get(style.metric))
    facts = [style.metric_format.format(metric)] if metric is not None else []
    if factors:
        facts.append(f"筛选得分主要来自{factors}")
    reason = "，".join(facts) + "。" if facts else "通过了当前方向的筛选条件。"
    risks = [RISK_NAMES[flag] for flag in pick.risk_flags if flag in RISK_NAMES]
    if any(flag.startswith("portfolio_sector_concentration:") for flag in pick.risk_flags):
        risks.append("与前列候选集中在同一行业或题材。")
    return {
        "code": pick.code,
        "name": pick.name,
        "score": round(max(0.0, min(100.0, pick.final_score)), 1),
        "score_detail": {
            "base": round(pick.screen_score, 2),
            "risk_penalty": round(pick.risk_penalty, 2),
            "portfolio_penalty": round(pick.portfolio_penalty, 2),
            "factors": [
                {"name": FACTOR_NAMES[name], "score": round(value, 1), "weight_pct": round(weights[name] * 100, 1)}
                for name, value in pick.factor_scores.items() if name in FACTOR_NAMES and weights.get(name, 0) > 0
            ],
        },
        "reason": reason,
        "risk": " ".join(risks[:2]) if risks else "本轮规则未标记具体风险。",
        "holding": style.holding,
        "review": style.review,
    }


def _add_model_explanations(candidates: list[dict], api_key: str, model: str) -> bool:
    facts = [{key: candidate[key] for key in ("code", "reason")} for candidate in candidates]
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": (
                        '把入选理由改写为一句简单中文。只使用提供的事实，不增加新闻、业绩、'
                        '预测、买卖建议或持仓时间。原样保留所有股票代码。只返回 JSON 对象，格式为 '
                        '{"explanations":[{"code":"股票代码","reason":"一句话理由"}]}。'
                    )},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                ],
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "max_tokens": 600,
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        items = json.loads(response.json()["choices"][0]["message"]["content"])["explanations"]
        explanations = {
            item["code"]: item["reason"].strip() for item in items
            if isinstance(item["reason"], str) and 1 <= len(item["reason"].strip()) <= 130
        }
        if len(items) != len(candidates) or set(explanations) != {item["code"] for item in candidates}:
            return False
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
        logger.info("Optional model explanation unavailable (%s)", type(exc).__name__)
        return False
    for candidate in candidates:
        candidate["reason"] = explanations[candidate["code"]]
    return True


def run_screen(
    direction: str,
    data_dir: Path,
    on_progress: Callable[[str], None],
    *,
    api_key: str = "",
    model: str = "",
) -> dict:
    style = DIRECTIONS[direction]
    config = load_strategy(style)
    filters = without_daily_filters(config.hard_filters)
    required = snapshot_columns(filters)
    on_progress("正在获取 A 股行情")
    try:
        snapshot = fetch_snapshot_with_fallback(
            SNAPSHOT_SOURCES,
            required_columns=required,
            fallback_snapshot_path=data_dir / "market.json",
            fallback_max_age_hours=18,
            cache_ttl_seconds=300,
        )
    except RuntimeError as exc:
        raise ScreeningDataError("暂时无法获取满足该方向条件的 A 股行情，请稍后重试。") from exc
    snapshot_time = snapshot.attrs.get("last_good_created_at") or utc_now()
    snapshot_source = snapshot.attrs.get("last_good_snapshot_source") or snapshot.attrs.get("snapshot_source", "")
    cached = bool(snapshot.attrs.get("cache_used") or snapshot.attrs.get("fallback_used"))
    stale = bool(snapshot.attrs.get("stale"))

    valid = snapshot.copy()
    valid["code"] = valid["code"].astype(str).str.strip().str.zfill(6)
    valid["name"] = valid["name"].fillna("").astype(str).str.strip()
    valid = valid.loc[
        valid["code"].str.fullmatch(r"(?:[036]\d{5}|[489]\d{5})")
        & valid["name"].ne("")
    ].drop_duplicates(subset="code").dropna(subset=required).reset_index(drop=True)

    on_progress("正在按方向筛选股票")
    eligible = apply_hard_filters(valid, filters)
    pool = eligible
    daily_count = 0
    if not pool.empty:
        pool = compute_screen_scores(pool, config).sort_values(
            ["screen_score", "code"], ascending=[False, True]
        ).head(SHORTLIST_LIMIT)
        on_progress("正在核对候选的日线走势")
        pool = enrich_daily_features(
            pool,
            max_rows=len(pool),
            source="auto",
            fetch_retries=0,
            cache_dir=data_dir / "daily",
            max_workers=6,
        )
        daily_count = int(pool.attrs.get("daily_success_count", 0))
        if requires_daily_features(config.hard_filters):
            if daily_count == 0:
                raise ScreeningDataError("没有获取到该方向所需的日线行情，请稍后重试。")
            pool = apply_hard_filters(pool, config.hard_filters)

    on_progress("正在计算评分和风险")
    candidates = []
    if not pool.empty:
        scored = compute_screen_scores(pool, config)
        rows = {row["code"]: row for row in scored.to_dict("records")}
        picks = [_candidate(row, rank) for rank, row in enumerate(rows.values(), start=1)]
        picks, _ = apply_risk_overlay(picks, profile=config.risk_profile, veto_high_risk=True)
        picks, _ = apply_portfolio_overlay(picks, profile=config.portfolio_profile)
        weights = normalized_factor_weights(config)
        candidates = [_explain(pick, rows[pick.code], style, weights) for pick in picks[:CANDIDATE_LIMIT]]

    ai_used = False
    if api_key and candidates:
        on_progress("正在整理入选说明")
        ai_used = _add_model_explanations(candidates, api_key, model)
    return {
        "direction": direction,
        "direction_label": style.title,
        "snapshot_time": snapshot_time,
        "snapshot_source": SOURCE_NAMES.get(snapshot_source, snapshot_source or "未知来源"),
        "cached": cached,
        "stale": stale,
        "daily_unavailable": bool(not eligible.empty and daily_count == 0),
        "ai_used": ai_used,
        "candidates": candidates,
    }
