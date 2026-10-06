"""CI-local historical Watch preview against real PostgreSQL; no production access."""
import html
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
application, core, ms, calendar, hub, auth, provider, owner, mail_id, cookie, csrf = __import__("microsoft_fixture").setup()
sys.path.insert(0, str(ROOT / "dashboard"))
import watch_preview_app  # noqa: E402
import operations_app  # noqa: E402

for route in list(core.app.routes):
    if getattr(route, "path", "") == "/watch-preview":
        application.router.routes.append(route)

client = TestClient(application, base_url="https://fixture.example.com")
client.cookies.set(auth.COOKIE_NAME, cookie)

fixtures = [
    ("BNN:pipe-nj-normal", "10/06/2026 1:00 PM | NJ | Hudson | Jersey City | Working Fire Alert | 123 Newark Ave | Second alarm transmitted. | nj101"),
    ("BNN:pipe-nj-reordered", "10/06/2026 1:05 PM | Hudson County | Working Fire Alert | NJ | Jersey City | 125 Newark Ave | Companies operating. | nj102"),
    ("BNN:pipe-ny-reordered", "10/06/2026 1:10 PM | Traffic Alert | Queens County | Astoria | NY | 31-00 47th Ave | MVA with injuries. | ny201"),
]
with core.db_conn() as conn:
    for alert_id, message in fixtures:
        conn.execute(
            """INSERT INTO alerts(
                 id,alert_id,source,category,subtype,status,event_action,title,message,priority,
                 county,municipality,location,tags,received_at,search_text
               ) VALUES(%s,%s,'BNN','PUBLIC_SAFETY','INCIDENT','ACTIVE','NEW',%s,%s,4,
                        NULL,NULL,'{}'::jsonb,ARRAY['bnn'],now(),%s)
               ON CONFLICT(alert_id) DO UPDATE SET message=EXCLUDED.message,county=NULL,municipality=NULL,
                  search_text=EXCLUDED.search_text,updated_at=now()""",
            (uuid4(), alert_id, "BNN pipe fixture", message, message),
        )
    before = conn.execute(
        """SELECT
             (SELECT count(*) FROM watch_items) AS watches,
             (SELECT count(*) FROM alert_watch_matches) AS matches,
             (SELECT count(*) FROM deliveries) AS deliveries"""
    ).fetchone()

response = client.get("/watch-preview", params={
    "source": "BNN", "q": "Hudson County", "window": "all", "min_priority": 1
})
assert response.status_code == 200, response.text
assert "BNN:pipe-nj-normal" in response.text
assert "BNN:pipe-nj-reordered" in response.text
assert "BNN:pipe-ny-reordered" not in response.text
assert "Would have matched" in response.text
assert "Continue to Watch Builder" in response.text
print("WATCH PREVIEW PASS: BNN + Hudson County finds both NJ pipe orders and excludes NY")

county = client.get("/watch-preview", params={
    "source": "BNN", "county": "Hudson", "window": "all", "min_priority": 1
})
assert county.status_code == 200
assert "BNN:pipe-nj-normal" in county.text and "BNN:pipe-nj-reordered" in county.text
assert "FIELD county matched" in county.text
assert "field=county" in county.text or "field%3Dcounty" in county.text
builder_match = re.search(
    r'<a class="primary-button" href="([^"]+)">Continue to Watch Builder</a>',
    county.text,
)
assert builder_match, county.text
builder_href = html.unescape(builder_match.group(1))
builder_query = parse_qs(urlparse(builder_href).query)
assert builder_query["source_filter"] == ["BNN"]
assert builder_query["match_field"] == ["county"]
assert builder_query["match_mode"] == ["FIELD"]
assert builder_query["search_term"] == ["Hudson"]
watch_source = (ROOT / "dashboard/spatial_watch_app.py").read_text()
watch_template = (ROOT / "dashboard/templates/watchlist.html").read_text()
assert '"source_filter": source_filter.strip()' in watch_source
assert '"alert_category_filter": alert_category_filter.strip()' in watch_source
assert 'p == prefill.min_priority' in watch_template
assert 'mm == prefill.match_mode' in watch_template
assert 'value="{{ prefill.match_field }}"' in watch_template
print("WATCH PREVIEW PASS: structured Hudson County rule previews as FIELD county and carries the exact proven rule into Watch Builder")

ny = client.get("/watch-preview", params={
    "source": "BNN", "q": "Queens County", "window": "all"
})
assert ny.status_code == 200
assert "BNN:pipe-ny-reordered" in ny.text
assert "BNN:pipe-nj-normal" not in ny.text
print("WATCH PREVIEW PASS: variable-order NY state/county segments classify independently of position")

choices = operations_app.alert_keyword_choices({
    "source": "BNN",
    "title": "BNN Incident",
    "message": "10/06/2026 1:15 PM | NJ | Hudson | Jersey City | Working Fire Alert | 100 Main St | Operations",
    "subtype": "INCIDENT",
    "category": "PUBLIC_SAFETY",
    "tags": ["bnn"],
}, limit=20)
normalized = {" ".join(value.upper().split()) for value in choices}
assert "HUDSON JERSEY" not in normalized
assert "JERSEY WORKING" not in normalized
assert any(value == "HUDSON" for value in normalized)
assert any(value == "JERSEY CITY" for value in normalized)
print("WATCH PREVIEW PASS: keyword suggestions never form phrases across a BNN pipe boundary")

with core.db_conn() as conn:
    after = conn.execute(
        """SELECT
             (SELECT count(*) FROM watch_items) AS watches,
             (SELECT count(*) FROM alert_watch_matches) AS matches,
             (SELECT count(*) FROM deliveries) AS deliveries"""
    ).fetchone()
assert dict(after) == dict(before), (before, after)
print("WATCH PREVIEW PASS: historical evaluation creates no Watch, Match, delivery or Notification")
