-- Replace YOUR_PROJECT_ID everywhere, then run this whole file in BigQuery.
-- View 1: daily percentage returns and a rate index starting at 100.
CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.forex.daily_returns` AS
WITH prepared AS (
  SELECT
    rate_date,
    currency,
    rate,
    LAG(rate) OVER (
      PARTITION BY currency ORDER BY rate_date
    ) AS previous_rate,
    FIRST_VALUE(rate) OVER (
      PARTITION BY currency ORDER BY rate_date
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    ) AS first_rate
  FROM `YOUR_PROJECT_ID.forex.rates`
)
SELECT
  rate_date,
  currency,
  rate,
  previous_rate,
  100 * SAFE_DIVIDE(rate - previous_rate, previous_rate) AS daily_return_pct,
  100 * SAFE_DIVIDE(rate, first_rate) AS normalized_rate
FROM prepared;

-- View 2: sample standard deviation over the latest seven daily observations.
CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.forex.rolling_volatility` AS
WITH calculated AS (
  SELECT
    rate_date,
    currency,
    daily_return_pct,
    STDDEV_SAMP(daily_return_pct) OVER (
      PARTITION BY currency
      ORDER BY rate_date
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS rolling_volatility_pct
  FROM `YOUR_PROJECT_ID.forex.daily_returns`
)
SELECT
  *,
  rate_date = MAX(rate_date) OVER (PARTITION BY currency) AS is_latest
FROM calculated;

-- View 3: latest rank plus best and worst daily return in the last 30 days.
CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.forex.currency_ranking` AS
WITH latest AS (
  SELECT currency, rate_date, rolling_volatility_pct
  FROM `YOUR_PROJECT_ID.forex.rolling_volatility`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY currency ORDER BY rate_date DESC
  ) = 1
),
recent AS (
  SELECT d.currency, d.rate_date, d.daily_return_pct
  FROM `YOUR_PROJECT_ID.forex.daily_returns` AS d
  JOIN latest AS l USING (currency)
  WHERE d.daily_return_pct IS NOT NULL
    AND d.rate_date BETWEEN DATE_SUB(l.rate_date, INTERVAL 29 DAY) AND l.rate_date
),
summary AS (
  SELECT
    currency,
    ARRAY_AGG(STRUCT(rate_date, daily_return_pct)
              ORDER BY daily_return_pct DESC LIMIT 1)[OFFSET(0)] AS best,
    ARRAY_AGG(STRUCT(rate_date, daily_return_pct)
              ORDER BY daily_return_pct ASC LIMIT 1)[OFFSET(0)] AS worst
  FROM recent
  GROUP BY currency
)
SELECT
  l.currency,
  l.rate_date AS latest_rate_date,
  l.rolling_volatility_pct AS current_volatility_pct,
  RANK() OVER (ORDER BY l.rolling_volatility_pct DESC) AS volatility_rank,
  s.best.rate_date AS best_day,
  s.best.daily_return_pct AS best_return_pct,
  s.worst.rate_date AS worst_day,
  s.worst.daily_return_pct AS worst_return_pct
FROM latest AS l
JOIN summary AS s USING (currency);
