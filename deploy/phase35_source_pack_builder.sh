#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="36833baf9e115d97a4a3fbab8eb08ff0eacaeddd"
BRANCH="feature/regional-event-source-pack-1"
SELF="deploy/phase35_source_pack_builder.sh"
TARGET="deploy/phase35_regional_event_source_pack.sh"
MIGRATION="deploy/postgis/init/025_regional_event_source_pack.sql"
TESTFILE="dashboard/tests/test_event_source_pack.py"

cd "$REPO"

echo "============================================================"
echo "#35 REGIONAL EVENT SOURCE PACK BUILDER"
echo "============================================================"

git fetch origin main "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"

[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ -z "$(git status --porcelain)" ]

python3 - <<'PY'
from pathlib import Path

# ------------------------------------------------------------------
# integration_runtime.py: JSON-LD event parser + reusable event window
# ------------------------------------------------------------------
p = Path("dashboard/integration_runtime.py")
s = p.read_text()

old = "from datetime import datetime, timezone\nfrom email.utils import parsedate_to_datetime\nfrom typing import Any\n"
new = "from datetime import datetime, timedelta, timezone\nfrom email.utils import parsedate_to_datetime\nfrom html.parser import HTMLParser\nfrom typing import Any\n"
if old not in s:
    raise SystemExit("integration_runtime import anchor missing")
s = s.replace(old, new, 1)

marker = "\ndef parse_events(body_text: str, parser_kind: str, parser_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:\n"
if marker not in s:
    raise SystemExit("parse_events marker missing")

insert = r'''

class _JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {str(k).lower(): str(v or "") for k, v in attrs}
        self._capture = "ld+json" in attr_map.get("type", "").lower()
        if self._capture:
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            payload = "".join(self._parts).strip()
            if payload:
                self.scripts.append(payload)
            self._capture = False
            self._parts = []


def _jsonld_types(value: Any) -> set[str]:
    raw = value if isinstance(value, list) else [value]
    output: set[str] = set()
    for item in raw:
        if item is None:
            continue
        text = str(item).strip().lower()
        if not text:
            continue
        output.add(text)
        output.add(text.rsplit("/", 1)[-1].rsplit("#", 1)[-1])
    return output


def _jsonld_walk(value: Any):
    if isinstance(value, dict):
        types = _jsonld_types(value.get("@type"))
        schema_event = any(
            item == "event"
            or item.endswith("event")
            or item in {"festival", "hackathon"}
            for item in types
        )
        if schema_event:
            yield value
        for child in value.values():
            yield from _jsonld_walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _jsonld_walk(child)


def _jsonld_address_text(address: Any) -> str | None:
    if isinstance(address, str):
        return _text(address)
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("streetAddress"),
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("postalCode"),
    ]
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id")
    parts.append(country)
    return _text(", ".join(str(x).strip() for x in parts if x))


def parse_jsonld_events(body_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    collector = _JsonLdCollector()
    collector.feed(body_text)
    defaults = config.get("defaults") or {}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    for script in collector.scripts:
        try:
            root = json.loads(script)
        except json.JSONDecodeError:
            continue

        for raw in _jsonld_walk(root):
            location = raw.get("location")
            if isinstance(location, list):
                location = next((x for x in location if isinstance(x, (dict, str))), None)

            venue = None
            address_obj: Any = None
            geo: Any = None
            if isinstance(location, dict):
                venue = location.get("name")
                address_obj = location.get("address")
                geo = location.get("geo")
            elif isinstance(location, str):
                venue = location

            if not isinstance(geo, dict):
                geo = raw.get("geo") if isinstance(raw.get("geo"), dict) else {}

            municipality = address_obj.get("addressLocality") if isinstance(address_obj, dict) else None
            state = address_obj.get("addressRegion") if isinstance(address_obj, dict) else None

            status = raw.get("eventStatus")
            if isinstance(status, str) and "/" in status:
                status = status.rsplit("/", 1)[-1]
            if isinstance(status, str) and status.lower().startswith("event"):
                status = status[5:]

            item = {
                "id": raw.get("@id") or raw.get("url"),
                "title": raw.get("name"),
                "description": raw.get("description"),
                "start": raw.get("startDate"),
                "end": raw.get("endDate"),
                "venue": venue or defaults.get("venue"),
                "address": _jsonld_address_text(address_obj) or defaults.get("address"),
                "municipality": municipality or defaults.get("municipality"),
                "state": state or defaults.get("state"),
                "url": raw.get("url"),
                "status": status,
                "latitude": geo.get("latitude") if isinstance(geo, dict) else None,
                "longitude": geo.get("longitude") if isinstance(geo, dict) else None,
            }
            mapping = {key: key for key in item}
            event = _event_from_mapping(item, mapping, defaults)
            dedupe_key = event.get("external_key") or event["fingerprint"]
            if dedupe_key in seen:
                continue
            seen.add(str(dedupe_key))
            output.append(event)

    return output


def _filter_event_window(events: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = config.get("defaults") or {}
    future_only = bool(config.get("future_only", defaults.get("future_only", False)))
    past_grace_hours = float(config.get("past_grace_hours", defaults.get("past_grace_hours", 0)) or 0)
    future_days_raw = config.get("future_days", defaults.get("future_days"))
    future_days = float(future_days_raw) if future_days_raw not in (None, "") else None

    if not future_only and future_days is None:
        return events

    now = datetime.now(timezone.utc)
    lower = now - timedelta(hours=past_grace_hours)
    upper = now + timedelta(days=future_days) if future_days is not None else None
    filtered: list[dict[str, Any]] = []

    for event in events:
        start = event.get("starts_at")
        end = event.get("ends_at")
        reference = end or start
        if future_only and reference is not None and reference < lower:
            continue
        if upper is not None and start is not None and start > upper:
            continue
        filtered.append(event)

    return filtered
'''
s = s.replace(marker, insert + marker, 1)

