from __future__ import annotations

import os
import time
from datetime import datetime

from geo_resolver import process_pending_alerts
from integration_engine import db_conn, mark_stale_events, run_due_integrations
from transit_engine import mark_stale_transit_observations, run_due_transit_integrations


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def main() -> None:
    interval = max(30, int(os.getenv("INTEGRATION_ENGINE_INTERVAL_SECONDS", "60")))
    geo_limit = max(1, min(int(os.getenv("ALERT_GEO_BATCH_SIZE", "50")), 500))
    log(f"integration engine starting interval={interval}s")
    while True:
        try:
            summaries = run_due_integrations(limit=25)
            if summaries:
                good = sum(1 for row in summaries if row["ok"])
                bad = len(summaries) - good
                events = sum(int(row.get("events") or 0) for row in summaries)
                changed = sum(int(row.get("changed") or 0) for row in summaries)
                log(f"poll complete integrations={len(summaries)} ok={good} error={bad} events={events} changed={changed}")
                for row in summaries:
                    if not row["ok"]:
                        log(f"ERROR {row['integration_key']}: {row.get('error')}")
            transit_summaries = run_due_transit_integrations(limit=12)
            if transit_summaries:
                good = sum(1 for row in transit_summaries if row["ok"])
                bad = len(transit_summaries) - good
                items = sum(int(row.get("items") or 0) for row in transit_summaries)
                changed = sum(int(row.get("changed") or 0) for row in transit_summaries)
                log(f"transit poll complete integrations={len(transit_summaries)} ok={good} error={bad} items={items} changed={changed}")
                for row in transit_summaries:
                    if not row["ok"]:
                        log(f"TRANSIT ERROR {row['integration_key']}: {row.get('error')}")
            stale_transit = mark_stale_transit_observations()
            if stale_transit:
                log(f"transit stale clear complete observations={stale_transit}")
            mark_stale_events()
            with db_conn() as conn:
                geo = process_pending_alerts(conn, limit=geo_limit, since_days=30)
            if geo.get("selected") or geo.get("errors"):
                log(
                    "alert geo complete "
                    f"selected={geo.get('selected', 0)} processed={geo.get('processed', 0)} "
                    f"precise={geo.get('precise', 0)} approximate={geo.get('approximate', 0)} "
                    f"unresolved={geo.get('unresolved', 0)} errors={geo.get('errors', 0)}"
                )
        except Exception as exc:
            log(f"engine cycle error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
