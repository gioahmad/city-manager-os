# Standard Alert Schema

## Status

**Schema version:** 1.0  
**Status:** Canonical for Phase 1

Every production source workflow must normalize its source-specific data into this alert contract before the central router sees it.

The goal is simple: the router should not need special logic for PSEG vs Fire vs Weather vs Traffic.

## Core Rule

> Source workflows understand their source. The router understands only the Standard Alert Schema.

## Canonical v1 Fields

### Identity

| Field | Required | Purpose |
|---|---|---|
| `schema_version` | Yes | Contract version; currently `1.0` |
| `alert_id` | Yes | Stable City Manager OS alert identity |
| `source` | Yes | Normalized source such as `PSEG`, `FIRE_SDR`, `NWS`, `NJ511` |
| `source_event_id` | Recommended | Stable ID from the upstream source when available |

### Classification / Lifecycle

| Field | Required | Purpose |
|---|---|---|
| `category` | Yes | Broad domain such as `UTILITY`, `FIRE`, `WEATHER`, `TRAFFIC`, `OEM`, `EVENT` |
| `subtype` | Yes | More specific incident type such as `POWER_OUTAGE`, `2ND_ALARM`, `FLASH_FLOOD_WARNING` |
| `status` | Yes | Current state of the event |
| `event_action` | Yes | What this payload represents in the event lifecycle |
| `priority` | Yes | Internal alert priority from 1 to 5 |

Recommended `status` values:

```text
ACTIVE
RESOLVED
CANCELLED
EXPIRED
UNKNOWN
```

Recommended `event_action` values:

```text
NEW
UPDATE
RESOLVED
CANCELLED
```

This allows a single real-world event to be updated without pretending each change is a brand-new incident.

## Human-Facing Content

| Field | Required | Purpose |
|---|---|---|
| `title` | Yes | Short display/notification title |
| `message` | Yes | Human-readable summary |
| `tags` | No | Normalized labels and optional ntfy tags |
| `click_url` | No | Preferred URL to open from a notification |
| `source_url` | No | Original/source reference URL |

The router may adapt presentation for each delivery channel later, but source workflows should provide a useful default `title` and `message`.

## Jurisdiction / Location

Top-level jurisdiction fields are intentionally duplicated from the location object because they are frequent routing keys.

| Field | Required | Purpose |
|---|---|---|
| `county` | No | Normalized county, e.g. `HUDSON` |
| `municipality` | No | Normalized municipality, e.g. `WEEHAWKEN` |
| `location` | Yes | Structured location object; fields may be blank/null |

### `location` object

```json
{
  "label": "4100 Park Avenue",
  "address": "4100 Park Ave",
  "state": "NJ",
  "zip": "07086",
  "latitude": 40.0,
  "longitude": -74.0,
  "block": "34",
  "lot": "12",
  "qualifier": "",
  "parcel_id": ""
}
```

All location subfields are optional in meaning; an unresolved source may send empty strings/nulls. GIS enrichment can populate them later.

For an area-wide alert, `location.label` may describe the area and the specific property fields remain blank.

## Time Fields

All timestamps should be ISO 8601 strings including timezone whenever possible.

| Field | Required | Purpose |
|---|---|---|
| `observed_at` | Yes | When the underlying event began/was first observed, if known; otherwise best source event time |
| `source_updated_at` | No | When the upstream source says this event was last updated |
| `received_at` | Yes | When City Manager OS/n8n received or normalized this payload |
| `expires_at` | No | Explicit expiration time if known |

Do not use poll time as event start time when a true source time exists.

## Metadata

`metadata` is the controlled escape hatch for source-specific useful data.

Example PSEG metadata:

```json
{
  "customers_out": 42,
  "customers_served": 25203,
  "etr": "2026-08-18T15:30:00-04:00",
  "jobs": 1,
  "circuits": 1
}
```

Example Fire metadata:

```json
{
  "alarm_level": 2,
  "dispatch_channel": "North Hudson Fire",
  "transcript_confidence": 0.92
}
```

Rules:

1. Routing-critical information belongs in canonical fields whenever possible.
2. Source-only details belong in `metadata`.
3. `metadata` must remain valid JSON.
4. Do not make the router depend on arbitrary metadata unless a feature explicitly promotes that field into the canonical schema later.
5. Do not store credentials/secrets in metadata.

## Stable `alert_id`

This is critical for deduplication and event updates.

Preferred rule:

```text
SOURCE + EVENT TYPE + STABLE SOURCE EVENT ID
```

Example:

```text
PSEG:OUTAGE:123456
NWS:ALERT:urn-oid-12345
NJ511:INCIDENT:98765
```

If the source has no stable event ID, the source workflow must create a deterministic ID from stable incident properties. It must **not** include poll time or a random value if that would cause the same event to look new on every poll.

`alert_id` stays the same across `NEW`, `UPDATE`, and `RESOLVED` payloads for the same underlying event.

## Priority Scale

City Manager OS uses an internal five-level priority scale:

```text
1 = Informational / very low
2 = Advisory / low
3 = Normal operational alert
4 = Important / high
5 = Critical / immediate attention
```

Source workflows map their own severity systems into this common scale.

Priority is not the same as category or status. A resolved critical incident may still have `priority: 5` while `status: RESOLVED` and `event_action: RESOLVED`.

## Search Text

The router should build a normalized `search_text` at runtime from canonical fields rather than requiring every source to send it.

Recommended ingredients:

```text
source
category
subtype
status
county
municipality
location.label
location.address
title
message
tags
```