old_parse = '''def parse_events(body_text: str, parser_kind: str, parser_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parser_kind = (parser_kind or "NONE").upper()
    config = parser_config or {}
    if parser_kind == "JSON_EVENTS":
        return parse_json_events(body_text, config)
    if parser_kind in {"RSS_EVENTS", "ATOM_EVENTS"}:
        return parse_rss_events(body_text, config)
    if parser_kind == "ICS_EVENTS":
        return parse_ics_events(body_text, config)
    if parser_kind == "NONE":
        return []
    raise ValueError(f"Unsupported parser kind: {parser_kind}")
'''
new_parse = '''def parse_events(body_text: str, parser_kind: str, parser_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parser_kind = (parser_kind or "NONE").upper()
    config = parser_config or {}
    if parser_kind == "JSON_EVENTS":
        events = parse_json_events(body_text, config)
    elif parser_kind in {"RSS_EVENTS", "ATOM_EVENTS"}:
        events = parse_rss_events(body_text, config)
    elif parser_kind == "ICS_EVENTS":
        events = parse_ics_events(body_text, config)
    elif parser_kind == "JSONLD_EVENTS":
        events = parse_jsonld_events(body_text, config)
    elif parser_kind == "NONE":
        return []
    else:
        raise ValueError(f"Unsupported parser kind: {parser_kind}")
    return _filter_event_window(events, config)
'''
if old_parse not in s:
    raise SystemExit("parse_events function anchor missing")
s = s.replace(old_parse, new_parse, 1)
p.write_text(s)

# ------------------------------------------------------------------
# source_onboarding.py + legacy integrations_app.py parser catalog
# ------------------------------------------------------------------
for filename in ("dashboard/source_onboarding.py", "dashboard/integrations_app.py"):
    p = Path(filename)
    s = p.read_text()
    old = 'PARSER_KINDS = ["NONE", "JSON_EVENTS", "RSS_EVENTS", "ATOM_EVENTS", "ICS_EVENTS"]'
    new = 'PARSER_KINDS = ["NONE", "JSON_EVENTS", "RSS_EVENTS", "ATOM_EVENTS", "ICS_EVENTS", "JSONLD_EVENTS"]'
    if old not in s:
        raise SystemExit(f"parser catalog anchor missing in {filename}")
    s = s.replace(old, new, 1)
    p.write_text(s)

p = Path("dashboard/source_onboarding.py")
s = p.read_text()
anchor = '    "ICS": dict(label="ICS Calendar", description="Public iCalendar feed normalized into regional event intelligence.", category="EVENTS", adapter_type="HTTP", method="GET", parser_kind="ICS_EVENTS", headers={"Accept": "text/calendar, text/plain"}, query={}, parser_config={"defaults": {"event_type": "EVENT", "default_timezone": "America/New_York"}}, poll_seconds=900, map_capable=False),\n'
addition = anchor + '    "JSONLD": dict(label="JSON-LD Events Page", description="Public webpage containing schema.org Event JSON-LD. No scraping selectors required.", category="EVENTS", adapter_type="HTTP", method="GET", parser_kind="JSONLD_EVENTS", headers={"Accept": "text/html, application/xhtml+xml"}, query={}, parser_config={"future_only": True, "past_grace_hours": 6, "defaults": {"event_type": "EVENT", "default_timezone": "America/New_York"}}, poll_seconds=1800, map_capable=True),\n'
if anchor not in s:
    raise SystemExit("source onboarding ICS template anchor missing")
