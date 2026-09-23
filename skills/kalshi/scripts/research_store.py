#!/usr/bin/env python3
"""research_store.py — Kalshi research continuity store (store of record).

A durable, queryable store of what a Kalshi research run already knows, so the
next run starts from accumulated facts instead of re-deriving them.  Implements
the approved design `research-continuity-design.md` (card t_367040dd) with a
single stdlib-only CLI.

Read-only against the exchange: every endpoint used here is public and
unauthenticated.  No signing, no account, no orders.  The store lives under the
profile root (`~/.hermes/profiles/kalshi-bot/research/kalshi.sqlite`), never in
the 24h-pruned scratch dir.

Usage:
    python3 research_store.py init [--db PATH]
    python3 research_store.py ingest-settled --family KXDIESELD [--since 2026-09-01]
                                            [--kind daily_ladder] [--rebuild]
                                            [--no-open] [--unit USD/gal]
    python3 research_store.py ingest-closes --symbol HO=F [--symbol RB=F]
                                            [--since 2026-04-01] [--unit USD/Bbl]
    python3 research_store.py ingest-quotes --family KXDIESELD [--backfill N]
                                            [--offset-hours 3]
    python3 research_store.py fit --family KXDIESELD --model diesel_ols
                                  [--estimator diff_ols] [--exog HO=F] [--lags 0:4]
                                  [--as-of DATE] [--threshold 0] [--bands 8]
    python3 research_store.py digest [--since-last-run] [--family F] [--max-lines 60]
    python3 research_store.py stale [--json]
    python3 research_store.py record-prediction --file pred.json
    python3 research_store.py resolve-predictions [--as-of DATE]
    python3 research_store.py series --family KXDIESELD [--tail 20]

Every subcommand accepts `--json` (one JSON object on stdout) and takes no
interactive input.  Environment: `KALSHI_RESEARCH_DB` overrides the DB path,
`KALSHI_API_BASE` overrides the exchange base URL.

The core is deliberately generic: `family`, `series_ticker`, `kind`, `source`
and `model_name` are opaque strings.  No market-family-specific column, branch
or default exists here — family behaviour (strike steps, which source, which
model, which lags) is data.
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

BASE = os.environ.get(
    "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
USER_AGENT = "hermes-agent-kalshi-research/1.0"

# The store of record: PROFILE ROOT, not cache/scratch (pruned 24h).
DEFAULT_DB = os.path.join(
    os.path.expanduser("~"),
    ".hermes",
    "profiles",
    "kalshi-bot",
    "research",
    "kalshi.sqlite",
)

# A research session's writes belong to one `runs` row.  A run row is reused
# while it is still open and younger than this window; otherwise it is closed
# and a new run starts.
RUN_WINDOW_S = 4 * 3600

QUOTE_REFETCH_MIN = 15     # open-ladder / live-quote freshness rule (§3)
CLOSE_REFETCH_MIN = 30     # today's partial close freshness rule (§3)

REV_KEY = "store_rev"      # reserved cache_meta key: global monotonic revision
DIGEST_KEY = "digest:last"  # reserved cache_meta key: last digest hash

# Deterministic source preference when several sources describe the same
# (series, obs_date).  The reconstructed band is the series of record.
SOURCE_PRIORITY = ("kalshi_settlement", "aaa_page", "yahoo_close",
                   "kalshi_expiration_value")

KINDS = ("daily_ladder", "weekly_threshold", "monthly_threshold",
         "price_series", "unknown")

# Generic estimators (stdlib).  A caller's model NAME is opaque data; the
# estimator that computes it is one of these.
ESTIMATORS = ("realized_vol", "diff_ols", "conditional_hit_rate", "calibration")

CANDLE_CHUNK = 90          # /markets/candlesticks accepts max 100 tickers


# --------------------------------------------------------------------------
# Schema (§1c of the design) — generic core, no family-specific columns
# --------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version(
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS series(            -- one row per tracked series
  id INTEGER PRIMARY KEY,
  family TEXT NOT NULL,                       -- opaque: 'KXDIESELD', 'HO=F'
  series_ticker TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,                         -- opaque: 'daily_ladder', ...
  settlement_tz TEXT,
  close_hhmm TEXT,
  strike_step REAL,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS observations(      -- reconstructed/actual series
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  obs_date TEXT NOT NULL,                     -- the print's own date
  value REAL,                                 -- point value when known
  value_lo REAL, value_hi REAL,               -- band bounds when interval-only
  unit TEXT,
  source TEXT NOT NULL,
  quality TEXT NOT NULL DEFAULT 'final',
  asof TEXT NOT NULL,                         -- when the SOURCE says so
  fetched_at TEXT NOT NULL,                   -- when we pulled it
  UNIQUE(series_id, obs_date, source));

CREATE TABLE IF NOT EXISTS settlements(       -- one row per settled event
  event_ticker TEXT PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  obs_date TEXT NOT NULL,
  close_ts INTEGER NOT NULL,
  band_lo REAL, band_hi REAL, mid REAL,
  n_strikes INTEGER, n_quoted INTEGER,
  status TEXT,
  asof TEXT NOT NULL, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS markets(           -- strikes: ladder + arithmetic
  market_ticker TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  floor_strike REAL, strike_type TEXT,
  result TEXT,
  close_ts INTEGER NOT NULL, close_time TEXT,
  rules_hash TEXT,
  asof TEXT NOT NULL, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS quotes(            -- calibration sample
  market_ticker TEXT NOT NULL,
  end_period_ts INTEGER NOT NULL,
  hours_before_close REAL NOT NULL,
  close_dollars REAL, yes_bid_dollars REAL, yes_ask_dollars REAL,
  volume_fp REAL, open_interest_fp REAL,
  asof TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(market_ticker, hours_before_close));

CREATE TABLE IF NOT EXISTS models(            -- DERIVED, persisted for drift
  series_id INTEGER NOT NULL REFERENCES series(id),
  model_name TEXT NOT NULL,
  fit_date TEXT NOT NULL,
  n_obs INTEGER, params TEXT NOT NULL,
  resid_sd REAL, r2 REAL,
  inputs_rev INTEGER NOT NULL,
  PRIMARY KEY(series_id, model_name, fit_date));

CREATE TABLE IF NOT EXISTS predictions(       -- yesterday's expectation
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  market_ticker TEXT, series_id INTEGER,
  event_ticker TEXT, target_date TEXT,
  p_yes REAL NOT NULL,
  market_price REAL,
  edge_points REAL,
  direction TEXT,
  point_forecast REAL,
  forecast_sd REAL,
  rationale TEXT,
  resolved_at TEXT, outcome TEXT, error REAL);

CREATE TABLE IF NOT EXISTS cache_meta(        -- the freshness contract
  key TEXT PRIMARY KEY,
  asof TEXT, fetched_at TEXT, expires_at TEXT,
  n_rows INTEGER, bytes INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS runs(              -- the ledger
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL, finished_at TEXT,
  prompt TEXT, families TEXT,
  digest_hash TEXT,
  store_rev INTEGER,
  outcome TEXT);

CREATE INDEX IF NOT EXISTS idx_obs_series_date ON observations(series_id, obs_date);
CREATE INDEX IF NOT EXISTS idx_obs_fetched ON observations(fetched_at);
CREATE INDEX IF NOT EXISTS idx_settle_series ON settlements(series_id, obs_date);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market_ticker);
CREATE INDEX IF NOT EXISTS idx_predictions_target ON predictions(target_date);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""

TABLE_NAMES = ("schema_version", "series", "observations", "settlements",
               "markets", "quotes", "models", "predictions", "cache_meta",
               "runs")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_iso():
    """UTC timestamp with microsecond precision.

    Microseconds matter because a run's `finished_at` is the lower bound of the
    next run's `--since-last-run` window: at second resolution a row written in
    the same second as the boundary would be invisible to the next digest.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text):
    """Parse an ISO-8601 UTC timestamp (or date) into unix seconds."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def day_of(text):
    """'YYYY-MM-DD' from any ISO timestamp/date string."""
    ts = parse_ts(text)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def fmt(v, nd=4):
    if v is None:
        return "n/a"
    return ("%%.%df" % nd) % v


def dnum(val):
    """Kalshi/yahoo numbers arrive as strings; coerce or None."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def pstdev(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def ols(y, X):
    """Least squares by Gaussian elimination; returns (beta, resid_sd, r2)."""
    n = len(y)
    k = len(X[0])
    if n <= k:
        raise ValueError("not enough observations (%d) for %d params" % (n, k))
    xtx = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)]
           for a in range(k)]
    xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    aug = [row[:] + [xty[a]] for a, row in enumerate(xtx)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(aug[r][c]))
        aug[c], aug[piv] = aug[piv], aug[c]
        pv = aug[c][c]
        if pv == 0:
            raise ValueError("singular design matrix")
        aug[c] = [v / pv for v in aug[c]]
        for r in range(k):
            if r != c:
                f = aug[r][c]
                aug[r] = [aug[r][j] - f * aug[c][j] for j in range(k + 1)]
    beta = [aug[r][k] for r in range(k)]
    res = [y[i] - sum(beta[a] * X[i][a] for a in range(k)) for i in range(n)]
    sse = sum(r * r for r in res)
    ybar = mean(y)
    sst = sum((v - ybar) ** 2 for v in y)
    sd = math.sqrt(sse / (n - k))
    r2 = (1 - sse / sst) if sst > 0 else None
    return beta, sd, r2


def normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def emit(payload, as_json, text_fn=None):
    """Print either the JSON payload or its human rendering."""
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    elif text_fn is not None:
        text_fn(payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))


# --------------------------------------------------------------------------
# HTTP (public endpoints only, with 429/5xx backoff)
# --------------------------------------------------------------------------

def http_get_json(url, tries=4, timeout=45):
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            last = "HTTP %s: %s — %s" % (e.code, e.reason, detail)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last)
        except urllib.error.URLError as e:
            last = "connection error: %s" % (e.reason,)
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def api(path, params=None):
    """GET a Kalshi endpoint.  List values become REPEATED params (orderbooks);
    comma-separated params must be joined by the caller (candlesticks)."""
    url = BASE + path
    if params:
        pairs = []
        for key, val in params.items():
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                for item in val:
                    pairs.append((key, item))
            else:
                pairs.append((key, val))
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
    return http_get_json(url)


