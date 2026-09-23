#!/usr/bin/env python3
"""Offline tests for research_store.py.

Stdlib only, no network: the fetch layer is monkeypatched with fixed fixtures,
so this suite is deterministic and runs anywhere.  Run it with:

    python3 tests/test_research_store.py

It covers what the card's acceptance criteria assert:
  1. the 12-table schema and the NOT NULL freshness contract,
  2. band reconstruction parity against the recorded truth,
  3. digest line budget and the five sections,
  4. staleness detection,
  5. fit drift (fit_date / n_obs / inputs_rev) and before->after deltas,
  6. run-boundary semantics that make `--since-last-run` a real window,
  7. CLI surface: every subcommand named in the design exists and has --json.
"""

import importlib.util
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "research_store.py")

spec = importlib.util.spec_from_file_location("research_store", SCRIPT)
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)


# --------------------------------------------------------------------------
# Fixtures — a small synthetic ladder whose truth is known exactly
# --------------------------------------------------------------------------

BASE_CLOSE = "2026-09-22T05:59:00Z"


def _mk_event(day, truth, step=0.005, n_above=4, n_below=4):
    """Synthetic daily ladder around `truth`, with strikes every `step`."""
    month = "SEP"
    ev = "KXTEST-%s%02d" % (month, day) if False else "KXTEST-26%s%02d" % (
        month, day)
    close = "2026-09-%02dT05:59:00Z" % day
    markets = []
    mid = round(round(truth / step) * step, 6)
    for k in range(-n_below, n_above + 1):
        strike = round(mid + k * step, 6)
        result = "yes" if strike <= truth else "no"
        markets.append({
            "ticker": "%s-T%.3f" % (ev, strike),
            "event_ticker": ev,
            "floor_strike": strike,
            "strike_type": "greater",
            "result": result,
            "status": "finalized",
            "close_time": close,
            "settlement_ts": close,
            "expiration_value": "%.4f" % truth,
            "rules_primary": "If X on day %d is above $%.3f then Yes." % (day,
                                                                         strike),
            "volume_fp": "10.00",
        })
    return markets


def fake_settled(series_ticker, min_close_ts, max_close_ts):
    """Deterministic settled ladder: day 22 truth 6.5276, day 21 truth 6.5107."""
    if series_ticker == "KXTEST":
        return _mk_event(22, 6.5276) + _mk_event(21, 6.5107) + _mk_event(20,
                                                                       6.5050)
    return []


def fake_open(series_ticker):
    if series_ticker != "KXTEST":
        return []
    close = "2026-09-23T05:59:00Z"
    out = []
    for k in range(-4, 5):
        strike = round(6.53 + k * 0.005, 6)
        out.append({
            "ticker": "KXTEST-26SEP23-T%.3f" % strike,
            "event_ticker": "KXTEST-26SEP23",
            "floor_strike": strike, "strike_type": "greater", "result": "",
            "status": "active", "close_time": close, "volume_fp": "5.00",
            "rules_primary": "r%d" % k,
        })
    return out


def fake_yahoo(symbol, period1, period2):
    # A series whose daily pct change VARIES (sinusoidal), so the exog columns
    # are not collinear with each other or with the constant term.
    tss, closes = [], []
    start = 1785000000
    for i in range(60):
        tss.append(start + i * 86400)
        closes.append(4.0 * (1.0 + 0.03 * math.sin(i / 3.0) + 0.004 * i))
    return {
        "meta": {"symbol": symbol, "regularMarketTime": tss[-1],
                 "regularMarketPrice": closes[-1], "gmtoffset": -14400,
                 "exchangeTimezoneName": "America/New_York"},
        "timestamp": tss,
        "indicators": {"quote": [{"close": closes}]},
    }


def fake_candles(series_ticker, market_tickers, start_ts, end_ts,
                 period_interval=60):
    out = {}
    for t in market_tickers:
        out[t] = [{"end_period_ts": int(start_ts) + 60,
                   "yes_bid": {"close_dollars": "0.4000"},
                   "yes_ask": {"close_dollars": "0.4200"},
                   "price": {},
                   "volume_fp": "12.00", "open_interest_fp": "30.00"}]
    return out


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs_test_")
        self.db = os.path.join(self.tmp, "t.sqlite")
        rs.fetch_settled_markets = fake_settled
        rs.fetch_open_markets = fake_open
        rs.fetch_yahoo_chart = fake_yahoo
        rs.fetch_candlesticks = fake_candles

    def init(self):
        args = type("A", (), {"db": self.db, "json": True})()
        rs.cmd_init(args)
        return rs.connect(self.db)

    def settled_args(self, **kw):
        base = dict(family="KXTEST", kind="daily_ladder", unit="USD/gal",
                    settlement_tz="America/New_York", close_hhmm="05:59Z",
                    strike_step=0.005, since="2026-09-01", rebuild=False,
                    no_open=True)
        base.update(kw)
        return type("A", (), base)()


