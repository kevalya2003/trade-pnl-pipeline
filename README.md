# Trade & PnL Data Pipeline

An ingestion and aggregation pipeline for trade executions: it lands a messy trade
feed into PostgreSQL without losing the bad rows, computes daily position and PnL in
SQL, and serves the result over a read-only API.

The companion project [`trade-data-quality`](../trade-data-quality) validates the data this pipeline
loads. Together they are one system: this half moves and aggregates the data, that
half proves it is fit to use.

## What it actually does

```
trades.csv  ->  idempotent upsert  ->  trade  ->  v_valid_trade  ->  daily_pnl  ->  views  ->  API
                (watermarked)                     (rules)           (SQL agg)
```

Running `tradepnl demo` against an empty database produces:

```
schema ready, 30 instruments seeded
generated 50040 feed rows
read 50040, upserted 50000, skipped 0 below watermark, collapsed 40 in-batch duplicates
wrote 10842 daily_pnl rows over all history
      trade_rows: 50000
      valid_rows: 49922
  daily_pnl_rows: 10842
     instruments: 30
       watermark: 2026-08-03 16:57:38+00:00
    invalid_rows: 78
```

The 78 invalid rows are deliberate. See [Dirty data on purpose](#dirty-data-on-purpose).

## Running it

Requires Docker and Python 3.10+.

```bash
docker compose up -d postgres
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export TRADEPNL_DATABASE_URL="postgresql+psycopg://trades:trades@localhost:55432/trades"

tradepnl demo            # schema, generate, load, aggregate, report
tradepnl serve           # read-only API on :8000
pytest                   # 25 tests against the real database
```

On Windows PowerShell, replace the `export` with
`$env:TRADEPNL_DATABASE_URL="postgresql+psycopg://trades:trades@localhost:55432/trades"`
and use `.venv\Scripts\`.

Individual steps:

| Command | What it does |
| --- | --- |
| `tradepnl init-db` | Create tables and views, seed the instrument dimension |
| `tradepnl generate --count 50000` | Write a synthetic feed to `data/trades.csv` |
| `tradepnl load --file data/trades.csv` | Upsert the feed, respecting the watermark |
| `tradepnl load --full` | Same, ignoring the watermark |
| `tradepnl pnl --since 2024-06-01` | Recompute the aggregate from a date |
| `tradepnl stats` | Row counts and current watermark |

## Design decisions

These are the parts worth arguing about. Each one had a plausible alternative.

### Grain: why `daily_pnl` is a table, not a view

`trade` is one row per execution; `daily_pnl` is one row per instrument per day. The
aggregate could have been a view, which would always be current and would never need
a refresh step. It is a table because position history is read far more often than it
changes, and recomputing a year of running positions from every individual execution
gets linearly slower as trades accumulate. The cost of that choice is a refresh step
that can lag reality, which is why the refresh is idempotent and cheap to re-run.

### Why the loader upserts instead of inserting

Batch jobs get retried. They die halfway through. Somebody eventually runs yesterday's
file again by hand. `INSERT ... ON CONFLICT (trade_id) DO UPDATE` makes the load
converge on the same state no matter how many times it runs, which means a retry is
always safe and never needs a human to work out what was already loaded.

`tests/test_load.py::test_loading_the_same_feed_twice_does_not_duplicate` is the proof.

### Why duplicates are collapsed before the insert

PostgreSQL raises `ON CONFLICT DO UPDATE command cannot affect row a second time` if a
single statement presents the same conflict target twice. A feed containing a
producer's retry does exactly that, so the batch is reduced to one record per
`trade_id` first, last occurrence winning. This is the kind of thing you only find by
running the code against real-shaped data, which is why the generator produces it.

### Why the `trade` table has almost no constraints

There is no foreign key on `instrument_id` and most columns are nullable. That looks
like sloppiness and is the opposite. A foreign key would abort an entire batch because
one row referenced an instrument the reference data had not yet received. Landing the
row and reporting on it later isolates the damage to the row.

The counter-argument is real and you should know it: for some datasets, silently
carrying on with five per cent of rows missing is worse than failing loudly, because
downstream consumers cannot tell the difference between "no trades" and "trades we
dropped". The honest answer is that it depends on whether consumers can tolerate gaps,
and here the gap is made explicit by `v_valid_trade` and by the quarantine table in
the companion project.

### Why the watermark lives in the database

`etl_watermark` is written in the same transaction as the rows it describes. If the
load rolls back, so does the watermark, so the two can never disagree. A file or an
Airflow variable would be updated separately and could drift out of step after a
crash, leaving rows loaded that the next run believes it still has to load.

The watermark is also monotonic: a backfill of old trades will not rewind it. That was
a bug during development — a `--full` run passed `None` as the floor instead of the
stored mark, so backfilling would have quietly caused the next run to reprocess
everything. `test_watermark_never_moves_backwards` exists because of it.

### Why aggregation is in SQL rather than Pandas

The database is already optimised for grouped window aggregation, it avoids moving
50,000 rows across the network to do arithmetic on them, and the logic stays reachable
from anything that speaks SQL — including a BI tool, which cannot import your Python
module. `sql/daily_pnl.sql` is a CTE chain; `sql/views.sql` builds the running totals
and rankings on top of it.

### Cost basis: average cost over buys, not FIFO

FIFO is what most jurisdictions require, but matching each sell against specific
earlier buy lots is inherently sequential and needs a recursive CTE or a row-by-row
pass. Average cost over buys is a recognised alternative and, critically, it *is*
expressible as a window function, because the running average of buys does not depend
on the sells interleaved with them. It is a documented simplification rather than an
accident. If this fed a real book, FIFO with lot tracking would be the next step.

### Why unrealised PnL is not accumulated

Realised PnL is a flow and sums across days. Unrealised PnL is a position marked at a
point in time, so summing it would count the same open position once per day.
`v_running_pnl` therefore accumulates realised only, and `total_pnl` is cumulative
realised plus the *current* day's unrealised.

## Dirty data on purpose

Clean synthetic data proves nothing, because handling clean data is trivial. The
generator injects defects that occur in real trade feeds:

| Defect | Count | Why it happens in reality |
| --- | --- | --- |
| Duplicate `trade_id` | 40 | A producer retries after a timeout |
| Null price | 25 | Field missing from an upstream message |
| Zero or negative quantity | 20 | Bad booking, or a cancellation modelled wrongly |
| Unknown `instrument_id` | 15 | Reference data lags the booking system |
| Invalid side | 10 | `B`/`S` from one venue where others send `BUY`/`SELL` |
| Null timestamp | 8 | Clock or serialisation failure |

That is 78 unusable rows plus 40 duplicates out of 50,040. `v_valid_trade` filters
them; the companion project explains them.

## Testing

25 tests, all against a real PostgreSQL instance rather than SQLite or mocks. The
pipeline depends on `ON CONFLICT`, `DISTINCT ON`, `FILTER` and window frames, none of
which behave identically elsewhere — a suite that passes against a database you do not
deploy to is testing the wrong system.

CI runs the same suite against a PostgreSQL service container, and sets
`TRADEPNL_REQUIRE_DB=1` so a missing database fails the build rather than skipping the
tests that matter and reporting a green tick.

The PnL expectations are worked out by hand in the test docstrings. Asserting against
a number the code itself produced would test nothing.

## Layout

```
src/tradepnl/
  models.py        schema, and the reasoning about grain and constraints
  generate.py      synthetic feed, including the defects
  load.py          idempotent upsert, watermark, in-batch dedup
  pnl.py           runs the aggregation
  api.py           read-only FastAPI over the views
  cli.py           init-db / generate / load / pnl / stats / demo / serve
  sql/
    views.sql      v_valid_trade, v_running_pnl, v_top_instruments_by_month
    daily_pnl.sql  the aggregation, as a CTE chain
tests/             25 tests against real PostgreSQL
```

## Known limitations

- The mark is the last execution of the day rather than an independent close price. A
  real system takes marks from a market data feed; using your own fills means a thin
  day moves your PnL.
- `daily_pnl` only has rows for days an instrument traded, so a position held without
  trading shows a gap rather than a carried-forward mark.
- No partitioning on `trade`. At 50,000 rows it is irrelevant; at 500 million it is
  the first thing to change.
- Average cost rather than FIFO, as described above.
