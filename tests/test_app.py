"""Offline tests: Yahoo responses are faked, so these run without internet.

Run from the project folder:  python -m unittest
"""
import json
import sys
import types
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

FRI, MON = 1763758800, 1764018000  # Fri 2025-11-21 and Mon 2025-11-24, 4 pm New York time


def chart(closes, adj, stamps, live=111.0, live_ts=MON):
    return json.dumps({"chart": {"result": [{
        "meta": {"regularMarketPrice": live, "regularMarketTime": live_ts, "gmtoffset": -18000,
                 "longName": "Test Corp", "fiftyTwoWeekHigh": 120, "fiftyTwoWeekLow": 80},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": adj}]},
    }]}})


class FetchChartTests(unittest.TestCase):
    def setUp(self):
        app.ARGS = types.SimpleNamespace(demo=False)
        self.responses = {}
        app.http_get = self.fake_get

    def fake_get(self, url, timeout=15):
        if "fc.yahoo.com" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "getcrumb" in url:
            return "crumb"
        ticker = url.split("/")[-1].split("?")[0]
        if ticker not in self.responses:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.responses[ticker]

    def test_missing_adjusted_close_falls_back_to_close(self):
        self.responses["A"] = chart([100.0, 110.0], [99.0, None], [FRI, MON])
        closes, days = app.fetch_live(["A"])
        self.assertEqual(closes["A"], [99.0, 111.0])
        self.assertEqual(days, ["2025-11-24"])

    def test_live_price_appended_when_todays_bar_missing(self):
        self.responses["A"] = chart([100.0], [99.0], [FRI])
        closes, _ = app.fetch_live(["A"])
        self.assertEqual(closes["A"], [99.0, 111.0])

    def test_unknown_ticker_is_skipped(self):
        self.responses["A"] = chart([100.0, 110.0], [99.0, 110.0], [FRI, MON])
        closes, _ = app.fetch_live(["A", "NOPE"])
        self.assertEqual(list(closes), ["A"])

    def test_summary_flattens_raw_values(self):
        self.responses["A"] = json.dumps({"quoteSummary": {"result": [{
            "financialData": {"targetMeanPrice": {"raw": 130.5, "fmt": "130.50"},
                              "recommendationKey": "buy"},
            "summaryDetail": {"forwardPE": {}},
        }]}})
        info = app.fetch_summary("A")
        self.assertEqual(info["targetMeanPrice"], 130.5)
        self.assertEqual(info["recommendationKey"], "buy")
        self.assertNotIn("forwardPE", info)


class CalculationTests(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(app.pct_change([100, 105, 110], 1), 4.7619, places=3)
        self.assertAlmostEqual(app.pct_change([100, 105, 110], 2), 10.0)
        self.assertIsNone(app.pct_change([100, 110], 5))  # not enough history

    def test_build_map_with_demo_data(self):
        app.ARGS = types.SimpleNamespace(demo=True)
        app._prices["fetched"] = 0
        data = app.build_map("1M")
        self.assertTrue(data["themes"])
        for theme in data["themes"]:
            self.assertLessEqual(theme["adv"], theme["count"])

    def test_themes_file_is_valid(self):
        themes = app.load_themes()
        self.assertTrue(themes)
        for name, tickers in themes.items():
            self.assertTrue(tickers, name)
            for t in tickers:
                self.assertRegex(t, app.TICKER_RE)


if __name__ == "__main__":
    unittest.main()