class TestSchema(StoreTestCase):
    def test_schema_has_expected_tables(self):
        conn = self.init()
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("series", "observations", "settlements", "markets", "quotes",
                  "models", "predictions", "cache_meta", "runs",
                  "schema_version"):
            self.assertIn(t, names)
        conn.close()

    def test_freshness_contract_is_not_null(self):
        """No fetch path may write a row without asof AND fetched_at."""
        conn = self.init()
        for table in ("observations", "settlements", "markets", "quotes"):
            cols = {r["name"]: r for r in conn.execute(
                "PRAGMA table_info(%s)" % table)}
            for col in ("asof", "fetched_at"):
                self.assertIn(col, cols, "%s.%s missing" % (table, col))
                self.assertEqual(cols[col]["notnull"], 1,
                                 "%s.%s must be NOT NULL" % (table, col))
        conn.close()

    def test_no_family_specific_columns(self):
        """The generic core must not leak market-family specifics."""
        conn = self.init()
        banned = ("diesel", "gas", "wti", "gasoline", "hormuz", "retail")
        for table in ("series", "observations", "settlements", "markets",
                      "quotes", "models", "predictions"):
            for r in conn.execute("PRAGMA table_info(%s)" % table):
                low = r["name"].lower()
                for word in banned:
                    self.assertNotIn(word, low,
                                     "%s.%s looks family-specific"
                                     % (table, r["name"]))
        conn.close()