s = s.replace(anchor, addition, 1)
p.write_text(s)

# ------------------------------------------------------------------
# integration_engine.py: silent baseline option + cross-source alert dedupe
# ------------------------------------------------------------------
p = Path("dashboard/integration_engine.py")
s = p.read_text()
old = 'def _upsert_event(conn, integration: dict[str, Any], event: dict[str, Any]) -> bool:\n'
new = 'def _upsert_event(conn, integration: dict[str, Any], event: dict[str, Any], *, suppress_alerts: bool = False) -> bool:\n'
if old not in s:
    raise SystemExit("_upsert_event signature anchor missing")
s = s.replace(old, new, 1)

old = '''        near_term_alert = bool(
            event.get("impact_level") == "ALERT"
            and (starts_at is None or (starts_at - datetime.now(timezone.utc)).total_seconds() <= 72 * 3600)
        )

        cur.execute(
'''
new = '''        near_term_alert = bool(
            not suppress_alerts
            and event.get("impact_level") == "ALERT"
            and (starts_at is None or (starts_at - datetime.now(timezone.utc)).total_seconds() <= 72 * 3600)
        )
        if near_term_alert:
            cur.execute(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM event_intelligence
                  WHERE fingerprint=%s
                    AND source_event_key<>%s
                    AND active=true
                    AND impact_level='ALERT'
                    AND (alert_pending=true OR alert_emitted_at IS NOT NULL)
                ) AS duplicate_alert
                """,
                (event["fingerprint"], source_event_key),
            )
            if bool(cur.fetchone()["duplicate_alert"]):
                near_term_alert = False

        cur.execute(
'''
if old not in s:
    raise SystemExit("near-term alert anchor missing")
s = s.replace(old, new, 1)

old = 'def run_integration(integration: dict[str, Any], *, run_type: str = "POLL", parse_and_store: bool = True) -> dict[str, Any]:\n'
new = 'def run_integration(integration: dict[str, Any], *, run_type: str = "POLL", parse_and_store: bool = True, suppress_alerts: bool = False) -> dict[str, Any]:\n'
if old not in s:
    raise SystemExit("run_integration signature anchor missing")
