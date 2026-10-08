"""Saved per-source display choices; alert records and delivery routing stay intact."""
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg.types.json import Jsonb

from app import app, db_conn, query_all, query_one


APPEARANCE_FIELDS = (
    ("mapping_link", "Mapping Center link", False),
    ("explanation", "Why this alert matched", False),
    ("watch_names", "Matched Watch names", False),
    ("keywords", "Matched keywords", False),
    ("source_link", "Original source link", False),
    ("pseg_etr", "Estimated restoration time", True),
    ("pseg_started", "Outage start time", True),
    ("pseg_operations", "Jobs, work in progress, and circuits", True),
    ("pseg_damage", "Damage details", True),
    ("pseg_change", "Change since the previous check", True),
    ("pseg_area", "Approximate outage area", True),
)
APPEARANCE_DEFAULTS = {
    key: key not in {"mapping_link", "explanation", "watch_names", "keywords"}
    for key, _, _ in APPEARANCE_FIELDS
}
CHANNELS = ("dashboard", "notification")


def resolve_appearance(settings: dict, source: str, channel: str) -> dict[str, bool]:
    """Resolve a channel from the complete workspace settings, ignoring invalid keys."""
    appearance = settings.get("alert_appearance") or {}
    saved = appearance.get(source, {}) if isinstance(appearance, dict) else {}
    values = saved.get(channel, {}) if isinstance(saved, dict) else {}
    return {key: values.get(key) if isinstance(values, dict) and type(values.get(key)) is bool else default
            for key, default in APPEARANCE_DEFAULTS.items()}


def _settings_and_sources(source_rows=None):
    settings = query_one("SELECT settings FROM workspace_config WHERE singleton=true").get("settings") or {}
    saved = settings.get("alert_appearance") or {}
    if source_rows is None:
        source_rows = query_all(
            "SELECT DISTINCT source FROM alerts WHERE source IS NOT NULL AND btrim(source)<>''"
        )
    sources = {row["source"] for row in source_rows if row.get("source")}
    sources.add("PSEG")
    if isinstance(saved, dict):
        sources.update(source for source in saved if isinstance(source, str) and source.strip())
    return settings, sorted(sources, key=str.casefold)


def alert_appearance_context(request: Request | None = None, source_rows=None) -> dict:
    settings, sources = _settings_and_sources(source_rows)
    samples = query_all("""
        SELECT sample.*,coalesce(evidence.items,'[]'::jsonb) AS watch_evidence
        FROM unnest(%s::text[]) AS catalog(source)
        JOIN LATERAL (
          SELECT id,alert_id,source,title,message,click_url,received_at,
                 jsonb_build_object('content_sections',metadata->'content_sections',
                                    'mapping_center_url',metadata->'mapping_center_url') AS metadata
          FROM alerts WHERE source=catalog.source ORDER BY received_at DESC LIMIT 1
        ) sample ON true
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object('watch_name',m.display_name,
                                            'match_reason',m.match_reason)
                           ORDER BY m.display_name) AS items
          FROM (
            SELECT DISTINCT ON (candidate.watch_key)
                   candidate.watch_key,candidate.display_name,candidate.match_reason
            FROM (
              SELECT w.id::text AS watch_key,w.display_name,awm.match_reason,
                     awm.matched_at AS happened_at
              FROM alert_watch_matches awm
              JOIN watch_items w ON w.id=awm.watch_item_id
              WHERE awm.alert_id=sample.id
              UNION ALL
              SELECT coalesce(w.id::text,ids.watch_id) AS watch_key,
                     coalesce(w.display_name,'Deleted Watch') AS display_name,
                     d.match_reasons->>((ids.position-1)::int) AS match_reason,
                     d.created_at AS happened_at
              FROM deliveries d
              CROSS JOIN LATERAL jsonb_array_elements_text(
                coalesce(d.matched_watch_ids,'[]'::jsonb)
              ) WITH ORDINALITY AS ids(watch_id,position)
              LEFT JOIN watch_items w ON w.watch_id=ids.watch_id
              WHERE d.alert_id=sample.id
            ) candidate
            ORDER BY candidate.watch_key,candidate.happened_at DESC
          ) m
        ) evidence ON true
    """, (sources,))
    selected = request.query_params.get("source", "") if request else ""
    return {
        "appearance_sources": sources,
        "appearance_selected_source": selected if selected in sources else "PSEG",
        "appearance_fields": APPEARANCE_FIELDS,
        "appearance_defaults": APPEARANCE_DEFAULTS,
        "appearance_settings": {
            source: {channel: resolve_appearance(settings, source, channel) for channel in CHANNELS}
            for source in sources
        },
        "appearance_samples": jsonable_encoder({sample["source"]: sample for sample in samples}),
        "appearance_can_edit": bool(request and getattr(request.state, "cmos_role", "") == "EXECUTIVE"),
    }


@app.post("/alerts/appearance")
async def save_alert_appearance(request: Request):
    if getattr(request.state, "cmos_role", "") != "EXECUTIVE":
        raise HTTPException(403, "Executive access is required to change alert appearance.")
    form = await request.form()
    allowed = {f"{channel}.{key}" for channel in CHANNELS for key in APPEARANCE_DEFAULTS}
    if set(form) - allowed - {"source"} or any(len(form.getlist(key)) != 1 for key in form):
        raise HTTPException(400, "Choose only the listed alert appearance options.")
    source = form.get("source", "")
    _, sources = _settings_and_sources()
    if source not in sources:
        raise HTTPException(400, "Choose a listed alert source.")
    if any(form[key] not in {"true", "false"} for key in form if key != "source"):
        raise HTTPException(400, "Alert appearance options must be on or off.")
    value = {source: {channel: {key: form.get(f"{channel}.{key}") == "true"
                              for key in APPEARANCE_DEFAULTS} for channel in CHANNELS}}
    with db_conn() as conn:
        conn.execute("""INSERT INTO workspace_config(singleton,settings)
            VALUES(true,jsonb_build_object('alert_appearance',%s::jsonb))
            ON CONFLICT(singleton) DO UPDATE SET settings=jsonb_set(
              workspace_config.settings,'{alert_appearance}',
              (CASE WHEN jsonb_typeof(workspace_config.settings->'alert_appearance')='object'
                    THEN workspace_config.settings->'alert_appearance' ELSE '{}'::jsonb END) || %s::jsonb)
            """, (Jsonb(value), Jsonb(value)))
        conn.commit()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "source": source, "settings": value[source]})
    return RedirectResponse("/alerts?" + urlencode({
        "source": source, "msg": f"Alert appearance saved for {source}."
    }) + "#alert-appearance", status_code=303)
