# Master Watchlist

## Status

**Schema version:** v1  
**Status:** Canonical for Phase 1

The Master Watchlist is the central list of things City Manager OS cares about. Each item exists once. Alerts are matched against these rows, and matching rows determine who should be notified and what additional GIS/context work should occur.

The v1 design intentionally works as a CSV first and maps cleanly into PostgreSQL later.

## Design Rules

1. **One row = one thing being watched.**
2. Keep the searchable value separate from the human-friendly name.
3. Store aliases once instead of duplicating watch rows.
4. Use structured alert fields when possible; use text matching when necessary.
5. GIS/property fields may remain blank until the item is enriched.
6. Recipient Boolean columns are acceptable for v1. They can be normalized into a separate assignment table later without changing the watch item itself.
7. Blank optional filters mean `ANY`.

## Watch Types

Initial supported values:

- `ADDRESS`
- `FACILITY`
- `PLACE`
- `AREA`
- `TOWN`
- `COUNTY`
- `PERSON`
- `KEYWORD`
- `PHRASE`
- `SOURCE`
- `CATEGORY`
- `INCIDENT_TYPE`

Additional types may be added later, but new types should not be created when one of these already describes the item.

## Canonical v1 Columns

### Identity / Administration

| Column | Required | Purpose |
|---|---|---|
| `watch_id` | Yes | Stable unique ID such as `W0001` |
| `active` | Yes | Master on/off switch |
| `watch_type` | Yes | Type from the supported list |
| `display_name` | Yes | Human-friendly label |
| `category` | No | Broad internal grouping |
| `subcategory` | No | More specific internal grouping |
| `parent_group` | No | Logical grouping such as `PORT_IMPERIAL` or `CRITICAL_INFRASTRUCTURE` |
| `tags` | No | Pipe-separated administrative tags |
| `starts_at` | No | Optional future activation date/time |
| `expires_at` | No | Optional expiration date/time for temporary watches |

### Search / Match

| Column | Required | Purpose |
|---|---|---|
| `search_term` | Yes | Primary normalized value to search for |
| `aliases` | No | Alternate values separated by `|` |
| `match_mode` | Yes | `FIELD`, `CONTAINS`, `WORD`, or `EXACT` |
| `match_field` | Conditional | Alert field to inspect; required for `FIELD` |
| `source_filter` | No | Restrict row to one/more sources, pipe-separated |
| `alert_category_filter` | No | Restrict row to one/more alert categories |
| `min_priority` | No | Minimum alert priority; default `1` |

### Location / Property / GIS

| Column | Required | Purpose |
|---|---|---|
| `address` | No | Clean street address |
| `municipality` | No | Municipality |
| `county` | No | County |
| `state` | No | State |
| `zip` | No | ZIP code |
| `block` | No | Tax block |
| `lot` | No | Tax lot |
| `qualifier` | No | Parcel qualifier |
| `parcel_id` | No | Stable parcel/PIN identifier |
| `latitude` | No | Latitude |
| `longitude` | No | Longitude |
| `gis_enabled` | No | Whether this row should participate in GIS enrichment |
| `gis_lookup` | No | Intended lookup type: `PARCEL`, `ADDRESS_POINT`, `AREA`, or blank |
| `nearby_enabled` | No | Whether proximity intelligence is enabled |
| `radius_ft` | No | Proximity radius in feet |

### Notes / Provenance

| Column | Required | Purpose |
|---|---|---|
| `source_notes` | No | Where/why the watch item was added |
| `notes` | No | Internal notes about the item |

### Recipients — v1

Initial Boolean recipient columns:

```text
gio
mayor
police
fire
dpw
oem
```

`TRUE` means that recipient is interested when this watch row matches. `FALSE` means they are not.

This is intentionally simple for the first version. If the number of recipients becomes unwieldy, these columns will be replaced by a `watch_item_recipients` table while preserving `watch_id` as the stable relationship key.

