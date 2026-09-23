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
    # An "open" ladder must close in the future: find_stale() selects open
    # ladders with `close_ts > now`, so an absolute close_time silently stops
    # being an open ladder the moment it passes.  Derive it from now.
    close_dt = datetime.now(timezone.utc) + timedelta(days=1)
    close = close_dt.strftime("%Y-%m-%dT05:59:00Z")
    day = close_dt.strftime("%d")
    month = close_dt.strftime("%b").upper()
    out = []
    for k in range(-4, 5):
        strike = round(6.53 + k * 0.005, 6)
        out.append({
            "ticker": "KXTEST-26%s%s-T%.3f" % (month, day, strike),
            "event_ticker": "KXTEST-26%s%s" % (month, day),
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
                    "resolve-predictions", "series", "record-position", "pnl"}
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

    def test_regression_does_not_resolve_without_a_target_date_print(self):
        """A target date with no print must NOT resolve against an older print.

        Only 2026-09-20/21/22 have prints.  A prediction targeting 09-23 used
        to walk back a day and score against the 09-22 print; it must instead
        resolve nothing and stay unresolved in the DB.
        """
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = os.path.join(self.tmp, "p_absent.json")
        with open(pfile, "w") as fh:
            json.dump({"family": "KXTEST", "target_date": "2026-09-23",
                       "p_yes": 0.4, "point_forecast": 6.5480,
                       "forecast_sd": 0.03}, fh)
        rs.record_prediction(conn, type("A", (), {"file": pfile})())
        res = rs.resolve_predictions(conn,
                                     type("A", (), {"as_of": "2026-10-01"})())
        self.assertEqual(res["resolved_count"], 0)
        self.assertEqual(res["resolved"], [])
        self.assertEqual(res["still_unresolvable"], 1)
        row = conn.execute("SELECT resolved_at, outcome, error FROM predictions"
                           ).fetchone()
        self.assertIsNone(row["resolved_at"], "no print on the target date =>"
                                              " the prediction must stay pending")
        self.assertIsNone(row["outcome"])
        self.assertIsNone(row["error"])
        conn.close()

    def test_boundary_scoring_resolves_against_the_target_date_print(self):
        """Resolving as-of a much later date still uses the target-date print."""
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = os.path.join(self.tmp, "p_boundary.json")
        with open(pfile, "w") as fh:
            json.dump({"family": "KXTEST", "target_date": "2026-09-21",
                       "p_yes": 0.4, "point_forecast": 6.5300,
                       "forecast_sd": 0.03}, fh)
        rs.record_prediction(conn, type("A", (), {"file": pfile})())
        # 09-22 (6.5275) and 09-20 (6.5075) also have prints; the 09-21 print
        # (6.5125) is the only correct basis.
        res = rs.resolve_predictions(conn,
                                     type("A", (), {"as_of": "2026-11-30"})())
        self.assertEqual(res["resolved_count"], 1)
        got = res["resolved"][0]
        self.assertEqual(got["target_date"], "2026-09-21")
        self.assertEqual(got["units"], "price")
        self.assertAlmostEqual(got["error"], 6.5300 - 6.5125, places=6)
        # 09-21 (6.5125) is above 09-20 (6.5075), so the print's direction is up.
        self.assertEqual(got["outcome"], "up")
        conn.close()


# --------------------------------------------------------------------------
# Positions & realized P&L — schema v2 (card t_4302ce7e)
# --------------------------------------------------------------------------

DIESEL_TICKER = "KXDIESELD-26SEP24-T6.515"


def _write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)
    return path


class TestPositionFees(unittest.TestCase):
    """The Kalshi taker fee: round UP to the next cent of
    M * 0.07 * C * P * (1-P)  (fee schedule fetched 2026-09-23)."""

    def test_taker_fee_rounds_up_to_the_next_cent(self):
        # 0.07 * 24.9 * 0.19 * 0.81 = 0.2682477 -> 0.27 (the real trade's fee)
        self.assertAlmostEqual(rs.taker_fee(24.9, 0.19), 0.27, places=6)

    def test_taker_fee_exact_cents_do_not_round_up(self):
        # 0.07 * 20 * 0.5 * 0.5 = 0.35 exactly: no spurious extra cent.
        self.assertAlmostEqual(rs.taker_fee(20, 0.5), 0.35, places=6)

    def test_taker_fee_respects_the_multiplier(self):
        # 2 * 0.2682477 = 0.5364954 -> 0.54
        self.assertAlmostEqual(rs.taker_fee(24.9, 0.19, multiplier=2), 0.54,
                               places=6)


