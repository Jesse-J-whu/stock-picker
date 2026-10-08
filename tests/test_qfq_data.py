import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch
import pandas as pd
from qfq_data import validate_history, aggregate, AkshareMarketData, reference_day
from market_data import MarketDataError
import strategy
from verify_output import verify


def frame():
    return pd.DataFrame({"date": pd.to_datetime(["2026-09-07", "2026-09-08", "2026-09-09"]),
        "open": [10., 11., 12.], "close": [11., 12., 13.],
        "high": [12., 13., 14.], "low": [9., 10., 11.], "vol": [100., 200., 300.]})


class DataTests(unittest.TestCase):
    def test_calendar_weekend(self):
        cal = pd.DataFrame({"trade_date": ["2026-09-04", "2026-09-07"]})
        self.assertEqual(reference_day(cal, date(2026, 9, 6)), "20260904")

    def test_calendar_stale_fails_closed(self):
        cal = pd.DataFrame({"trade_date": ["2026-09-04"]})
        with self.assertRaises(MarketDataError):
            reference_day(cal, date(2026, 9, 9))

    def test_valid(self):
        self.assertEqual(len(validate_history(frame(), "2026-09-09", 13)), 3)

    def test_short_is_not_network_failure(self):
        self.assertEqual(len(validate_history(frame().tail(1), "2026-09-09", 13)), 1)

    def test_reject_stale(self):
        with self.assertRaises(MarketDataError):
            validate_history(frame().head(2), "2026-09-09", 13)

    def test_reject_unadjusted_latest_mismatch(self):
        with self.assertRaises(MarketDataError):
            validate_history(frame(), "2026-09-09", 14)

    def test_reject_duplicates(self):
        with self.assertRaises(MarketDataError):
            validate_history(pd.concat([frame(), frame().tail(1)]), "2026-09-09", 13)

    def test_reject_nan(self):
        f = frame()
        f.loc[0, "vol"] = float("nan")
        with self.assertRaises(MarketDataError):
            validate_history(f, "2026-09-09", 13)

    def test_reject_bad_ohlc(self):
        f = frame()
        f.loc[0, "high"] = 1
        with self.assertRaises(MarketDataError):
            validate_history(f, "2026-09-09", 13)

    def test_week_aggregation(self):
        w = aggregate(frame(), "week", 130).iloc[0]
        self.assertEqual((w.open, w.close, w.high, w.low, w.vol), (10, 13, 14, 9, 600))

    def test_month_aggregation(self):
        self.assertEqual(aggregate(frame(), "month", 60).iloc[0].vol, 600)

    def test_volume_not_price_adjusted(self):
        f = frame()
        f.loc[0, ["open", "close", "high", "low"]] *= 0.5
        self.assertEqual(aggregate(f, "week", 1).iloc[0].vol, 600)

    @patch("qfq_data.time.sleep")
    @patch("qfq_data.requests.post")
    def test_rate_limit_waits_for_next_minute(self, post, sleep):
        limited = unittest.mock.Mock()
        limited.raise_for_status.return_value = None
        limited.json.return_value = {"code": -1, "msg": "接口频率超限(1次/分钟)"}
        success = unittest.mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {"fields": ["ts_code"],
                                                           "items": [["000001.SZ"]]}}
        post.side_effect = [limited, success]
        provider = object.__new__(AkshareMarketData)
        provider.token = "test-only"
        result = provider.request("daily_basic", {"trade_date": "20260909"}, "ts_code")
        self.assertEqual(result.iloc[0].ts_code, "000001.SZ")
        sleep.assert_called_once_with(65)

    def test_daily_basic_is_cached_per_trade_date(self):
        day = "20260909"
        frame = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(4000)],
                              "trade_date": [day] * 4000,
                              "circ_mv": [200_000.0] * 4000})
        provider = object.__new__(AkshareMarketData)
        with tempfile.TemporaryDirectory() as temp:
            provider.cache = Path(temp)
            with patch.object(provider, "request", return_value=frame) as request:
                first = provider.daily_basic(day)
                second = provider.daily_basic(day)
            self.assertEqual(len(first), 4000)
            self.assertEqual(second.iloc[0].ts_code, "000000.SZ")
            request.assert_called_once()

    def test_publish_metadata(self):
        provider = object.__new__(AkshareMarketData)
        provider.trade_date = "2026-09-09"
        provider.frames = {"000001": frame()}
        provider.raw = pd.DataFrame({"close": [13]})
        provider.audit = {"coverage": 1.0, "universe": 1, "valid_histories": 1}
        provider.stats = {"evaluated": 1, "insufficient_history": 0, "errors": 0}
        with tempfile.TemporaryDirectory() as temp, patch.object(strategy, "MARKET_DATA", provider):
            html = Path(temp) / "index.html"
            data = Path(temp) / "data.json"
            strategy.generate_html([], str(html))
            strategy.save_data_json([], str(data))
            content = html.read_text(encoding="utf-8")
            result = json.loads(data.read_text(encoding="utf-8"))
            self.assertIn("前复权", content)
            self.assertIn("2026-09-09", content)
            self.assertEqual(result["adjustment"], "qfq")
            self.assertEqual(result["timezone"], "Asia/Shanghai")
            self.assertEqual(result["trade_date"], "2026-09-09")
            self.assertEqual(result["data_quality"]["strategy_counts"]["errors"], 0)
            self.assertIn("Verified", verify(result, content))
            result["count"] = 1
            with self.assertRaisesRegex(ValueError, "Result count mismatch"):
                verify(result, content)
            result["count"] = 0
            result["data_quality"]["coverage"] = 0.99
            with self.assertRaisesRegex(ValueError, "Incomplete data coverage"):
                verify(result, content)
