"""Load daily USD exchange rates into BigQuery Sandbox with batch jobs."""

import argparse
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

API_URL = "https://api.frankfurter.dev/v2/rates"
CURRENCIES = ("EUR", "GBP", "JPY", "INR", "CHF", "AUD", "CAD")
DATASET = "forex"
LOCATION = "US"
SCHEMA = [
    bigquery.SchemaField("rate_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("currency", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("rate", "FLOAT64", mode="REQUIRED"),
    bigquery.SchemaField("loaded_at", "TIMESTAMP", mode="REQUIRED"),
]


def fetch_rates(backfill: bool) -> list[dict]:
    """Call the free API once: latest rates, or a 90-calendar-day range."""
    today = date.today()
    params = {"base": "USD", "quotes": ",".join(CURRENCIES)}
    if backfill:
        params.update({"from": (today - timedelta(days=89)).isoformat(),
                       "to": today.isoformat()})
    response = requests.get(API_URL, params=params, timeout=30)
    response.raise_for_status()  # Makes API failures visible in Actions logs.
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Frankfurter returned an unexpected response shape")

    loaded_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in payload:
        currency, rate = item.get("quote"), item.get("rate")
        if currency not in CURRENCIES or not isinstance(rate, (int, float)):
            continue
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"Invalid rate for {currency}: {rate}")
        rows.append({"rate_date": item["date"], "currency": currency,
                     "rate": float(rate), "loaded_at": loaded_at})
    if not rows:
        raise ValueError("Frankfurter returned no usable target-currency rates")
    return rows


def table(client: bigquery.Client, name: str) -> str:
    return f"{client.project}.{DATASET}.{name}"


def load_rows(client: bigquery.Client, rows: list[dict]) -> int:
    """Stage the batch, select only new keys, then batch-append to rates.

    BigQuery Sandbox rejects MERGE/DML.  The LEFT JOIN performs an existence
    check instead.  The workflow concurrency setting prevents two runs racing.
    """
    rates, staging = table(client, "rates"), table(client, "rates_staging")
    client.get_dataset(f"{client.project}.{DATASET}")  # Fail clearly if absent.
    client.create_table(bigquery.Table(rates, schema=SCHEMA), exists_ok=True)
    client.create_table(bigquery.Table(staging, schema=SCHEMA), exists_ok=True)

    stage_config = bigquery.LoadJobConfig(
        schema=SCHEMA, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    client.load_table_from_json(rows, staging, job_config=stage_config,
                                location=LOCATION).result()

    # SELECT is allowed in Sandbox; no streaming insert or DML is used.
    sql = f"""
      SELECT s.rate_date, s.currency, s.rate, s.loaded_at
      FROM `{staging}` AS s
      LEFT JOIN `{rates}` AS r
        USING (rate_date, currency)
      WHERE r.rate_date IS NULL
    """
    missing = []
    for row in client.query(sql, location=LOCATION).result():
        missing.append({"rate_date": row.rate_date.isoformat(),
                        "currency": row.currency, "rate": row.rate,
                        "loaded_at": row.loaded_at.isoformat()})
    if missing:
        append_config = bigquery.LoadJobConfig(
            schema=SCHEMA, write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
        client.load_table_from_json(missing, rates, job_config=append_config,
                                    location=LOCATION).result()
    return len(missing)


def export_dashboard(client: bigquery.Client) -> None:
    """Export small, public analytics snapshots for the static Pages dashboard."""
    names = ("daily_returns", "rolling_volatility", "currency_ranking")
    try:
        for name in names:
            client.get_table(table(client, name))
    except NotFound:
        logging.warning("SQL views do not exist yet; dashboard export skipped")
        return
    queries = {
        "rates": f"SELECT rate_date,currency,normalized_rate FROM `{table(client, names[0])}` ORDER BY rate_date,currency",
        "volatility": f"SELECT currency,rolling_volatility_pct FROM `{table(client, names[1])}` WHERE is_latest ORDER BY currency",
        "ranking": f"SELECT * FROM `{table(client, names[2])}` ORDER BY volatility_rank",
    }
    output = {"generated_at": datetime.now(timezone.utc).isoformat()}
    for key, sql in queries.items():
        output[key] = [dict(row.items()) for row in
                       client.query(sql, location=LOCATION).result()]
    Path("docs").mkdir(exist_ok=True)
    Path("docs/data.json").write_text(json.dumps(output, default=str, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="load the last 90 calendar days in one run")
    args = parser.parse_args()
    client = bigquery.Client()  # Reads the service-account credential file.
    rows = fetch_rates(args.backfill)
    added = load_rows(client, rows)
    logging.info("Fetched %d rows; appended %d new rows", len(rows), added)
    export_dashboard(client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        main()
    except Exception:
        logging.exception("Forex pipeline failed")
        raise
