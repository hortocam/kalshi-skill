# Kalshi — Hermes Agent Skill

A [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) skill for querying
[Kalshi](https://kalshi.com), the CFTC-regulated US event-contract exchange:
markets, prices, orderbooks, spreads, price history, and trade flow.

**Read-only.** Every endpoint this skill uses is public — no API key, no account,
no request signing. Trading and portfolio data require RSA-signed authentication
and are deliberately out of scope (see [Roadmap](#roadmap)).

## Install

```bash
# Add this repo as a skill tap, then install from the hub
hermes skills tap add hortocam/kalshi-skill
hermes skills install hortocam/kalshi-skill/skills/kalshi --category finance
```

Installing into a specific profile:

```bash
hermes -p <profile> skills tap add hortocam/kalshi-skill
hermes -p <profile> skills install hortocam/kalshi-skill/skills/kalshi --category finance
```

Or clone and copy into place:

```bash
cp -r skills/kalshi ~/.hermes/skills/finance/kalshi
```

Installs are scanned by the Skills Hub guard (community trust) before landing.
Skills load on the next session — a running session won't see it until it restarts.

**Heads up:** the hub's GitHub lookups run unauthenticated unless `GITHUB_TOKEN`
is set, and unauthenticated GitHub allows only 60 requests/hour — easily
exhausted by a hub index build, after which tap searches silently return nothing.
Set `GITHUB_TOKEN` (a `repo`-scoped PAT is plenty) in `~/.hermes/.env` if you
plan to use taps.

## What's in here

```
skills/kalshi/
├── SKILL.md                    # triggers, workflow, pitfalls, verification
├── references/api-endpoints.md # full endpoint reference + field notes
└── scripts/kalshi.py           # CLI helper (stdlib only, no dependencies)
```

## Usage

```bash
python3 skills/kalshi/scripts/kalshi.py status
python3 skills/kalshi/scripts/kalshi.py trending --limit 10
python3 skills/kalshi/scripts/kalshi.py trending --category Crypto
python3 skills/kalshi/scripts/kalshi.py search "inflation"
python3 skills/kalshi/scripts/kalshi.py markets --series KXNBAGAME --limit 20
python3 skills/kalshi/scripts/kalshi.py market KXNBAGAME-26OCT20PHINYK-PHI
python3 skills/kalshi/scripts/kalshi.py event  KXNBAGAME-26OCT20PHINYK
python3 skills/kalshi/scripts/kalshi.py series KXNBAGAME
python3 skills/kalshi/scripts/kalshi.py book KXNBAGAME-26OCT20PHINYK-PHI --depth 10
python3 skills/kalshi/scripts/kalshi.py history KXNBAGAME-26OCT20PHINYK-PHI --interval 1w --period 60
python3 skills/kalshi/scripts/kalshi.py trades --ticker KXNBAGAME-26OCT20PHINYK-PHI --limit 20
```

The script has no dependencies beyond the Python 3 standard library.
`KALSHI_API_BASE` overrides the base URL, e.g. to point at the demo exchange:

```bash
KALSHI_API_BASE=https://demo-api.kalshi.co/trade-api/v2 \
  python3 skills/kalshi/scripts/kalshi.py status
```

## Kalshi concepts worth knowing up front

- **Series → Event → Market.** `KXNBAGAME` (series) → `KXNBAGAME-26OCT20PHINYK`
  (event) → `KXNBAGAME-26OCT20PHINYK-PHI` (market: "Philadelphia wins").
- **Prices are dollars and probabilities.** `0.36`/`0.37` means the market
  implies 36–37%. Contracts settle at $1.00 or $0.00.
- **Volume is a contract count, not dollars.**
- **Kalshi returns bids for both sides and no asks.** A NO bid at `p` is a YES
  ask at `1 - p`, so the helper derives asks from the opposite book.

## Gotchas this skill encodes

Found the hard way against the live API, and all documented in `SKILL.md`:

- `/series` has **no pagination and no text search** — it's one ~18MB dump of
  every series on the exchange. Search and trending are done client-side with a
  10-minute cache.
- `/markets/orderbooks` needs **repeated** `tickers` params. Comma-separated
  returns `200` with a bogus single entry and an empty book — a silent failure
  that looks like illiquid markets.
- `/markets/candlesticks` is the opposite: `market_tickers` **is**
  comma-separated.
- `liquidity_dollars` is deprecated and **always returns `0.0000`**.
- Price history needs the **series** ticker in the path, not just the market
  ticker.
- Market `status` (read) and the `status` filter (query) use **different
  vocabularies** — filter with `open`, read `active`.
- Combo (MVE) markets pollute list results with zero-volume noise; excluded by
  default.

## Roadmap

Parity with the read-only Polymarket skill is the goal for v1. Planned additions:

- RSA-PSS signed requests (`KALSHI-ACCESS-*` headers) for portfolio reads —
  balance, positions, fills — so position sizing can reference a real account.
- Order placement / cancellation.
- WebSocket streaming for live prices.

These are separate, opt-in additions; the keyless read path will stay keyless.

## License

MIT — see [LICENSE](LICENSE).