class TestPositionSchema(StoreTestCase):
    def test_v1_store_migrates_to_v2_preserving_rows(self):
        """A v1 store (no positions table, no predictions.position_id) must
        migrate in place with every prior row intact."""
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        # one v1 prediction row, then regress to the exact v1 shape
        pfile = _write_json(os.path.join(self.tmp, "p.json"),
                            {"market_ticker": "KXTEST-26SEP22-T6.525",
                             "p_yes": 0.4, "target_date": "2026-09-22"})
        rs.record_prediction(conn, type("A", (), {"file": pfile})())
        conn.commit()
        pred = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
        obs = conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
        self.assertGreater(pred, 0)
        # Regress the store to its exact v1 shape (v1 objects + a version=1
        # row, as a real pre-migration store has), then re-run init.
        conn.executescript("DROP TABLE positions;")
        conn.execute("ALTER TABLE predictions DROP COLUMN position_id")
        conn.execute("UPDATE schema_version SET version = 1, applied_at ="
                     " '2026-09-23T00:00:00.000000Z'")
        conn.commit()
        rs.init_db(conn)
        after = rs.table_counts(conn)
        self.assertEqual(after["observations"], obs)
        self.assertEqual(after["predictions"], pred)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(predictions)")}
        self.assertIn("position_id", cols)
        vers = [r["version"] for r in conn.execute(
            "SELECT version FROM schema_version ORDER BY version")]
        self.assertEqual(vers, [1, 2], "the v1 version row must survive")
        row = conn.execute(
            "SELECT market_ticker, p_yes FROM predictions LIMIT 1").fetchone()
        self.assertIsNotNone(row["market_ticker"])
        conn.close()

    def test_positions_table_enforces_checks(self):
        conn = self.init()
        base = ("INSERT INTO positions(market_ticker, side, contracts,"
                " fill_price, fee, opened_at, created_at)"
                " VALUES ('KXX-1', ?, 10, 0.2, 0.1, '2026-09-23T00:00:00Z',"
                " '2026-09-23T00:00:00Z')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(base, ("maybe",))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(base.replace("10, 0.2", "0, 0.2"),
                         ("yes",))                            # contracts <= 0
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(base.replace("10, 0.2, 0.1", "10, 1.0, 0.1"),
                         ("yes",))                            # fill_price >= 1
        conn.close()


class TestRecordPosition(StoreTestCase):
    def _record(self, conn, obj, **kw):
        args = type("A", (), dict(file=_write_json(
            os.path.join(self.tmp, "pos.json"), obj), json=True, **kw))()
        return rs.record_position(conn, args)

    def test_record_position_computes_the_fee(self):
        """The fee MUST be computed from the formula when omitted, never
        accepted on faith."""
        conn = self.init()
        out = self._record(conn, {"market_ticker": DIESEL_TICKER,
                                  "side": "yes", "contracts": 24.9,
                                  "fill_price": 0.19})
        self.assertEqual(out["count"], 1)
        row = conn.execute("SELECT * FROM positions").fetchone()
        self.assertEqual(row["market_ticker"], DIESEL_TICKER)
        self.assertEqual(row["side"], "yes")
        self.assertAlmostEqual(row["fee"], 0.27, places=6)
        self.assertIsNotNone(row["opened_at"])
        self.assertIsNotNone(row["created_at"])
        self.assertIsNone(row["settled_at"])
        self.assertIsNone(row["realized_pnl"])
        conn.close()

    def test_record_position_honors_explicit_fee_and_opened_at(self):
        conn = self.init()
        self._record(conn, {"market_ticker": DIESEL_TICKER, "side": "no",
                            "contracts": 5, "fill_price": 0.81, "fee": 0.10,
                            "opened_at": "2026-09-23T18:06:00Z"})
        row = conn.execute("SELECT * FROM positions").fetchone()
        self.assertAlmostEqual(row["fee"], 0.10, places=6)
        self.assertEqual(row["opened_at"], "2026-09-23T18:06:00Z")
        conn.close()

    def test_record_position_is_idempotent(self):
        conn = self.init()
        payload = {"market_ticker": DIESEL_TICKER, "side": "yes",
                   "contracts": 24.9, "fill_price": 0.19,
                   "opened_at": "2026-09-23T18:06:00Z"}
        first = self._record(conn, payload)
        self.assertEqual(first["count"], 1)
        second = self._record(conn, payload)
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM positions").fetchone()["c"], 1)
        conn.close()

    def test_record_position_links_and_backfills_the_prediction(self):
        conn = self.init()
        pfile = _write_json(os.path.join(self.tmp, "p.json"),
                            {"market_ticker": DIESEL_TICKER, "p_yes": 0.12,
                             "target_date": "2026-09-24"})
        pred = rs.record_prediction(conn, type("A", (), {"file": pfile})())
        pid = pred["written"][0]["id"]
        self._record(conn, {"market_ticker": DIESEL_TICKER, "side": "yes",
                            "contracts": 24.9, "fill_price": 0.19,
                            "prediction_id": pid})
        row = conn.execute("SELECT prediction_id FROM positions").fetchone()
        self.assertEqual(row["prediction_id"], pid)
        back = conn.execute(
            "SELECT position_id FROM predictions WHERE id = ?", (pid,)).fetchone()
        self.assertIsNotNone(back["position_id"])
        conn.close()

    def test_record_position_rejects_an_unknown_prediction(self):
        conn = self.init()
        with self.assertRaises(SystemExit):
            self._record(conn, {"market_ticker": DIESEL_TICKER, "side": "yes",
                                "contracts": 1, "fill_price": 0.5,
                                "prediction_id": 999})
        conn.close()