s = s.replace(old, new, 1)
old = '                    if _upsert_event(event_conn, integration, event):\n'
new = '                    if _upsert_event(event_conn, integration, event, suppress_alerts=suppress_alerts):\n'
if old not in s:
    raise SystemExit("_upsert_event call anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)
PY

cat > "$MIGRATION" <<'SQL'
-- #35 Regional Event Source Pack 1
-- Register broad public event sources without requiring provider-specific
-- alert systems. New sources start paused and are activated only after TEST.

ALTER TABLE integrations
  DROP CONSTRAINT IF EXISTS integrations_parser_kind_check;

ALTER TABLE integrations
  ADD CONSTRAINT integrations_parser_kind_check
  CHECK (parser_kind IN (
    'NONE','JSON_EVENTS','RSS_EVENTS','ATOM_EVENTS','ICS_EVENTS','JSONLD_EVENTS'
  ));

INSERT INTO integrations (
  integration_key,name,active,category,adapter_type,endpoint_url,method,
  auth_type,auth_config,request_headers,request_query,parser_kind,parser_config,
  poll_seconds,timeout_seconds,max_response_bytes,allow_redirects,verify_tls,
  notes,provider_template,source_owner,access_instructions,geography_scope,
  relevance_keywords,attention_config,map_config
)
VALUES
(
  'HUDSON_COUNTY_EVENTS','Hudson County / Visit Hudson Events',false,'EVENTS','HTTP',
  'https://www.visithudson.org/calendar/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"COUNTY_EVENT","county":"Hudson","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official Hudson County tourism/cultural events page. Uses generic schema.org Event JSON-LD when exposed by the public page.',
  'JSONLD','Hudson County Office of Cultural and Heritage Affairs / Tourism Development',
  'Public source; no account required. Leave paused if the site stops exposing parseable Event JSON-LD.',
  'Hudson County, NJ','Hudson County, Weehawken, Hoboken, Jersey City, Union City, North Bergen, West New York, Secaucus',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Hudson County","Weehawken","Hoboken","Jersey City","Union City","North Bergen","West New York","Secaucus"]}'::jsonb,
  '{}'::jsonb
),
(
  'BERGEN_COUNTY_EVENTS','Bergen County Official Events',false,'EVENTS','HTTP',
  'https://bergencountynj.gov/events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"COUNTY_EVENT","county":"Bergen","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official Bergen County events archive. Uses generic schema.org Event JSON-LD when exposed by the public page.',
  'JSONLD','Bergen County',
  'Public source; no account required. Leave paused if the archive does not expose parseable Event JSON-LD.',
  'Bergen County, NJ / Meadowlands corridor','Bergen County, East Rutherford, Meadowlands, MetLife Stadium, Overpeck',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Bergen County","East Rutherford","Meadowlands","MetLife Stadium","Overpeck"]}'::jsonb,
  '{}'::jsonb
),
(
  'JERSEY_CITY_CULTURAL_EVENTS','Jersey City Cultural Affairs Events',false,'EVENTS','HTTP',
  'https://jerseycityculture.org/events/?ical=1','GET','NONE','{}'::jsonb,
  '{"Accept":"text/calendar, text/plain","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'ICS_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"MUNICIPAL_EVENT","municipality":"Jersey City","county":"Hudson","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Jersey City Office of Cultural Affairs public events calendar.',
  'ICS','Jersey City Office of Cultural Affairs',
  'Public iCalendar source; no account required.',
  'Jersey City, NJ','Jersey City, Liberty State Park, waterfront, festival, parade, concert',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Jersey City","Liberty State Park","waterfront","festival","parade","concert"]}'::jsonb,
  '{}'::jsonb
),
(
  'METLIFE_OFFICIAL_EVENTS','MetLife Stadium Official Events',false,'EVENTS','HTTP',
  'https://www.metlifestadium.com/events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"MetLife Stadium","municipality":"East Rutherford","county":"Bergen","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,3000000,true,true,
  'Official MetLife Stadium event calendar. Ticketmaster remains the structured metro discovery source when its API key is available.',
  'JSONLD','MetLife Stadium','Public venue source; no account required.',
  'East Rutherford / Meadowlands','MetLife Stadium, Meadowlands, Giants, Jets, concert, Route 3, NJ Transit',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["MetLife Stadium","Meadowlands","Giants","Jets","concert","Route 3","NJ Transit"]}'::jsonb,
  '{}'::jsonb
),
(
  'JAVITS_OFFICIAL_EVENTS','Javits Center Official Calendar',false,'EVENTS','HTTP',
  'https://www.javitscenter.com/calendar/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"Javits Center","municipality":"Manhattan","state":"NY","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Javits Center public calendar for major conventions and West Side demand.',
  'JSONLD','Javits Center','Public venue source; no account required.',
  'Manhattan West Side / Lincoln Tunnel approach','Javits Center, convention, expo, Comic Con, West Side, Lincoln Tunnel',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Javits Center","convention","expo","Comic Con","West Side","Lincoln Tunnel"]}'::jsonb,
  '{}'::jsonb
),
(
  'PRUDENTIAL_OFFICIAL_EVENTS','Prudential Center Official Events',false,'EVENTS','HTTP',
  'https://www.prucenter.com/events','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"Prudential Center","municipality":"Newark","county":"Essex","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official Prudential Center public event calendar; many events are also covered by Ticketmaster.',
  'JSONLD','Prudential Center','Public venue source; no account required.',
  'Newark, NJ','Prudential Center, Newark, Devils, Seton Hall, concert',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Prudential Center","Newark","Devils","Seton Hall","concert"]}'::jsonb,
  '{}'::jsonb
),
(
  'NJPAC_OFFICIAL_EVENTS','NJPAC Official Events',false,'EVENTS','HTTP',
  'https://www.njpac.org/tickets-events/','GET','NONE','{}'::jsonb,
  '{"Accept":"text/html, application/xhtml+xml","User-Agent":"CityManagerOS/1.0"}'::jsonb,
  '{}'::jsonb,'JSONLD_EVENTS',
  '{"future_only":true,"past_grace_hours":6,"defaults":{"event_type":"VENUE_EVENT","venue":"NJPAC","municipality":"Newark","county":"Essex","state":"NJ","default_timezone":"America/New_York"}}'::jsonb,
  1800,20,5000000,true,true,
  'Official New Jersey Performing Arts Center public event calendar.',
  'JSONLD','NJPAC','Public venue source; no account required.',
  'Newark, NJ','NJPAC, Newark, concert, performance',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["NJPAC","Newark","concert","performance"]}'::jsonb,
  '{}'::jsonb
),
(
  'EVENTBRITE_DISCOVERY_PLACEHOLDER','Eventbrite Discovery Placeholder',false,'EVENTS','HTTP',
  '','GET','NONE','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'NONE','{}'::jsonb,
  3600,15,1000000,true,true,
  'Placeholder only. Eventbrite broad public event search was deprecated/shut down; use organizer- or venue-specific access only if a future supported source is identified.',
  'CUSTOM','Eventbrite',
  'Do not activate as broad discovery. Add a supported organizer/venue endpoint later through Source Onboarding if useful.',
  'Regional','Eventbrite',
  '{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["Eventbrite"]}'::jsonb,
  '{}'::jsonb
)
ON CONFLICT (integration_key) DO UPDATE SET
  name=EXCLUDED.name,
  active=integrations.active,
  category=EXCLUDED.category,
  adapter_type=EXCLUDED.adapter_type,
  endpoint_url=EXCLUDED.endpoint_url,
  method=EXCLUDED.method,
  auth_type=EXCLUDED.auth_type,
  auth_config=EXCLUDED.auth_config,
  request_headers=EXCLUDED.request_headers,
  request_query=EXCLUDED.request_query,
  parser_kind=EXCLUDED.parser_kind,
  parser_config=EXCLUDED.parser_config,
  poll_seconds=EXCLUDED.poll_seconds,
  timeout_seconds=EXCLUDED.timeout_seconds,
  max_response_bytes=EXCLUDED.max_response_bytes,
  allow_redirects=EXCLUDED.allow_redirects,
  verify_tls=EXCLUDED.verify_tls,
  notes=EXCLUDED.notes,
  provider_template=EXCLUDED.provider_template,
  source_owner=EXCLUDED.source_owner,
  access_instructions=EXCLUDED.access_instructions,
  geography_scope=EXCLUDED.geography_scope,
  relevance_keywords=EXCLUDED.relevance_keywords,
  attention_config=EXCLUDED.attention_config,
  map_config=EXCLUDED.map_config,
  updated_at=now();

