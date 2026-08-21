-- Views over the trade and daily_pnl tables.
--
-- Everything here is deliberately computed in SQL rather than pulled into Pandas.
-- Three reasons: the database is already optimised for grouped window aggregation,
-- it avoids moving the full table across the network to do arithmetic on it, and the
-- logic stays reachable from anything that speaks SQL, including a BI tool.

-- The single definition of a usable trade. The aggregation reads from this rather
-- than from `trade` directly, so the rules live in exactly one place. The companion
-- data quality project reports on the rows that fall outside it.
CREATE OR REPLACE VIEW v_valid_trade AS
SELECT
    t.trade_id,
    t.instrument_id,
    upper(t.side)                          AS side,
    t.quantity,
    t.price,
    t.executed_at,
    (t.executed_at AT TIME ZONE 'UTC')::date AS trade_date
FROM trade t
WHERE t.instrument_id IS NOT NULL
  AND t.side IS NOT NULL
  AND upper(t.side) IN ('BUY', 'SELL')
  AND t.quantity IS NOT NULL
  AND t.quantity > 0
  AND t.price IS NOT NULL
  AND t.price > 0
  AND t.executed_at IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM instrument i WHERE i.instrument_id = t.instrument_id
  );

-- Running realised PnL per instrument.
--
-- Note that only realised PnL is accumulated. Unrealised PnL is a position marked at
-- a point in time, not a flow, so summing it across days would double count the same
-- open position. total_pnl is therefore cumulative realised plus the *current* day's
-- unrealised, which is the figure a desk would actually recognise.
CREATE OR REPLACE VIEW v_running_pnl AS
WITH daily AS (
    SELECT
        d.pnl_date,
        d.instrument_id,
        i.symbol,
        i.asset_class,
        d.trade_count,
        d.closing_position,
        d.avg_cost,
        d.close_price,
        d.realised_pnl,
        d.unrealised_pnl
    FROM daily_pnl d
    JOIN instrument i ON i.instrument_id = d.instrument_id
)
SELECT
    pnl_date,
    instrument_id,
    symbol,
    asset_class,
    trade_count,
    closing_position,
    avg_cost,
    close_price,
    realised_pnl,
    unrealised_pnl,
    SUM(realised_pnl) OVER w                    AS cumulative_realised_pnl,
    SUM(realised_pnl) OVER w + unrealised_pnl   AS total_pnl
FROM daily
WINDOW w AS (
    PARTITION BY instrument_id
    ORDER BY pnl_date
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
);

-- Instruments ranked by realised PnL within each month.
--
-- RANK rather than ROW_NUMBER: two instruments that earned the same amount should
-- share a rank rather than be ordered arbitrarily.
CREATE OR REPLACE VIEW v_top_instruments_by_month AS
WITH monthly AS (
    SELECT
        date_trunc('month', d.pnl_date)::date AS month,
        d.instrument_id,
        i.symbol,
        i.asset_class,
        SUM(d.realised_pnl) AS realised_pnl,
        SUM(d.trade_count)  AS trade_count
    FROM daily_pnl d
    JOIN instrument i ON i.instrument_id = d.instrument_id
    GROUP BY 1, 2, 3, 4
)
SELECT
    month,
    instrument_id,
    symbol,
    asset_class,
    realised_pnl,
    trade_count,
    RANK() OVER (PARTITION BY month ORDER BY realised_pnl DESC) AS pnl_rank
FROM monthly;
