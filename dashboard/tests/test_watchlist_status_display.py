import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode


class WatchlistStatusDisplay(unittest.TestCase):
    def test_labels_counts_and_filters_keep_internal_states_and_active_eligibility(self):
        now = datetime.now(timezone.utc)
        matched_at = now - timedelta(hours=1)
        cases = [
            ("on", {}, "Watching", "On"),
            ("scheduled", {"starts_at": now + timedelta(days=1)}, "Watching", "Scheduled"),
            ("held", {"last_delivery_status": "SUPPRESSED", "matches_7d": 1}, "Matching", "Repeat held"),
            ("pending", {"last_delivery_status": "PENDING", "matches_7d": 1}, "Matching", "Notification pending"),
            ("new_match", {"last_match_at": matched_at, "matches_7d": 1}, "Matching", "Notification pending"),
            ("delivered", {"last_match_at": matched_at, "last_delivery_at": now,
                           "last_delivery_status": "SENT", "matches_7d": 1}, "Watching", "On"),
            ("failed", {"last_delivery_status": "FAILED", "matches_7d": 1}, "Delivery Problem", "Delivery Problem"),
            ("unrouted", {"active_recipient_count": 0, "matches_7d": 1}, "Needs Recipient", "Needs Recipient"),
            ("paused", {"active": False, "matches_7d": 1}, "Paused", "Paused"),
            ("expired", {"expires_at": now - timedelta(days=1), "matches_7d": 1}, "Expired", "Expired"),
        ]
        rows = [{"id": key, "active": True, "active_recipient_count": 1,
                 "watch_type": "PHRASE", "matches_7d": None, **values}
                for key, values, _, _ in cases]

        def query_all(sql, params=None):
            self.assertTrue(sql.lstrip().startswith(("WITH ", "SELECT ")))
            return [dict(row) for row in rows] if "FROM watch_items w" in sql else []

        # Load the real read-only handler without FastAPI or a database service.
        path = Path(__file__).resolve().parents[1] / "spatial_watch_app.py"
        names = {"_safe_int", "_distance_label", "_local_value", "_watch_state",
                 "_saved_watch_preview_url", "spatial_watchlist"}
        nodes = [node for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        namespace = {
            "datetime": datetime, "timezone": timezone, "LOCAL_ZONE": timezone.utc,
            "urlencode": urlencode, "Query": lambda default, **kwargs: default,
            "query_all": query_all, "query_one": lambda *args: {},
            "_watch_health": lambda: {}, "COUNTIES": {"NJ": []},
            "MATCH_MODES": {"CONTAINS"}, "SPATIAL_WATCH_TYPES": [],
            "SPATIAL_DURATIONS": {}, "BULK_WATCH_LIMIT": 250,
            "templates": SimpleNamespace(TemplateResponse=lambda **kwargs: kwargs["context"]),
        }
        nodes = ast.parse("from __future__ import annotations").body + nodes
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        request = SimpleNamespace(state=SimpleNamespace())
        page = namespace["spatial_watchlist"](request)
        self.assertEqual(
            {row["id"]: (row["state_label"], row["state_display"]) for row in page["items"]},
            {key: (internal, display) for key, _, internal, display in cases},
        )
        self.assertEqual(page["counts"]["on"], 7)
        self.assertEqual(page["counts"]["matched"], 8)
        self.assertEqual(page["counts"]["watching"], 3)
        self.assertEqual(page["counts"]["matching"], 3)
        for state, expected in {
            "active": {"on", "held", "pending", "new_match", "delivered", "failed", "unrouted"},
            "matched": {"held", "pending", "new_match", "delivered", "failed", "unrouted", "paused", "expired"},
            "watching": {"on", "scheduled", "delivered"},
            "matching": {"held", "pending", "new_match"},
        }.items():
            with self.subTest(state=state):
                filtered = namespace["spatial_watchlist"](request, state=state)
                self.assertEqual({row["id"] for row in filtered["items"]}, expected)


if __name__ == "__main__":
    unittest.main()