# --- fetch layer (monkeypatched by the offline test suite) -----------------

def fetch_settled_markets(series_ticker, min_close_ts, max_close_ts):
    """One paginated call replaces the whole per-event reconstruction loop."""
    out, cursor, pages = [], None, 0
    while True:
        params = {
            "status": "settled",
            "series_ticker": series_ticker,
            "limit": 1000,
            "mve_filter": "exclude",
            "min_close_ts": int(min_close_ts),
            "max_close_ts": int(max_close_ts),
        }
        if cursor:
            params["cursor"] = cursor
        data = api("/markets", params)
        out.extend(data.get("markets") or [])
        pages += 1
        cursor = data.get("cursor")
        if not cursor or pages >= 10:
            break
    return out


def fetch_open_markets(series_ticker):
    data = api("/markets", {
        "status": "open", "series_ticker": series_ticker, "limit": 1000,
        "mve_filter": "exclude",
    })
    return data.get("markets") or []


def fetch_yahoo_chart(symbol, period1, period2):
    url = "%s/%s?period1=%d&period2=%d&interval=1d" % (
        YAHOO_CHART, urllib.parse.quote(symbol), int(period1), int(period2))
    data = http_get_json(url, tries=3, timeout=40)
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        raise RuntimeError("no chart data for %s" % symbol)
    return results[0]


def fetch_candlesticks(series_ticker, market_tickers, start_ts, end_ts,
                       period_interval=60):
    """Batched candlesticks.

    `market_tickers` is COMMA-separated (the opposite of /markets/orderbooks,
    which repeats `tickers`) and the response nests under
    {"markets": [{"market_ticker", "candlesticks"}]} — see
    references/api-endpoints.md for both traps.
    """
    out = {}
    for group in chunks(list(market_tickers), CANDLE_CHUNK):
        data = api("/markets/candlesticks", {
            "market_tickers": ",".join(group),
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "period_interval": period_interval,
        })
        for entry in data.get("markets") or []:
            out[entry.get("market_ticker")] = entry.get("candlesticks") or []
    return out


# --------------------------------------------------------------------------
# Store plumbing
# --------------------------------------------------------------------------

def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def open_store(args):
    path = os.path.expanduser(args.db)
    if not os.path.exists(path):
        raise SystemExit(
            "store not initialised: %s\nrun: research_store.py init --db %s"
            % (path, path))
    return connect(path)


def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()
    if not row or row["v"] is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_iso()))
    conn.commit()


def table_counts(conn):
    return {t: conn.execute("SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
            for t in TABLE_NAMES}


def get_rev(conn):
    row = conn.execute("SELECT n_rows FROM cache_meta WHERE key = ?",
                       (REV_KEY,)).fetchone()
    return int(row["n_rows"]) if row and row["n_rows"] is not None else 0


def bump_rev(conn, delta, note="writes"):
    rev = get_rev(conn) + max(0, delta)
    stamp = now_iso()
    conn.execute(
        "INSERT INTO cache_meta(key, asof, fetched_at, expires_at, n_rows,"
        " bytes, note) VALUES (?, ?, ?, NULL, ?, NULL, ?)"
        " ON CONFLICT(key) DO UPDATE SET asof=excluded.asof,"
        " fetched_at=excluded.fetched_at, n_rows=excluded.n_rows,"
        " note=excluded.note",
        (REV_KEY, stamp, stamp, rev, "reserved: global monotonic store revision"))
    return rev


def set_meta(conn, key, note, n_rows=None, expires_at=None, asof=None):
    stamp = now_iso()
    conn.execute(
        "INSERT INTO cache_meta(key, asof, fetched_at, expires_at, n_rows,"
        " bytes, note) VALUES (?, ?, ?, ?, ?, NULL, ?)"
        " ON CONFLICT(key) DO UPDATE SET asof=excluded.asof,"
        " fetched_at=excluded.fetched_at, expires_at=excluded.expires_at,"
        " n_rows=excluded.n_rows, note=excluded.note",
        (key, asof or stamp, stamp, expires_at, n_rows, note))


def get_meta(conn, key):
    return conn.execute("SELECT * FROM cache_meta WHERE key = ?",
                        (key,)).fetchone()


def resolve_series(conn, family, kind=None, create=True, unit=None,
                   settlement_tz=None, close_hhmm=None, strike_step=None):
    """Find (or create) the series row for a family / series ticker.

    `family` and `series_ticker` are opaque; a family may own several series.
    """
    row = conn.execute(
        "SELECT * FROM series WHERE series_ticker = ? OR family = ?"
        " ORDER BY (series_ticker = ?) DESC, id LIMIT 1",
        (family, family, family)).fetchone()
    if row:
        return row
    if not create:
        return None
    conn.execute(
        "INSERT INTO series(family, series_ticker, kind, settlement_tz,"
        " close_hhmm, strike_step, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (family, family, kind or "unknown", settlement_tz, close_hhmm,
         strike_step, now_iso()))
    return conn.execute("SELECT * FROM series WHERE series_ticker = ?",
                        (family,)).fetchone()


def begin_run(conn, families=None, prompt=None, window_s=RUN_WINDOW_S):
    """Join the open run, or close it and start a new one (§ runs ledger)."""
    now = now_iso()
    now_ts = time.time()
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None and row["finished_at"] is None:
        started = parse_ts(row["started_at"]) or 0
        if now_ts - started <= window_s:
            return row["id"]
        conn.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, store_rev = ?"
            " WHERE id = ?", (now, "partial", get_rev(conn), row["id"]))
    digest = get_meta(conn, DIGEST_KEY)
    conn.execute(
        "INSERT INTO runs(started_at, finished_at, prompt, families,"
        " digest_hash, store_rev, outcome) VALUES (?, NULL, ?, ?, ?, ?, NULL)",
        (now, prompt, json.dumps(sorted(families or [])),
         digest["note"] if digest else None, get_rev(conn)))
    return conn.execute("SELECT MAX(id) i FROM runs").fetchone()["i"]


def touch_run(conn, run_id, outcome="ok"):
    """Mark the run's outcome.  `store_rev` stays at its run-START value: the
    next digest's rev delta (`store rev N (+37)`) is measured against the
    revision the previous run began from, so overwriting it with the current
    revision would always report a delta of zero."""
    conn.execute("UPDATE runs SET outcome = ? WHERE id = ?", (outcome, run_id))


def last_write_iso(conn):
    """Timestamp of the most recent fetch/write anywhere in the store."""
    stamps = []
    for table in ("observations", "settlements", "markets", "quotes"):
        row = conn.execute("SELECT MAX(fetched_at) f FROM %s" % table).fetchone()
        if row and row["f"]:
            stamps.append(row["f"])
    row = conn.execute("SELECT MAX(resolved_at) f FROM predictions").fetchone()
    if row and row["f"]:
        stamps.append(row["f"])
    return max(stamps) if stamps else None


def close_open_run(conn, outcome="ok"):
    """Finish the run currently open, if any.

    The digest is the run boundary: a run's digest is generated at its START
    (before any research decision), so `digest` first closes the previous run
    and then opens the new one.

    `finished_at` is stamped with the PREVIOUS run's own START, because that is
    exactly the boundary of its work: the previous run's digest was generated at
    its start, and every `fetched_at` it wrote is after it.  Using the current
    wall clock instead (or the last write timestamp) would start the new run's
    window at or after rows the previous run had already written, hiding them
    from the next digest.  The next run's `--since-last-run` window is therefore
    `(previous start, now]`.
    """
    row = conn.execute("SELECT * FROM runs WHERE finished_at IS NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE runs SET finished_at = ?, outcome = ?, store_rev = ?"
        " WHERE id = ?",
        (row["started_at"], outcome, get_rev(conn), row["id"]))
    return row["id"]


# --------------------------------------------------------------------------
# ingest-settled
# --------------------------------------------------------------------------

