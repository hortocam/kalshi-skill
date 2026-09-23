---
name: kalshi
description: "Query Kalshi: markets, prices, orderbooks, history."
version: 0.1.0
author: Cameron Horton (hortocam), Hermes Agent
license: MIT
tags: [kalshi, prediction-markets, market-data, trading]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kalshi, prediction-markets, market-data, trading]
    related_skills: []
---

# Kalshi — Prediction Market Data

Query prediction market data from Kalshi, the CFTC-regulated US event-contract exchange.
Every endpoint in this skill is public: no API key, no account, no request signing.
Trading and portfolio endpoints exist but are deliberately out of scope here — they
require RSA-signed authentication.

See `references/api-endpoints.md` for the full endpoint reference with curl examples,
and `references/research-continuity.md` for the research continuity store
(`scripts/research_store.py`) that lets a bot profile accumulate market research
across runs instead of re-deriving it.

## When to Use

- User asks about prediction markets, event odds, or "what are the odds of X?"
- User asks about Kalshi specifically
- User wants market prices, orderbooks, spreads, or price history
- User wants to find the most liquid/active markets on Kalshi
- User wants to monitor or track prediction market movements
- Don't use for: Polymarket (see the `polymarket` skill), or placing/cancelling
  orders and reading account balances (needs signed auth — tell the user it's out
  of scope rather than improvising a signer)

## Key Concepts

- **Series → Event → Market.** A series is a recurring contract family
  (`KXNBAGAME`), an event is one instance (`KXNBAGAME-26OCT20PHINYK`), and a
  market is one tradeable outcome (`...-PHI` = "Philadelphia wins").
- **Prices are dollars and probabilities.** Quoted `0.36`/`0.37` means the market
  implies 36%–37%. A contract settles at $1.00 (yes) or $0.00 (no).
- **Quote fields are `yes_bid_dollars` / `yes_ask_dollars`** on the market object.
  There is no separate probability field, and the legacy cent-denominated fields
  (`yes_bid`, `volume`, …) no longer exist in the schema.
- **`title` and `expiration_time` on a market are formally deprecated** but
  `title` is still the only market-level descriptive string, so read it and fall
  back to `yes_sub_title`. Use `expected_expiration_time` /
  `latest_expiration_time` for timing. **`liquidity_dollars` always returns
  `0.0000`** and is useless — judge liquidity from the orderbook and
  `volume_24h_fp`.
- **Volume and open interest are contract counts, not dollars.** `volume_fp` is
  lifetime, `volume_24h_fp` is rolling 24h, `open_interest_fp` is outstanding.
  They arrive as strings (`"1234.56"`) — parse before formatting.
- **`status` on a market is lowercase** (`active`, `initialized`, `closed`),
  while the API's own `status` *filter* takes `open`/`closed`/`settled`. Same
  word, different values — don't pass one where the other is expected.
- **Kalshi returns bids for both sides and no asks.** Buying NO at 0.89 is the
  same trade as selling YES at 0.11, so the yes ask is derived from the best NO
  bid: `yes_ask = 1 - best_no_bid`.
- **Combo markets (MVE) pollute list results.** They are synthetic multi-leg
  markets with zero volume; the helper excludes them by default. Pass
  `mve_filter=only` to see them deliberately.

## How to Run

```bash
python3 scripts/kalshi.py trending --limit 10
```

The helper takes no arguments beyond its subcommand; its only environment
variable is `KALSHI_API_BASE`, which overrides the production base URL so you can
point at the demo exchange (`https://demo-api.kalshi.co/trade-api/v2`) to test
against an empty book.

## Quick Reference

| Command | Returns |
|---|---|
| `status` | Exchange/shard status and whether trading is active |
| `categories` | All categories with their tags |
| `trending [--limit N] [--category C]` | Series ranked by traded volume |
| `markets [--series T] [--limit N] [--status S]` | Markets, quoted |
| `market <ticker>` | One market: quotes, volume, rules, close time |
| `event <event_ticker>` | Event detail plus every market under it |
| `series <series_ticker>` | Series metadata and lifetime volume |
| `book <ticker> [--depth N]` | Orderbook ladder |
| `history <ticker> [--interval I] [--period P]` | Candlestick price history |
| `trades [--ticker T] [--limit N]` | Recent trades across the exchange or one market |
| `search <query> [--limit N]` | Series matching a title/tag/category, with live markets |

`--interval` accepts `1d, 1w, 1m, 3m, 6m, 1y, all`; `--period` accepts `1`, `60`,
or `1440` (minutes).

## Procedure

Work from discovery to a specific market, deepest-detail last. Each step states
what you should have in hand before moving on.

1. **Find the market family.**
   `kalshi.py search "<topic>"` or `kalshi.py trending --category <C>`.
   *Done when:* you have a series ticker whose title actually matches the topic,
   plus its lifetime volume (sanity-check that the family is liquid).

