"""Export a dated, read-only GitHub Pages build from the running local app."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from shutil import copyfile
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "lazy_picker" / "web"
SHANGHAI = ZoneInfo("Asia/Shanghai")
PAGES = ("index.html", "market.html", "sectors.html")
PICK_FIELDS = ("name", "code", "score", "reason", "risk", "holding", "review")
RESULT_FIELDS = ("direction_label", "snapshot_time", "snapshot_source", "stale", "cached", "daily_unavailable")


def export(api_root: str, output: Path) -> None:
    def read(path: str):
        with urlopen(api_root.rstrip("/") + path, timeout=35) as response:
            return json.load(response)

    market = read("/api/market")
    sectors = read("/api/sectors")
    trade_date = market["trade_date"]
    if not market["indices"] or not trade_date or sectors["trade_date"] != trade_date:
        raise ValueError("大盘与板块必须有同一交易日的数据。")

    directions = read("/api/directions")
    runs = {}
    for direction in directions:
        key = direction["id"]
        run = read("/api/runs/latest?direction=" + quote(key, safe=""))
        result = run.get("result") if run else None
        if not result or result.get("stale"):
            continue
        captured_at = datetime.fromisoformat(result["snapshot_time"]).astimezone(SHANGHAI)
        if captured_at.date().isoformat() != trade_date or (captured_at.hour, captured_at.minute) < (15, 0):
            continue
        published = {field: result[field] for field in RESULT_FIELDS}
        published["candidates"] = []
        for pick in result["candidates"]:
            public_pick = {field: pick[field] for field in PICK_FIELDS}
            if "score_detail" in pick:
                public_pick["score_detail"] = pick["score_detail"]
            if "advice" in pick:
                public_pick["advice"] = pick["advice"]
            published["candidates"].append(public_pick)
        runs[key] = {"result": published}
    if not runs:
        raise ValueError("请先在本机完成至少一个方向的收盘后筛选。")

    # The ranking is complete; only the leading boards get constituent snapshots.
    sectors["members"] = {}
    tasks = []
    for category, group in sectors["groups"].items():
        if group and group["trade_date"] == trade_date:
            sectors["members"][category] = {}
            tasks.extend((category, row["id"], group["source"]) for row in group["rows"][:5])

    def read_members(task):
        category, sector_id, source = task
        try:
            detail = read(f"/api/sectors/{category}/{quote(sector_id, safe='')}")
        except OSError:
            return category, sector_id, None
        valid = detail.get("members") and detail["trade_date"] == trade_date and detail["source"] == source
        return category, sector_id, detail if valid else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        for category, sector_id, detail in pool.map(read_members, tasks):
            if detail:
                sectors["members"][category][sector_id] = detail

    snapshot = {
        "picker": {"directions": directions, "runs": runs},
        "assistant": {"rules": read("/api/assistant/rules")["rules"], "ai_available": False},
        "market": market,
        "sectors": sectors,
    }
    data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assets_dir = output / "assets"
    data_dir = output / "data"
    assets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    for page in PAGES:
        template = (WEB / page).read_text(encoding="utf-8")
        (output / page).write_text(template.replace('data-mode="local"', 'data-mode="snapshot"'), encoding="utf-8")
    for asset in WEB.iterdir():
        if asset.is_file() and asset.suffix in {".css", ".js", ".png", ".webp", ".svg"}:
            copyfile(asset, assets_dir / asset.name)
    (data_dir / "snapshot.json").write_text(data, encoding="utf-8")
    (output / ".nojekyll").touch()
    print(f"{trade_date} 收盘快照：{len(runs)}/{len(directions)} 个方向，"
          f"{sum(map(len, sectors['members'].values()))} 个板块有成分明细。输出：{output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-root", default="http://127.0.0.1:8011")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    export(args.api_root, args.output)