def _obs_date_for(event_ticker, close_time, settlement_tz):
    """The ladder's own date — the print's date, not the fetch date.

    The event ticker's day stamp (`KXDIESELD-26SEP22`, `KXAAAGASD-26SEP22`) is
    authoritative: it is the date the market's own rules resolve against.  For
    both the 05:59Z diesel ladder and the 03:59Z gas ladder that date is also
    the UTC date of `close_time`, so UTC is the fallback.  Do NOT convert
    `close_time` into `settlement_tz` here — for a 03:59Z close that lands on
    the previous ET day and would disagree with the event's own label.
    """
    if event_ticker:
        tail = event_ticker.rsplit("-", 1)[-1]
        if len(tail) == 7 and tail[:2].isdigit():
            try:
                return datetime.strptime(tail, "%y%b%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
    ts = parse_ts(close_time)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _rules_hash(text):
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_market(conn, m, series_row, now, unit=None):
    """Upsert one market row.  asof = the source's own claim."""
    close_ts = parse_ts(m.get("close_time")) or 0
    asof = m.get("settlement_ts") or m.get("close_time") or now
    conn.execute(
        "INSERT INTO markets(market_ticker, event_ticker, floor_strike,"
        " strike_type, result, close_ts, close_time, rules_hash, asof,"
        " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(market_ticker) DO UPDATE SET"
        " event_ticker=excluded.event_ticker, floor_strike=excluded.floor_strike,"
        " strike_type=excluded.strike_type, result=excluded.result,"
        " close_ts=excluded.close_ts, close_time=excluded.close_time,"
        " rules_hash=excluded.rules_hash, asof=excluded.asof,"
        " fetched_at=excluded.fetched_at",
        (m.get("ticker"), m.get("event_ticker"), dnum(m.get("floor_strike")),
         m.get("strike_type"), m.get("result") or None, close_ts,
         m.get("close_time"), _rules_hash(m.get("rules_primary")), asof, now))


def _ladder_band(markets):
    """band_lo = max floor_strike settled YES; band_hi = min NO strike above it."""
    yes = [dnum(m.get("floor_strike")) for m in markets if m.get("result") == "yes"]
    yes = [v for v in yes if v is not None]
    if not yes:
        return None, None, None
    lo = max(yes)
    above = [dnum(m.get("floor_strike")) for m in markets
             if m.get("result") == "no"]
    above = [v for v in above if v is not None and v > lo]
    hi = min(above) if above else None
    return lo, hi, ((lo + hi) / 2 if hi is not None else None)


def ingest_settled(conn, args):
    now = now_iso()
    now_ts = int(time.time())
    before = table_counts(conn)
    series_row = resolve_series(
        conn, args.family, kind=args.kind, settlement_tz=args.settlement_tz,
        close_hhmm=args.close_hhmm, strike_step=args.strike_step)
    if args.kind and series_row["kind"] != args.kind:
        conn.execute("UPDATE series SET kind = ? WHERE id = ?",
                     (args.kind, series_row["id"]))
    sid = series_row["id"]

    since_ts = parse_ts(args.since) if args.since else now_ts - 30 * 86400
    markets = fetch_settled_markets(series_row["series_ticker"], since_ts, now_ts)

    events = {}
    for m in markets:
        events.setdefault(m.get("event_ticker"), []).append(m)

    stored = {r["event_ticker"]: r for r in conn.execute(
        "SELECT event_ticker, status FROM settlements WHERE series_id = ?",
        (sid,))}
    written_rows = 0
    skipped = 0
    settled_events = []
    for event_ticker in sorted(events):
        sub = events[event_ticker]
        done = stored.get(event_ticker)
        if done is not None and done["status"] == "finalized" and not args.rebuild:
            skipped += 1
            continue
        for m in sub:
            _write_market(conn, m, series_row, now)
            written_rows += 1
        lo, hi, mid = _ladder_band(sub)
        statuses = {m.get("status") for m in sub}
        results_present = all(m.get("result") in ("yes", "no") for m in sub)
        if any(s == "disputed" for s in statuses):
            status = "disputed"
        elif any(s in ("amended", "determined") for s in statuses):
            status = "amended"
        elif results_present and all(s == "finalized" for s in statuses):
            status = "finalized"
        else:
            status = "unsettled"
        close_time = sub[0].get("close_time")
        close_ts = parse_ts(close_time) or 0
        obs_date = _obs_date_for(event_ticker, close_time,
                                 series_row["settlement_tz"])
        if lo is None:
            # No settled YES strike: no band, no observation.  Record the ladder
            # anyway (the settlement row is what makes it queryable).
            continue
        n_quoted = sum(1 for m in sub if (dnum(m.get("volume_fp")) or 0) > 0)
        asof = sub[0].get("settlement_ts") or close_time or now
        conn.execute(
            "INSERT INTO settlements(event_ticker, series_id, obs_date,"
            " close_ts, band_lo, band_hi, mid, n_strikes, n_quoted, status,"
            " asof, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(event_ticker) DO UPDATE SET"
            " obs_date=excluded.obs_date, close_ts=excluded.close_ts,"
            " band_lo=excluded.band_lo, band_hi=excluded.band_hi,"
            " mid=excluded.mid, n_strikes=excluded.n_strikes,"
            " n_quoted=excluded.n_quoted, status=excluded.status,"
            " asof=excluded.asof, fetched_at=excluded.fetched_at",
            (event_ticker, sid, obs_date, close_ts, lo, hi, mid, len(sub),
             n_quoted, status, asof, now))
        if status == "finalized":
            # STABLE: the reconstructed band for a past date.
            conn.execute(
                "INSERT INTO observations(series_id, obs_date, value, value_lo,"
                " value_hi, unit, source, quality, asof, fetched_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, 'kalshi_settlement', 'final', ?, ?)"
                " ON CONFLICT(series_id, obs_date, source) DO UPDATE SET"
                " value_lo=excluded.value_lo, value_hi=excluded.value_hi,"
                " unit=excluded.unit, asof=excluded.asof,"
                " fetched_at=excluded.fetched_at",
                (sid, obs_date, lo, hi, args.unit, asof, now))
        # The exchange's own settlement print (expiration_value) is the ground
        # truth the reconstruction is scored against — a second source on the
        # same date, not a replacement for the band.
        truth = dnum(sub[0].get("expiration_value"))
        if truth is not None or sub[0].get("expiration_value"):
            stored_truth = truth if truth is not None else None
            conn.execute(
                "INSERT INTO observations(series_id, obs_date, value, value_lo,"
                " value_hi, unit, source, quality, asof, fetched_at)"
                " VALUES (?, ?, ?, NULL, NULL, ?, 'kalshi_expiration_value',"
                " ?, ?, ?)"
                " ON CONFLICT(series_id, obs_date, source) DO UPDATE SET"
                " value=excluded.value, quality=excluded.quality,"
                " asof=excluded.asof, fetched_at=excluded.fetched_at",
                (sid, obs_date, stored_truth, args.unit,
                 "final" if status == "finalized" else "provisional", asof, now))
        settled_events.append({"event": event_ticker, "obs_date": obs_date,
                               "band_lo": lo, "band_hi": hi, "mid": mid,
                               "n_strikes": len(sub), "n_quoted": n_quoted,
                               "status": status,
                               "truth": truth})

    open_events = []
    if not args.no_open:
        try:
            opens = fetch_open_markets(series_row["series_ticker"])
        except RuntimeError:
            opens = []
        for m in opens:
            _write_market(conn, m, series_row, now)
            written_rows += 1
        by_event = {}
        for m in opens:
            by_event.setdefault(m.get("event_ticker"), []).append(m)
        for event_ticker, sub in sorted(by_event.items()):
            open_events.append({
                "event": event_ticker,
                "close_time": sub[0].get("close_time"),
                "n_strikes": len(sub),
                "n_quoted": sum(
                    1 for m in sub if (dnum(m.get("volume_fp")) or 0) > 0),
            })

    after = table_counts(conn)
    delta = sum(max(0, after[t] - before[t]) for t in TABLE_NAMES)
    rev = bump_rev(conn, delta) if delta else get_rev(conn)
    conn.commit()
    return {
        "command": "ingest-settled",
        "family": args.family,
        "series_ticker": series_row["series_ticker"],
        "series_id": sid,
        "kind": args.kind or series_row["kind"],
        "window": {"min_close_ts": since_ts, "max_close_ts": now_ts,
                   "since": iso(since_ts)},
        "api_markets": len(markets),
        "events_seen": len(events),
        "events_ingested": len(settled_events),
        "events_skipped_finalized": skipped,
        "rebuild": bool(args.rebuild),
        "settled_events": settled_events[-12:],
        "open_events": open_events,
        "rows_delta": {t: after[t] - before[t] for t in TABLE_NAMES
                       if after[t] != before[t]},
        "store_rev": rev,
        "counts": after,
    }


# --------------------------------------------------------------------------
# ingest-closes
# --------------------------------------------------------------------------

def ingest_closes(conn, args):
    now = now_iso()
    now_ts = int(time.time())
    before = table_counts(conn)
    out = []
    for symbol in args.symbol:
        series_row = resolve_series(conn, args.family or symbol,
                                    kind="price_series")
        sid = series_row["id"]
        period1 = parse_ts(args.since) if args.since else now_ts - 180 * 86400
        chart = fetch_yahoo_chart(symbol, period1, now_ts)
        meta = chart.get("meta") or {}
        tss = chart.get("timestamp") or []
        quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        gmtoffset = int(meta.get("gmtoffset") or 0)
        rmt = meta.get("regularMarketTime")
        newest_bar_ts = max(tss) if tss else None
        rows = 0
        newest = None
        for ts, close in zip(tss, closes):
            if close is None:
                continue
            local = datetime.fromtimestamp(ts + gmtoffset, tz=timezone.utc)
            obs_date = local.strftime("%Y-%m-%d")
            # Yahoo only gives an exact asof for the current session; a daily
            # bar's timestamp is the session start, so historical bars carry
            # their own timestamp as asof.
            asof = (iso(rmt) if (rmt and ts == newest_bar_ts) else iso(ts))
            conn.execute(
                "INSERT INTO observations(series_id, obs_date, value, value_lo,"
                " value_hi, unit, source, quality, asof, fetched_at)"
                " VALUES (?, ?, ?, NULL, NULL, ?, 'yahoo_close', 'final', ?, ?)"
                " ON CONFLICT(series_id, obs_date, source) DO UPDATE SET"
                " value=excluded.value, unit=excluded.unit, asof=excluded.asof,"
                " fetched_at=excluded.fetched_at",
                (sid, obs_date, float(close), args.unit, asof, now))
            rows += 1
            newest = {"obs_date": obs_date, "value": float(close), "asof": asof}
        set_meta(conn, "closes:%s:%s" % (symbol, now[:7]),
                 "yahoo daily closes", n_rows=rows, asof=iso(rmt) if rmt else now)
        out.append({"symbol": symbol, "series_id": sid, "rows": rows,
                    "bars": len(tss), "newest": newest,
                    "regular_market_price": meta.get("regularMarketPrice"),
                    "regular_market_time": iso(rmt) if rmt else None,
                    "exchange_timezone": meta.get("exchangeTimezoneName")})
    after = table_counts(conn)
    delta = sum(max(0, after[t] - before[t]) for t in TABLE_NAMES)
    rev = bump_rev(conn, delta) if delta else get_rev(conn)
    conn.commit()
    return {"command": "ingest-closes", "symbols": out,
            "rows_delta": {t: after[t] - before[t] for t in TABLE_NAMES
                           if after[t] != before[t]},
            "store_rev": rev, "counts": after}


# --------------------------------------------------------------------------
# ingest-quotes
# --------------------------------------------------------------------------

def _bar_quotes(bar):
    """Read a candlestick's quotes; the batched `price` block is often sparse,
    so fall back to the yes_bid/yes_ask OHLC blocks (see api-endpoints.md)."""
    bid = dnum((bar.get("yes_bid") or {}).get("close_dollars"))
    ask = dnum((bar.get("yes_ask") or {}).get("close_dollars"))
    price = bar.get("price") or {}
    close = dnum(price.get("close_dollars"))
    if close is None:
        close = dnum(price.get("mean_dollars"))
    if close is None and bid is not None and ask is not None:
        close = (bid + ask) / 2
    if close is None:
        close = bid if bid is not None else ask
    return {"close": close, "bid": bid, "ask": ask,
            "volume": dnum(bar.get("volume_fp")),
            "open_interest": dnum(bar.get("open_interest_fp"))}


def ingest_quotes(conn, args):
    now = now_iso()
    now_ts = int(time.time())
    before = table_counts(conn)
    series_row = resolve_series(conn, args.family, create=False)
    if series_row is None:
        raise SystemExit("unknown family %s — run ingest-settled first"
                         % args.family)
    sid = series_row["id"]

    settled = conn.execute(
        "SELECT s.event_ticker, s.close_ts, s.obs_date FROM settlements s"
        " WHERE s.series_id = ? ORDER BY s.close_ts DESC", (sid,)).fetchall()
    if args.since:
        since_ts = parse_ts(args.since) or 0
        settled = [r for r in settled if r["close_ts"] >= since_ts]
    # --backfill N selects a FIXED window (the N most recent settled events),
    # NOT "the N most recent events lacking quotes": a fixed window makes an
    # identical re-run a no-op, which is the idempotency contract.  Deepen the
    # back-fill by raising N or by passing --since.
    want = args.backfill if args.backfill is not None else 1
    pending = settled[:max(0, want)]

    # Settled events: the bar fully before the close is immutable, so this is
    # a one-time back-fill, not a capture race.
    targets = [{"event": r["event_ticker"], "close_ts": r["close_ts"],
                "settled": True} for r in pending]

    # Scope the live capture to THIS family's events.  Kalshi's own ticker
    # scheme prefixes an event with its series ticker, so this needs no
    # family-specific knowledge.
    prefix = series_row["series_ticker"] + "-"
    open_rows = conn.execute(
        "SELECT event_ticker, MIN(close_ts) close_ts FROM markets"
        " WHERE close_ts > ? AND event_ticker LIKE ?"
        " GROUP BY event_ticker ORDER BY close_ts",
        (now_ts, prefix + "%")).fetchall()
    for r in open_rows:
        tick = conn.execute(
            "SELECT COUNT(*) c FROM markets WHERE event_ticker = ?",
            (r["event_ticker"],)).fetchone()["c"]
        if tick:
            targets.append({"event": r["event_ticker"],
                            "close_ts": r["close_ts"], "settled": False})

    written, per_event = 0, []
    for t in targets:
        event = t["event"]
        market_rows = conn.execute(
            "SELECT market_ticker, close_ts FROM markets WHERE event_ticker = ?",
            (event,)).fetchall()
        if not market_rows:
            continue
        tickers = [m["market_ticker"] for m in market_rows]
        start = t["close_ts"] - int(args.offset_hours * 3600)
        end = t["close_ts"] + 60 if t["settled"] else now_ts
        if start >= end:
            start = end - 3600
        bars = fetch_candlesticks(series_row["series_ticker"], tickers,
                                  max(0, start), end)
        n = 0
        for tick in tickers:
            cand = bars.get(tick) or []
            if not cand:
                continue
            pick = None
            for bar in cand:
                if t["settled"]:
                    if bar.get("end_period_ts") <= t["close_ts"]:
                        pick = bar
                elif pick is None or bar.get("end_period_ts") > pick.get(
                        "end_period_ts"):
                    pick = bar
            if pick is None:
                continue
            ep = int(pick.get("end_period_ts"))
            if not t["settled"]:
                # Do not re-write the same bar for a live event.
                got = conn.execute(
                    "SELECT MAX(end_period_ts) e FROM quotes WHERE"
                    " market_ticker = ?", (tick,)).fetchone()["e"]
                if got is not None and ep <= got:
                    continue
            q = _bar_quotes(pick)
            hbc = round((t["close_ts"] - ep) / 3600.0, 4)
            conn.execute(
                "INSERT INTO quotes(market_ticker, end_period_ts,"
                " hours_before_close, close_dollars, yes_bid_dollars,"
                " yes_ask_dollars, volume_fp, open_interest_fp, asof,"
                " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(market_ticker, hours_before_close) DO UPDATE SET"
                " end_period_ts=excluded.end_period_ts,"
                " close_dollars=excluded.close_dollars,"
                " yes_bid_dollars=excluded.yes_bid_dollars,"
                " yes_ask_dollars=excluded.yes_ask_dollars,"
                " volume_fp=excluded.volume_fp,"
                " open_interest_fp=excluded.open_interest_fp,"
                " asof=excluded.asof, fetched_at=excluded.fetched_at",
                (tick, ep, hbc, q["close"], q["bid"], q["ask"], q["volume"],
                 q["open_interest"], iso(ep), now))
            n += 1
            written += 1
        per_event.append({"event": event, "settled": t["settled"],
                          "tickers": len(tickers), "quotes_written": n,
                          "capture_offset_hours": round(
                              (t["close_ts"] - start) / 3600.0, 4)})

    after = table_counts(conn)
    delta = sum(max(0, after[t] - before[t]) for t in TABLE_NAMES)
    rev = bump_rev(conn, delta) if delta else get_rev(conn)
    conn.commit()
    return {"command": "ingest-quotes", "family": args.family,
            "offset_hours": args.offset_hours,
            "events": per_event, "quotes_written": written,
            "rows_delta": {t: after[t] - before[t] for t in TABLE_NAMES
                           if after[t] != before[t]},
            "store_rev": rev, "counts": after}


# --------------------------------------------------------------------------
# fit (DERIVED)
# --------------------------------------------------------------------------

def series_observations(conn, series_id, as_of=None):
    """Observations of one series, one row per date, deterministic source pick."""
    rows = conn.execute(
        "SELECT obs_date, value, value_lo, value_hi, source, asof, fetched_at"
        " FROM observations WHERE series_id = ?", (series_id,)).fetchall()
    by_date = {}
    for r in rows:
        if as_of and r["obs_date"] and r["obs_date"] > as_of:
            continue
        val = r["value"]
        if val is None and r["value_lo"] is not None and r["value_hi"] is not None:
            val = (r["value_lo"] + r["value_hi"]) / 2
        if val is None:
            continue
        cur = by_date.get(r["obs_date"])
        if cur is None:
            by_date[r["obs_date"]] = {"date": r["obs_date"], "value": val,
                                      "source": r["source"]}
            continue
        if _src_rank(r["source"]) < _src_rank(cur["source"]):
            by_date[r["obs_date"]] = {"date": r["obs_date"], "value": val,
                                      "source": r["source"]}
    return [by_date[d] for d in sorted(by_date)]


def _obs_at(obs, date):
    """Index of the observation dated exactly `date`, or None if there is none.

    A print's date is not interchangeable with a neighbouring date's print: a
    forecast for one date scored against another date's number measures nothing.
    """
    for i, o in enumerate(obs):
        if o["date"] == date:
            return i
    return None


def _src_rank(source):
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def fit_realized_vol(conn, series_row, args, obs):
    if len(obs) < 8:
        raise SystemExit("not enough observations for realized_vol (%d)" % len(obs))
    vals = [o["value"] for o in obs]
    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals))
            if vals[i - 1] > 0 and vals[i] > 0]
    windows = {}
    for label, w in (("5d", 5), ("10d", 10), ("20d", 20), ("all", len(rets))):
        if len(rets) < max(2, w):
            continue
        seg = rets[-w:]
        sd = pstdev(seg)
        windows[label] = {
            "daily_vol": sd,
            "annualized": sd * math.sqrt(252) if sd is not None else None,
            "n": len(seg),
        }
    params = {
        "windows": windows,
        "last_value": vals[-1],
        "last_obs_date": obs[-1]["date"],
        "first_obs_date": obs[0]["date"],
        "inputs": {"primary": series_row["series_ticker"]},
    }
    return params, len(rets), None, None