UPDATE integrations
SET provider_template='GENERIC_JSON',
    source_owner='Ticketmaster',
    access_instructions='Add TICKETMASTER_API_KEY to the dashboard/integration-engine environment, run browser TEST, seed silently, then activate.',
    geography_scope='25-mile Weehawken metro',
    relevance_keywords='MetLife Stadium, Meadowlands, Madison Square Garden, Prudential Center, Barclays Center, Yankee Stadium, Citi Field, concert, sports',
    attention_config='{"watch_threshold":45,"alert_threshold":75,"relevance_keywords":["MetLife Stadium","Meadowlands","Madison Square Garden","Prudential Center","Barclays Center","Yankee Stadium","Citi Field","concert","sports"]}'::jsonb,
    notes='High-value optional metro discovery source. Requires TICKETMASTER_API_KEY. Discovery API covers Ticketmaster and additional Ticketmaster discovery platforms.',
    updated_at=now()
WHERE integration_key='TICKETMASTER_METRO_EVENTS';
SQL

cat > "$TESTFILE" <<'PYTEST'
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import integration_engine
import integration_runtime as rt
import source_onboarding as so


def test_jsonld_event_parser_extracts_schema_event():
    html = '''
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Event","@id":"event-1","name":"MetLife Test Concert",
       "startDate":"2099-09-11T20:00:00-04:00",
       "endDate":"2099-09-11T23:00:00-04:00",
       "url":"https://example.test/event-1",
       "location":{"@type":"Place","name":"MetLife Stadium",
         "address":{"streetAddress":"1 Stadium Dr","addressLocality":"East Rutherford","addressRegion":"NJ","postalCode":"07073"},
         "geo":{"@type":"GeoCoordinates","latitude":40.8,"longitude":-74.0}}}
    ]}
    </script></head></html>
    '''
    events = rt.parse_events(
        html,
        "JSONLD_EVENTS",
        {"future_only": True, "defaults": {"default_timezone": "America/New_York"}},
    )
    assert len(events) == 1
    event = events[0]
    assert event["title"] == "MetLife Test Concert"
    assert event["venue"] == "MetLife Stadium"
    assert event["municipality"] == "East Rutherford"
    assert event["state"] == "NJ"
    assert event["latitude"] == 40.8
    assert event["longitude"] == -74.0
    assert event["impact_level"] in {"WATCH", "ALERT"}


