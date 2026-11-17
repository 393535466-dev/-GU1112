"""Prevent old recommendations and stale source dates from appearing current."""
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from lazy_picker import picker
from lazy_picker.engine import snapshot


class SnapshotDateTests(unittest.TestCase):
    def test_provider_date_wins_over_fetch_time(self):
        old = pd.DataFrame({'code': ['000001'], 'MAX_TRADE_DATE': ['2026-11-12']})
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot_date(old, date(2026, 11, 16))

    def test_mixed_snapshot_keeps_only_requested_session(self):
        frame = pd.DataFrame({'code': ['000001', '000002', '000003'],
                              'trade_date': ['20261116', '20261113', None]})
        result = snapshot.validate_snapshot_date(frame, date(2026, 11, 16))
        self.assertEqual(result.code.tolist(), ['000001'])
        self.assertEqual(result.quote_date.tolist(), ['2026-11-16'])

    def test_undated_source_is_not_accepted_as_current(self):
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot_date(pd.DataFrame({'price': [10]}), date(2026, 11, 16))

    def test_old_source_continues_to_next_provider(self):
        old = pd.DataFrame({'code': ['000001'], 'quote_date': ['2026-11-12']})
        current = pd.DataFrame({'code': ['000001'], 'quote_date': ['2026-11-16']})
        with patch.object(snapshot, 'fetch_cn_snapshot', side_effect=[old, current]), patch.object(
            snapshot, '_source_disabled_reason', return_value=None
        ), patch.object(snapshot, '_record_source_failure'), patch.object(snapshot, '_record_source_success'):
            result = snapshot.fetch_snapshot_with_fallback(['old', 'current'], expected_trade_date=date(2026, 11, 16))
            self.assertEqual(result.attrs['snapshot_source'], 'current')

    def test_legacy_callers_without_date_requirement_keep_existing_behavior(self):
        frame = pd.DataFrame({'price': [10]})
        self.assertIs(snapshot.validate_snapshot_date(frame, None), frame)


class ResultFreshnessTests(unittest.TestCase):
    def result(self, stamp, quote_date='2026-11-16'):
        return {'screening_version': picker.SCREENING_VERSION, 'snapshot_time': stamp,
                'quote_trade_date': quote_date, 'stale': False}

    def test_old_and_unverified_results_require_refresh(self):
        now = datetime.fromisoformat('2026-11-17T00:30:00+08:00')
        for result in [self.result('2026-11-13T16:00:00+08:00', '2026-11-13'),
                       {'snapshot_time': '2026-11-17T00:29:00+08:00'}]:
            self.assertTrue(picker.result_freshness(result, now)['needs_refresh'])

    def test_completed_close_survives_midnight_and_weekend(self):
        for stamp, day, now in [
            ('2026-11-16T16:00:00+08:00', '2026-11-16', '2026-11-17T08:00:00+08:00'),
            ('2026-11-13T16:00:00+08:00', '2026-11-13', '2026-11-15T18:00:00+08:00'),
        ]:
            with self.subTest(now=now):
                self.assertFalse(picker.result_freshness(self.result(stamp, day), datetime.fromisoformat(now))['needs_refresh'])

    def test_intraday_result_expires_at_close_and_after_five_minutes(self):
        for stamp, now in [
            ('2026-11-16T14:59:00+08:00', '2026-11-16T15:01:00+08:00'),
            ('2026-11-16T10:00:00+08:00', '2026-11-16T10:06:00+08:00'),
        ]:
            with self.subTest(now=now):
                self.assertTrue(picker.result_freshness(self.result(stamp), datetime.fromisoformat(now))['needs_refresh'])

    def test_today_quote_is_required_from_opening_auction(self):
        self.assertEqual(snapshot.expected_quote_date(datetime.fromisoformat('2026-11-17T09:15:00+08:00')), date(2026, 11, 17))


class ScreeningEntryTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            {'code': code, 'name': name, 'price': 20, 'change_pct': change,
             'amount': 8e8, 'turnover_rate': 5, 'volume_ratio': 2,
             'pe_ratio': 20, 'pb_ratio': 2, 'quote_date': '2026-11-16'}
            for code, name, change in [('000001', '上涨候选', 3), ('000002', '下跌股票', -6), ('000003', '陈旧日线', 4)]
        ])

    def enrich(self, frame, **kwargs):
        frame = frame.copy()
        frame['daily_trade_date'] = frame.code.map({'000001': '2026-11-16', '000003': '2026-11-13'})
        frame['daily_quality_flags'] = ''
        frame['daily_quality_score'] = 100
        frame['daily_close'] = 20
        frame['signal_score'] = 70
        frame.attrs['daily_success_count'] = len(frame)
        return frame

    def test_short_entry_excludes_decliners_and_old_daily_data(self):
        with TemporaryDirectory() as folder, patch.object(picker, 'expected_quote_date', return_value=date(2026, 11, 16)), patch.object(
            picker, 'get_effective_trading_date', return_value=date(2026, 11, 16)
        ), patch.object(picker, 'fetch_snapshot_with_fallback', return_value=self.frame()), patch.object(
            picker, 'enrich_daily_features', side_effect=self.enrich
        ):
            result = picker.run_screen('short', Path(folder), lambda message: None)
            self.assertEqual([row['code'] for row in result['candidates']], ['000001'])
            self.assertEqual(result['candidates'][0]['change_pct'], 3)
            self.assertEqual(result['quote_trade_date'], '2026-11-16')

    def test_failed_refresh_cannot_recommend_stale_cache(self):
        frame = self.frame()
        frame.attrs['stale'] = True
        with TemporaryDirectory() as folder, patch.object(picker, 'expected_quote_date', return_value=date(2026, 11, 16)), patch.object(
            picker, 'fetch_snapshot_with_fallback', return_value=frame
        ), patch.object(picker, 'enrich_daily_features') as enrich:
            with self.assertRaises(picker.ScreeningDataError):
                picker.run_screen('short', Path(folder), lambda message: None)
            enrich.assert_not_called()

    def test_empty_market_does_not_fill_quota_with_falling_stocks(self):
        frame = self.frame()
        frame['change_pct'] = -6
        with TemporaryDirectory() as folder, patch.object(picker, 'expected_quote_date', return_value=date(2026, 11, 16)), patch.object(
            picker, 'fetch_snapshot_with_fallback', return_value=frame
        ), patch.object(picker, 'enrich_daily_features') as enrich:
            result = picker.run_screen('short', Path(folder), lambda message: None)
            self.assertEqual(result['candidates'], [])
            enrich.assert_not_called()


if __name__ == '__main__':
    unittest.main()