class TestSettledIngest(StoreTestCase):
    def test_band_reconstruction_parity(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        rows = {r["event_ticker"]: r for r in conn.execute(
            "SELECT * FROM settlements")}
        ev22 = rows["KXTEST-26SEP22"]
        self.assertAlmostEqual(ev22["mid"], 6.5275, places=6)
        truth = conn.execute(
            "SELECT value FROM observations WHERE obs_date = '2026-09-22'"
            " AND source = 'kalshi_expiration_value'").fetchone()["value"]
        self.assertAlmostEqual(truth, 6.5276, places=6)
        self.assertLessEqual(abs(ev22["mid"] - truth), 0.002)
        ev21 = rows["KXTEST-26SEP21"]
        self.assertAlmostEqual(ev21["mid"], 6.5125, places=6)
        conn.close()

    def test_idempotent_reingest(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        before = rs.table_counts(conn)
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        after = rs.table_counts(conn)
        self.assertEqual(before, after)
        conn.close()

    def test_rebuild_flag_refetches(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        out = rs.ingest_settled(conn, self.settled_args(rebuild=True))
        conn.commit()
        self.assertEqual(out["events_skipped_finalized"], 0)
        self.assertGreater(out["events_ingested"], 0)
        conn.close()

    def test_obs_date_comes_from_the_event_ticker(self):
        """A 03:59Z close must not shift the print's date back a day."""
        got = rs._obs_date_for("KXAAAGASD-26SEP22", "2026-09-22T03:59:00Z",
                               "America/New_York")
        self.assertEqual(got, "2026-09-22")


class TestCloses(StoreTestCase):
    def test_closes_ingested_and_idempotent(self):
        conn = self.init()
        args = type("A", (), {"symbol": ["HO=F"], "since": "2026-04-01",
                              "unit": "USD/Bbl", "family": None, "json": True})()
        first = rs.ingest_closes(conn, args)
        conn.commit()
        before = rs.table_counts(conn)
        second = rs.ingest_closes(conn, args)
        conn.commit()
        after = rs.table_counts(conn)
        self.assertEqual(before, after)
        self.assertGreater(first["symbols"][0]["rows"], 0)
        self.assertEqual(first["symbols"][0]["rows"],
                         second["symbols"][0]["rows"])
        conn.close()


class TestQuotes(StoreTestCase):
    def test_quotes_written_with_fixed_window_and_idempotent(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        args = type("A", (), {"family": "KXTEST", "backfill": 2,
                              "offset_hours": 3.0, "since": None,
                              "json": True})()
        rs.ingest_quotes(conn, args)
        conn.commit()
        before = rs.table_counts(conn)
        out2 = rs.ingest_quotes(conn, args)
        conn.commit()
        after = rs.table_counts(conn)
        self.assertEqual(before, after, "a fixed --backfill window must be a"
                                        " no-op on re-run")
        self.assertEqual(out2["quotes_written"], before["quotes"])
        conn.close()


class TestDigest(StoreTestCase):
    def test_every_subcommand_named_in_the_design_exists(self):
        p = rs.build_parser()
        sub = {a.dest for a in p._actions if a.dest == "command"}
        self.assertTrue(sub)
        names = p._subparsers._group_actions[0].choices
        expected = {"init", "ingest-settled", "ingest-closes", "ingest-quotes",
                    "fit", "digest", "stale", "record-prediction",
                    "resolve-predictions", "series"}
        self.assertEqual(expected, set(names))

    def test_json_flag_on_every_subcommand(self):
        p = rs.build_parser()
        for name, sp in p._subparsers._group_actions[0].choices.items():
            self.assertIn("--json", sp._option_string_actions,
                          "%s lacks --json" % name)

    def test_digest_has_five_sections_and_respects_max_lines(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args(no_open=False))
        rs.ingest_closes(conn, type(
            "A", (), {"symbol": ["HO=F"], "since": None, "unit": "USD",
                      "family": None, "json": True})())
        conn.commit()
        payload = rs.build_digest(conn, max_lines=60, since_last_run=True)
        self.assertLessEqual(payload["line_count"], 60)
        titles = [s["title"] for s in payload["sections"]]
        self.assertEqual(len(titles), 5)
        self.assertTrue(titles[0].startswith("1)"))
        self.assertTrue(titles[4].startswith("5)"))
        self.assertIn("stale artifacts:", payload["digest"].splitlines()[1])

    def test_digest_truncates_to_a_small_cap(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args(no_open=False))
        conn.commit()
        payload = rs.build_digest(conn, max_lines=12, since_last_run=True)
        self.assertLessEqual(payload["line_count"], 12)


class TestRuns(StoreTestCase):
    def test_since_last_run_window_covers_the_previous_runs_work(self):
        conn = self.init()
        # run A: digest opens run #1, then ingest
        rs.cmd_digest(conn, type("A", (), {"family": None, "max_lines": 60,
                                           "since_last_run": True,
                                           "json": True})())
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        # run B: the digest must see run A's rows as new
        payload = rs.build_digest(conn, since_last_run=True, roll_run=True)
        self.assertGreater(payload["new_rows"], 0)
        conn.close()

    def test_no_new_work_reports_nothing_new(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        rs.cmd_digest(conn, type("A", (), {"family": None, "max_lines": 60,
                                           "since_last_run": True,
                                           "json": True})())
        payload = rs.build_digest(conn, since_last_run=True, roll_run=True)
        self.assertEqual(payload["new_rows"], 0)
        conn.close()

    def test_store_rev_delta_measures_the_previous_run(self):
        conn = self.init()
        rs.cmd_digest(conn, type("A", (), {"family": None, "max_lines": 60,
                                           "since_last_run": True,
                                           "json": True})())
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        payload = rs.build_digest(conn, since_last_run=True, roll_run=True)
        self.assertGreater(payload["store_rev"], 0)
        conn.close()


class TestStale(StoreTestCase):
    def test_open_ladder_flagged_when_old(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args(no_open=False))
        conn.commit()
        # backdate the fetch so the 15-minute rule is violated
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("UPDATE markets SET fetched_at = ? WHERE close_ts > ?",
                     (old, int(datetime.now(timezone.utc).timestamp())))
        conn.commit()
        items = rs.find_stale(conn)
        self.assertTrue(any(i["class"] == "open_ladder" for i in items))
        conn.close()

    def test_fresh_store_is_not_stale(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args(no_open=False))
        conn.commit()
        self.assertEqual(rs.find_stale(conn), [])
        conn.close()

    def test_non_final_settlement_flagged(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        conn.execute("UPDATE settlements SET status = 'amended'")
        conn.commit()
        items = rs.find_stale(conn)
        self.assertTrue(any(i["class"] == "settlement_revalidation"
                            for i in items))
        conn.close()


class TestFit(StoreTestCase):
    def _store_with_diesel(self):
        conn = self.init()
        args = self.settled_args()
        # A longer synthetic ladder so the regression has degrees of freedom.
        # The truth sequence must VARY in its daily change: a perfectly linear
        # series (constant d) is collinear with the constant term and the OLS
        # fit would be genuinely unidentified.
        events = []
        wave = [0.0, 0.004, -0.003, 0.006, 0.001, -0.005, 0.007, -0.002]
        truth = 6.40
        for day in range(2, 23):
            truth = round(truth + wave[day % len(wave)], 4)
            events += _mk_event(day, truth)
        rs.fetch_settled_markets = lambda *a, **k: events
        rs.ingest_settled(conn, args)
        rs.ingest_closes(conn, type(
            "A", (), {"symbol": ["HO=F"], "since": None, "unit": "USD/Bbl",
                      "family": None, "json": True})())
        conn.commit()
        return conn

    def test_realized_vol_persisted_with_metadata(self):
        conn = self._store_with_diesel()
        out = rs.fit(conn, type("A", (), {
            "family": "KXTEST", "model": "vol", "estimator": "realized_vol",
            "exog": None, "lags": None, "as_of": None, "threshold": 0.0,
            "bucket_step": 0.01, "bands": 8})())
        self.assertGreater(out["n_obs"], 0)
        row = conn.execute("SELECT * FROM models WHERE model_name='vol'"
                           ).fetchone()
        self.assertIsNotNone(row["fit_date"])
        self.assertIsNotNone(row["n_obs"])
        self.assertIsNotNone(row["inputs_rev"])
        conn.close()

    def test_refit_reports_a_before_after_delta(self):
        conn = self._store_with_diesel()
        common = {"family": "KXTEST", "model": "m", "estimator": "diff_ols",
                  "exog": "HO=F", "lags": "0:2", "threshold": 0.0,
                  "bucket_step": 0.01, "bands": 8}
        first = rs.fit(conn, type("A", (), dict(common, as_of="2026-09-15"))())
        self.assertTrue(first["first_fit"])
        second = rs.fit(conn, type("A", (), dict(common, as_of=None))())
        self.assertFalse(second["first_fit"])
        self.assertIn("resid_sd", second["delta"])
        self.assertIn("n_obs", second["delta"])
        self.assertNotEqual(second["delta"]["n_obs"][0],
                            second["delta"]["n_obs"][1])
        conn.close()

    def test_diff_ols_ar_term_is_not_the_dependent(self):
        """Regression guard: the AR column must be the PREVIOUS change, not the
        current one (which made R2 == 1 and resid_sd == 0)."""
        conn = self._store_with_diesel()
        out = rs.fit(conn, type("A", (), {
            "family": "KXTEST", "model": "m", "estimator": "diff_ols",
            "exog": "HO=F", "lags": "0:2", "as_of": None, "threshold": 0.0,
            "bucket_step": 0.01, "bands": 8})())
        self.assertGreater(out["resid_sd"], 0.0)
        self.assertLess(out["r2"], 1.0)
        conn.close()


class TestPredictions(StoreTestCase):
    def test_round_trip_resolution(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = os.path.join(self.tmp, "p.json")
        with open(pfile, "w") as fh:
            json.dump({"family": "KXTEST", "target_date": "2026-09-22",
                       "p_yes": 0.4, "point_forecast": 6.5300,
                       "forecast_sd": 0.03}, fh)
        rec = rs.record_prediction(conn, type("A", (), {"file": pfile})())
        self.assertEqual(rec["count"], 1)
        res = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        self.assertEqual(res["resolved_count"], 1)
        self.assertEqual(res["resolved"][0]["units"], "price")
        self.assertAlmostEqual(res["resolved"][0]["error"], 6.5300 - 6.5275,
                               places=6)
        conn.close()

    def test_probability_prediction_scored_in_probability_units(self):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = os.path.join(self.tmp, "p2.json")
        with open(pfile, "w") as fh:
            json.dump({"market_ticker": "KXTEST-26SEP22-T6.530", "p_yes": 0.2,
                       "target_date": "2026-09-22"}, fh)
        rs.record_prediction(conn, type("A", (), {"file": pfile})())
        res = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        self.assertEqual(res["resolved"][0]["units"], "probability")
        # The prediction carried no family, so it resolves off the settled
        # market.  Strike T6.530 against truth 6.5276 settles NO (6.530 is not
        # above the print), so the probability error is p_yes - 0.0.
        self.assertEqual(res["resolved"][0]["outcome"], "no")
        self.assertAlmostEqual(res["resolved"][0]["error"], 0.2, places=6)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