def test_future_only_window_drops_historical_events():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    body = f'''[
      {{"id":"old","title":"Old","start":"{old}"}},
      {{"id":"future","title":"Future","start":"{future}"}}
    ]'''
    events = rt.parse_events(
        body,
        "JSON_EVENTS",
        {
            "future_only": True,
            "past_grace_hours": 1,
            "mapping": {"id": "id", "title": "title", "start": "start"},
            "defaults": {"default_timezone": "UTC"},
        },
    )
    assert [e["title"] for e in events] == ["Future"]


def test_source_onboarding_exposes_jsonld_template():
    assert "JSONLD_EVENTS" in so.PARSER_KINDS
    assert so.SOURCE_TEMPLATES["JSONLD"]["parser_kind"] == "JSONLD_EVENTS"


def test_run_integration_supports_silent_baseline():
    assert "suppress_alerts" in inspect.signature(integration_engine.run_integration).parameters
PYTEST

cat > "$TARGET" <<'DEPLOY'
#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="36833baf9e115d97a4a3fbab8eb08ff0eacaeddd"
BRANCH="feature/regional-event-source-pack-1"
MIGRATION="deploy/postgis/init/025_regional_event_source_pack.sql"

cd "$REPO"

PRODUCTION_TOUCHED=0
PROMOTED=0
ENGINE_STOPPED=0

cleanup_on_exit() {
  rc=$?
  if [ "$ENGINE_STOPPED" = "1" ]; then
    docker start citymanager-integration-engine >/dev/null 2>&1 || true
  fi

  if [ "$rc" -ne 0 ] && [ "$PROMOTED" = "0" ] && [ "$PRODUCTION_TOUCHED" = "1" ]; then
    echo
    echo "=== #35 FAILURE: DEACTIVATE NEW PACK + RESTORE MAIN APP ==="
    docker exec -i citymanager-postgis sh -lc \
      'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' || true
UPDATE integrations
SET active=false
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
);
SQL
    git switch main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
    docker compose -f dashboard/docker-compose.yml build citymanager-dashboard || true
    docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
      citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine || true
  fi

  if [ "$rc" -ne 0 ]; then
    echo
    echo "============================================================"
    echo "#35 REGIONAL EVENT SOURCE PACK: FAIL rc=$rc"
    echo "============================================================"
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

health_dashboard() {
  docker exec citymanager-dashboard python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=4); raise SystemExit(0 if r.status==200 else 1)' >/dev/null 2>&1
}

echo "============================================================"
echo "#35 REGIONAL EVENT SOURCE PACK 1"
echo "============================================================"

echo
echo "=== 1. PREFLIGHT ==="
git fetch origin main "$BRANCH"
[ -z "$(git status --porcelain)" ]
[ "$(git branch --show-current)" = "$BRANCH" ]
[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
echo "Feature HEAD: $(git rev-parse HEAD)"
echo "Base main: $BASE"
echo "Preflight: PASS"

echo
echo "=== 2. STATIC VALIDATION ==="
python3 -m py_compile \
  dashboard/integration_runtime.py \
  dashboard/integration_engine.py \
  dashboard/source_onboarding.py \
  dashboard/integrations_app.py
bash -n "$0"
grep -Fq 'JSONLD_EVENTS' dashboard/integration_runtime.py
grep -Fq 'JSONLD_EVENTS' "$MIGRATION"
grep -Fq 'HUDSON_COUNTY_EVENTS' "$MIGRATION"
grep -Fq 'EVENTBRITE_DISCOVERY_PLACEHOLDER' "$MIGRATION"
echo "Static validation: PASS"

echo
echo "=== 3. BUILD FEATURE IMAGE ==="
docker compose -f dashboard/docker-compose.yml build citymanager-dashboard

echo
echo "=== 4. TARGETED TESTS ==="
docker compose -f dashboard/docker-compose.yml run --rm --no-deps \
  -v "$REPO/dashboard:/src:ro" -w /src -e PYTHONPATH=/src:/app \
  --entrypoint pytest citymanager-dashboard -p no:cacheprovider -q \
  tests/test_event_source_pack.py \
  tests/test_source_onboarding.py \
  tests/test_attention_engine.py \
  tests/test_gis_import.py

echo "Targeted tests: PASS"

echo
echo "=== 5. BACKUP SAFETY GATE ==="
./deploy/postgis/backup.sh
./deploy/postgis/verify-backup.sh
echo "Backup safety gate: PASS"

echo
echo "=== 6. APPLY ADDITIVE SOURCE PACK MIGRATION ==="
PRODUCTION_TOUCHED=1
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"

echo
echo "=== 7. VERIFY SOURCE REGISTRY ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT integration_key,active,parser_kind,provider_template,endpoint_url
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
)
ORDER BY integration_key;