class TestPositionResolution(StoreTestCase):
    """resolve-predictions settles linked/matching positions.  fill_price is
    ALWAYS the price paid for the side bought (NO price for side='no')."""

    def _setup(self, outcome_ticker):
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = _write_json(os.path.join(self.tmp, "p.json"),
                            {"market_ticker": outcome_ticker, "p_yes": 0.2,
                             "target_date": "2026-09-22"})
        pred = rs.record_prediction(conn, type("A", (), {"file": pfile})())
        return conn, pred["written"][0]["id"]

    def _resolve_and_settle(self, conn, pid, side, fill, contracts=24.9):
        conn.execute("SELECT 1")  # keep linters honest about the conn use
        self._pos_file = _write_json(os.path.join(self.tmp, "pos.json"),
                                     {"market_ticker": "KXTEST-26SEP22-T6.530",
                                      "side": side, "contracts": contracts,
                                      "fill_price": fill, "prediction_id": pid})
        args = type("A", (), {"file": self._pos_file, "json": True})()
        rs.record_position(conn, args)
        res = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        self.assertEqual(res["resolved_count"], 1)
        pos = conn.execute("SELECT * FROM positions").fetchone()
        pred = conn.execute(
            "SELECT resolved_at, outcome FROM predictions WHERE id = ?",
            (pid,)).fetchone()
        return pos, res, pred

    def test_yes_win_settles_positive(self):
        # T6.525 <= print 6.5276 -> settles YES
        conn, pid = self._setup("KXTEST-26SEP22-T6.525")
        pos, res, pred = self._resolve_and_settle(conn, pid, "yes", 0.19)
        self.assertEqual(pred["outcome"], "yes")
        self.assertEqual(pos["side"], "yes")
        self.assertEqual(pos["settled_at"], pred["resolved_at"])
        self.assertAlmostEqual(pos["realized_pnl"], 24.9 * 0.81 - 0.27,
                               places=6)
        self.assertAlmostEqual(res["positions_settled"][0]["realized_pnl"],
                               19.90, places=2)
        conn.close()

    def test_yes_loss_settles_negative(self):
        # T6.530 > print 6.5276 -> settles NO
        conn, pid = self._setup("KXTEST-26SEP22-T6.530")
        pos, res, pred = self._resolve_and_settle(conn, pid, "yes", 0.19)
        self.assertEqual(pred["outcome"], "no")
        self.assertAlmostEqual(pos["realized_pnl"], -(24.9 * 0.19) - 0.27,
                               places=6)
        self.assertAlmostEqual(res["positions_settled"][0]["realized_pnl"],
                               -5.00, places=2)
        conn.close()

    def test_no_win_settles_positive_at_the_no_price(self):
        conn, pid = self._setup("KXTEST-26SEP22-T6.530")
        pos, res, pred = self._resolve_and_settle(conn, pid, "no", 0.81)
        self.assertEqual(pred["outcome"], "no")
        fee = rs.taker_fee(24.9, 0.81)
        self.assertAlmostEqual(pos["realized_pnl"], 24.9 * 0.19 - fee,
                               places=6)
        conn.close()

    def test_no_loss_settles_negative_at_the_no_price(self):
        conn, pid = self._setup("KXTEST-26SEP22-T6.525")
        pos, res, pred = self._resolve_and_settle(conn, pid, "no", 0.81)
        self.assertEqual(pred["outcome"], "yes")
        fee = rs.taker_fee(24.9, 0.81)
        self.assertAlmostEqual(pos["realized_pnl"], -(24.9 * 0.81) - fee,
                               places=6)
        conn.close()

    def test_reresolve_is_idempotent(self):
        conn, pid = self._setup("KXTEST-26SEP22-T6.525")
        pos1, res1, _ = self._resolve_and_settle(conn, pid, "yes", 0.19)
        res2 = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        self.assertEqual(res2["resolved_count"], 0)
        self.assertEqual(res2["positions_settled"], [])
        pos2 = conn.execute("SELECT * FROM positions").fetchone()
        self.assertEqual(pos2["settled_at"], pos1["settled_at"])
        self.assertEqual(pos2["realized_pnl"], pos1["realized_pnl"])
        conn.close()

    def test_side_match_settles_an_unlinked_position(self):
        """A position matching the prediction's market_ticker+side settles
        even with no explicit link (direction 'up' -> side 'yes')."""
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        conn.commit()
        pfile = _write_json(os.path.join(self.tmp, "p.json"),
                            {"market_ticker": "KXTEST-26SEP22-T6.530",
                             "p_yes": 0.2, "target_date": "2026-09-22",
                             "direction": "up"})
        pred = rs.record_prediction(conn, type("A", (), {"file": pfile})())
        pid = pred["written"][0]["id"]
        posfile = _write_json(os.path.join(self.tmp, "pos.json"),
                              {"market_ticker": "KXTEST-26SEP22-T6.530",
                               "side": "yes", "contracts": 10,
                               "fill_price": 0.2})
        rs.record_position(conn, type("A", (), {"file": posfile, "json": True})())
        res = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        self.assertEqual(len(res["positions_settled"]), 1)
        row = conn.execute("SELECT realized_pnl, settled_at FROM positions"
                           ).fetchone()
        self.assertAlmostEqual(row["realized_pnl"], -(10 * 0.2) - 0.12,
                               places=6)
        self.assertIsNotNone(row["settled_at"])
        # The prediction row itself is not force-linked by a side match.
        back = conn.execute("SELECT position_id FROM predictions WHERE id = ?",
                            (pid,)).fetchone()
        self.assertIsNone(back["position_id"])
        conn.close()

    def test_price_unit_outcomes_never_settle_positions(self):
        """'up'/'down' outcomes are print directions, not settlements."""
        conn, pid = self._setup("KXTEST-26SEP22-T6.530")
        # give the prediction a series_id + point forecast so it resolves in
        # price units (the ticker alone never carries a series_id)
        sid = conn.execute("SELECT id FROM series LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE predictions SET point_forecast = 6.53,"
                     " forecast_sd = 0.03, series_id = ? WHERE id = ?",
                     (sid, pid))
        conn.commit()
        posfile = _write_json(os.path.join(self.tmp, "pos.json"),
                              {"market_ticker": "KXTEST-26SEP22-T6.530",
                               "side": "yes", "contracts": 10,
                               "fill_price": 0.2, "prediction_id": pid})
        rs.record_position(conn, type("A", (), {"file": posfile, "json": True})())
        res = rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        row = conn.execute("SELECT outcome FROM predictions").fetchone()
        self.assertIn(row["outcome"], ("up", "down", "flat"))
        self.assertEqual(res["positions_settled"], [])
        pos = conn.execute("SELECT settled_at FROM positions").fetchone()
        self.assertIsNone(pos["settled_at"])
        conn.close()

    def test_unlinked_positions_stay_open(self):
        """Positions with no linked prediction and no ticker match stay open."""
        conn, pid = self._setup("KXTEST-26SEP22-T6.530")
        posfile = _write_json(os.path.join(self.tmp, "pos.json"),
                              {"market_ticker": "KXOTHER-26SEP30-X",
                               "side": "yes", "contracts": 10,
                               "fill_price": 0.3})
        rs.record_position(conn, type("A", (), {"file": posfile, "json": True})())
        rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        pos = conn.execute(
            "SELECT settled_at FROM positions WHERE market_ticker ="
            " 'KXOTHER-26SEP30-X'").fetchone()
        self.assertIsNone(pos["settled_at"])
        conn.close()