This keeps search normalization consistent in one place.

## Example — PSEG New Outage

```json
{
  "schema_version": "1.0",
  "alert_id": "PSEG:OUTAGE:123456",
  "source": "PSEG",
  "source_event_id": "123456",
  "category": "UTILITY",
  "subtype": "POWER_OUTAGE",
  "status": "ACTIVE",
  "event_action": "NEW",
  "title": "PSEG - Weehawken - 42 Out",
  "message": "42 customers are without power in Weehawken.",
  "priority": 4,
  "county": "HUDSON",
  "municipality": "WEEHAWKEN",
  "location": {
    "label": "Weehawken",
    "address": "",
    "state": "NJ",
    "zip": "",
    "latitude": null,
    "longitude": null,
    "block": "",
    "lot": "",
    "qualifier": "",
    "parcel_id": ""
  },
  "tags": ["power", "outage"],
  "click_url": "https://outagecenter.pseg.com/",
  "source_url": "https://outagecenter.pseg.com/",
  "observed_at": "2026-08-18T13:00:00-04:00",
  "source_updated_at": "2026-08-18T13:05:00-04:00",
  "received_at": "2026-08-18T13:05:15-04:00",
  "expires_at": null,
  "metadata": {
    "customers_out": 42,
    "customers_served": 25203,
    "etr": "2026-08-18T15:30:00-04:00"
  }
}
```

## Example — Fire

```json
{
  "schema_version": "1.0",
  "alert_id": "FIRE_SDR:INCIDENT:20260818-4100-PARK-01",
  "source": "FIRE_SDR",
  "source_event_id": "",
  "category": "FIRE",
  "subtype": "2ND_ALARM",
  "status": "ACTIVE",
  "event_action": "NEW",
  "title": "2nd Alarm - Weehawken",
  "message": "Second alarm transmitted for a working fire at 4100 Park Avenue.",
  "priority": 5,
  "county": "HUDSON",
  "municipality": "WEEHAWKEN",
  "location": {
    "label": "4100 Park Avenue",
    "address": "4100 Park Ave",
    "state": "NJ",
    "zip": "07086",
    "latitude": null,
    "longitude": null,
    "block": "",
    "lot": "",
    "qualifier": "",
    "parcel_id": ""
  },
  "tags": ["fire", "2nd-alarm"],
  "click_url": "",
  "source_url": "",
  "observed_at": "2026-08-18T14:10:00-04:00",
  "source_updated_at": null,
  "received_at": "2026-08-18T14:10:12-04:00",
  "expires_at": null,
  "metadata": {
    "alarm_level": 2,
    "transcript_confidence": 0.92
  }
}
```

## Example — Weather

```json
{
  "schema_version": "1.0",
  "alert_id": "NWS:ALERT:example-12345",
  "source": "NWS",
  "source_event_id": "example-12345",
  "category": "WEATHER",
  "subtype": "FLASH_FLOOD_WARNING",
  "status": "ACTIVE",
  "event_action": "NEW",
  "title": "Flash Flood Warning - Hudson County",
  "message": "Flash Flood Warning affecting portions of Hudson County.",
  "priority": 5,
  "county": "HUDSON",
  "municipality": "",
  "location": {
    "label": "Hudson County",
    "address": "",
    "state": "NJ",
    "zip": "",
    "latitude": null,
    "longitude": null,
    "block": "",
    "lot": "",
    "qualifier": "",
    "parcel_id": ""
  },
  "tags": ["weather", "flood"],
  "click_url": "",
  "source_url": "",
  "observed_at": "2026-08-18T14:20:00-04:00",
  "source_updated_at": "2026-08-18T14:20:00-04:00",
  "received_at": "2026-08-18T14:20:05-04:00",
  "expires_at": "2026-08-18T17:00:00-04:00",
  "metadata": {
    "severity": "Severe",
    "certainty": "Likely"
  }
}
```

## Example — Traffic

```json
{
  "schema_version": "1.0",
  "alert_id": "NJ511:INCIDENT:98765",
  "source": "NJ511",
  "source_event_id": "98765",
  "category": "TRAFFIC",
  "subtype": "ROAD_CLOSURE",
  "status": "ACTIVE",
  "event_action": "NEW",
  "title": "Road Closure - Weehawken",
  "message": "Roadway closed due to an incident near the Lincoln Tunnel approach.",
  "priority": 4,
  "county": "HUDSON",
  "municipality": "WEEHAWKEN",
  "location": {
    "label": "Lincoln Tunnel Approach",
    "address": "",
    "state": "NJ",
    "zip": "",
    "latitude": null,
    "longitude": null,
    "block": "",
    "lot": "",
    "qualifier": "",
    "parcel_id": ""
  },
  "tags": ["traffic", "closure"],
  "click_url": "",
  "source_url": "",
  "observed_at": "2026-08-18T14:30:00-04:00",
  "source_updated_at": "2026-08-18T14:35:00-04:00",
  "received_at": "2026-08-18T14:35:08-04:00",
  "expires_at": null,
  "metadata": {
    "direction": "eastbound"
  }
}
```

## v1 Required Fields

At minimum every source must provide:

```text
schema_version
alert_id
source
category
subtype
status
event_action
title
message
priority
location
observed_at
received_at
metadata
```

`county`, `municipality`, `source_event_id`, URLs, tags, expiration, and detailed location fields may be blank/null when unavailable.

## Versioning Rule

New optional fields can usually be added without changing the major schema version.

A breaking change that renames/removes fields or changes their meaning requires a new schema version and an explicit migration decision.
