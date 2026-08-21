-- Populate daily_pnl from validated trades.
--
-- Cost basis method: average cost over buys.
--
-- FIFO is what most jurisdictions actually require, but matching each sell against
-- specific earlier buy lots is inherently sequential and cannot be expressed with
-- window functions -- it needs a recursive CTE or a row-by-row pass in application
-- code. Average cost over buys is a recognised alternative, it is a documented
-- simplification rather than an accident, and critically it *is* expressible as a
-- window function because the running average of buys does not depend on the sells
-- interleaved with them. If this fed a real book, FIFO with lot tracking would be
-- the correct next step.
--
-- The statement is an upsert on (pnl_date, instrument_id), so recomputing is
-- idempotent for the same reason the loader is: a re-run after a failure must
-- converge on the same state rather than double count.

WITH clean AS (
    SELECT
        trade_id,
        instrument_id,
        side,
        quantity,
        price,
        executed_at,
        trade_date
    FROM v_valid_trade
    WHERE (CAST(:since AS date) IS NULL OR trade_date >= CAST(:since AS date))
),

-- Running cost and quantity of buys *strictly before* the current row. Evaluating
-- the average as at the moment just before a sell is what makes the realised figure
-- meaningful: a sell is matched against the book as it stood when the sell happened.
positions AS (
    SELECT
        instrument_id,
        trade_date,
        trade_id,
        side,
        quantity,
        price,
        CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END AS signed_qty,
        SUM(CASE WHEN side = 'BUY' THEN quantity * price ELSE 0 END) OVER prior AS prior_buy_cost,
        SUM(CASE WHEN side = 'BUY' THEN quantity ELSE 0 END)         OVER prior AS prior_buy_qty
    FROM clean
    WINDOW prior AS (
        PARTITION BY instrument_id
        ORDER BY executed_at, trade_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
),

realised AS (
    SELECT
        instrument_id,
        trade_date,
        signed_qty,
        CASE
            WHEN side = 'SELL' AND COALESCE(prior_buy_qty, 0) > 0
                THEN (price - (prior_buy_cost / prior_buy_qty)) * quantity
            ELSE 0
        END AS realised_pnl
    FROM positions
),

by_day AS (
    SELECT
        instrument_id,
        trade_date,
        COUNT(*)          AS trade_count,
        SUM(signed_qty)   AS net_qty,
        SUM(realised_pnl) AS realised_pnl
    FROM realised
    GROUP BY instrument_id, trade_date
),

-- Closing position is the cumulative net quantity across every day up to and
-- including this one, so it survives days with no trades in the instrument.
cumulative AS (
    SELECT
        instrument_id,
        trade_date,
        trade_count,
        realised_pnl,
        SUM(net_qty) OVER (
            PARTITION BY instrument_id
            ORDER BY trade_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS closing_position
    FROM by_day
),

-- The mark. Last execution of the day stands in for a closing price; a production
-- system would take this from a market data feed rather than from its own fills.
marks AS (
    SELECT DISTINCT ON (instrument_id, trade_date)
        instrument_id,
        trade_date,
        price AS close_price
    FROM clean
    ORDER BY instrument_id, trade_date, executed_at DESC, trade_id DESC
),

-- Average cost as at the end of each day. An aggregate wrapped in a window function:
-- the inner SUM collapses each day, the outer SUM accumulates across days.
eod_cost AS (
    SELECT
        instrument_id,
        trade_date,
        SUM(SUM(CASE WHEN side = 'BUY' THEN quantity * price ELSE 0 END)) OVER running
            AS cum_buy_cost,
        SUM(SUM(CASE WHEN side = 'BUY' THEN quantity ELSE 0 END)) OVER running
            AS cum_buy_qty
    FROM clean
    GROUP BY instrument_id, trade_date
    WINDOW running AS (
        PARTITION BY instrument_id
        ORDER BY trade_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
)

INSERT INTO daily_pnl (
    pnl_date, instrument_id, trade_count, closing_position,
    avg_cost, close_price, realised_pnl, unrealised_pnl, computed_at
)
SELECT
    c.trade_date,
    c.instrument_id,
    c.trade_count,
    c.closing_position,
    CASE WHEN e.cum_buy_qty > 0 THEN e.cum_buy_cost / e.cum_buy_qty END AS avg_cost,
    m.close_price,
    c.realised_pnl,
    CASE
        WHEN e.cum_buy_qty > 0
            THEN (m.close_price - (e.cum_buy_cost / e.cum_buy_qty)) * c.closing_position
        ELSE 0
    END AS unrealised_pnl,
    now()
FROM cumulative c
JOIN marks     m ON m.instrument_id = c.instrument_id AND m.trade_date = c.trade_date
LEFT JOIN eod_cost e ON e.instrument_id = c.instrument_id AND e.trade_date = c.trade_date
ON CONFLICT (pnl_date, instrument_id) DO UPDATE SET
    trade_count      = EXCLUDED.trade_count,
    closing_position = EXCLUDED.closing_position,
    avg_cost         = EXCLUDED.avg_cost,
    close_price      = EXCLUDED.close_price,
    realised_pnl     = EXCLUDED.realised_pnl,
    unrealised_pnl   = EXCLUDED.unrealised_pnl,
    computed_at      = EXCLUDED.computed_at;
