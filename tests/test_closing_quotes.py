"""Regression coverage for missing close rankings after midnight."""
from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from lazy_picker import market, sectors


class ClosingQuoteTests(unittest.TestCase):
    def test_quote_window_includes_overnight_and_stops_at_auction(self):
        cases = [
            ('2026-11-16T16:00', date(2026, 11, 16), date(2026, 11, 17), True),
            ('2026-11-17T00:18', date(2026, 11, 16), date(2026, 11, 17), True),
            ('2026-11-17T09:14', date(2026, 11, 16), date(2026, 11, 17), True),
            ('2026-11-17T09:15', date(2026, 11, 16), date(2026, 11, 17), False),
            ('2026-11-17T12:00', date(2026, 11, 16), date(2026, 11, 17), False),
            ('2026-11-14T12:00', date(2026, 11, 13), date(2026, 11, 16), True),
        ]
        for stamp, session, next_day, allowed in cases:
            with self.subTest(stamp=stamp), patch.object(
                sectors, 'get_effective_trading_date', return_value=session
            ), patch.object(sectors, 'get_next_trading_date', return_value=next_day):
                now = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                self.assertEqual(sectors.closing_quote_session(now), (session, allowed))

    def test_old_cache_can_be_replaced_overnight(self):
        now = datetime(2026, 11, 17, 0, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
        frame = pd.DataFrame([
            {'label': 'industry_a', '板块': '行业甲', '涨跌幅': 2},
            {'label': 'industry_b', '板块': '行业乙', '涨跌幅': -3},
        ])
        with TemporaryDirectory() as folder, patch.object(sectors, 'get_market_now', return_value=now), patch.object(
            sectors, '_fetch', return_value=frame
        ) as fetch:
            path = Path(folder)
            cache = path / 'sectors_industry.json'
            cache.write_text(json.dumps({'trade_date': '2026-11-12'}))
            session, available = sectors.closing_quote_session(now)
            summary = market._sectors(path, session, available, False)
            self.assertEqual(summary['trade_date'], '2026-11-16')
            self.assertEqual(summary['leaders'][0]['name'], '行业甲')
            self.assertEqual(summary['laggards'][0]['name'], '行业乙')
            fetch.assert_called_once()
            fetch.reset_mock()
            self.assertIsNone(sectors.get_sector_group(path, 'industry', date(2026, 11, 18), after_close=False))
            fetch.assert_not_called()

    def test_missing_report_sectors_retry_overnight(self):
        now = datetime(2026, 11, 17, 0, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
        report = {'trade_date': '2026-11-16', 'generated_at': '2026-11-16T15:00:00+08:00', 'sectors': None}
        refreshed = {
            **report, 'sectors': {'leaders': [], 'laggards': []},
            'indices': [{'change_pct': 1}], 'breadth': None, 'limits': None,
        }
        with TemporaryDirectory() as folder, patch.object(market, 'get_market_now', return_value=now), patch.object(
            market, '_build_report', return_value=refreshed
        ) as build:
            path = Path(folder)
            (path / 'market_review.json').write_text(json.dumps(report))
            result = market.get_market_report(path)
            self.assertEqual(result['sectors'], refreshed['sectors'])
            self.assertEqual(result['reused_sections'], [])
            self.assertTrue(build.call_args.kwargs['after_close'])

    def test_real_calendar_dates_and_timezone(self):
        cases = [
            ('2026-11-16T23:59:59+08:00', '2026-11-16', True),
            ('2026-11-17T00:00:00+08:00', '2026-11-16', True),
            ('2026-11-16T16:00:00+00:00', '2026-11-16', True),
            ('2026-11-17T09:15:00+08:00', '2026-11-16', False),
            ('2026-11-17T14:59:59+08:00', '2026-11-16', False),
            ('2026-11-17T15:00:00+08:00', '2026-11-17', True),
            ('2026-11-15T18:00:00+08:00', '2026-11-13', True),
            ('2026-10-01T18:00:00+08:00', '2026-09-30', True),
        ]
        for stamp, expected_day, available in cases:
            with self.subTest(stamp=stamp):
                session, allowed = sectors.closing_quote_session(datetime.fromisoformat(stamp))
                self.assertEqual((session.isoformat(), allowed), (expected_day, available))

    def test_missing_calendar_does_not_date_spot_quotes(self):
        now = datetime.fromisoformat('2026-11-14T18:00:00+08:00')
        with patch.object(sectors, 'get_next_trading_date', return_value=None):
            self.assertFalse(sectors.closing_quote_session(now)[1])

    def test_fetch_crossing_auction_does_not_overwrite_close(self):
        before = datetime.fromisoformat('2026-11-17T09:14:59+08:00')
        after = datetime.fromisoformat('2026-11-17T09:15:01+08:00')
        frame = pd.DataFrame([{'label': 'industry_a', '板块': '行业甲', '涨跌幅': 9}])
        saved = {
            'trade_date': '2026-11-16', 'generated_at': '2026-11-16T16:00:00+08:00',
            'source': '新浪财经', 'rows': [{'id': 'industry_a', 'change_pct': 2}],
        }
        with TemporaryDirectory() as folder, patch.object(
            sectors, 'get_market_now', side_effect=[before, after, after]
        ), patch.object(sectors, '_fetch', return_value=frame):
            path = Path(folder)
            cache = path / 'sectors_industry.json'
            cache.write_text(json.dumps(saved))
            result = sectors.get_sector_group(path, 'industry', date(2026, 11, 16), after_close=True, refresh=True)
            self.assertEqual(result['rows'], saved['rows'])
            self.assertTrue(result['refresh_failed'])
            self.assertEqual(json.loads(cache.read_text()), saved)

    def test_source_failure_preserves_only_matching_session(self):
        now = datetime.fromisoformat('2026-11-17T00:18:00+08:00')
        saved = {
            'trade_date': '2026-11-16', 'generated_at': '2026-11-16T16:00:00+08:00',
            'source': '新浪财经', 'rows': [{'id': 'industry_a', 'change_pct': 2}],
        }
        with TemporaryDirectory() as folder, patch.object(sectors, 'get_market_now', return_value=now), patch.object(
            sectors, '_fetch', return_value=None
        ):
            path = Path(folder)
            (path / 'sectors_industry.json').write_text(json.dumps(saved))
            result = sectors.get_sector_group(path, 'industry', date(2026, 11, 16), after_close=True, refresh=True)
            self.assertEqual(result['rows'], saved['rows'])
            self.assertTrue(result['refresh_failed'])
            self.assertIsNone(sectors.get_sector_group(path, 'industry', date(2026, 11, 17), after_close=True))

    def test_constituent_fetch_crossing_auction_has_no_misdated_members(self):
        session = date(2026, 11, 16)
        group = {'source': '新浪财经', 'rows': [{'id': 'industry_a'}]}
        frame = pd.DataFrame([{'code': '000001', 'name': '示例', 'changepercent': 5, 'amount': 100}])
        with TemporaryDirectory() as folder, patch.object(
            sectors, 'closing_quote_session', side_effect=[(session, True), (session, False)]
        ), patch.object(sectors, 'get_sector_group', return_value=group), patch.object(
            sectors, '_fetch', return_value=frame
        ):
            detail = sectors.get_sector_detail(Path(folder), 'industry', 'industry_a')
            self.assertIsNone(detail['members'])

    def test_partial_refresh_keeps_same_day_sections_and_temperature(self):
        now = datetime.fromisoformat('2026-11-17T12:00:00+08:00')
        saved = {
            'trade_date': '2026-11-16', 'generated_at': '2026-11-16T16:00:00+08:00',
            'indices': [{'change_pct': 1}], 'breadth': {'up': 80, 'down': 20},
            'limits': {'up': 9, 'down': 1}, 'sectors': {'leaders': [], 'laggards': []},
        }
        partial = {**saved, 'breadth': None, 'limits': None, 'sectors': None}
        with TemporaryDirectory() as folder, patch.object(market, 'get_market_now', return_value=now), patch.object(
            market, '_build_report', return_value=partial
        ):
            path = Path(folder)
            (path / 'market_review.json').write_text(json.dumps(saved))
            result = market.get_market_report(path, refresh=True)
            for field in ('breadth', 'limits', 'sectors'):
                self.assertEqual(result[field], saved[field])
                self.assertIn(field, result['reused_sections'])
            self.assertEqual(result['temperature'], market.market_temperature([1], saved['breadth'], saved['limits']))

    def test_previous_day_sections_are_not_carried_into_new_report(self):
        now = datetime.fromisoformat('2026-11-17T16:00:00+08:00')
        saved = {
            'trade_date': '2026-11-16', 'generated_at': '2026-11-16T16:00:00+08:00',
            'breadth': {'up': 80, 'down': 20}, 'limits': {'up': 9, 'down': 1},
            'sectors': {'leaders': [], 'laggards': []},
        }
        partial = {
            'trade_date': '2026-11-17', 'indices': [{'change_pct': 1}],
            'breadth': None, 'limits': None, 'sectors': None,
        }
        with TemporaryDirectory() as folder, patch.object(market, 'get_market_now', return_value=now), patch.object(
            market, '_build_report', return_value=partial
        ):
            path = Path(folder)
            (path / 'market_review.json').write_text(json.dumps(saved))
            result = market.get_market_report(path, refresh=True)
            self.assertEqual(result['reused_sections'], [])
            self.assertIsNone(result['sectors'])


if __name__ == '__main__':
    unittest.main()
