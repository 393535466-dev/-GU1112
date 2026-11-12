"""Closing-session industry and concept rankings and constituent summaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path
import re
from statistics import median
from threading import Lock
from typing import Callable, Literal

import pandas as pd

from .engine.normalize import safe_float, safe_text
from .engine.source_guard import call_with_timeout
from .engine.trading_calendar import get_effective_trading_date, get_market_now

logger = logging.getLogger(__name__)
SectorCategory = Literal["industry", "concept"]
_NAMES = {"industry": "行业", "concept": "概念"}
_LOCKS = {category: Lock() for category in _NAMES}
_CACHE_TTL = timedelta(minutes=5)
_SECTOR_ID = re.compile(r"[a-zA-Z0-9_]{1,64}")


def _fetch(fetch: Callable, **kwargs) -> pd.DataFrame | None:
    try:
        return call_with_timeout(fetch, timeout_sec=7, label=fetch.__name__, **kwargs)
    except Exception as exc:
        logger.info("Sector data %s unavailable: %s", fetch.__name__, type(exc).__name__)
        return None


def rank_sectors(frame: pd.DataFrame, source: str) -> dict | None:
    if source == "新浪财经":
        id_col, name_col, amount_col = "label", "板块", "总成交额"
        count_col, leader_col, leader_change_col = "公司家数", "股票名称", "个股-涨跌幅"
    else:
        id_col, name_col, amount_col = "板块代码", "板块名称", "成交额"
        count_col, leader_col, leader_change_col = "成分股数", "领涨股票", "领涨股票-涨跌幅"
    if not {id_col, name_col, "涨跌幅"}.issubset(frame.columns):
        return None

    unique = {}
    for row in frame.to_dict("records"):
        code, name = safe_text(row[id_col]), safe_text(row[name_col])
        change = safe_float(row["涨跌幅"])
        if not _SECTOR_ID.fullmatch(code) or not name or change is None or code in unique:
            continue
        amount = safe_float(row.get(amount_col))
        count = safe_float(row.get(count_col))
        unique[code] = {
            "id": code, "name": name, "change_pct": change,
            "amount_yi": round(amount / 1e8, 2) if amount is not None and amount >= 0 else None,
            "member_count": int(count) if count is not None and count >= 0 else None,
            "leader": safe_text(row.get(leader_col)) or None,
            "leader_change_pct": safe_float(row.get(leader_change_col)),
        }
    if not unique:
        return None

    rows = sorted(unique.values(), key=lambda row: (-row["change_pct"], row["id"]))
    amounts = sorted(
        (row for row in rows if row["amount_yi"] is not None),
        key=lambda row: (-row["amount_yi"], row["id"]),
    )
    for position, row in enumerate(rows, start=1):
        row.update(rank=position, amount_rank=None)
    for position, row in enumerate(amounts, start=1):
        row["amount_rank"] = position
    changes = [row["change_pct"] for row in rows]
    return {
        "source": source, "rows": rows, "amount_coverage": len(amounts),
        "summary": {
            "up": sum(value > 0 for value in changes),
            "down": sum(value < 0 for value in changes),
            "flat": sum(value == 0 for value in changes),
            "median_pct": median(changes),
        },
    }


def get_sector_group(
    data_dir: Path, category: SectorCategory, session: date, *, after_close: bool, refresh: bool = False,
) -> dict | None:
    import akshare as ak

    path = data_dir / f"sectors_{category}.json"
    with _LOCKS[category]:
        cached = None
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous["trade_date"] == session.isoformat():
                age = get_market_now("cn") - datetime.fromisoformat(previous["generated_at"])
                cached = previous
                if not after_close or (not refresh and age < _CACHE_TTL):
                    return cached
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if not after_close:
            return cached
        eastmoney = ak.stock_board_industry_name_em if category == "industry" else ak.stock_board_concept_name_em
        for source, fetch, kwargs in (
            ("新浪财经", ak.stock_sector_spot, {"indicator": _NAMES[category]}),
            ("东方财富", eastmoney, {}),
        ):
            frame = _fetch(fetch, **kwargs)
            group = rank_sectors(frame, source) if frame is not None else None
            if group:
                group.update(trade_date=session.isoformat(), generated_at=get_market_now("cn").isoformat())
                pending = path.with_suffix(".tmp")
                pending.write_text(json.dumps(group, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                pending.replace(path)
                return group
        return {**cached, "refresh_failed": True} if cached else None


def _market_session() -> tuple[date, bool]:
    now = get_market_now("cn")
    session = get_effective_trading_date("cn", current_time=now)
    return session, session == now.date() and now.hour >= 15


def get_sector_overview(data_dir: Path, *, refresh: bool = False) -> dict:
    session, after_close = _market_session()
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {
            category: pool.submit(
                get_sector_group, data_dir, category, session, after_close=after_close, refresh=refresh,
            )
            for category in _NAMES
        }
        groups = {category: task.result() for category, task in tasks.items()}
    return {"trade_date": session.isoformat(), "groups": groups}


def summarize_members(frame: pd.DataFrame, source: str) -> dict | None:
    if source == "新浪财经":
        code, name, change, amount = "code", "name", "changepercent", "amount"
    else:
        code, name, change, amount = "代码", "名称", "涨跌幅", "成交额"
    if not {code, name, change}.issubset(frame.columns):
        return None

    rows = []
    for row in frame.drop_duplicates(code).to_dict("records"):
        ticker, label = safe_text(row[code]), safe_text(row[name])
        pct = safe_float(row[change])
        value = safe_float(row.get(amount))
        if not ticker.isdigit() or len(ticker) != 6 or not label or pct is None or value is None or value <= 0:
            continue
        rows.append({"code": ticker, "name": label, "change_pct": pct})
    if not rows:
        return None
    positive = sorted((row for row in rows if row["change_pct"] > 0), key=lambda row: -row["change_pct"])
    negative = sorted((row for row in rows if row["change_pct"] < 0), key=lambda row: row["change_pct"])
    return {
        "up": len(positive), "down": len(negative), "flat": len(rows) - len(positive) - len(negative),
        "leaders": positive[:3], "laggards": negative[:3],
    }


def get_sector_detail(data_dir: Path, category: SectorCategory, sector_id: str) -> dict | None:
    import akshare as ak

    if not _SECTOR_ID.fullmatch(sector_id):
        return None
    session, after_close = _market_session()
    group = get_sector_group(data_dir, category, session, after_close=after_close)
    if group is None or not any(row["id"] == sector_id for row in group["rows"]):
        return None
    members = None
    if after_close:
        if group["source"] == "新浪财经":
            frame = _fetch(ak.stock_sector_detail, sector=sector_id)
        else:
            fetch = ak.stock_board_industry_cons_em if category == "industry" else ak.stock_board_concept_cons_em
            frame = _fetch(fetch, symbol=sector_id)
        if frame is not None:
            members = summarize_members(frame, group["source"])
    return {"trade_date": session.isoformat(), "source": group["source"], "members": members}
