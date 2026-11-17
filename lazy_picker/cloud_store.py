"""Immutable screening results in a private Vercel Blob store.

The list API selects the newest object, so reading a mutable CDN-cached
"latest.json" can never return an older version of a just-completed run.
"""

import json
import hashlib
import logging
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from vercel.blob import BlobNotFoundError
from vercel.blob import sync as blob

logger = logging.getLogger(__name__)
_CLOSE_FILES = ("market_review.json", "sectors_industry.json", "sectors_concept.json")
_PUBLISHED = {}


def _encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _put(path: str, value: dict):
    return blob.put_blob(path, _encode(value), access="private", content_type="application/json")


def read_run(run_id: str) -> dict | None:
    try:
        return json.loads(blob.get_blob(f"runs/{run_id}.json", access="private").body)
    except BlobNotFoundError:
        return None


def latest_result(direction: str) -> dict | None:
    latest = max(blob.query_blobs(prefix=f"completed/{direction}/"),
                 key=lambda item: (item.uploaded_at, item.pathname), default=None)
    if latest is None:
        return None
    return json.loads(blob.get_blob(latest.url, access="private").body)


def save_run(run: dict):
    # Publish the history record before the completed-result index.
    _put(f"runs/{run['id']}.json", run)
    if run["status"] == "completed":
        _put(f"completed/{run['direction']}/{run['id']}.json", run)


def _read_close(path: Path, session: str) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value.get("trade_date") == session else None
    except (OSError, ValueError, TypeError):
        return None


@contextmanager
def closing_cache(data_dir: Path, session: str):
    """Keep closing boards across cold starts; never restore a different day."""
    for name in _CLOSE_FILES:
        if _read_close(data_dir / name, session) is not None:
            continue
        try:
            entry = max(blob.query_blobs(prefix=f"closing/{session}/{name}/"),
                        key=lambda item: item.uploaded_at, default=None)
            if entry is not None:
                value = json.loads(blob.get_blob(entry.url, access="private").body)
                if value.get("trade_date") == session:
                    body = _encode(value)
                    pending = data_dir / f".{name}.{uuid4().hex}.tmp"
                    pending.write_bytes(body)
                    pending.replace(data_dir / name)
                    _PUBLISHED[(session, name)] = hashlib.sha256(body).hexdigest()
        except Exception:
            logger.warning("Closing cache restore unavailable: %s", name, exc_info=True)
    try:
        yield
    finally:
        for name in _CLOSE_FILES:
            value = _read_close(data_dir / name, session)
            if value is None:
                continue
            digest = hashlib.sha256(_encode(value)).hexdigest()
            if _PUBLISHED.get((session, name)) == digest:
                continue
            try:
                _put(f"closing/{session}/{name}/{uuid4().hex}.json", value)
                _PUBLISHED[(session, name)] = digest
            except Exception:
                logger.warning("Closing cache persistence unavailable: %s", name, exc_info=True)