def fit_diff_ols(conn, series_row, args, obs):
    if not obs:
        raise SystemExit("no observations for %s" % series_row["series_ticker"])
    exog_row = None
    exog_pct = {}
    if args.exog:
        exog_row = resolve_series(conn, args.exog, create=False)
        if exog_row is None:
            raise SystemExit("unknown exog series %s — ingest it first" % args.exog)
        eobs = series_observations(conn, exog_row["id"], as_of=args.as_of)
        for i in range(1, len(eobs)):
            prev, cur = eobs[i - 1]["value"], eobs[i]["value"]
            if prev:
                exog_pct[eobs[i]["date"]] = (cur - prev) / prev
    lag_min, lag_max = _parse_lags(args.lags)
    primary = {o["date"]: o["value"] for o in obs}
    dates = [o["date"] for o in obs]
    exog_dates = sorted(exog_pct)

    def exog_at(date, lag):
        """Pct change of the exog series `lag` steps before `date`.

        The exog series runs on its own (trading-day) calendar while the
        primary ladder prints daily, so the exog grid is stepped by its own
        index: lag 0 = the most recent exog change at or before `date`, lag k =
        the one k entries earlier.  Using max(1, lag) here would map lag 0 and
        lag 1 to the same column and make the design matrix singular.
        """
        idx = None
        for i, d in enumerate(exog_dates):
            if d <= date:
                idx = i
            else:
                break
        if idx is None or idx - lag < 0:
            return None
        return exog_pct[exog_dates[idx - lag]]

    y, X, used = [], [], []
    # Each row predicts the change INTO dates[i], so every regressor must be
    # knowable at dates[i-1].  The autoregressive term is therefore the change
    # into dates[i-1] (one day back) and the exog lags are measured strictly
    # before dates[i].  Reading the current change into the AR column would
    # make the regressor equal the dependent (a tautology with R2 = 1).
    for i in range(2, len(dates)):
        d = dates[i]
        prev2 = primary[dates[i - 2]]
        prev = primary[dates[i - 1]]
        cur = primary[d]
        if prev2 is None or prev is None or cur is None:
            continue
        row = [1.0]
        if args.lags and lag_min <= 0:
            row.append(prev - prev2)
        bad = False
        for lag in range(lag_min, lag_max + 1):
            v = exog_at(d, lag) if exog_row is not None else None
            if exog_row is not None and v is None:
                bad = True
                break
            row.append(v if v is not None else 0.0)
        if bad:
            continue
        y.append(cur - prev)
        X.append(row)
        used.append(d)
    if len(y) < 8:
        raise SystemExit("not enough aligned obs for diff_ols (%d)" % len(y))
    try:
        beta, sd, r2 = ols(y, X)
    except ValueError as e:
        raise SystemExit(
            "diff_ols cannot be fitted on this input (%s): %d aligned obs, %d"
            " features.  A perfectly collinear regressor (e.g. a primary series"
            " with a constant daily change, or an exog window with no variation)"
            " makes the fit unidentified." % (e, len(y), len(X[0])))
    names = ["const"]
    if args.lags and lag_min <= 0:
        names.append("d_primary_lag1")
    names += ["exog_pct_lag%d" % l for l in range(lag_min, lag_max + 1)]
    params = {
        "coefficients": dict(zip(names, beta)),
        "lag_range": [lag_min, lag_max],
        "dependent": "d(%s)" % series_row["series_ticker"],
        "inputs": {"primary": series_row["series_ticker"],
                   "exog": exog_row["series_ticker"] if exog_row else None},
        "n_aligned": len(y),
        "first_obs_date": used[0],
        "last_obs_date": used[-1],
        "last_features": dict(zip(names, X[-1])),
        "predicted_last": sum(b * v for b, v in zip(beta, X[-1])),
    }
    return params, len(y), sd, r2


