"""Offline tests: Yahoo responses are faked, so these run without internet.

Run from the project folder:  python -m unittest
"""
import json
import sys
import time
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


class RefreshTests(unittest.TestCase):
    def setUp(self):
        app.ARGS = types.SimpleNamespace(demo=False)
        app._prices.update(closes={}, dates=[], fetched=0.0, asof=None, error=None, busy=False)
        self.orig_fetch_live, self.orig_load = app.fetch_live, app.load_themes
        app.load_themes = lambda: {"T": ["A", "B"]}

    def tearDown(self):
        app.fetch_live, app.load_themes = self.orig_fetch_live, self.orig_load

    def test_symbols_missing_from_a_refresh_keep_last_prices(self):
        app.fetch_live = lambda tickers: ({"A": [1.0, 2.0], "B": [3.0, 4.0]}, ["d1"])
        app.refresh_prices(force=True, wait=True)
        app.fetch_live = lambda tickers: ({"A": [2.0, 5.0]}, ["d2"])  # Yahoo skipped B
        app.refresh_prices(force=True, wait=True)
        self.assertEqual(app._prices["closes"], {"A": [2.0, 5.0], "B": [3.0, 4.0]})
        self.assertIsNone(app._prices["error"])

    def test_stale_refresh_runs_in_background(self):
        import threading
        release = threading.Event()
        app.fetch_live = lambda tickers: ({"A": [1.0, 2.0]}, ["d1"])
        app.refresh_prices(force=True, wait=True)

        def slow(tickers):
            release.wait(5)
            return {"A": [2.0, 3.0]}, ["d2"]
        app.fetch_live = slow
        app._prices["fetched"] = 0  # make the data stale
        start = time.time()
        app.refresh_prices()  # must not wait for the slow download
        self.assertLess(time.time() - start, 1)
        self.assertEqual(app._prices["closes"]["A"], [1.0, 2.0])  # still serving old data
        release.set()
        app.refresh_prices(force=False, wait=True)
        self.assertEqual(app._prices["closes"]["A"], [2.0, 3.0])


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

    def test_demo_info_has_52_week_range(self):
        app.ARGS = types.SimpleNamespace(demo=True)
        app._prices["fetched"] = 0
        app.refresh_prices(force=True, wait=True)
        app._info_cache.clear()
        info = app.get_info("NVDA")
        self.assertLessEqual(info["fiftyTwoWeekLow"], info["fiftyTwoWeekHigh"])

    def test_themes_file_is_valid(self):
        themes = app.load_themes()
        self.assertTrue(themes)
        for name, tickers in themes.items():
            self.assertTrue(tickers, name)
            for t in tickers:
                self.assertRegex(t, app.TICKER_RE)


class PasswordTests(unittest.TestCase):
    """The optional password (APP_PASSWORD) used when the app is hosted online."""

    def setUp(self):
        import os
        import threading
        from http.server import ThreadingHTTPServer
        app.ARGS = types.SimpleNamespace(demo=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.env = os.environ
        self.old = os.environ.pop("APP_PASSWORD", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.pop("APP_PASSWORD", None)
        if self.old is not None:
            self.env["APP_PASSWORD"] = self.old

    def status(self, path, password=None):
        import base64
        import urllib.request
        req = urllib.request.Request(self.base + path)
        if password is not None:
            token = base64.b64encode(f"me:{password}".encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_no_password_by_default(self):
        self.assertEqual(self.status("/"), 200)

    def test_password_required_when_set(self):
        self.env["APP_PASSWORD"] = "s3cret"
        self.assertEqual(self.status("/"), 401)
        self.assertEqual(self.status("/", "wrong"), 401)
        self.assertEqual(self.status("/", "s3cret"), 200)
        self.assertEqual(self.status("/api/map?period=1D", "s3cret"), 200)

    def test_health_check_skips_password(self):
        self.env["APP_PASSWORD"] = "s3cret"
        self.assertEqual(self.status("/healthz"), 200)


class UpdateNoticeTests(unittest.TestCase):
    def setUp(self):
        app.ARGS = types.SimpleNamespace(demo=False, update_check=True)
        self._orig = (app.current_version, app._latest_release)
        app._latest_release = lambda: ("v1.2.0", "https://example.invalid/r")

    def tearDown(self):
        app.current_version, app._latest_release = self._orig

    def test_newer_release_is_flagged(self):
        app.current_version = lambda: "v1.1.9"
        self.assertTrue(app.version_info()["update"])

    def test_same_or_older_release_is_not(self):
        app.current_version = lambda: "v1.2.0"
        self.assertFalse(app.version_info()["update"])
        app.current_version = lambda: "v1.10.0"
        self.assertFalse(app.version_info()["update"])

    def test_git_clone_demo_and_opt_out_skip_the_check(self):
        app.current_version = lambda: None
        self.assertFalse(app.version_info()["update"])
        app.current_version = lambda: "v1.0.0"
        app.ARGS = types.SimpleNamespace(demo=True, update_check=True)
        self.assertFalse(app.version_info()["update"])
        app.ARGS = types.SimpleNamespace(demo=False, update_check=False)
        self.assertFalse(app.version_info()["update"])


if __name__ == "__main__":
    unittest.main()