2. **Pull its open markets.**
   `kalshi.py markets --series <SERIES> --limit 20`.
   *Done when:* you can name the specific market ticker and its yes bid/ask.
   Skip anything at 0%/0% — an unquoted market cannot be traded or reasoned about.

3. **Confirm what the market actually means before interpreting it.**
   `kalshi.py market <ticker>` and read `Yes means` / `Rules`.
   *Done when:* you have read the resolution rule and the close time. Titles are
   abbreviated; the rules are authoritative and are the usual reason a "sure
   thing" is not one.

4. **Check the price is real, not an artifact.**
   `kalshi.py book <ticker> --depth 10`.
   *Done when:* you know the spread and whether meaningful size rests at the
   quote. A 1-point spread on a 36% market is normal; a 20-point spread means the
   "price" is one stale order, not a market.

5. **Get the trend.**
   `kalshi.py history <ticker> --interval 1w --period 60`.
   *Done when:* you can say whether the current price moved or has been flat.
   Empty history means the market is newly listed, not that the feed broke.

6. **Cross-check with flow.**
   `kalshi.py trades --ticker <ticker> --limit 20` (or exchange-wide with no
   `--ticker`).
   *Done when:* you have checked whether recent trades occurred near the current
   quote or far from it.

**Reporting:** quote the market as `"<question>" — yes <bid>%/<ask>% (<series> vol,
close <date>)`. Always include the spread, because it is the round-trip cost of
any position and often exceeds the edge being claimed. If the user asks for a
position size, that needs their account balance, which this skill cannot read.

## Pitfalls

- **`/series` has no pagination and no server-side sort.** It returns every series
  on the exchange in one ~18MB response, so `search` and `trending` do the
  ranking client-side and cache the dump for 10 minutes. First call is slow;
  later ones are instant. Don't "fix" a slow first search by hammering it.
- **There is no text-search endpoint.** `search` matches series titles
  (then tickers, categories, and tags as a fallback). Kalshi titles are short, so
  search a noun ("bitcoin", "hurricanes", "inflation"), not a question.
- **`/markets?status=open` returns `status: "active"` on each market.** Filter
  with `open`; read `active`.
- **Price history needs the series ticker, not just the market ticker.** The
  endpoint is `/series/{series}/markets/{ticker}/candlesticks`. The helper walks
  market → event → series for you; if you call the API directly you must do the
  same, or you get a 404.
- **Candlesticks only include CLOSED periods,** and the newest market in a series
  may have exactly one. A short or empty result on a fresh market is expected.
- **`liquidity_dollars` always returns `0.0000`** — it is explicitly deprecated in
  the spec and will never show liquidity. Use the orderbook depth and
  `volume_24h_fp` instead.
- **`/markets/orderbooks` needs repeated `tickers` params.** A comma-separated
  list returns `200` with one bogus entry whose ticker is `"A,B"` and an empty
  book. It looks like the markets are empty; they aren't.
- **`/markets/candlesticks` is the opposite** — `market_tickers` is
  comma-separated. The two batch endpoints disagree on convention.
- **`title` and `expiration_time` on a market are marked deprecated in the spec**
  but `title` is still the only market-level descriptive string, so the helper
  reads it and falls back to `yes_sub_title`. Durable timing fields are
  `expected_expiration_time` and `latest_expiration_time`.
- **Yes and No titles are often identical** on two-outcome events (both markets
  carry the event's team name; only the ticker suffix distinguishes them, and
  Kalshi really does return `no_sub_title` equal to `yes_sub_title`). Read the
  ticker suffix or the rules before picking a side.
- **Combo (MVE) markets return zero volume and empty books.** They are excluded
  by default; if you switch that off you will fill your output with them.
- **This skill is read-only.** Portfolio, balance, positions, and order placement
  all return `401 token_authentication_failure` without RSA-signed requests. Do
  not present a read-only price query as a portfolio-aware recommendation.
- **US-only.** Kalshi is a US exchange; market availability and eligibility rules
  are US-framed. Read-only data is otherwise unrestricted.

## Verification

Prove the skill is live and correctly installed:

1. `python3 scripts/kalshi.py status` → prints `Exchange active: True` and a
   shard list. A connectivity problem fails here first, with an HTTP/connection
   message on stderr.
2. `python3 scripts/kalshi.py trending --limit 3` → three series with volume.
   Kalshi's top-volume series by lifetime volume are the crypto and sports
   families; if you see `$0`-volume entries at the top, the parser has drifted.
3. `python3 scripts/kalshi.py markets --limit 3` → three quoted markets with both
   a yes bid and a yes ask. One-sided or all-`n/a` quotes mean the quote field
   names changed.
4. `python3 scripts/kalshi.py book <ticker-from-step-3>` → a ladder whose best
   yes ask equals `100% - best no bid`. That identity is the check that the
   orderbook derivation is still correct.