def fit_conditional_hit_rate(conn, series_row, args, obs):
    vals = [o["value"] for o in obs]
    if len(vals) < 10:
        raise SystemExit("not enough observations (%d)" % len(vals))
    chg = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    step = args.bucket_step
    edges = [(-1e9, -2 * step), (-2 * step, -step), (-step, 0.0),
             (0.0, step), (step, 2 * step), (2 * step, 1e9)]

    def label(lo, hi):
        return "%s..%s" % ("-inf" if lo < -1e8 else "%+.4f" % lo,
                           "+inf" if hi > 1e8 else "%+.4f" % hi)

    states = []
    for lo, hi in edges:
        idx = [i for i in range(len(chg) - 1) if lo < chg[i] <= hi]
        if not idx:
            continue
        nxt = [chg[i + 1] for i in idx]
        hits = sum(1 for v in nxt if v > args.threshold)
        states.append({"state": label(lo, hi), "n": len(idx),
                       "p_hit": hits / len(idx),
                       "mean_next_change": mean(nxt)})
    params = {"states": states, "threshold": args.threshold,
              "bucket_step": step, "dependent": series_row["series_ticker"],
              "inputs": {"primary": series_row["series_ticker"]},
              "last_obs_date": obs[-1]["date"]}
    return params, len(chg), pstdev(chg), None


def fit_calibration(conn, series_row, args, obs):
    rows = conn.execute(
        "SELECT q.market_ticker, q.close_dollars, m.result FROM quotes q"
        " JOIN markets m ON m.market_ticker = q.market_ticker"
        " WHERE m.result IN ('yes','no')", ()).fetchall()
    pairs = [(dnum(r["close_dollars"]), 1 if r["result"] == "yes" else 0)
             for r in rows if dnum(r["close_dollars"]) is not None]
    if len(pairs) < 5:
        raise SystemExit("not enough settled quote samples (%d)" % len(pairs))
    nb = max(2, args.bands)
    bands = []
    for b in range(nb):
        lo, hi = b / nb, (b + 1) / nb
        seg = [p for p in pairs if (lo <= p[0] < hi) or (b == nb - 1 and p[0] == hi)]
        if not seg:
            bands.append({"lo": lo, "hi": hi, "n": 0, "yes_rate": None,
                          "mean_price": None, "edge": None})
            continue
        yes = sum(p[1] for p in seg)
        mp = mean([p[0] for p in seg])
        bands.append({"lo": lo, "hi": hi, "n": len(seg),
                      "yes_rate": yes / len(seg), "mean_price": mp,
                      "edge": yes / len(seg) - mp})
    edges = [b["edge"] for b in bands if b["edge"] is not None and b["n"]]
    params = {"bands": bands, "n_bands": nb,
              "mean_edge": mean(edges) if edges else None,
              "inputs": {"quotes": series_row["series_ticker"]},
              "last_obs_date": obs[-1]["date"] if obs else None}
    return params, len(pairs), None, None


ESTIMATOR_FNS = {
    "realized_vol": fit_realized_vol,
    "diff_ols": fit_diff_ols,
    "conditional_hit_rate": fit_conditional_hit_rate,
    "calibration": fit_calibration,
}


def _parse_lags(spec):
    if not spec:
        return 0, 4
    if ":" in spec:
        a, b = spec.split(":", 1)
        return int(a), int(b)
    v = int(spec)
    return v, v


