#!/usr/bin/env python3
"""Kalshi CLI helper — query prediction market data.

Read-only. Every command in this script uses Kalshi's public endpoints,
which require no authentication and no API key.

Usage:
    python3 kalshi.py status
    python3 kalshi.py categories
    python3 kalshi.py trending [--limit 10] [--category Crypto]
    python3 kalshi.py markets [--series TICKER] [--limit 10] [--status open]
    python3 kalshi.py market <ticker>
    python3 kalshi.py event <event_ticker>
    python3 kalshi.py series <series_ticker>
    python3 kalshi.py book <ticker> [--depth 10]
    python3 kalshi.py history <ticker> [--interval 1w] [--period 60]
    python3 kalshi.py trades [--ticker TICKER] [--limit 10]
    python3 kalshi.py search <query> [--limit 5]

Environment:
    KALSHI_API_BASE   Override the API base URL (e.g. the demo environment
                      https://demo-api.kalshi.co/trade-api/v2). Defaults to
                      production.
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# The docs' primary host. Both are the same production exchange.
BASE = os.environ.get("KALSHI_API_BASE", DEFAULT_BASE)

# /series is a single unfiltered dump of every series on the exchange (~18MB).
# Cache it briefly so repeated search/trending calls don't re-download it.
SERIES_CACHE_TTL = 600
SERIES_CACHE = os.path.join(
    tempfile.gettempdir(), "hermes_kalshi_series_cache.json"
)

INTERVALS = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "all": 3650,
}


def _get(path: str, **params):
    """GET a Kalshi endpoint and return parsed JSON."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        print(f"HTTP {e.code}: {e.reason} — {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _num(val, default=0.0) -> float:
    """Kalshi returns fixed-point values as strings ('12.34'); coerce safely."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _price(val) -> str:
    """Format a dollar price (0.00-1.00) as a percentage.

    Kalshi prices are dollar amounts per contract and double as probabilities:
    0.11 means the market implies 11%.
    """
    if val is None or val == "":
        return "n/a"
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(val)


def _vol(val) -> str:
    """Format a contract count (volume / open interest)."""
    v = _num(val)
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:,.0f}"


def _ago(ts) -> str:
    """Relative time from an ISO timestamp."""
    if not ts:
        return "?"
    try:
        from datetime import datetime, timezone

        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        delta = time.time() - dt.timestamp()
    except (ValueError, TypeError):
        return str(ts)
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    if delta < 172800:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _mid(m: dict):
    """Best bid/ask for yes. Returns (bid, ask) as floats or None.

    Kalshi quotes yes_bid/yes_ask directly on the market object; the spread is
    the round-trip cost. A market with no quotes (empty book) returns None.
    """
    bid = m.get("yes_bid_dollars")
    ask = m.get("yes_ask_dollars")
    b = _num(bid, None) if bid not in (None, "") else None
    a = _num(ask, None) if ask not in (None, "") else None
    return (b, a)


def _market_title(m: dict) -> str:
    """Market question text.

    'title' is marked deprecated in the OpenAPI spec but is still the only
    market-level descriptive string and is still populated, so prefer it and
    fall back through the side-specific shortened titles.
    """
    return m.get("title") or m.get("yes_sub_title") or m.get("ticker") or "?"


def _print_market_line(m: dict, indent: str = "") -> None:
    """One-line market summary with yes bid/ask, spread, and volume."""
    bid, ask = _mid(m)
    if bid is not None and ask is not None:
        quoted = f"Yes {_price(bid)} / {_price(ask)}"
        if bid > 0 and ask > 0 and ask >= bid:
            quoted += f" (spread {(ask - bid) * 100:.1f}pts)"
    else:
        quoted = "no quotes"
    status = m.get("status", "?")
    flag = "" if status == "active" else f" [{status.upper()}]"
    print(f"{indent}{_market_title(m)}{flag}")
    print(
        f"{indent}  {quoted}  |  24h vol: {_vol(m.get('volume_24h_fp'))}"
        f"  |  open interest: {_vol(m.get('open_interest_fp'))}"
    )
    print(f"{indent}  ticker: {m.get('ticker', '')}")


def load_series(force: bool = False) -> list:
    """All series, with a short-lived cache (the endpoint has no pagination)."""
    if not force and os.path.exists(SERIES_CACHE):
        age = time.time() - os.path.getmtime(SERIES_CACHE)
        if age < SERIES_CACHE_TTL:
            try:
                with open(SERIES_CACHE) as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
    data = _get("/series", include_volume="true")
    series = data.get("series", [])
    try:
        with open(SERIES_CACHE, "w") as fh:
            json.dump(series, fh)
    except OSError:
        pass
    return series


def cmd_status():
    """Exchange status and trading state."""
    st = _get("/exchange/status")
    print(f"Exchange active: {st.get('exchange_active')}")
    print(f"Trading active:  {st.get('trading_active')}")
    print(
        "Transfers active: "
        f"{st.get('intra_exchange_transfers_active')}"
    )
    for idx in st.get("exchange_index_statuses", []):
        print(
            f"  shard {idx.get('exchange_index')} ({idx.get('description')}): "
            f"trading={idx.get('trading_active')} exchange={idx.get('exchange_active')}"
        )


def cmd_categories():
    """List categories and their tags."""
    data = _get("/search/tags_by_categories")
    cats = data.get("tags_by_categories", {})
    print(f"{len(cats)} categories:\n")
    for cat, tags in cats.items():
        if tags:
            print(f"  {cat}")
            print(f"    tags: {', '.join(tags)}")
        else:
            print(f"  {cat}  (no tags)")


def cmd_trending(limit: int = 10, category: str = None):
    """Top series by traded volume. Kalshi has no server-side volume sort."""
    if category:
        data = _get("/series", category=category, include_volume="true")
        series = data.get("series", [])
        label = f"category '{category}'"
    else:
        series = load_series()
        label = "all categories"

    ranked = sorted(series, key=lambda s: -_num(s.get("volume_fp")))
    ranked = ranked[:limit]
    print(f"Top {len(ranked)} series by volume ({label}):\n")
    for i, s in enumerate(ranked, 1):
        print(f"{i}. {s.get('title', '?')}")
        print(
            f"   volume: {_vol(s.get('volume_fp'))} contracts"
            f"  |  series: {s.get('ticker')}"
            f"  |  category: {s.get('category') or ','.join(s.get('categories') or [])}"
        )
        print()


def cmd_markets(series: str = None, limit: int = 10, status: str = "open"):
    """List markets, optionally filtered to one series."""
    params = {
        "limit": min(limit, 1000),
        "status": status,
        "mve_filter": "exclude",
    }
    if series:
        params["series_ticker"] = series
    data = _get("/markets", **params)
    markets = data.get("markets", [])
    header = f"markets in {series}" if series else "markets"
    print(f"{len(markets)} {header} (status={status}):\n")
    for m in markets:
        _print_market_line(m)
        print()


def cmd_market(ticker: str):
    """Full detail for one market."""
    m = _get(f"/markets/{urllib.parse.quote(ticker)}").get("market", {})
    if not m:
        print(f"No market found: {ticker}")
        return
    bid, ask = _mid(m)
    print(f"Market: {_market_title(m)}")
    print(f"Ticker: {m.get('ticker')}   Event: {m.get('event_ticker')}")
    print(f"Status: {m.get('status')}   Type: {m.get('market_type')}")
    print()
    print(f"  Yes bid:  {_price(bid)}")
    print(f"  Yes ask:  {_price(ask)}")
    if bid is not None and ask is not None and ask >= bid:
        print(f"  Spread:   {(ask - bid) * 100:.1f} points")
    print(f"  Last trade: {_price(m.get('last_price_dollars'))}")
    print(f"  Previous:   {_price(m.get('previous_price_dollars'))}")
    print()
    print(f"  Volume (all time): {_vol(m.get('volume_fp'))} contracts")
    print(f"  Volume (24h):      {_vol(m.get('volume_24h_fp'))} contracts")
    print(f"  Open interest:     {_vol(m.get('open_interest_fp'))} contracts")
    print()
    # 'liquidity_dollars' is deprecated and always returns 0.0000 — omitted on
    # purpose. 'expiration_time' is deprecated; the two live fields are these.
    print(f"  Open:            {m.get('open_time')}")
    print(f"  Close:           {m.get('close_time')}")
    print(f"  Expected expiry: {m.get('expected_expiration_time')}")
    print(f"  Latest expiry:   {m.get('latest_expiration_time')}")
    if m.get("result"):
        print(f"  Result: {m.get('result')}")
    if m.get("strike_type"):
        print(f"  Strike: {m.get('strike_type')} {m.get('floor_strike', '')}")
    if m.get("yes_sub_title"):
        print(f"  Yes means: {m.get('yes_sub_title')}")
    if m.get("no_sub_title"):
        print(f"  No means:  {m.get('no_sub_title')}")
    rules = m.get("rules_primary") or ""
    if rules:
        print(f"\n  Rules: {rules[:600]}")


def cmd_event(event_ticker: str, with_markets: bool = True):
    """Event detail plus its markets."""
    ev = _get(
        f"/events/{urllib.parse.quote(event_ticker)}",
        with_nested_markets="true" if with_markets else "false",
    ).get("event", {})
    if not ev:
        print(f"No event found: {event_ticker}")
        return
    print(f"Event: {ev.get('title', '?')}")
    print(f"Ticker: {ev.get('event_ticker')}   Series: {ev.get('series_ticker')}")
    print(f"Category: {ev.get('category')}")
    if ev.get("mutually_exclusive"):
        print("Mutually exclusive: yes (markets here are competing outcomes)")
    if ev.get("sub_title"):
        print(f"Subtitle: {ev.get('sub_title')}")
    sources = ev.get("settlement_sources") or []
    if sources:
        names = ", ".join(s.get("name", "")[:40] for s in sources[:6])
        print(f"Settlement sources: {names}")
    markets = ev.get("markets", [])
    print(f"\nMarkets: {len(markets)}\n")
    for m in markets:
        _print_market_line(m, indent="  ")
        print()


def cmd_series(series_ticker: str):
    """Series detail (the recurring market family)."""
    s = _get(
        f"/series/{urllib.parse.quote(series_ticker)}", include_volume="true"
    ).get("series", {})
    if not s:
        print(f"No series found: {series_ticker}")
        return
    print(f"Series: {s.get('title', '?')}")
    print(f"Ticker: {s.get('ticker')}")
    print(f"Category: {s.get('category') or ','.join(s.get('categories') or [])}")
    print(f"Frequency: {s.get('frequency')}")
    print(f"Volume: {_vol(s.get('volume_fp'))} contracts")
    if s.get("tags"):
        print(f"Tags: {', '.join(s['tags'])}")
    if s.get("contract_url"):
        print(f"Contract: {s.get('contract_url')}")


def cmd_book(ticker: str, depth: int = 10):
    """Orderbook ladder.

    Kalshi returns bids for BOTH sides and no asks: buying NO at 0.89 is the
    same trade as selling YES at 0.11, so implied asks are derived as
    1 - (opposite best bid).
    """
    data = _get(
        f"/markets/{urllib.parse.quote(ticker)}/orderbook", depth=depth
    )
    book = data.get("orderbook_fp") or {}
    yes_bids = [(float(p), float(c)) for p, c in book.get("yes_dollars", [])]
    no_bids = [(float(p), float(c)) for p, c in book.get("no_dollars", [])]

    if not yes_bids and not no_bids:
        print(f"Orderbook for {ticker} is empty (no resting orders).")
        return

    yes_bids.sort(key=lambda x: -x[0])
    no_bids.sort(key=lambda x: -x[0])

    print(f"Orderbook: {ticker}\n")
    best_yes_bid = yes_bids[0][0] if yes_bids else None
    best_yes_ask = (1 - no_bids[0][0]) if no_bids else None
    if best_yes_bid is not None and best_yes_ask is not None:
        print(
            f"  Best yes bid {_price(best_yes_bid)}  |  "
            f"Best yes ask {_price(best_yes_ask)}  |  "
            f"Spread {(best_yes_ask - best_yes_bid) * 100:.1f} points"
        )
        if best_yes_ask < best_yes_bid:
            print("  (crossed book — usually a stale resting order)")
    print()

    print("  YES bids (buy YES):")
    for price, size in yes_bids[:depth]:
        print(f"    {_price(price):>7}  |  {size:>12,.2f} contracts")
    if not yes_bids:
        print("    (none)")

    print("\n  YES ask, implied from NO bids (buy NO = sell YES):")
    implied = sorted(
        ((1 - p, c) for p, c in no_bids if 0 <= p <= 1), key=lambda x: x[0]
    )
    for price, size in implied[:depth]:
        print(f"    {_price(price):>7}  |  {size:>12,.2f} contracts")
    if not implied:
        print("    (none)")

    print("\n  NO bids (buy NO directly):")
    for price, size in no_bids[:depth]:
        print(f"    {_price(price):>7}  |  {size:>12,.2f} contracts")
    if not no_bids:
        print("    (none)")


def cmd_history(ticker: str, interval: str = "1w", period: int = 60):
    """Candlestick price history.

    Requires the SERIES ticker as well as the market ticker, so resolve the
    market's event first. Only periods with data are returned.
    """
    market = _get(f"/markets/{urllib.parse.quote(ticker)}").get("market", {})
    if not market:
        print(f"No market found: {ticker}")
        return
    event_ticker = market.get("event_ticker")
    event = _get(f"/events/{urllib.parse.quote(event_ticker)}").get("event", {})
    series = event.get("series_ticker")
    if not series:
        print(f"Could not resolve a series ticker via event {event_ticker}", file=sys.stderr)
        sys.exit(1)

    days = INTERVALS.get(interval, 7)
    now = int(time.time())
    data = _get(
        f"/series/{urllib.parse.quote(series)}/markets/{urllib.parse.quote(ticker)}/candlesticks",
        start_ts=now - days * 86400,
        end_ts=now,
        period_interval=period,
    )
    candles = data.get("candlesticks", [])
    if not candles:
        print(f"No price history for {ticker} over interval={interval}.")
        print("(Newly listed markets have no closed periods yet.)")
        return

    print(f"Price history: {_market_title(market)}")
    print(f"Series {series} | interval={interval} | period={period}m | {len(candles)} points\n")
    from datetime import datetime, timezone

    for c in candles:
        ts = c.get("end_period_ts")
        when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        vol = _num(c.get("volume_fp"))
        bar = "█" * int(round(_num(bid) * 40))
        print(
            f"  {when}  bid {_price(bid):>7}  ask {_price(ask):>7}"
            f"  vol {vol:>10,.0f}  {bar}"
        )


def cmd_trades(limit: int = 10, ticker: str = None):
    """Recent trades across the exchange, or for one market."""
    params = {"limit": min(limit, 1000)}
    if ticker:
        params["ticker"] = ticker
    data = _get("/markets/trades", **params)
    trades = data.get("trades", [])
    if not trades:
        print("No recent trades.")
        return
    scope = ticker or "all markets"
    print(f"Recent trades ({len(trades)}, {scope}):\n")
    for t in trades:
        side = (t.get("taker_outcome_side") or "?").upper()
        price = _price(t.get("yes_price_dollars"))
        count = _num(t.get("count_fp"))
        block = " [BLOCK]" if t.get("is_block_trade") else ""
        print(
            f"  {side:3} {price:>7}  x{count:>10,.2f}"
            f"  {_ago(t.get('created_time')):>10}{block}"
        )
        print(f"      {t.get('ticker', '')}")


def cmd_search(query: str, limit: int = 5):
    """Search series titles.

    Kalshi exposes no keyless text-search endpoint, so this matches against the
    series list (cached) and then pulls the live markets for each match.
    """
    q = query.lower()
    series = load_series()
    hits = [s for s in series if q in (s.get("title") or "").lower()]
    if not hits:
        hits = [
            s
            for s in series
            if q in (s.get("ticker") or "").lower()
            or q in (s.get("category") or "").lower()
            or any(q in (t or "").lower() for t in (s.get("tags") or []))
        ]
    if not hits:
        print(f'No series matched "{query}".')
        return
    hits.sort(key=lambda s: -_num(s.get("volume_fp")))
    hits = hits[:limit]
    print(f'{len(hits)} series matching "{query}":\n')
    for s in hits:
        print(f"=== {s.get('title', '?')} ===")
        print(
            f"  series: {s.get('ticker')}  |  volume: {_vol(s.get('volume_fp'))}"
            f"  |  category: {s.get('category') or ','.join(s.get('categories') or [])}"
        )
        data = _get(
            "/markets", series_ticker=s.get("ticker"), status="open", limit=5
        )
        markets = data.get("markets", [])
        if not markets:
            print("  (no open markets)")
        for m in markets[:5]:
            _print_market_line(m, indent="  ")
        print()


def _flag(args: list, name: str, default=None):
    """Read '--name value' from an argv list."""
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return default


def main():
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "status":
        cmd_status()
    elif cmd == "categories":
        cmd_categories()
    elif cmd == "trending":
        cmd_trending(
            limit=int(_flag(rest, "--limit", 10)), category=_flag(rest, "--category")
        )
    elif cmd == "markets":
        cmd_markets(
            series=_flag(rest, "--series"),
            limit=int(_flag(rest, "--limit", 10)),
            status=_flag(rest, "--status", "open"),
        )
    elif cmd == "market" and rest:
        cmd_market(rest[0])
    elif cmd == "event" and rest:
        cmd_event(rest[0])
    elif cmd == "series" and rest:
        cmd_series(rest[0])
    elif cmd == "book" and rest:
        cmd_book(rest[0], depth=int(_flag(rest, "--depth", 10)))
    elif cmd == "history" and rest:
        cmd_history(
            rest[0],
            interval=_flag(rest, "--interval", "1w"),
            period=int(_flag(rest, "--period", 60)),
        )
    elif cmd == "trades":
        cmd_trades(
            limit=int(_flag(rest, "--limit", 10)), ticker=_flag(rest, "--ticker")
        )
    elif cmd == "search" and rest:
        limit = int(_flag(rest, "--limit", 5))
        # Strip flags so '--limit N' is not folded into the query string.
        words = []
        skip = False
        for tok in rest:
            if skip:
                skip = False
                continue
            if tok.startswith("--"):
                skip = True
                continue
            words.append(tok)
        if not words:
            print("Usage: kalshi.py search <query> [--limit N]")
            sys.exit(2)
        cmd_search(" ".join(words), limit=limit)
    else:
        print(f"Unknown or incomplete command: {cmd}")
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
