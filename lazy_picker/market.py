"""A-share close review, adapted from DSA's market overview and Market Light."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from threading import Lock
from typing import Callable

import pandas as pd

from .engine.daily import _fetch_daily_sina, _fetch_daily_tencent
from .engine.source_guard import call_with_timeout
from .engine.snapshot import fetch_snapshot_with_fallback
from .engine.trading_calendar import get_effective_trading_date, get_market_now
from .picker import SNAPSHOT_SOURCES, SOURCE_NAMES
from .sectors import get_sector_group

logger = logging.getLogger(__name__)
INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
}
_CACHE_TTL = timedelta(minutes=5)
_MARKET_LOCK = Lock()


class MarketUnavailable(RuntimeError):
    """No usable index close was returned for the selected trading session."""


def index_summary(frame: pd.DataFrame, session: date) -> dict | None:
    bars = frame[["date", "close"]].copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce").dt.date
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.dropna().loc[
        lambda rows: (rows["date"] <= session) & rows["close"].between(0, float("inf"), inclusive="neither")
    ]
    bars = bars.sort_values("date").drop_duplicates("date", keep="last")
    if len(bars) < 2 or bars.iloc[-1]["date"] != session:
        return None

    close = bars["close"]
    current = float(close.iloc[-1])
    ma20 = float(close.iloc[-20:].mean()) if len(close) >= 20 else None
    ma60 = float(close.iloc[-60:].mean()) if len(close) >= 60 else None
    if ma20 is None or ma60 is None:
        trend, detail = "数据不足", "至少需要 60 个已收盘交易日才能判断均线趋势。"
    elif current > ma20 > ma60:
        trend, detail = "偏上行", "收盘高于 20 日均线，20 日均线也高于 60 日均线。"
    elif current < ma20 < ma60:
        trend, detail = "偏下行", "收盘低于 20 日均线，20 日均线也低于 60 日均线。"
    else:
        trend, detail = "均线分化", "收盘与两条均线的排列方向不一致。"

    bars["ma20"] = close.rolling(20).mean()
    return {
        "close": round(current, 2),
        "change_pct": round((current / float(close.iloc[-2]) - 1) * 100, 2),
        "change_5d": round((current / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else None,
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma60": round(ma60, 2) if ma60 is not None else None,
        "trend": trend,
        "trend_detail": detail,
        "series": [
            {
                "date": day.isoformat(),
                "close": round(float(price), 2),
                "ma20": round(float(average), 2) if pd.notna(average) else None,
            }
            for day, price, average in bars.tail(60).itertuples(index=False, name=None)
        ],
    }


def market_temperature(changes: list[float], breadth: dict | None, limits: dict | None) -> dict:
    # DSA uses a neutral 50 for an unavailable dimension, with weights 45/35/20.
    up, down = (breadth["up"], breadth["down"]) if breadth else (0, 0)
    limit_up, limit_down = (limits["up"], limits["down"]) if limits else (0, 0)
    index_change = sum(changes) / len(changes)
    breadth_score = int(100 * up / (up + down)) if up + down else 50
    index_score = int(max(0, min(100, 50 + index_change * 12)))
    limit_score = int(100 * limit_up / (limit_up + limit_down)) if limit_up + limit_down else 50
    score = round(breadth_score * .45 + index_score * .35 + limit_score * .20)
    label = "强势" if score >= 70 else "偏暖" if score >= 55 else "震荡" if score >= 40 else "偏弱"
    return {"score": score, "label": label, "index_change": round(index_change, 2)}


def _index(symbol: str, session: date) -> dict | None:
    for source, fetch in (("新浪财经", _fetch_daily_sina), ("腾讯行情", _fetch_daily_tencent)):
        try:
            result = index_summary(fetch(symbol, lookback_days=120), session)
            if result is not None:
                return {**result, "code": symbol, "name": INDICES[symbol], "source": source}
        except (KeyError, ValueError, TypeError, OSError, RuntimeError) as exc:
            logger.info("Index %s unavailable from %s: %s", symbol, source, type(exc).__name__)
    return None


def _optional(fetch: Callable, *args, timeout: float, **kwargs):
    try:
        return call_with_timeout(fetch, *args, timeout_sec=timeout, label=fetch.__name__, **kwargs)
    except Exception as exc:
        logger.info("Market data %s unavailable: %s", fetch.__name__, type(exc).__name__)
        return None


def _breadth(data_dir: Path) -> dict | None:
    frame = _optional(
        fetch_snapshot_with_fallback, SNAPSHOT_SOURCES,
        required_columns=["code", "change_pct", "amount"],
        fallback_snapshot_path=data_dir / "review_snapshot.json",
        fallback_max_age_hours=.05,
        cache_ttl_seconds=120,
        timeout=19,
    )
    if frame is None or frame.attrs.get("stale"):
        return None
    sample = frame.dropna(subset=["code"]).drop_duplicates("code")
    change = pd.to_numeric(sample["change_pct"], errors="coerce")
    amount = pd.to_numeric(sample["amount"], errors="coerce")
    mask = amount.between(0, float("inf"), inclusive="neither") & change.abs().lt(float("inf"))
    change = change[mask]
    if change.empty:
        return None
    source = frame.attrs.get("last_good_snapshot_source") or frame.attrs.get("snapshot_source")
    return {
        "up": int((change > 0).sum()), "down": int((change < 0).sum()),
        "flat": int((change == 0).sum()), "amount_yi": round(float(amount[mask].sum()) / 1e8, 1),
        "source": SOURCE_NAMES.get(source, "A 股行情快照"),
    }


def _sectors(data_dir: Path, session: date, after_close: bool, refresh: bool) -> dict | None:
    group = get_sector_group(data_dir, "industry", session, after_close=after_close, refresh=refresh)
    if not group:
        return None
    rows = group["rows"]
    return {
        "source": group["source"],
        "leaders": [row for row in rows if row["change_pct"] > 0][:3],
        "laggards": [row for row in reversed(rows) if row["change_pct"] < 0][:3],
    }


def _limit_count(fetch: Callable, session: date) -> int | None:
    frame = _optional(fetch, date=session.strftime("%Y%m%d"), timeout=7)
    return int(frame["代码"].nunique()) if frame is not None and "代码" in frame else None


def _build_report(data_dir: Path, session: date, *, after_close: bool, refresh: bool = False) -> dict:
    import akshare as ak

    with ThreadPoolExecutor(max_workers=8) as pool:
        index_tasks = [pool.submit(_index, symbol, session) for symbol in INDICES]
        up_task = pool.submit(_limit_count, ak.stock_zt_pool_em, session)
        down_task = pool.submit(_limit_count, ak.stock_zt_pool_dtgc_em, session)
        breadth_task = pool.submit(_breadth, data_dir) if after_close else None
        sector_task = pool.submit(_sectors, data_dir, session, after_close, refresh)

        indices = [index for task in index_tasks if (index := task.result()) is not None]
        up, down = up_task.result(), down_task.result()
        limits = {"up": up, "down": down} if up is not None and down is not None else None
        breadth = breadth_task.result() if breadth_task else None
        sectors = sector_task.result()

    if not indices:
        raise MarketUnavailable("暂时没有取得最近交易日的指数收盘数据，请稍后重试。")
    available = {index["code"] for index in indices}
    return {
        "trade_date": session.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "indices": indices,
        "breadth": breadth,
        "limits": limits,
        "sectors": sectors,
        "temperature": market_temperature([index["change_pct"] for index in indices], breadth, limits),
        "missing_indices": [name for code, name in INDICES.items() if code not in available],
    }


def _read_report(path: Path, session: date) -> dict | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("trade_date") == session.isoformat():
            return report
    except (OSError, ValueError, AttributeError):
        pass
    return None


def get_market_report(data_dir: Path, *, refresh: bool = False) -> dict:
    now = get_market_now("cn")
    session = get_effective_trading_date("cn", current_time=now)
    after_close = session == now.date() and now.hour >= 15
    path = data_dir / "market_review.json"
    with _MARKET_LOCK:
        cached = _read_report(path, session)
        if cached and not refresh:
            age = now - datetime.fromisoformat(cached["generated_at"])
            # Preserve closing snapshots when revisiting on a non-trading day.
            if age < _CACHE_TTL or not after_close:
                return {**cached, "cached": True}
        try:
            report = _build_report(data_dir, session, after_close=after_close, refresh=refresh)
        except MarketUnavailable:
            if cached:
                return {**cached, "cached": True, "refresh_failed": True}
            raise
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        pending.replace(path)
        return report