def _diff_params(old, new, limit=3):
    """Top scalar changes between two param sets (drift is a query, not prose)."""
    flat_old, flat_new = {}, {}

    def walk(prefix, obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(("%s.%s" % (prefix, k)) if prefix else str(k), v, out)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk("%s[%d]" % (prefix, i), v, out)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            out[prefix] = float(obj)

    walk("", old, flat_old)
    walk("", new, flat_new)
    diffs = []
    for key in sorted(set(flat_old) | set(flat_new)):
        if key.startswith("inputs") or key.endswith("last_features"):
            continue
        a, b = flat_old.get(key), flat_new.get(key)
        if a is None or b is None or a == b:
            continue
        diffs.append((key, a, b, abs(b - a)))
    diffs.sort(key=lambda d: -d[3])
    return [{"param": k, "from": a, "to": b} for k, a, b, _ in diffs[:limit]]


def fit(conn, args):
    series_row = resolve_series(conn, args.family, create=False)
    if series_row is None:
        raise SystemExit("unknown family %s — ingest it first" % args.family)
    estimator = args.estimator or args.model
    if estimator not in ESTIMATOR_FNS:
        raise SystemExit("unknown estimator/model %s (estimators: %s; pass"
                         " --estimator to name your own model)"
                         % (estimator, ", ".join(sorted(ESTIMATOR_FNS))))
    obs = series_observations(conn, series_row["id"], as_of=args.as_of)
    params, n_obs, resid_sd, r2 = ESTIMATOR_FNS[estimator](
        conn, series_row, args, obs)
    fit_date = args.as_of or day_of(now_iso())
    prev = conn.execute(
        "SELECT * FROM models WHERE series_id = ? AND model_name = ?"
        " ORDER BY fit_date DESC LIMIT 1",
        (series_row["id"], args.model)).fetchone()
    params_json = json.dumps(params, sort_keys=True)
    conn.execute(
        "INSERT INTO models(series_id, model_name, fit_date, n_obs, params,"
        " resid_sd, r2, inputs_rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(series_id, model_name, fit_date) DO UPDATE SET"
        " n_obs=excluded.n_obs, params=excluded.params,"
        " resid_sd=excluded.resid_sd, r2=excluded.r2,"
        " inputs_rev=excluded.inputs_rev",
        (series_row["id"], args.model, fit_date, n_obs, params_json, resid_sd,
         r2, get_rev(conn)))
    set_meta(conn, "model:%s:%s" % (series_row["series_ticker"], args.model),
             "fitted %s" % estimator, n_rows=n_obs, asof=fit_date + "T00:00:00Z")
    conn.commit()
    delta = {}
    if prev is not None:
        old = json.loads(prev["params"])
        delta = {
            "previous_fit_date": prev["fit_date"],
            "n_obs": [prev["n_obs"], n_obs],
            "resid_sd": [prev["resid_sd"], resid_sd],
            "r2": [prev["r2"], r2],
            "params_changed": _diff_params(old, params),
        }
    return {
        "command": "fit", "family": args.family, "model_name": args.model,
        "estimator": estimator, "fit_date": fit_date, "n_obs": n_obs,
        "resid_sd": resid_sd, "r2": r2, "inputs_rev": get_rev(conn),
        "as_of": args.as_of,
        "params": params,
        "delta": delta,
        "first_fit": prev is None,
    }


# --------------------------------------------------------------------------
# stale (§3 freshness contract, machine-checkable)
# --------------------------------------------------------------------------

def find_stale(conn, now_ts=None):
    now_ts = now_ts or int(time.time())
    now = iso(now_ts)
    out = []

    def age_min(stamp):
        ts = parse_ts(stamp)
        if ts is None:
            return None
        return round((now_ts - ts) / 60.0, 1)

    # open ladder / live quotes: refetch within 15 min (§3)
    rows = conn.execute(
        "SELECT event_ticker, MAX(fetched_at) f, MAX(close_time) ct FROM markets"
        " WHERE close_ts > ? GROUP BY event_ticker", (now_ts,)).fetchall()
    for r in rows:
        a = age_min(r["f"])
        if a is None or a > QUOTE_REFETCH_MIN:
            out.append({"class": "open_ladder", "key": r["event_ticker"],
                        "asof": r["ct"], "fetched_at": r["f"],
                        "age_minutes": a,
                        "rule": "open ladder refetched when fetched_at older"
                                " than %d min" % QUOTE_REFETCH_MIN})

    qrows = conn.execute(
        "SELECT q.market_ticker, MAX(q.fetched_at) f, MAX(m.close_time) ct"
        " FROM quotes q JOIN markets m ON m.market_ticker = q.market_ticker"
        " WHERE m.close_ts > ? GROUP BY q.market_ticker", (now_ts,)).fetchall()
    for r in qrows:
        a = age_min(r["f"])
        if a is None or a > QUOTE_REFETCH_MIN:
            out.append({"class": "quote_sample", "key": r["market_ticker"],
                        "asof": r["ct"], "fetched_at": r["f"],
                        "age_minutes": a,
                        "rule": "a 15-minute-old quote is not a price you can"
                                " act on"})

    # today's partial close: newest bar behind the session date, or stale (§3)
    today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    for s in conn.execute(
            "SELECT id, series_ticker FROM series WHERE kind = 'price_series'"):
        row = conn.execute(
            "SELECT obs_date, fetched_at FROM observations WHERE series_id = ?"
            " ORDER BY obs_date DESC LIMIT 1", (s["id"],)).fetchone()
        if row is None:
            out.append({"class": "today_close", "key": s["series_ticker"],
                        "asof": None, "fetched_at": None, "age_minutes": None,
                        "rule": "no closes stored yet"})
            continue
        a = age_min(row["fetched_at"])
        if row["obs_date"] < today or a is None or a > CLOSE_REFETCH_MIN:
            out.append({"class": "today_close", "key": s["series_ticker"],
                        "asof": row["obs_date"], "fetched_at": row["fetched_at"],
                        "age_minutes": a,
                        "rule": "refetch when newest bar date < session date or"
                                " fetched_at older than %d min"
                                % CLOSE_REFETCH_MIN})

    # settled ladders that are not final: revalidate (§2 STABLE guard)
    for r in conn.execute(
            "SELECT event_ticker, status, asof, fetched_at FROM settlements"
            " WHERE status != 'finalized'"):
        out.append({"class": "settlement_revalidation", "key": r["event_ticker"],
                    "asof": r["asof"], "fetched_at": r["fetched_at"],
                    "age_minutes": age_min(r["fetched_at"]),
                    "rule": "quality != final (%s) — revalidate" % r["status"]})

    # DERIVED: invalidated by any input row fetched after the fit date (§3)
    for m in conn.execute(
            "SELECT mo.rowid rid, mo.model_name, mo.fit_date, mo.inputs_rev,"
            " s.series_ticker FROM models mo JOIN series s ON s.id = mo.series_id"):
        newest = conn.execute(
            "SELECT MAX(date(fetched_at)) d FROM observations WHERE series_id ="
            " (SELECT id FROM series WHERE series_ticker = ?)",
            (m["series_ticker"],)).fetchone()["d"]
        if newest is not None and newest > m["fit_date"]:
            out.append({"class": "model",
                        "key": "%s/%s" % (m["series_ticker"], m["model_name"]),
                        "asof": m["fit_date"], "fetched_at": newest,
                        "age_minutes": None,
                        "rule": "input rows fetched after fit_date"})

    # cached artifacts with an explicit expiry
    for r in conn.execute(
            "SELECT key, asof, fetched_at, expires_at FROM cache_meta"
            " WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)):
        out.append({"class": "cache_meta", "key": r["key"], "asof": r["asof"],
                    "fetched_at": r["fetched_at"], "age_minutes": None,
                    "rule": "expires_at %s has passed" % r["expires_at"]})

    return out


def cmd_stale(conn, args):
    items = find_stale(conn)
    by_class = {}
    for it in items:
        by_class[it["class"]] = by_class.get(it["class"], 0) + 1
    payload = {"command": "stale", "stale_count": len(items),
               "by_class": by_class, "items": items,
               "checked_at": now_iso()}

    def text(p):
        print("stale artifacts: %d" % p["stale_count"])
        for it in p["items"]:
            print("  [%s] %s — %s (fetched_at %s)"
                  % (it["class"], it["key"], it["rule"], it["fetched_at"]))
        if not p["items"]:
            print("  (none — every freshness rule holds)")

    emit(payload, args.json, text)
    return payload


# --------------------------------------------------------------------------
# digest (§4) — generated from rows, never hand-written
# --------------------------------------------------------------------------

DIGEST_CAPS = [16, 18, 10, 10, 8]   # per-section line caps, section 1..5


def _new_rows(conn, table, since, limit=6):
    where, params = "", []
    if since:
        where = " WHERE fetched_at > ?"
        params = [since]
    rows = conn.execute(
        "SELECT * FROM %s%s ORDER BY fetched_at DESC, rowid DESC LIMIT %d"
        % (table, where, limit), params).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) c FROM %s%s" % (table, where), params).fetchone()["c"]
    return rows, total


def digest_sections(conn, family=None, since=None, has_prior_run=False):
    now_ts = int(time.time())
    fams = [r["family"] for r in conn.execute(
        "SELECT DISTINCT family FROM series ORDER BY family")]
    if family:
        fams = [f for f in fams if f == family]
    sections = []

    # ---- 1) EXPECTATION CHECK -------------------------------------------
    lines = []
    s1_series = {}
    preds = conn.execute(
        "SELECT * FROM predictions ORDER BY COALESCE(resolved_at, '') DESC, id DESC"
        " LIMIT 8").fetchall()
    resolved = conn.execute(
        "SELECT COUNT(*) c, AVG(error) e FROM predictions"
        " WHERE resolved_at IS NOT NULL AND point_forecast IS NOT NULL"
        " AND error IS NOT NULL").fetchone()
    resolved_any = conn.execute(
        "SELECT COUNT(*) c FROM predictions WHERE resolved_at IS NOT NULL"
    ).fetchone()["c"]
    # A prediction is scored against ONE family of units.  A probability
    # prediction scores in probability units (|p_yes - outcome|); a point
    # forecast scores in the series' own units against forecast_sd.  Mixing the
    # two (z-scoring a probability error against a price sd) is meaningless, so
    # the sd is only used when the prediction carries a point forecast, and the
    # mean error below averages price-unit predictions only.
    inside = 0
    scored = 0
    for p in conn.execute(
            "SELECT * FROM predictions WHERE resolved_at IS NOT NULL"
            " AND error IS NOT NULL"):
        if p["point_forecast"] is None:
            continue
        if not p["forecast_sd"] or p["forecast_sd"] <= 0:
            continue
        scored += 1
        if abs(p["error"]) <= p["forecast_sd"]:
            inside += 1
    for p in preds:
        label = p["market_ticker"] or p["event_ticker"] or p["target_date"] or "?"
        if p["resolved_at"]:
            sd_txt = ""
            if p["point_forecast"] is not None and p["forecast_sd"]:
                z = p["error"] / p["forecast_sd"]
                mark = "✔" if abs(z) <= 1 else "✗"
                sd_txt = " (%+.2f sd) %s" % (z, mark)
            elif p["outcome"] in ("yes", "no"):
                sd_txt = " (probability, settled %s)" % p["outcome"]
            lines.append("%s %s resolved %s err %+.4f%s"
                         % (label, p["target_date"] or "", p["outcome"] or "?",
                            p["error"], sd_txt))
        else:
            actual = None
            if p["series_id"] and p["target_date"]:
                row = conn.execute(
                    "SELECT value, value_lo, value_hi, obs_date FROM observations"
                    " WHERE series_id = ? AND obs_date <= ? ORDER BY obs_date DESC"
                    " LIMIT 1", (p["series_id"], p["target_date"])).fetchone()
                if row is not None:
                    val = row["value"]
                    if val is None and row["value_lo"] is not None and row[
                            "value_hi"] is not None:
                        val = (row["value_lo"] + row["value_hi"]) / 2
                    actual = (row["obs_date"], val)
            pf = ("expected %s" % fmt(p["point_forecast"])
                  if p["point_forecast"] is not None else
                  "P=%s" % fmt(p["p_yes"], 2))
            if actual is not None:
                err = (p["point_forecast"] - actual[1]
                       if p["point_forecast"] is not None else None)
                lines.append("%s %s  %s  actual %s  err %s"
                             % (label, p["target_date"] or "", pf,
                                fmt(actual[1]),
                                fmt(err) if err is not None else "n/a"))
            else:
                lines.append("%s %s  %s  open, no print stored yet"
                             % (label, p["target_date"] or "", pf))
    if not preds:
        lines.append("no prior prediction to score")
    ledger = ("running ledger: %d resolved (%d price-unit, %d probability),"
              " mean price err %s"
              % (resolved_any, resolved["c"], resolved_any - resolved["c"],
                 fmt(resolved["e"]) if resolved["e"] is not None else "n/a"))
    if scored:
        ledger += ", %d/%d point forecasts inside ±1 sd" % (inside, scored)
    lines.append(ledger)
    sections.append(("1) EXPECTATION CHECK", lines))
    s1_series["preds"] = len(preds)

    # ---- 2) NEW SINCE LAST RUN -----------------------------------------
    lines = []
    obs_rows, obs_total = _new_rows(conn, "observations", since, limit=8)
    settle_rows, settle_total = _new_rows(conn, "settlements", since, limit=4)
    quote_rows, quote_total = _new_rows(conn, "quotes", since, limit=6)
    market_rows, market_total = _new_rows(conn, "markets", since, limit=3)
    total_new = obs_total + settle_total + quote_total + market_total
    if since:
        label = "NEW SINCE LAST RUN"
    elif has_prior_run:
        label = "STORE CONTENTS (no --since-last-run window given)"
    else:
        label = "STORE CONTENTS (no prior run)"
    lines.append("rows: observations %d  settlements %d  quotes %d  markets %d"
                 % (obs_total, settle_total, quote_total, market_total))
    for f in fams:
        srow = conn.execute(
            "SELECT * FROM series WHERE family = ? LIMIT 1", (f,)).fetchone()
        if srow is None:
            continue
        obs = series_observations(conn, srow["id"])
        if len(obs) >= 2:
            tail = obs[-5:]
            seq = ", ".join(fmt(o["value"]) for o in tail)
            lines.append("%-12s last %d prints: %s   (%+.4f latest)"
                         % (srow["series_ticker"], len(tail), seq,
                            tail[-1]["value"] - tail[-2]["value"]))
        elif obs:
            lines.append("%-12s only one print stored: %s"
                         % (srow["series_ticker"], fmt(obs[0]["value"])))
    for r in settle_rows:
        srow = conn.execute("SELECT series_ticker FROM series WHERE id = ?",
                            (r["series_id"],)).fetchone()
        lines.append("%-12s %s  (%s, %s]  n=%s quoted=%s %s"
                     % (srow["series_ticker"] if srow else "?", r["event_ticker"],
                        fmt(r["band_lo"], 3), fmt(r["band_hi"], 3),
                        r["n_strikes"], r["n_quoted"], r["status"]))
    if quote_rows:
        lines.append("quotes newest: %s @ -%.2fh = %s"
                     % (quote_rows[0]["market_ticker"],
                        quote_rows[0]["hours_before_close"] or 0,
                        fmt(quote_rows[0]["close_dollars"], 2)))
    if not total_new and since:
        lines.append("nothing new since the last run")
    sections.append(("2) %s%s" % (label,
                                  " (%d rows)" % total_new if total_new else ""),
                    lines))

    # ---- 3) MODEL DRIFT -------------------------------------------------
    lines = []
    groups = conn.execute(
        "SELECT mo.series_id, mo.model_name, s.series_ticker,"
        " COUNT(*) n FROM models mo JOIN series s ON s.id = mo.series_id"
        " GROUP BY mo.series_id, mo.model_name ORDER BY mo.model_name").fetchall()
    for g in groups:
        fits = conn.execute(
            "SELECT * FROM models WHERE series_id = ? AND model_name = ?"
            " ORDER BY fit_date DESC LIMIT 2",
            (g["series_id"], g["model_name"])).fetchall()
        if len(fits) < 2:
            f0 = fits[0]
            lines.append("%-14s %s n=%s resid_sd=%s (single fit, no drift yet)"
                         % (f0["model_name"], f0["fit_date"], f0["n_obs"],
                            fmt(f0["resid_sd"], 5)))
            continue
        new, old = fits[0], fits[1]
        bit = "%-14s resid_sd %s -> %s" % (
            new["model_name"], fmt(old["resid_sd"], 5), fmt(new["resid_sd"], 5))
        if old["resid_sd"] and new["resid_sd"]:
            bit += " (%+.1f%%)" % (100 * (new["resid_sd"] / old["resid_sd"] - 1))
        bit += "   R2 %s -> %s   n %s -> %s" % (
            fmt(old["r2"], 3), fmt(new["r2"], 3), old["n_obs"], new["n_obs"])
        lines.append(bit)
        diffs = _diff_params(json.loads(old["params"]), json.loads(new["params"]),
                             limit=2)
        for d in diffs:
            lines.append("                %-28s %s -> %s"
                         % (d["param"], fmt(d["from"], 5), fmt(d["to"], 5)))
    if not lines:
        lines.append("no fitted models yet")
    sections.append(("3) MODEL DRIFT", lines))

    # ---- 4) OPEN MARKET DELTA ------------------------------------------
    lines = []
    events = conn.execute(
        "SELECT m.event_ticker, MIN(m.close_time) close_time,"
        " MIN(m.close_ts) close_ts, COUNT(*) n,"
        " (SELECT COUNT(*) FROM quotes q JOIN markets m2"
        "   ON m2.market_ticker = q.market_ticker"
        "   WHERE m2.event_ticker = m.event_ticker) quoted"
        " FROM markets m WHERE m.close_ts > ? GROUP BY m.event_ticker"
        " ORDER BY m.close_ts LIMIT 4", (now_ts,)).fetchall()
    for ev in events:
        lines.append("%-24s close %s   %d strikes, %d quoted"
                     % (ev["event_ticker"], ev["close_time"], ev["n"],
                        ev["quoted"]))
        movers = []
        for m in conn.execute(
                "SELECT market_ticker, floor_strike FROM markets"
                " WHERE event_ticker = ? ORDER BY floor_strike", (
                    ev["event_ticker"],)):
            qs = conn.execute(
                "SELECT close_dollars, fetched_at, hours_before_close FROM quotes"
                " WHERE market_ticker = ? ORDER BY fetched_at DESC LIMIT 2",
                (m["market_ticker"],)).fetchall()
            if len(qs) >= 2 and qs[0]["close_dollars"] is not None and qs[1][
                    "close_dollars"] is not None:
                mv = abs(qs[0]["close_dollars"] - qs[1]["close_dollars"])
                if mv >= 0.05:
                    movers.append("%s %s->%s" % (
                        m["market_ticker"].rsplit("-", 1)[-1],
                        fmt(qs[1]["close_dollars"], 2),
                        fmt(qs[0]["close_dollars"], 2)))
        if movers:
            lines.append("   moved > 5 pts: " + ", ".join(movers))
    if not lines:
        lines.append("no open ladders stored")
    sections.append(("4) OPEN MARKET DELTA", lines))

    # ---- 5) UNRESOLVED / DUE -------------------------------------------
    lines = []
    for p in conn.execute(
            "SELECT * FROM predictions WHERE resolved_at IS NULL ORDER BY"
            " COALESCE(target_date,'') LIMIT 6"):
        lines.append("%s %s  P=%s  pending" % (
            p["market_ticker"] or p["event_ticker"] or "?", p["target_date"] or "",
            fmt(p["p_yes"], 2)))
    for ev in conn.execute(
            "SELECT event_ticker, MIN(close_ts) close_ts, COUNT(*) n FROM markets"
            " WHERE close_ts > ? GROUP BY event_ticker ORDER BY close_ts LIMIT 4",
            (now_ts,)):
        lines.append("%s settles %s (%s)" % (
            ev["event_ticker"], iso(ev["close_ts"]), fmt(
                (ev["close_ts"] - now_ts) / 3600.0, 1) + "h"))
    if not lines:
        lines.append("nothing unsettled and nothing pending")
    sections.append(("5) UNRESOLVED / DUE", lines))

    return sections, total_new


def build_digest(conn, family=None, since=None, max_lines=60, since_last_run=False,
                 roll_run=False):
    now_ts = int(time.time())
    rev = get_rev(conn)
    stale = find_stale(conn, now_ts)
    if roll_run:
        # The digest is the run boundary: close the previous run so the
        # "since last run" window is real, then open this run.
        close_open_run(conn, outcome="ok")
    last_run = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC"
        " LIMIT 1").fetchone()
    open_run = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if since_last_run and since is None:
        if last_run is not None:
            since = last_run["finished_at"] or last_run["started_at"]
    sections, total_new = digest_sections(conn, family=family, since=since,
                                          has_prior_run=last_run is not None)

    head = []
    if last_run is None:
        last_txt = "last run: none"
    else:
        age_h = (now_ts - (parse_ts(last_run["finished_at"]) or now_ts)) / 3600.0
        last_txt = "last run: #%d %s %s (%.1fh ago)" % (
            last_run["id"], last_run["outcome"] or "?", last_run["started_at"],
            age_h)
    rev_delta = (rev - (last_run["store_rev"] or 0)) if last_run is not None else rev
    fams = [r["family"] for r in conn.execute(
        "SELECT DISTINCT family FROM series ORDER BY family")]
    head.append("KALSHI RESEARCH CONTINUITY   generated %s   %s"
                % (now_iso(), last_txt))
    head.append("store rev %d (%+d)   stale artifacts: %d   families: %s"
                % (rev, rev_delta, len(stale), " ".join(fams) or "(none)"))
    if since_last_run and since:
        head.append("new-since window: fetched_at > %s" % since)
    if open_run is not None:
        head.append("current run: #%d open, started %s"
                    % (open_run["id"], open_run["started_at"]))

    lines = list(head) + [""]
    caps = list(DIGEST_CAPS)
    budget = max(5, max_lines - len(lines))
    caps = [min(c, len(s[1])) for c, s in zip(caps, sections)]
    total = sum(c + 1 for c in caps)
    trim_order = [4, 3, 2, 1, 0]
    cursor = 0
    while total > budget:
        idx = trim_order[cursor % len(trim_order)]
        if caps[idx] > 0 and not (idx == 0 and total <= 6):
            caps[idx] -= 1
            total -= 1
            cursor += 1
            continue
        cursor += 1
        if cursor > 4 * (len(sections) + 1) * max(caps) + 20:
            break
    rendered = []
    for i, (title, slines) in enumerate(sections):
        rendered.append(title)
        body = slines[:caps[i]]
        rendered.extend("   " + b for b in body)
        if caps[i] < len(slines):
            rendered.append("   … %d more (truncated oldest-first)"
                            % (len(slines) - caps[i]))
    lines.extend(rendered)
    lines = lines[:max_lines]

    digest_text = "\n".join(lines)
    digest_hash = hashlib.sha256(digest_text.encode("utf-8")).hexdigest()[:16]
    payload = {
        "command": "digest", "generated_at": now_iso(),
        "since_last_run": bool(since_last_run), "since": since,
        "family": family, "max_lines": max_lines, "line_count": len(lines),
        "store_rev": rev, "stale_count": len(stale),
        "new_rows": total_new,
        "last_run": dict(last_run) if last_run is not None else None,
        "open_run": dict(open_run) if open_run is not None else None,
        "digest_hash": digest_hash,
        "sections": [{"title": t, "lines": l} for t, l in sections],
        "digest": digest_text,
    }
    return payload


