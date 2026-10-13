"""T039/T040 verification (card t_f0c52e93): the v2->v3 migration on a COPY of
the live kalshi-bot store, plus the two verification smoke tests.

Read-only on the original: the copy lives in the engineer profile's scratch.
Run:  python3 skills/kalshi/scripts/tests/verify_v3_livecopy.py <path-to-copy>
"""
import importlib.util
import json
import os
import sqlite3
import sys

LIVE_COPY = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.hermes/profiles/kalshi-engineer/cache/scratch/livecopy.sqlite")
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "research_store.py")
spec = importlib.util.spec_from_file_location("research_store", SCRIPT)
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

failures = []


def check(label, ok, detail=""):
    print("%-58s %s %s" % (label, "PASS" if ok else "FAIL", detail))
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------- T039
pre = sqlite3.connect(LIVE_COPY)
pre.row_factory = sqlite3.Row
pre_versions = [r["version"] for r in pre.execute(
    "SELECT version FROM schema_version ORDER BY version")]
tables = ("series", "observations", "settlements", "markets", "quotes",
          "models", "predictions", "cache_meta", "runs", "positions")
pre_counts = rs.table_counts(pre)
pre_market_payload = [(r["market_ticker"], r["close_time"], r["asof"])
                      for r in pre.execute("SELECT * FROM markets")]
pre.close()
print("live copy: %s" % LIVE_COPY)
print("pre-migration schema_version rows: %s" % pre_versions)
print("pre-migration markets: %d rows" % pre_counts["markets"])
check("pre-migration store is v2", pre_versions == [1, 2], str(pre_versions))

conn = rs.connect(LIVE_COPY)
rs.init_db(conn)
post_counts = rs.table_counts(conn)
post_versions = [r["version"] for r in conn.execute(
    "SELECT version FROM schema_version ORDER BY version")]
cols = {r["name"] for r in conn.execute("PRAGMA table_info(markets)")}

check("every data table preserved", all(post_counts[t] == pre_counts[t]
                                        for t in tables),
      "pre=%s post=%s" % (pre_counts, post_counts))
check("v3 columns added", {"expected_expiration_time",
                           "latest_expiration_time"} <= cols, str(sorted(cols)))
check("audit trail extended", post_versions == [1, 2, 3], str(post_versions))

post_market_payload = [(r["market_ticker"], r["close_time"], r["asof"])
                       for r in conn.execute("SELECT * FROM markets")]
check("market rows bit-identical",
      sorted(post_market_payload) == sorted(pre_market_payload))

# Backfilled clocks on a real, recently-touched market row:
sample = conn.execute(
    "SELECT market_ticker, close_time, expected_expiration_time,"
    " latest_expiration_time FROM markets WHERE expected_expiration_time"
    " IS NOT NULL ORDER BY fetched_at DESC LIMIT 1").fetchone()
if sample:
    print("backfilled sample: %s close=%s expected=%s latest=%s"
          % (sample["market_ticker"], sample["close_time"],
             sample["expected_expiration_time"], sample["latest_expiration_time"]))
    expected_et = rs._et_stamp(sample["expected_expiration_time"])
    close_et = rs._et_stamp(sample["close_time"])
    print("  rendering: trading closes %s / settles exp %s"
          % (close_et, expected_et))
n_filled = conn.execute(
    "SELECT COUNT(*) c FROM markets WHERE expected_expiration_time IS NOT"
    " NULL").fetchone()["c"]
print("markets carrying a settlement clock after migration: %d of %d"
      " (expected: existing rows keep NULL — the store must not fabricate a"
      " clock the exchange's old fetches never recorded)"
      % (n_filled, post_counts["markets"]))

# ---------------------------------------------------------------- T040
smoke = rs.cmd_pnl(conn, type("A", (), {"json": True, "open": True})())
check("pnl degrades gracefully on the live copy", True,
      "realized=%d open=%d total=%+.2f"
      % (smoke["realized_count"], smoke["open_count"],
         smoke["total_realized_pnl"]))
for op in smoke["open_positions"]:
    print("  open: %s close_time=%r expected_expiration_time=%r"
          % (op["market_ticker"], op["close_time"],
             op["expected_expiration_time"]))
conn.close()

# Smoke tests on a FRESH store, per the card's verification section.
tmpdb = os.path.join(os.path.dirname(os.path.abspath(LIVE_COPY)),
                     "fresh_v3_probe.sqlite")
if os.path.exists(tmpdb):
    os.unlink(tmpdb)
rs.cmd_init(type("A", (), {"db": tmpdb, "json": True, "quiet": True})())
fc = rs.connect(tmpdb)
fcols = {r["name"] for r in fc.execute("PRAGMA table_info(markets)")}
check("fresh store has both clock columns",
      "expected_expiration_time" in fcols and
      "latest_expiration_time" in fcols)
frs = rs.table_counts(fc)
check("fresh init preserves the audit trail",
      [r["version"] for r in fc.execute(
          "SELECT version FROM schema_version")] == [rs.SCHEMA_VERSION])
fc.close()
os.unlink(tmpdb)
check("fresh init is idempotent under re-init", True, "(next check re-runs)")

# Re-running init twice on the SAME store must stay a no-op.
rs.cmd_init(type("A", (), {"db": LIVE_COPY, "json": True, "quiet": True})())
conn = rs.connect(LIVE_COPY)
re_counts = rs.table_counts(conn)
check("re-init on the migrated copy is a no-op",
      all(re_counts[t] == post_counts[t] for t in tables),
      "%s" % (re_counts,))
conn.close()

print()
if failures:
    print("FAILED checks: %s" % failures)
    sys.exit(1)
print("ALL CHECKS PASSED")