## Match Modes

### `FIELD`

Compare `search_term` against one structured alert field named by `match_field`.

Examples:

```text
watch_type: TOWN
search_term: WEEHAWKEN
match_mode: FIELD
match_field: municipality
```

```text
watch_type: SOURCE
search_term: PSEG
match_mode: FIELD
match_field: source
```

### `CONTAINS`

Search term or alias appears anywhere in the selected searchable text/field.

Useful for addresses and longer phrases.

### `WORD`

Whole word/phrase matching. Prefer this for shorter terms where substring matching could create false positives.

### `EXACT`

Normalized value must exactly equal the inspected value.

## `match_field`

Recommended initial values:

```text
search_text
source
category
subtype
county
municipality
title
message
```

If `match_mode=FIELD`, `match_field` is required.

For text-oriented watches, `search_text` is the normal default. The normalized `search_text` should combine useful alert content such as source, category, subtype, county, municipality, title, message, and tags.

## Aliases

Use pipe-separated values:

```text
4100 PARK AVE|4100 PARK AVENUE|FORTY ONE HUNDRED PARK
```

Do not use commas for alias separation because the master file is CSV.

Matching should be case-insensitive after normalization.

Avoid aliases so broad that they generate unrelated matches.

## Filters

`source_filter` and `alert_category_filter` are optional restrictions applied **before** the watch item is evaluated.

Example:

```text
watch_type: ADDRESS
search_term: 4100 PARK AVE
source_filter: FIRE_SDR|POLICE_FEED
alert_category_filter: FIRE|POLICE
```

Blank means the watch is allowed to match alerts from any source/category.

## GIS / Future Property Intelligence

An address or facility can begin with only:

```text
search_term
address
municipality
```

Later enrichment may populate:

```text
block
lot
qualifier
parcel_id
latitude
longitude
```

If `nearby_enabled=TRUE`, `radius_ft` defines the future proximity search area.

Example:

```text
4100 PARK AVE
nearby_enabled: TRUE
radius_ft: 500
```

A future GIS workflow can then find nearby parcels, facilities, flood zones, or other watched locations without changing the original watch item.

## Temporary Watch Items

`starts_at` and `expires_at` allow future temporary watches without deleting rows.

Examples:

- construction project
- special event
- temporary road closure
- short-term operational concern

Blank values mean no time restriction.

## Required Fields for v1

Every row must have:

```text
watch_id
active
watch_type
display_name
search_term
match_mode
```

Additionally:

```text
match_field
```

is required whenever `match_mode=FIELD`.

All other columns are optional unless a later module specifically requires them.

## Example Flow

```text
Incoming Alert
     ↓
Normalize structured fields + search_text
     ↓
Load active/in-date watch rows
     ↓
Apply source/category restrictions
     ↓
Evaluate match mode + aliases
     ↓
Matched watch rows
     ↓
Optional GIS/proximity enrichment
     ↓
Collect checked recipients
     ↓
Deduplicate recipients
     ↓
Subscriber Directory
     ↓
ntfy
```

## What Does Not Belong in This File

Do not put these directly in the Watchlist CSV:

- ntfy credentials
- passwords/API keys
- entire parcel datasets
- entire flood-zone datasets
- large external GIS geometries
- confidential case notes that do not need to be part of routing

The watchlist should reference/enrich against those systems rather than duplicate them.

## v1 Decisions

- **Alias delimiter:** `|`
- **Case matching:** case-insensitive after normalization
- **Recipient model:** Boolean recipient columns for v1
- **Primary identifier:** `watch_id`
- **Blank optional filters:** treated as `ANY`
- **GIS strategy:** identifiers/metadata on the watch row; large GIS datasets stored separately
- **Proximity unit:** feet via `radius_ft`
- **Required match modes:** `FIELD`, `CONTAINS`, `WORD`, `EXACT`