SELECT conname,pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid='integrations'::regclass
  AND conname='integrations_parser_kind_check';
SQL

COUNT="$(docker exec -i citymanager-postgis sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT count(*)
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
);
SQL
)"
[ "$COUNT" = "9" ]
echo "Source registry: 9/9 PASS"

echo
echo "=== 8. DEPLOY FEATURE BUILD ==="
docker compose -f dashboard/docker-compose.yml up -d --force-recreate \
  citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine
for _ in $(seq 1 45); do
  health_dashboard && break
  sleep 2
done
health_dashboard
echo "Dashboard internal health: PASS"

echo
echo "=== 9. CONTROLLED READ-ONLY TEST + SILENT BASELINE ==="
docker stop citymanager-integration-engine >/dev/null
ENGINE_STOPPED=1

docker exec -i citymanager-dashboard python - <<'PY'
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

from integration_engine import load_integration, run_integration

BASE = "http://127.0.0.1:8000"
TOKEN = os.environ.get("CMOS_AUTOMATION_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("CMOS_AUTOMATION_TOKEN is required")

DB = dict(
    host=os.getenv("DB_HOST", "citymanager-postgis"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "citymanager"),
    user=os.getenv("DB_USER", "citymanager_app"),
    password=os.environ["DB_PASSWORD"],
    row_factory=dict_row,
)

CANDIDATES = [
    "HUDSON_COUNTY_EVENTS",
    "BERGEN_COUNTY_EVENTS",
    "JERSEY_CITY_CULTURAL_EVENTS",
    "METLIFE_OFFICIAL_EVENTS",
    "JAVITS_OFFICIAL_EVENTS",
    "PRUDENTIAL_OFFICIAL_EVENTS",
    "NJPAC_OFFICIAL_EVENTS",
]


def one(sql: str, params=()):
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def request(path: str, data: dict[str, str] | None = None):
    headers = {"X-CMOS-Automation-Key": TOKEN}
    payload = None
    method = "GET"
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(BASE + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def event_count(source_id):
    return one("SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s", (source_id,))["n"]


def test_and_maybe_activate(key: str):
    row = one("SELECT * FROM integrations WHERE integration_key=%s", (key,))
    if not row:
        raise AssertionError(f"missing source {key}")

    before = event_count(row["id"])
    status, body = request(f"/integrations/onboarding/{row['id']}/test", {})
    if status != 200:
        print(f"{key}: TEST HTTP {status}; LEFT PAUSED")
        return

    row = one("SELECT * FROM integrations WHERE integration_key=%s", (key,))
    after = event_count(row["id"])
    if after != before:
        raise AssertionError(f"{key}: browser TEST stored production events")

    summary = row.get("last_test_summary") or {}
    if isinstance(summary, str):
        summary = json.loads(summary)
    items = int(summary.get("items_found") or 0)
    print(
        f"{key}: test_ok={row['last_test_ok']} http={summary.get('http_status')} "
        f"items={items} bytes={summary.get('response_bytes')} error={summary.get('error')}"
    )

    if not row["last_test_ok"] or items <= 0:
        print(f"{key}: LEFT PAUSED (no parseable current events)")
        return

    integration = load_integration(integration_key=key)
    outcome = run_integration(
        integration,
        run_type="MANUAL",
        parse_and_store=True,
        suppress_alerts=True,
    )
    if not outcome.get("ok"):
        raise AssertionError(f"{key}: silent baseline failed: {outcome.get('error')}")

    pending = one(
        "SELECT count(*) AS n FROM event_intelligence WHERE source_integration_id=%s AND alert_pending=true",
        (row["id"],),
    )["n"]
    if pending != 0:
        raise AssertionError(f"{key}: silent baseline produced pending alerts")

    status, _ = request(f"/integrations/onboarding/{row['id']}/activate", {})
    if status != 200:
        raise AssertionError(f"{key}: activation HTTP {status}")
    active = one("SELECT active FROM integrations WHERE id=%s", (row["id"],))["active"]
    if not active:
        raise AssertionError(f"{key}: activation did not persist")
    print(f"{key}: SILENT BASELINE + ACTIVE")


for key in CANDIDATES:
    test_and_maybe_activate(key)

# Ticketmaster is valuable but must remain NEEDS SETUP until its secret exists.
tm = one("SELECT * FROM integrations WHERE integration_key='TICKETMASTER_METRO_EVENTS'")
if not tm:
    raise AssertionError("Ticketmaster source template missing")
if os.getenv("TICKETMASTER_API_KEY", "").strip():
    test_and_maybe_activate("TICKETMASTER_METRO_EVENTS")
else:
    print("TICKETMASTER_METRO_EVENTS: NEEDS SETUP (TICKETMASTER_API_KEY missing)")
    if tm["active"]:
        raise AssertionError("Ticketmaster active without required key")

# Eventbrite broad discovery is intentionally a documented placeholder.
eb = one("SELECT * FROM integrations WHERE integration_key='EVENTBRITE_DISCOVERY_PLACEHOLDER'")
assert eb and not eb["active"] and eb["parser_kind"] == "NONE"
print("EVENTBRITE_DISCOVERY_PLACEHOLDER: REGISTERED / PAUSED BY DESIGN")

print("SOURCE PACK CONTROLLED ACCEPTANCE: PASS")
PY

docker start citymanager-integration-engine >/dev/null
ENGINE_STOPPED=0

echo
echo "=== 10. POST-SEED SOURCE STATE ==="
docker exec -i citymanager-postgis sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT integration_key,active,parser_kind,last_test_ok,last_test_at,
       COALESCE(last_test_summary->>'items_found','') AS test_items,
       COALESCE(last_test_summary->>'error','') AS test_error
FROM integrations
WHERE integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS','EVENTBRITE_DISCOVERY_PLACEHOLDER'
)
ORDER BY integration_key;

SELECT i.integration_key,
       count(e.id) AS stored,
       count(e.id) FILTER (WHERE e.active) AS active_events,
       count(e.id) FILTER (WHERE e.alert_pending) AS pending_alerts,
       min(e.starts_at) FILTER (WHERE e.active) AS next_start,
       max(e.starts_at) FILTER (WHERE e.active) AS latest_start
FROM integrations i
LEFT JOIN event_intelligence e ON e.source_integration_id=i.id
WHERE i.integration_key IN (
  'HUDSON_COUNTY_EVENTS','BERGEN_COUNTY_EVENTS','JERSEY_CITY_CULTURAL_EVENTS',
  'METLIFE_OFFICIAL_EVENTS','JAVITS_OFFICIAL_EVENTS','PRUDENTIAL_OFFICIAL_EVENTS',
  'NJPAC_OFFICIAL_EVENTS','TICKETMASTER_METRO_EVENTS'
)
GROUP BY i.integration_key
ORDER BY i.integration_key;
SQL

echo
echo "=== 11. FEATURE HEALTH ==="
./deploy/cmos-health

echo
echo "=== 12. PROMOTE VERIFIED FEATURE ==="
git switch main
git merge --ff-only "$BRANCH"
git push origin main
PROMOTED=1
echo "Promoted main: $(git rev-parse HEAD)"

echo
echo "=== 13. POST-PROMOTION REGRESSION ==="
./deploy/cmos-e2e-secure
./deploy/security/verify-db-credential-alignment.sh FINAL
./deploy/postgis/verify-backup.sh
./deploy/cmos-health

echo
echo "============================================================"
echo "#35 REGIONAL EVENT SOURCE PACK 1: PASS"
echo "SOURCE REGISTRY: 9/9 PASS"
echo "JSON-LD PARSER: PASS"
echo "READ-ONLY TEST GATE: PASS"
echo "SILENT BASELINE: PASS"
echo "CROSS-SOURCE ALERT DEDUPE: INSTALLED"
echo "SECURE E2E: PASS"
echo "HEALTH: PASS"
echo "============================================================"
DEPLOY

chmod +x "$TARGET"
bash -n "$TARGET"
python3 -m py_compile \
  dashboard/integration_runtime.py \
  dashboard/integration_engine.py \
  dashboard/source_onboarding.py \
  dashboard/integrations_app.py

git add \
  dashboard/integration_runtime.py \
  dashboard/integration_engine.py \
  dashboard/source_onboarding.py \
  dashboard/integrations_app.py \
  "$MIGRATION" \
  "$TESTFILE" \
  "$TARGET"
git rm -q "$SELF"
git diff --cached --check

git commit -m "Build regional event source pack 1"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Prepared feature HEAD: $(git rev-parse HEAD)"
echo "Builder: PASS"
echo
exec bash "$TARGET"
