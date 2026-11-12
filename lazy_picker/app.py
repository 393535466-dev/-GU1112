"""Local API, job runner and history for the standalone stock picker."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing, contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .assistant import model_answer, product_rules
from .market import MarketUnavailable, get_market_report
from .picker import ScreeningDataError, directions, run_screen, utc_now
from .sectors import SectorCategory, get_sector_detail, get_sector_overview

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = Path(__file__).resolve().parent / "web"
logger = logging.getLogger(__name__)
load_dotenv(ROOT / ".env")

DirectionId = Literal["short", "swing", "defensive", "long"]


class RunRequest(BaseModel):
    direction: DirectionId


class AssistantRequest(BaseModel):
    direction: DirectionId
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


@contextmanager
def database():
    with closing(sqlite3.connect(DATA / "screening.sqlite3", timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            yield connection


def initialize_database():
    DATA.mkdir(exist_ok=True)
    with database() as db:
        db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                result TEXT
            );
            CREATE INDEX IF NOT EXISTS completed_runs
                ON runs(direction, completed_at) WHERE status = 'completed';
        """)
        db.execute(
            "UPDATE runs SET status = 'failed', message = ?, completed_at = ? "
            "WHERE status IN ('queued', 'running')",
            ("服务曾重启，请重新筛选。", utc_now()),
        )


def run_payload(row: sqlite3.Row | None):
    if row is None:
        return None
    payload = dict(row)
    if payload["result"] is not None:
        payload["result"] = json.loads(payload["result"])
    return payload


def update_run(run_id: str, status: str, message: str, result: dict | None = None):
    finished_at = utc_now() if status in {"completed", "failed"} else None
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
    with database() as db:
        db.execute(
            "UPDATE runs SET status = ?, message = ?, completed_at = ?, result = ? WHERE id = ?",
            (status, message, finished_at, serialized, run_id),
        )


def execute_run(run_id: str, direction: DirectionId):
    def progress(message: str):
        update_run(run_id, "running", message)

    try:
        result = run_screen(
            direction,
            DATA,
            progress,
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            model=os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-flash",
        )
        update_run(run_id, "completed", "筛选完成", result)
    except ScreeningDataError as exc:
        update_run(run_id, "failed", str(exc))
    except Exception:
        logger.exception("Stock screening run failed")
        update_run(run_id, "failed", "本次筛选未完成，请重试。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="screening") as worker:
        app.state.worker = worker
        yield


app = FastAPI(title="懒人选股", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/market.html", include_in_schema=False)
@app.get("/market", include_in_schema=False)
def market_page():
    return FileResponse(WEB / "market.html", headers={"Cache-Control": "no-store"})


@app.get("/api/market")
def market_report(refresh: bool = False):
    try:
        return get_market_report(DATA, refresh=refresh)
    except MarketUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/sectors.html", include_in_schema=False)
@app.get("/sectors", include_in_schema=False)
def sectors_page():
    return FileResponse(WEB / "sectors.html", headers={"Cache-Control": "no-store"})


@app.get("/api/sectors")
def sector_overview(refresh: bool = False):
    return get_sector_overview(DATA, refresh=refresh)


@app.get("/api/sectors/{category}/{sector_id}")
def sector_detail(category: SectorCategory, sector_id: str):
    detail = get_sector_detail(DATA, category, sector_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到该交易日的板块数据。")
    return detail


@app.get("/api/directions")
def available_directions():
    return directions()


@app.get("/api/assistant/rules")
def assistant_rules():
    return {"rules": product_rules(), "ai_available": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())}


@app.post("/api/assistant")
def ask_assistant(request: AssistantRequest):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return {"mode": "rules"}
    result = None
    if request.run_id:
        with database() as db:
            row = db.execute(
                "SELECT result FROM runs WHERE id = ? AND direction = ? AND status = 'completed'",
                (request.run_id, request.direction),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="这轮筛选结果已不可用，请重新打开候选。")
        result = json.loads(row["result"])
    answer = model_answer(request.question, request.direction, result, DATA, api_key=api_key,
                          model=os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-flash")
    return {"mode": "ai", "answer": answer} if answer else {"mode": "rules"}


@app.get("/api/runs/active")
def active_run():
    with database() as db:
        return run_payload(db.execute(
            "SELECT * FROM runs WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone())


@app.get("/api/runs/latest")
def latest_run(direction: DirectionId):
    with database() as db:
        return run_payload(db.execute(
            "SELECT * FROM runs WHERE direction = ? AND status = 'completed' "
            "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
            (direction,),
        ).fetchone())


@app.post("/api/runs", status_code=202)
def start_run(request: RunRequest):
    run_id = uuid4().hex
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT * FROM runs WHERE status IN ('queued', 'running') LIMIT 1").fetchone()
        if active:
            return run_payload(active)
        db.execute(
            "INSERT INTO runs (id, direction, status, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, request.direction, "queued", "准备筛选", utc_now()),
        )
        row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    try:
        app.state.worker.submit(execute_run, run_id, request.direction)
    except RuntimeError:
        update_run(run_id, "failed", "服务正在停止，请稍后重试。")
        raise HTTPException(status_code=503, detail="服务正在停止，请稍后重试。") from None
    return run_payload(row)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with database() as db:
        row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到筛选任务。")
    return run_payload(row)
