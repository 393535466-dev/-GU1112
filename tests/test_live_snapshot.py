"""Live quote timestamps, units and coverage must survive the provider boundary."""
from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

from lazy_picker.engine import snapshot


def quote(code="600519", stamp="20261118143001", prefix="sh"):
    fields = [""] * 50
    for index, value in {1: "测试股票", 2: code, 3: "1442.66", 30: stamp,
                         32: "-1.03", 37: "431602", 38: "0.24", 39: "21.95",
                         44: "18034.43", 45: "18034.43", 46: "6.65", 49: "0.79"}.items():
        fields[index] = value
    return f'v_{prefix}{code}="' + "~".join(fields) + '";'


class LiveSnapshotTests(unittest.TestCase):
    def test_timestamp_and_units_come_from_quote(self):
        row = snapshot._parse_tencent_quotes(quote(), ["sh600519"])[0]
        self.assertEqual(row["quote_date"], "2026-11-18")
        self.assertEqual(row["quote_timestamp"], "2026-11-18T14:30:01+08:00")
        self.assertEqual(row["amount"], 4316020000)
        self.assertAlmostEqual(row["total_mv"], 1803443000000)
        self.assertEqual(row["volume_ratio"], .79)
        self.assertEqual(row["change_pct"], -1.03)

    def test_invalid_date_and_unrequested_stock_are_discarded(self):
        text = quote(stamp="14:30:01") + quote(code="600000") + 'v_sh600519="";'
        self.assertEqual(snapshot._parse_tencent_quotes(text, ["sh600519"]), [])

    def test_yesterday_and_future_quotes_are_not_relabelled_today(self):
        for stamp in ["20261117150000", "20261119150000"]:
            frame = pd.DataFrame(snapshot._parse_tencent_quotes(quote(stamp=stamp), ["sh600519"]))
            with self.assertRaises(ValueError):
                snapshot.validate_snapshot_date(frame, date(2026, 11, 18))

    def test_few_current_quotes_cannot_masquerade_as_full_market(self):
        frame = pd.DataFrame({"quote_date": ["2026-11-18"] + ["2026-11-17"] * 9})
        frame.attrs["universe_size"] = 10
        with self.assertRaisesRegex(ValueError, "覆盖"):
            snapshot.validate_snapshot_date(frame, date(2026, 11, 18))

    def test_a_failed_batch_does_not_become_a_partial_success(self):
        with patch.object(snapshot, "_cn_quote_symbols", return_value=["sh600519"]), patch.object(
            snapshot, "_fetch_tencent_batch", side_effect=RuntimeError("network failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                snapshot._fetch_tencent()

    def test_empty_batch_is_not_a_valid_market(self):
        with patch.object(snapshot, "_cn_quote_symbols", return_value=["sh600519"]), patch.object(
            snapshot, "_fetch_tencent_batch", return_value=[]
        ):
            with self.assertRaisesRegex(RuntimeError, "no dated quotes"):
                snapshot._fetch_tencent()

    def test_live_source_works_while_daily_source_still_has_yesterday(self):
        live = pd.DataFrame(snapshot._parse_tencent_quotes(quote(), ["sh600519"]))
        old = pd.DataFrame({"MAX_TRADE_DATE": ["2026-11-17"]})
        with patch.object(snapshot, "fetch_cn_snapshot", side_effect=[old, live]), patch.object(
            snapshot, "_source_disabled_reason", return_value=None
        ), patch.object(snapshot, "_record_source_failure"), patch.object(snapshot, "_record_source_success"):
            frame = snapshot.fetch_snapshot_with_fallback(
                ["em_datacenter", "tencent"], expected_trade_date=date(2026, 11, 18))
        self.assertEqual(frame.attrs["snapshot_source"], "tencent")
        self.assertEqual(frame.quote_date.tolist(), ["2026-11-18"])


if __name__ == "__main__":
    unittest.main()
