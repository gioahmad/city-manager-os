from datetime import date, time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import operations_engine


class Cursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return [{"id": "routine", "scheduled_time": time(8), "grace_minutes": 15}]


def test_ensure_runs_applies_date_windows_and_preserves_touched_runs():
    cursor = Cursor()
    service_date = date(2026, 9, 23)

    assert operations_engine.ensure_runs(cursor, service_date) == 1

    delete_sql, delete_params = cursor.calls[0]
    select_sql, select_params = cursor.calls[1]
    upsert_sql, _ = cursor.calls[2]
    assert "rr.issue_id IS NULL" in delete_sql
    assert "r.starts_on" in delete_sql and "r.ends_on" in delete_sql
    assert delete_params == (service_date,)
    assert "starts_on <= %s::date" in select_sql
    assert "ends_on >= %s::date" in select_sql
    assert select_params == (service_date, service_date, service_date)
    assert "operations_routine_runs.issue_id IS NULL" in upsert_sql
    assert "operations_routine_runs.acknowledged_at IS NULL" in upsert_sql
    assert "operations_routine_runs.exception_note IS NULL" in upsert_sql