class TestPnlReport(StoreTestCase):
    def _two_settled(self):
        """One winner (yes @0.19 on a YES outcome) and one loser."""
        conn = self.init()
        rs.ingest_settled(conn, self.settled_args())
        args = type("A", (), {"family": "KXTEST", "backfill": 2,
                              "offset_hours": 3.0, "since": None,
                              "json": True})()
        rs.ingest_quotes(conn, args)   # stores real quote bars for T6.525/530
        conn.commit()
        for ticker, side, fill in (("KXTEST-26SEP22-T6.525", "yes", 0.19),
                                   ("KXTEST-26SEP22-T6.530", "yes", 0.60)):
            pfile = _write_json(os.path.join(self.tmp, "p_%s.json" % side),
                                {"market_ticker": ticker, "p_yes": 0.2,
                                 "target_date": "2026-09-22"})
            pred = rs.record_prediction(conn, type("A", (), {"file": pfile})())
            posfile = _write_json(os.path.join(self.tmp, "pos.json"),
                                  {"market_ticker": ticker, "side": side,
                                   "contracts": 10, "fill_price": fill,
                                   "prediction_id": pred["written"][0]["id"]})
            rs.record_position(conn, type("A", (), {"file": posfile,
                                                    "json": True})())
        rs.resolve_predictions(conn, type("A", (), {"as_of": None})())
        conn.commit()
        return conn

    def test_pnl_totals(self):
        conn = self._two_settled()
        out = rs.cmd_pnl(conn, type("A", (), {"open": False, "json": True})())
        self.assertEqual(out["realized_count"], 2)
        self.assertEqual(out["open_count"], 0)
        expect = (10 * 0.81 - rs.taker_fee(10, 0.19)) + (-(10 * 0.60) - rs.taker_fee(10, 0.60))
        self.assertAlmostEqual(out["total_realized_pnl"], expect, places=6)
        self.assertAlmostEqual(out["total_realized_pnl"],
                               8.1 - 0.11 + -6.0 - 0.17, places=6)
        conn.close()

    def test_pnl_open_marks_from_stored_quotes(self):
        conn = self._two_settled()
        # one position stays open (no matching prediction); T6.510 exists in
        # the synthetic ladder and settles NO, but nothing predicts it
        posfile = _write_json(os.path.join(self.tmp, "pos_open.json"),
                              {"market_ticker": "KXTEST-26SEP22-T6.510",
                               "side": "yes", "contracts": 4,
                               "fill_price": 0.30})
        rs.record_position(conn, type("A", (), {"file": posfile,
                                                "json": True})())
        conn.commit()
        out = rs.cmd_pnl(conn, type("A", (), {"open": True, "json": True})())
        self.assertEqual(out["realized_count"], 2)
        self.assertEqual(out["open_count"], 1)
        op = out["open_positions"][0]
        self.assertIn("mark", op)
        self.assertIsNotNone(op["mark"])
        self.assertIn("mark_to_market", op)
        self.assertEqual(op["mark_to_market"],
                         "UNREALIZED mark from stored quotes - not realized")
        self.assertAlmostEqual(op["mark"], 4 * (0.40 - 0.30), places=6)
        conn.close()

    def test_pnl_mark_prefers_bid_then_ask_then_last(self):
        conn = self.init()
        conn.execute(
            "INSERT INTO quotes(market_ticker, end_period_ts,"
            " hours_before_close, close_dollars, yes_bid_dollars,"
            " yes_ask_dollars, volume_fp, open_interest_fp, asof, fetched_at)"
            " VALUES ('KXM-1', 1, 2.0, 0.50, 0.44, 0.46, 1, 1, ?, ?)",
            (rs.now_iso(), rs.now_iso()))
        conn.execute(
            "INSERT INTO quotes(market_ticker, end_period_ts,"
            " hours_before_close, close_dollars, yes_bid_dollars,"
            " yes_ask_dollars, volume_fp, open_interest_fp, asof, fetched_at)"
            " VALUES ('KXM-1', 2, 1.0, 0.60, NULL, NULL, 1, 1, ?, ?)",
            (rs.now_iso(), rs.now_iso()))
        conn.commit()
        posfile = _write_json(os.path.join(self.tmp, "pos.json"),
                              {"market_ticker": "KXM-1", "side": "yes",
                               "contracts": 10, "fill_price": 0.40})
        rs.record_position(conn, type("A", (), {"file": posfile,
                                                "json": True})())
        conn.commit()
        out = rs.cmd_pnl(conn, type("A", (), {"open": True, "json": True})())
        # most recent row (hours_before_close 1.0) has bid only
        self.assertAlmostEqual(out["open_positions"][0]["mark"],
                               10 * (0.60 - 0.40), places=6)
        conn.close()

    def test_pnl_on_an_empty_store_degrades_gracefully(self):
        conn = self.init()
        out = rs.cmd_pnl(conn, type("A", (), {"open": True, "json": True})())
        self.assertEqual(out["realized_count"], 0)
        self.assertEqual(out["open_count"], 0)
        self.assertEqual(out["total_realized_pnl"], 0.0)
        self.assertEqual(out["open_positions"], [])
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