def cmd_digest(conn, args):
    payload = build_digest(conn, family=args.family, max_lines=args.max_lines,
                           since_last_run=args.since_last_run, roll_run=True)
    run_id = begin_run(conn, families=None, prompt=None)
    payload["run_id"] = run_id
    set_meta(conn, DIGEST_KEY, payload["digest_hash"],
             n_rows=payload["line_count"])
    conn.commit()

    def text(p):
        print(p["digest"])

    emit(payload, args.json, text)
    return payload


# --------------------------------------------------------------------------
# predictions
# --------------------------------------------------------------------------

def record_prediction(conn, args):
    with open(os.path.expanduser(args.file)) as fh:
        data = json.load(fh)
    items = data if isinstance(data, list) else [data]
    run_id = begin_run(conn, families=[i.get("family") for i in items
                                       if i.get("family")])
    written = []
    for it in items:
        if it.get("p_yes") is None:
            raise SystemExit("prediction requires p_yes")
        sid = None
        if it.get("family"):
            srow = resolve_series(conn, it["family"], create=False)
            sid = srow["id"] if srow else None
        edge = it.get("edge_points")
        if edge is None and it.get("market_price") is not None:
            edge = it["p_yes"] - it["market_price"]
        cur = conn.execute(
            "INSERT INTO predictions(run_id, market_ticker, series_id,"
            " event_ticker, target_date, p_yes, market_price, edge_points,"
            " direction, point_forecast, forecast_sd, rationale)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, it.get("market_ticker"), sid, it.get("event_ticker"),
             it.get("target_date"), it["p_yes"], it.get("market_price"), edge,
             it.get("direction"), it.get("point_forecast"),
             it.get("forecast_sd"), it.get("rationale")))
        written.append({"id": cur.lastrowid,
                        "market_ticker": it.get("market_ticker"),
                        "target_date": it.get("target_date"),
                        "p_yes": it["p_yes"], "edge_points": edge})
    touch_run(conn, run_id)
    conn.commit()
    return {"command": "record-prediction", "run_id": run_id,
            "written": written, "count": len(written)}


