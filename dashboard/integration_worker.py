from __future__ import annotations

import os
import time
from datetime import datetime

from integration_engine import mark_stale_events, run_due_integrations


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def main() -> None:
    interval = max(30, int(os.getenv("INTEGRATION_ENGINE_INTERVAL_SECONDS", "60")))
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
            mark_stale_events()
        except Exception as exc:
            log(f"engine cycle error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
