# Kalshi API Endpoints Reference

Every endpoint below is **public and unauthenticated** — no API key, no request
signing. Verified against the live exchange.

## Base URL

```
https://api.elections.kalshi.com/trade-api/v2
```

`https://external-api.kalshi.com/trade-api/v2` is the same production exchange
(the docs' primary host); Kalshi states the `elections` subdomain historically
served all markets, not just elections. Demo environment (separate order books,
no real money) is `https://demo-api.kalshi.co/trade-api/v2`.

The authoritative machine-readable spec is
[`https://docs.kalshi.com/openapi.yaml`](https://docs.kalshi.com/openapi.yaml)
(OpenAPI 3.0, ~116 operations). Perps (margin) live in a separate spec,
`perps_openapi.yaml`, and are not covered here.

### Authentication boundary

| Surface | Auth |
|---|---|
| `exchange`, `events`, `markets`, `series`, `search`, `live_data`, `milestones`, `structured_targets`, `historical/*` (market data) | none |
| `portfolio/*` (balance, positions, fills, orders), `account/*`, `api_keys/*`, `fcm/*`, `communications/*` | RSA-signed |
| `trade-api/v2/markets/trades` | none (public tape) |
| `historical/orders`, `historical/fills`, `historical/positions` | RSA-signed (account history) |

Signed requests need three headers and an RSA-PSS signature over
`timestamp + METHOD + path` (query string excluded):

```
KALSHI-ACCESS-KEY: <key id>
KALSHI-ACCESS-TIMESTAMP: <unix ms>
KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp + method + path))
```

Unauthenticated calls to protected endpoints return
`401 {"error":{"code":"token_authentication_failure","message":"..."}}`.

---

## Exchange

```
GET /exchange/status
```
```json
{"exchange_active": true, "trading_active": true,
 "intra_exchange_transfers_active": true,
 "exchange_index_statuses": [{"exchange_index": 0, "description": "Default",
                              "exchange_active": true, "trading_active": true}]}
```

```
GET /exchange/schedule
```
Maintenance windows and per-weekday trading hours.

---

## Markets — `GET /markets`

| Param | Notes |
|---|---|
| `limit` | default 100, max 1000 |
| `cursor` | pagination; pass the `cursor` from the previous response |
| `event_ticker` | one event ticker only |
| `series_ticker` | filter to a series |
| `status` | `unopened`, `open`, `paused`, `closed`, `settled` (empty = all) |
| `tickers` | comma-separated explicit list |
| `mve_filter` | `only` / `exclude` — multivariate (combo) events |
| `min_close_ts` / `max_close_ts` | unix seconds |
| `min_created_ts` / `max_created_ts` | unix seconds |
| `min_updated_ts` / `max_updated_ts` | metadata changes only |

Response:
```json
{"cursor": "...", "markets": [
  {"ticker": "KXNBAGAME-26OCT20PHINYK-PHI",
   "event_ticker": "KXNBAGAME-26OCT20PHINYK",
   "title": "Philadelphia wins",
   "status": "active",
   "market_type": "binary",
   "yes_bid_dollars": "0.3600", "yes_ask_dollars": "0.3700",
   "yes_bid_size_fp": "83.35", "yes_ask_size_fp": "1455.99",
   "no_bid_dollars": "0.6300",  "no_ask_dollars": "0.6400",
   "last_price_dollars": "0.3700", "previous_price_dollars": "0.3700",
   "volume_fp": "34761.00", "volume_24h_fp": "761.00",
   "open_interest_fp": "26500.00", "liquidity_dollars": "0.0000",
   "open_time": "2026-08-20T15:28:00Z", "close_time": "2026-10-22T23:00:00Z",
   "expiration_time": "2026-10-22T23:00:00Z",
   "strike_type": "structured", "yes_sub_title": "Philadelphia",
   "rules_primary": "If Philadelphia wins ... resolves to Yes.",
   "can_close_early": true, "is_provisional": false}]}
```

**Field notes**

- Prices are dollar strings (`"0.3600"`), 2–4 decimals, and double as
  probabilities. Cent-denominated twins (`yes_bid`, `yes_ask`, `volume`, …) are
  gone from the schema — use the `_dollars` / `_fp` variants.
- `volume_fp` / `volume_24h_fp` / `open_interest_fp` are **contract counts**
  (strings). `_fp` = fixed point.
- `market_type` is `binary` or `scalar`.
- `strike_type` (`structured`, `custom`, `binary`, …) explains how the title
  maps to a number; `floor_strike` / `cap_strike` / `functional_strike` appear
  on threshold markets.
- `custom_strike` and `mve_selected_legs` appear on combo markets only.
- `result` is populated only after settlement.

**Deprecated fields (present in responses, do not build on them)**

| Field | Status |
|---|---|
| `liquidity_dollars` | **Always returns `"0.0000"`** — use the orderbook instead |
| `expiration_time` | Superseded by `expected_expiration_time` / `latest_expiration_time` |
| `title` | Still populated and still the only market-level descriptive string; prefer `yes_sub_title` as the durable field |
| `subtitle` | Deprecated; event-level `sub_title` is live |

**Market `status` lifecycle enum:** `initialized`, `inactive`, `active`,
`closed`, `determined`, `disputed`, `amended`, `finalized`. Note this is a
different vocabulary from the `status` *query filter*
(`unopened`/`open`/`paused`/`closed`/`settled`) — filter with `open`, read
`active`.

```
GET /markets/{ticker}
```
Single market: `{"market": {...}}`. Same object as above.

```
GET /markets/{ticker}/orderbook?depth=N
```
`depth` 0 or negative returns all levels; 1–100 otherwise.

```json
{"orderbook_fp": {
  "yes_dollars": [["0.3600", "83.35"], ["0.3500", "1293.06"]],
  "no_dollars":  [["0.6300", "1455.99"], ["0.6200", "329.13"]]}}
```

**Only resting bids are returned, for both sides.** Kalshi has no ask book
because a NO bid at `p` is a YES ask at `1 - p`. Derive:

```
yes_ask        = 1 - best_no_bid
no_ask         = 1 - best_yes_bid
```

Ladders arrive sorted ascending for `yes_dollars` and descending for
`no_dollars` — do not assume an order, sort explicitly. An empty market returns
both arrays empty.

```
GET /markets/orderbooks?tickers=A&tickers=B&tickers=C
```

Batched orderbooks. **The `tickers` parameter must be REPEATED, not
comma-separated.** Passing `?tickers=A,B` returns HTTP 200 with a single
malformed entry whose `ticker` is the literal string `"A,B"` and an empty book —
a silent failure that looks like "these markets are empty". Repeat the key once
per ticker:

```
curl "…/markets/orderbooks?tickers=KXNBAGAME-26OCT20PHINYK-PHI&tickers=KXNBAGAME-26OCT20PHINYK-NYK"
```

```json
{"orderbooks": [
  {"ticker": "KXNBAGAME-26OCT20PHINYK-PHI",
   "orderbook_fp": {"yes_dollars": [["0.3600", "83.35"]], "no_dollars": [["0.6300", "1455.99"]]}}]}
```

Note each element is `{ticker, orderbook_fp}`, a different shape from the single
`/markets/{ticker}/orderbook` response.

```
GET /markets/trades?ticker=T&limit=N&min_ts=&max_ts=&cursor=
```
Public trade tape (no auth). `limit` default 100, max 1000.

```json
{"cursor": "...", "trades": [
  {"trade_id": "0723e1aa-...", "ticker": "KXMLBHR-26SEP222040AZCOL-COLCCARRIGG16-1",
   "created_time": "2026-09-22T20:17:18.331442Z",
   "count_fp": "2798.08",
   "yes_price_dollars": "0.1100", "no_price_dollars": "0.8900",
   "taker_side": "yes", "taker_outcome_side": "yes",
   "taker_book_side": "bid", "is_block_trade": false}]}
```

`count_fp` is contracts for that trade. `taker_side` / `taker_outcome_side` say
which side the aggressor took — useful for reading flow direction.

```
GET /markets/candlesticks?market_tickers=A,B&start_ts=&end_ts=&period_interval=
```

Batched candlesticks. Here `market_tickers` **is** comma-separated (unlike
`/markets/orderbooks`), max 100 tickers. `start_ts`, `end_ts`, and
`period_interval` are all required. Response is `{"markets": [{candlesticks: [...]}, ...]}`.

The batched `price` block can be richer than the single-market one — it may carry
`mean_dollars` alongside the OHLC — so read it when present and fall back to the
`yes_bid`/`yes_ask` blocks otherwise.

---

## Candlesticks / price history

```
GET /series/{series_ticker}/markets/{ticker}/candlesticks
      ?start_ts=<unix>&end_ts=<unix>&period_interval=1|60|1440
      &include_latest_before_start=true
```

**The series ticker is part of the path** — resolve market → `event_ticker` →
`GET /events/{event_ticker}` → `series_ticker` first. Calling with only the
market ticker 404s.

`period_interval` is minutes and accepts exactly `1`, `60`, or `1440`.
`include_latest_before_start` prepends a synthetic candle carrying the last
known price so a chart has a starting point.

```json
{"candlesticks": [
  {"end_period_ts": 1790107200,
   "yes_bid": {"open_dollars": "0.0000", "high_dollars": "0.0900",
               "low_dollars": "0.0000", "close_dollars": "0.0900"},
   "yes_ask": {"open_dollars": "0.1200", "high_dollars": "0.1200",
               "low_dollars": "0.1100", "close_dollars": "0.1100"},
   "price": {},
   "volume_fp": "0.00", "open_interest_fp": "0.00"}]}
```

Only **closed** periods are returned, so a brand-new market yields an empty
array or a single candle. `price` is frequently an empty object — read the
`yes_bid` / `yes_ask` OHLC blocks instead.

A sibling `GET /series/{series_ticker}/events/{ticker}/candlesticks` exists for
event-level candles, plus
`GET /series/{series_ticker}/events/{ticker}/forecast_percentile_history` for
forecast distributions.

---

## Events — `GET /events`

| Param | Notes |
|---|---|
| `limit` | default 200, max 200 |
| `cursor` | pagination |
| `status` | `unopened`, `open`, `closed`, `settled` |
| `series_ticker` | filter to a series |
| `tickers` | comma-separated explicit list |
| `with_nested_markets` | `true` embeds each event's `markets` array |
| `with_milestones` | `true` adds related milestones |
| `min_close_ts`, `min_updated_ts` | unix seconds |

```json
{"cursor": "...", "events": [
  {"event_ticker": "KXELONMARS-99", "series_ticker": "KXELONMARS",
   "title": "Will Elon Musk visit Mars in his lifetime?",
   "sub_title": "Before 2099", "category": "World",
   "mutually_exclusive": false, "exchange_index": 0,
   "last_updated_ts": "2026-08-28T21:35:33.691337Z",
   "settlement_sources": [{"name": "Reuters", "url": "https://www.reuters.com"}]}],
 "milestones": []}
```

**Events carry no `volume` field.** For event or market liquidity, read the
nested markets' `volume_fp` / `volume_24h_fp`, or use the parent series'
`volume_fp`. `mutually_exclusive: true` marks a set of competing outcomes (only
one can resolve yes) — that is the flag that tells you N markets are one
question, not N independent bets. `settlement_sources` is what the market
resolves against; it is the first thing to read before disagreeing with a price.

```
GET /events/{event_ticker}[?with_nested_markets=true]
```
```json
{"event": {...}, "markets": [...]}
```
Note the shape difference: with `with_nested_markets=true` the markets are a
**sibling** of `event`, not nested inside it.

```
GET /events/{event_ticker}/metadata
GET /events/multivariate
GET /events/fee_changes
```

---

## Series — `GET /series`

**No pagination, no sort, no query-text search.** One response with every series
on the exchange (~14,000 series, ~18MB). Cache it.

| Param | Notes |
|---|---|
| `category` | series whose categories include this value |
| `tags` | filter by tag |
| `include_volume` | `true` adds lifetime `volume_fp` |
| `min_updated_ts` | unix seconds |

```json
{"series": [
  {"ticker": "KXNBAGAME", "title": "NBA Game", "category": "Sports",
   "categories": ["Sports"], "tags": ["Basketball"], "frequency": "custom",
   "volume_fp": "11676049072.00",
   "fee_type": "...", "fee_multiplier": 1,
   "contract_url": "https://assets.kalshi.com/regulatory/.../BASKETBALLGAMEWIN.pdf",
   "exchange_index": 0,
   "settlement_sources": [{"name": "the Governing League", "url": "..."}]}]}
```

`volume_fp` is **lifetime** contracts traded across the whole family — a series
ranking, not a per-market number. `contract_url` points at the official product
certification (the legal terms) and is worth surfacing when a user questions
resolution.

```
GET /series/{series_ticker}[?include_volume=true]
```
```json
{"series": {...}}
```

```
GET /series/fee_changes
```

---

## Discovery — search, categories, tags

```
GET /search/tags_by_categories
```
```json
{"tags_by_categories": {
  "Crypto": ["BTC", "15 min", "Hourly", "ETH", "SOL", "DOGE", "XRP"],
  "Economics": ["Jobs & Economy", "Inflation", "Fed", "GDP"],
  "Companies": null}}
```
A `null` value means the category has no tags, not that it is empty.

```
GET /search/filters_by_sport
```

**There is no keyless text-search endpoint.** `/public-search` exists only on
Polymarket. To search Kalshi, download `/series` (cache it), match on
`title`/`ticker`/`category`/`tags`, then fetch `/markets?series_ticker=...`.
Kalshi titles are short and terse (`"NBA Game"`, `"Bitcoin price up down"`), so
search a noun, not a question.

---

## Other public families

| Endpoint | Returns |
|---|---|
| `GET /milestones`, `/milestones/{id}` | Milestone records referenced by events |
| `GET /structured_targets`, `/{id}` | Reusable structured strike targets |
| `GET /incentive_programs` | Liquidity/market-making incentive programs |
| `GET /live_data/batch`, `/live_data/{type}/milestone/{id}` | Live in-game/event data |
| `GET /live_data/events/{event_ticker}` | Live data for an event |
| `GET /live_data/weather/{city}` (+ `/calibrations`) | Weather index feeds |
| `GET /live_data/milestone/{id}/game_stats` | Sports game stats |
| `GET /historical/cutoff` | Timestamp where live data ends and historical begins |
| `GET /historical/markets`, `/{ticker}`, `/{ticker}/candlesticks` | Market data older than the cutoff (public) |

Account-scoped historical endpoints (`/historical/orders`, `/historical/fills`,
`/historical/positions`) require signing.

---

## Rate limits

Token-bucket, **per authenticated tier**, refilling at a per-second budget.
Most requests cost 10 tokens; `GET /account/endpoint_costs` lists non-default
costs (needs auth).

| Tier | Read/s | Write/s |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,200 | 1,200 |
| Paragon | 2,400 | 2,400 |
| Prime | 4,800 | 4,800 |
| Prestige | 12,000 | 9,600 |

Limits are enforced against a key, but the budget is generous enough that
read-only discovery will not approach it. On `429`
(`{"error": "too many requests"}`) apply exponential backoff — no `Retry-After`
header is sent and there is no penalty cooldown.

---

## Field cross-reference

Market → everything else:

```
ticker                    → /markets/{ticker}, /markets/{ticker}/orderbook, /markets/trades
event_ticker              → /events/{event_ticker}          → series_ticker
series_ticker             → /series/{series_ticker}
(market, series)          → /series/{series}/markets/{ticker}/candlesticks
```

Polymarket equivalents, for anyone porting between the two:

| Polymarket | Kalshi |
|---|---|
| Gamma `/public-search?q=` | none — filter the `/series` dump |
| Gamma `/events`, `/markets` | `/events`, `/markets` |
| `outcomePrices` JSON string | `yes_bid_dollars` / `yes_ask_dollars` |
| `clobTokenIds` | `ticker` (one per market; yes/no are separate markets) |
| `conditionId` | `event_ticker` + `ticker` |
| CLOB `/price`, `/midpoint`, `/spread` | derived from `yes_bid_dollars` / `yes_ask_dollars` |
| CLOB `/book` (bids **and** asks) | `/markets/{t}/orderbook` (bids only; derive asks) |
| CLOB `/prices-history` | `/series/{s}/markets/{t}/candlesticks` |
| Data API `/trades` | `/markets/trades` |
| USDC volume in dollars | contract counts (`volume_fp`) |
| EIP-712 wallet auth | RSA-PSS key auth |