def resolve_predictions(conn, args):
    """Score ripe predictions.

    `error` is scored in the units of `forecast_sd` (design §1c).  When a
    prediction carries a point forecast AND its series has a stored print dated
    exactly `target_date`, the error is in the series' own units (price) and the
    outcome is the print's direction.  A print for a *different* date is not a
    substitute: with no print on the target date the prediction is left
    unresolved and counted in `still_unresolvable`.  Otherwise a market-ticker
    prediction is scored in probability units against the settled result, and
    `units` says which happened — a probability error must never be z-scored
    against a price sd, so the units are reported explicitly.
    """
    as_of = args.as_of or day_of(now_iso())
    rows = conn.execute(
        "SELECT * FROM predictions WHERE resolved_at IS NULL AND target_date IS"
        " NOT NULL AND target_date <= ?", (as_of,)).fetchall()
    now = now_iso()
    resolved, skipped = [], 0
    for p in rows:
        outcome = None
        error = None
        units = None
        # Prefer the point-forecast path: that is the one forecast_sd describes.
        # It scores ONLY against a print dated exactly `target_date`.  When the
        # series has no print for that date the prediction stays unresolved:
        # scoring a 9/30 forecast against an 8/31 print produces a plausible
        # error that measures nothing, and a stale-but-wrong number is worse
        # than an honest "not yet resolvable".
        if p["series_id"] is not None and p["point_forecast"] is not None:
            obs = series_observations(conn, p["series_id"])
            i = _obs_at(obs, p["target_date"])
            if i is not None:
                cur = obs[i]
                prev = obs[i - 1] if i > 0 else None
                error = p["point_forecast"] - cur["value"]
                units = "price"
                if prev is None:
                    outcome = "flat"
                elif cur["value"] > prev["value"]:
                    outcome = "up"
                elif cur["value"] < prev["value"]:
                    outcome = "down"
                else:
                    outcome = "flat"
        if error is None and p["market_ticker"]:
            m = conn.execute("SELECT result FROM markets WHERE market_ticker = ?",
                             (p["market_ticker"],)).fetchone()
            if m is None or m["result"] not in ("yes", "no"):
                skipped += 1
                continue
            outcome = m["result"]
            error = p["p_yes"] - (1.0 if outcome == "yes" else 0.0)
            units = "probability"
        if error is None:
            skipped += 1
            continue
        conn.execute(
            "UPDATE predictions SET resolved_at = ?, outcome = ?, error = ?"
            " WHERE id = ?", (now, outcome, error, p["id"]))
        resolved.append({"id": p["id"], "target_date": p["target_date"],
                         "outcome": outcome, "error": error, "units": units,
                         "p_yes": p["p_yes"],
                         "point_forecast": p["point_forecast"]})
    conn.commit()
    return {"command": "resolve-predictions", "as_of": as_of,
            "resolved": resolved, "resolved_count": len(resolved),
            "still_unresolvable": skipped}


# --------------------------------------------------------------------------
# series
# --------------------------------------------------------------------------

def cmd_series(conn, args):
    srow = resolve_series(conn, args.family, create=False)
    if srow is None:
        raise SystemExit("unknown family %s" % args.family)
    rows = conn.execute(
        "SELECT obs_date, value, value_lo, value_hi, unit, source, quality,"
        " asof, fetched_at FROM observations WHERE series_id = ?"
        " ORDER BY obs_date DESC LIMIT ?", (srow["id"], args.tail)).fetchall()
    items = []
    for r in rows:
        val = r["value"]
        mid = None
        if val is None and r["value_lo"] is not None and r["value_hi"] is not None:
            mid = (r["value_lo"] + r["value_hi"]) / 2
        items.append({"obs_date": r["obs_date"], "value": val, "mid": mid,
                      "value_lo": r["value_lo"], "value_hi": r["value_hi"],
                      "source": r["source"], "quality": r["quality"],
                      "asof": r["asof"], "fetched_at": r["fetched_at"]})
    items.reverse()
    full = series_observations(conn, srow["id"])
    units = [r["unit"] for r in conn.execute(
        "SELECT unit FROM observations WHERE series_id = ? AND unit IS NOT NULL"
        " LIMIT 1", (srow["id"],)).fetchall()]
    payload = {"command": "series", "family": args.family,
               "series_ticker": srow["series_ticker"], "kind": srow["kind"],
               "unit": units[0] if units else None,
               "n_observations": len(full),
               "tail": args.tail, "rows": items}

    def text(p):
        print("series %s (%s) — %d observations, tail %d"
              % (p["series_ticker"], p["kind"], p["n_observations"], p["tail"]))
        prev = None
        for it in p["rows"]:
            v = it["value"] if it["value"] is not None else it["mid"]
            chg = ""
            if prev is not None and v is not None:
                chg = "  chg %+.4f" % (v - prev)
            prev = v if v is not None else prev
            band = ("band (%s, %s]" % (fmt(it["value_lo"], 3),
                                       fmt(it["value_hi"], 3))
                    if it["value_lo"] is not None else
                    "value %s" % fmt(it["value"]))
            print("  %s  %-26s %-12s %s%s"
                  % (it["obs_date"], band, it["source"], it["quality"], chg))

    emit(payload, args.json, text)
    return payload


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_common(sp):
    sp.add_argument("--db", default=os.environ.get("KALSHI_RESEARCH_DB", DEFAULT_DB),
                    help="path to the store (default: profile root)")
    sp.add_argument("--json", action="store_true", help="emit JSON on stdout")


def build_parser():
    p = argparse.ArgumentParser(
        prog="research_store.py",
        description="Kalshi research continuity store (read-only, stdlib only).")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create/patch the store schema")
    add_common(sp)
    sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("ingest-settled", help="ingest a family's settled ladder")
    add_common(sp)
    sp.add_argument("--family", required=True)
    sp.add_argument("--since", default=None, help="YYYY-MM-DD (default: 30 days)")
    sp.add_argument("--kind", default=None, choices=list(KINDS))
    sp.add_argument("--unit", default=None)
    sp.add_argument("--settlement-tz", default=None)
    sp.add_argument("--close-hhmm", default=None)
    sp.add_argument("--strike-step", type=float, default=None)
    sp.add_argument("--rebuild", action="store_true",
                    help="re-fetch events already stored as finalized")
    sp.add_argument("--no-open", action="store_true",
                    help="skip the currently open ladder")

    sp = sub.add_parser("ingest-closes", help="ingest daily closes (Yahoo)")
    add_common(sp)
    sp.add_argument("--symbol", action="append", required=True)
    sp.add_argument("--since", default=None, help="YYYY-MM-DD (default: 180 days)")
    sp.add_argument("--unit", default=None)
    sp.add_argument("--family", default=None,
                    help="override the family label (default: the symbol)")

    sp = sub.add_parser("ingest-quotes", help="sample market-implied quotes")
    add_common(sp)
    sp.add_argument("--family", required=True)
    sp.add_argument("--backfill", type=int, default=None,
                    help="fixed window: the N most recent settled events"
                         " (default 1); deeper back-fill = raise N or use"
                         " --since")
    sp.add_argument("--since", default=None,
                    help="only settled events closing on/after YYYY-MM-DD")
    sp.add_argument("--offset-hours", type=float, default=3.0)

    sp = sub.add_parser("fit", help="fit a DERIVED model and persist its params")
    add_common(sp)
    sp.add_argument("--family", required=True)
    sp.add_argument("--model", required=True,
                    help="model NAME (opaque, e.g. diesel_ols)")
    sp.add_argument("--estimator", default=None, choices=list(ESTIMATORS),
                    help="generic estimator to run (default: the model name)")
    sp.add_argument("--exog", default=None, help="exogenous series ticker")
    sp.add_argument("--lags", default=None, help="e.g. 0:4")
    sp.add_argument("--as-of", default=None,
                    help="fit using only obs_date <= DATE (allows a refit delta)")
    sp.add_argument("--threshold", type=float, default=0.0)
    sp.add_argument("--bucket-step", type=float, default=0.01)
    sp.add_argument("--bands", type=int, default=8)

    sp = sub.add_parser("digest", help="what changed since last run")
    add_common(sp)
    sp.add_argument("--since-last-run", action="store_true")
    sp.add_argument("--family", default=None)
    sp.add_argument("--max-lines", type=int, default=60)

    sp = sub.add_parser("stale", help="artifacts violating a freshness rule")
    add_common(sp)

    sp = sub.add_parser("record-prediction", help="write the run's expectation")
    add_common(sp)
    sp.add_argument("--file", required=True)

    sp = sub.add_parser("resolve-predictions", help="score ripe predictions")
    add_common(sp)
    sp.add_argument("--as-of", default=None)

    sp = sub.add_parser("series", help="tail a family's observation series")
    add_common(sp)
    sp.add_argument("--family", required=True)
    sp.add_argument("--tail", type=int, default=20)

    return p


def cmd_init(args):
    path = os.path.expanduser(args.db)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    conn = connect(path)
    init_db(conn)
    counts = table_counts(conn)
    rev = bump_rev(conn, 0, note="init")
    conn.commit()
    payload = {"command": "init", "db": path, "schema_version": SCHEMA_VERSION,
               "tables": list(TABLE_NAMES), "counts": counts,
               "store_rev": rev, "created_at": now_iso()}
    conn.close()

    def text(p):
        print("store: %s" % p["db"])
        print("schema_version %d, %d tables: %s"
              % (p["schema_version"], len(p["tables"]), " ".join(p["tables"])))
        print("store rev %d" % p["store_rev"])

    emit(payload, args.json, text)
    return payload


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "init":
        cmd_init(args)
        return 0

    conn = open_store(args)
    try:
        if args.command == "ingest-settled":
            payload = ingest_settled(conn, args)
        elif args.command == "ingest-closes":
            payload = ingest_closes(conn, args)
        elif args.command == "ingest-quotes":
            payload = ingest_quotes(conn, args)
        elif args.command == "fit":
            payload = fit(conn, args)
        elif args.command == "digest":
            payload = cmd_digest(conn, args)
            return 0
        elif args.command == "stale":
            cmd_stale(conn, args)
            return 0
        elif args.command == "record-prediction":
            payload = record_prediction(conn, args)
        elif args.command == "resolve-predictions":
            payload = resolve_predictions(conn, args)
        elif args.command == "series":
            cmd_series(conn, args)
            return 0
        else:
            raise SystemExit("unknown command %s" % args.command)

        def text(p):
            print(json.dumps(p, indent=2, sort_keys=True, default=str))

        emit(payload, args.json, text)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
