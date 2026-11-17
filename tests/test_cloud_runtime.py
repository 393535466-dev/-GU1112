"""Exercise cloud startup, persistent result reads, and request completion."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from lazy_picker import app, cloud_store, runtime
from lazy_picker.picker import ScreeningDataError


class CloudRuntimeTests(unittest.TestCase):
    def test_cloud_startup_never_initializes_sqlite_or_background_worker(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"VERCEL": "1"}), \
             patch.object(app, "DATA", Path(directory) / "cache"), \
             patch.object(app, "initialize_database", side_effect=AssertionError("read-only root")), \
             patch.object(app, "ThreadPoolExecutor", side_effect=AssertionError("background work")):
            async def start():
                async with app.lifespan(app.app):
                    self.assertTrue(app.DATA.is_dir())
                    self.assertEqual(app.health()["runtime"], "vercel")
            asyncio.run(start())

    def test_cloud_cache_uses_temporary_directory(self):
        with patch.dict(os.environ, {"VERCEL": "1"}), patch.object(runtime.tempfile, "gettempdir", return_value="/tmp"):
            self.assertEqual(runtime.data_directory(Path("/var/task")), Path("/tmp/lazy-stock-picker"))

    def test_hosts_include_exact_deployment_aliases_without_wildcard(self):
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_URL": "preview.vercel.app",
                                   "VERCEL_PROJECT_PRODUCTION_URL": "production.vercel.app",
                                   "LAZY_PICKER_ALLOWED_HOSTS": "stocks.example.com"}, clear=True):
            self.assertEqual(runtime.allowed_hosts(), ["localhost", "127.0.0.1", "preview.vercel.app",
                                                       "production.vercel.app", "stocks.example.com"])

    def test_cloud_request_returns_only_after_result_is_saved(self):
        events = []
        def screen(*args, **kwargs):
            events.append("screen")
            return {"candidates": []}
        def save(run):
            events.append("save")
            self.assertEqual(run["status"], "completed")
        with patch.dict(os.environ, {"VERCEL": "1"}), patch.object(app, "run_screen", side_effect=screen), \
             patch.object(cloud_store, "save_run", side_effect=save), \
             patch.object(app, "result_freshness", return_value={"needs_refresh": False}):
            run = app.start_run(app.RunRequest(direction="short"))
        self.assertEqual(events, ["screen", "save"])
        self.assertEqual(run["status"], "completed")

    def test_bad_market_data_is_saved_as_failed_without_candidates(self):
        with patch.object(app, "run_screen", side_effect=ScreeningDataError("行情日期不符")), \
             patch.object(cloud_store, "save_run") as save:
            run = app.cloud_run("short")
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run["result"])
        self.assertEqual(run["message"], "行情日期不符")
        save.assert_called_once()

    def test_persistence_failure_is_not_reported_as_success(self):
        with patch.object(app, "run_screen", return_value={"candidates": []}), \
             patch.object(cloud_store, "save_run", side_effect=OSError("store unavailable")), \
             self.assertLogs(app.logger, level="ERROR"):
            with self.assertRaises(HTTPException) as raised:
                app.cloud_run("short")
        self.assertEqual(raised.exception.status_code, 503)


class CloudStoreTests(unittest.TestCase):
    def test_cold_start_restores_same_day_boards_without_republishing(self):
        value = {"trade_date": "2026-11-17", "generated_at": "2026-11-17T16:00:00+08:00", "sectors": []}
        entry = SimpleNamespace(uploaded_at=datetime.now(timezone.utc), url="private-industry")
        def listing(**kwargs):
            return iter([entry] if "sectors_industry.json" in kwargs["prefix"] else [])
        with TemporaryDirectory() as directory, patch.object(cloud_store, "_PUBLISHED", {}), \
             patch.object(cloud_store.blob, "query_blobs", side_effect=listing), \
             patch.object(cloud_store.blob, "get_blob", return_value=SimpleNamespace(body=json.dumps(value).encode())), \
             patch.object(cloud_store.blob, "put_blob") as put:
            with cloud_store.closing_cache(Path(directory), "2026-11-17"):
                restored = json.loads((Path(directory) / "sectors_industry.json").read_text())
                self.assertEqual(restored, value)
            put.assert_not_called()

    def test_closing_cache_never_labels_old_data_as_today(self):
        entry = SimpleNamespace(uploaded_at=datetime.now(timezone.utc), url="old")
        with TemporaryDirectory() as directory, patch.object(cloud_store, "_PUBLISHED", {}), \
             patch.object(cloud_store.blob, "query_blobs", side_effect=lambda **kwargs: iter([entry])), \
             patch.object(cloud_store.blob, "get_blob", return_value=SimpleNamespace(body=b'{"trade_date":"2026-11-16"}')), \
             patch.object(cloud_store.blob, "put_blob") as put:
            with cloud_store.closing_cache(Path(directory), "2026-11-17"):
                self.assertEqual(list(Path(directory).iterdir()), [])
            put.assert_not_called()

    def test_separate_requests_read_the_same_immutable_completed_result(self):
        objects = {}
        def put(path, body, **kwargs):
            self.assertEqual(kwargs["access"], "private")
            self.assertNotIn(path, objects)
            objects[path] = body
        run = {"id": "a" * 32, "direction": "short", "status": "completed", "result": {"candidates": []}}
        with patch.object(cloud_store.blob, "put_blob", side_effect=put):
            cloud_store.save_run(run)
        with patch.object(cloud_store.blob, "get_blob",
                          side_effect=lambda path, **kwargs: SimpleNamespace(body=objects[path])):
            self.assertEqual(cloud_store.read_run(run["id"]), run)
        self.assertEqual(len(objects), 2)

    def test_latest_does_not_depend_on_list_order_or_mutable_cached_pointer(self):
        now = datetime.now(timezone.utc)
        older = SimpleNamespace(uploaded_at=now-timedelta(minutes=1), pathname="completed/short/z.json", url="old")
        newer = SimpleNamespace(uploaded_at=now, pathname="completed/short/a.json", url="new")
        with patch.object(cloud_store.blob, "query_blobs", return_value=iter([newer, older])), \
             patch.object(cloud_store.blob, "get_blob", return_value=SimpleNamespace(body=b'{"id":"new"}')) as get:
            self.assertEqual(cloud_store.latest_result("short"), {"id": "new"})
            get.assert_called_once_with("new", access="private")

    def test_failed_run_does_not_replace_latest_success(self):
        run = {"id": "b" * 32, "direction": "short", "status": "failed", "result": None}
        with patch.object(cloud_store.blob, "put_blob") as put:
            cloud_store.save_run(run)
        self.assertEqual(put.call_count, 1)
        self.assertTrue(put.call_args.args[0].startswith("runs/"))


if __name__ == "__main__":
    unittest.main()
