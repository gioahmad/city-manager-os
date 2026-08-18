# Alert Router

## Purpose

The Central Alert Router receives normalized alerts, evaluates the Master Watchlist, determines recipients, suppresses duplicates, sends ntfy notifications, and records delivery.

## Preferred Flow

```text
Source Workflow
     ↓
Standard Alert Schema
     ↓
Central Alert Router
     ↓
Master Watchlist Match
     ↓
Collect Recipients
     ↓
Deduplicate
     ↓
Subscriber Directory
     ↓
ntfy
     ↓
Delivery Log
```

## Responsibilities

The router should:

1. Validate/normalize incoming alert fields
2. Build searchable text
3. Match structured fields and text against active watchlist rows
4. Record which watch items matched
5. Collect recipients from all matching rows
6. Deduplicate recipient/topic destinations
7. Check prior delivery if necessary
8. Publish through ntfy
9. Log outcome

## Source Workflow Rule

Source workflows should not know individual subscribers.

They should only:

```text
collect → parse → detect → normalize → send to router
```

## Matching Concepts

- Structured field matches: source, category, municipality, county, incident type
- Text matches: addresses, facilities, phrases, aliases
- Future spatial matches: point within radius/area, nearby watched property, flood-zone intersection

## Duplicate Handling

One incident may match many watchlist rows. A recipient should generally receive one notification per alert rather than one copy per matched row.

Store match reasons for audit/troubleshooting.

Example:

```text
watch:W001 WEEHAWKEN
watch:W014 2ND ALARM
watch:W028 4100 PARK AVE
```

## Delivery Log

Recommended key:

```text
alert_id | subscriber_id
```

Log at least alert ID, subscriber, ntfy topic, timestamp, status, and matched watch IDs.
