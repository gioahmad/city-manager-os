import re
import uuid

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from operations_app import app
from app import (
    MATCH_MODES,
    WATCH_TYPES,
    csv_array,
    execute,
    make_watch_id,
    query_all,
    query_one,
    templates,
    validate_watch,
)


def slugify(value: str):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:80] or uuid.uuid4().hex[:8]


def taxonomy(section_id: str, subsection_id: str):
    section = None
    subsection = None

    if section_id:
        section = query_one(
            """
            SELECT id, name
            FROM rule_sections
            WHERE id=%s
            """,
            (int(section_id),),
        )

        if not section:
            raise HTTPException(status_code=400, detail="Invalid section")

    if subsection_id:
        subsection = query_one(
            """
            SELECT id, section_id, name
            FROM rule_subsections
            WHERE id=%s
            """,
            (int(subsection_id),),
        )

        if not subsection:
            raise HTTPException(status_code=400, detail="Invalid subsection")

        if section and subsection["section_id"] != section["id"]:
            raise HTTPException(
                status_code=400,
                detail="Subsection does not belong to selected section",
            )

    return section, subsection


def set_recipients(watch_item_id, recipient_ids):
    execute(
        """
        UPDATE watch_item_recipients
        SET active=false
        WHERE watch_item_id=%s
        """,
        (watch_item_id,),
    )

    for rid in recipient_ids:
        try:
            sid = uuid.UUID(rid)
        except Exception:
            continue

        execute(
            """
            INSERT INTO watch_item_recipients (
              watch_item_id,
              subscriber_id,
              active
            )
            VALUES (%s,%s,true)
            ON CONFLICT (watch_item_id, subscriber_id)
            DO UPDATE SET active=true
            """,
            (watch_item_id, sid),
        )


@app.get("/rules", response_class=HTMLResponse)
def rules_center(
    request: Request,
    q: str = "",
    state: str = "active",
    msg: str = "",
):
    where = []
    params = []

    if state == "active":
        where.append("w.active=true")
    elif state == "paused":
        where.append("w.active=false")

    if q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            """
            (
              w.display_name ILIKE %s
              OR w.search_term ILIKE %s
              OR w.category ILIKE %s
              OR w.subcategory ILIKE %s
              OR array_to_string(w.aliases, ',') ILIKE %s
              OR array_to_string(w.tags, ',') ILIKE %s
            )
            """
        )
        params.extend([needle] * 6)

    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rules = query_all(
        f"""
        SELECT
          w.id,
          w.watch_id,
          w.active,
          w.watch_type,
          w.display_name,
          w.search_term,
          w.aliases,
          w.match_mode,
          w.match_field,
          w.category,
          w.subcategory,
          w.tags,
          w.min_priority,
          w.municipality,
          w.address,
          w.notes,
          w.rule_section_id,
          w.rule_subsection_id,
          rs.name AS section_name,
          rss.name AS subsection_name,
          COALESCE(
            array_agg(DISTINCT s.id::text)
              FILTER (WHERE wir.active AND s.id IS NOT NULL),
            ARRAY[]::text[]
          ) AS recipient_ids,
          COALESCE(
            array_agg(DISTINCT s.name)
              FILTER (WHERE wir.active AND s.id IS NOT NULL),
            ARRAY[]::text[]
          ) AS recipient_names
        FROM watch_items w
        LEFT JOIN rule_sections rs
          ON rs.id=w.rule_section_id
        LEFT JOIN rule_subsections rss
          ON rss.id=w.rule_subsection_id
        LEFT JOIN watch_item_recipients wir
          ON wir.watch_item_id=w.id
        LEFT JOIN subscribers s
          ON s.id=wir.subscriber_id
        {clause}
        GROUP BY
          w.id,
          rs.name,
          rss.name,
          rs.sort_order,
          rss.sort_order
        ORDER BY
          w.active DESC,
          COALESCE(rs.sort_order,999),
          COALESCE(rss.sort_order,999),
          w.display_name
        LIMIT 350
        """,
        params,
    )

    sections = query_all(
        """
        SELECT
          id,
          name,
          slug,
          active,
          sort_order
        FROM rule_sections
        ORDER BY sort_order, name
        """
    )

    subsections = query_all(
        """
        SELECT
          ss.id,
          ss.section_id,
          ss.name,
          ss.slug,
          ss.active,
          ss.sort_order,
          s.name AS section_name
        FROM rule_subsections ss
        JOIN rule_sections s
          ON s.id=ss.section_id
        ORDER BY s.sort_order, ss.sort_order, ss.name
        """
    )

    subscribers = query_all(
        """
        SELECT
          id::text AS id,
          subscriber_id,
          name,
          ntfy_topic
        FROM subscribers
        WHERE active=true
        ORDER BY
          CASE WHEN subscriber_id='GIO_CATCHALL' THEN 0 ELSE 1 END,
          name
        """
    )

    counts = query_one(
        """
        SELECT
          (SELECT count(*) FROM watch_items) AS total_rules,
          (SELECT count(*) FROM watch_items WHERE active=true) AS active_rules,
          (SELECT count(*) FROM rule_sections WHERE active=true) AS sections,
          (SELECT count(*) FROM rule_subsections WHERE active=true) AS subsections,
          (SELECT count(*) FROM subscribers WHERE active=true) AS subscribers
        """
    )

    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={
            "rules": rules,
            "sections": sections,
            "subsections": subsections,
            "subscribers": subscribers,
            "counts": counts,
            "q": q,
            "state": state,
            "msg": msg,
            "watch_types": WATCH_TYPES,
            "match_modes": sorted(MATCH_MODES),
            "page": "rules",
        },
    )


@app.post("/rules/quick-add")
def rules_quick_add(
    search_term: str = Form(...),
    display_name: str = Form(""),
    aliases: str = Form(""),
    watch_type: str = Form("PHRASE"),
    section_id: str = Form(""),
    subsection_id: str = Form(""),
    min_priority: int = Form(1),
    municipality: str = Form(""),
    notes: str = Form(""),
    match_mode: str = Form("CONTAINS"),
    match_field: str = Form(""),
    tags: str = Form(""),
    recipient_ids: list[str] = Form(default=[]),
):
    search_term = search_term.strip()

    if not search_term:
        raise HTTPException(status_code=400, detail="Keyword is required")

    display_name = display_name.strip() or search_term
    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)

    section, subsection = taxonomy(section_id, subsection_id)

    row = query_one(
        """
        INSERT INTO watch_items (
          watch_id,
          active,
          watch_type,
          display_name,
          search_term,
          aliases,
          match_mode,
          match_field,
          category,
          subcategory,
          tags,
          min_priority,
          municipality,
          notes,
          rule_section_id,
          rule_subsection_id
        )
        VALUES (
          %s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        RETURNING id
        """,
        (
            make_watch_id(display_name),
            watch_type.strip().upper(),
            display_name,
            search_term,
            csv_array(aliases),
            match_mode,
            match_field.strip() or None,
            section.get("name") if section else None,
            subsection.get("name") if subsection else None,
            csv_array(tags),
            min_priority,
            municipality.strip() or None,
            notes.strip() or None,
            section.get("id") if section else None,
            subsection.get("id") if subsection else None,
        ),
    )

    set_recipients(row["id"], recipient_ids)

    return RedirectResponse(
        url="/rules?msg=Rule+created",
        status_code=303,
    )


@app.post("/rules/{item_id}/update")
def rules_update(
    item_id: uuid.UUID,
    display_name: str = Form(...),
    search_term: str = Form(...),
    aliases: str = Form(""),
    watch_type: str = Form("PHRASE"),
    section_id: str = Form(""),
    subsection_id: str = Form(""),
    min_priority: int = Form(1),
    municipality: str = Form(""),
    notes: str = Form(""),
    match_mode: str = Form("CONTAINS"),
    match_field: str = Form(""),
    tags: str = Form(""),
    active: str | None = Form(None),
    recipient_ids: list[str] = Form(default=[]),
):
    display_name = display_name.strip()
    search_term = search_term.strip()

    if not display_name or not search_term:
        raise HTTPException(
            status_code=400,
            detail="Name and keyword are required",
        )

    match_mode = match_mode.upper().strip()
    validate_watch(match_mode, match_field, min_priority)

    section, subsection = taxonomy(section_id, subsection_id)

    execute(
        """
        UPDATE watch_items
        SET
          active=%s,
          watch_type=%s,
          display_name=%s,
          search_term=%s,
          aliases=%s,
          match_mode=%s,
          match_field=%s,
          category=%s,
          subcategory=%s,
          tags=%s,
          min_priority=%s,
          municipality=%s,
          notes=%s,
          rule_section_id=%s,
          rule_subsection_id=%s,
          updated_at=now()
        WHERE id=%s
        """,
        (
            active is not None,
            watch_type.strip().upper(),
            display_name,
            search_term,
            csv_array(aliases),
            match_mode,
            match_field.strip() or None,
            section.get("name") if section else None,
            subsection.get("name") if subsection else None,
            csv_array(tags),
            min_priority,
            municipality.strip() or None,
            notes.strip() or None,
            section.get("id") if section else None,
            subsection.get("id") if subsection else None,
            item_id,
        ),
    )

    set_recipients(item_id, recipient_ids)

    return RedirectResponse(
        url="/rules?msg=Rule+updated",
        status_code=303,
    )


@app.post("/rules/{item_id}/toggle")
def rules_toggle(item_id: uuid.UUID):
    execute(
        """
        UPDATE watch_items
        SET active=NOT active,
            updated_at=now()
        WHERE id=%s
        """,
        (item_id,),
    )

    return RedirectResponse(
        url="/rules?msg=Rule+status+changed",
        status_code=303,
    )


@app.post("/rules/section/create")
def rules_section_create(
    name: str = Form(...),
    sort_order: int = Form(100),
):
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Section name required",
        )

    execute(
        """
        INSERT INTO rule_sections (
          name,
          slug,
          active,
          sort_order
        )
        VALUES (%s,%s,true,%s)
        """,
        (name, slugify(name), sort_order),
    )

    return RedirectResponse(
        url="/rules?msg=Section+created",
        status_code=303,
    )


@app.post("/rules/section/{section_id}/update")
def rules_section_update(
    section_id: int,
    name: str = Form(...),
    sort_order: int = Form(100),
    active: str | None = Form(None),
):
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Section name required",
        )

    execute(
        """
        UPDATE rule_sections
        SET
          name=%s,
          slug=%s,
          active=%s,
          sort_order=%s,
          updated_at=now()
        WHERE id=%s
        """,
        (
            name,
            slugify(name),
            active is not None,
            sort_order,
            section_id,
        ),
    )

    execute(
        """
        UPDATE watch_items
        SET
          category=%s,
          updated_at=now()
        WHERE rule_section_id=%s
        """,
        (name, section_id),
    )

    return RedirectResponse(
        url="/rules?msg=Section+updated",
        status_code=303,
    )


@app.post("/rules/subsection/create")
def rules_subsection_create(
    section_id: int = Form(...),
    name: str = Form(...),
    sort_order: int = Form(100),
):
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Subsection name required",
        )

    execute(
        """
        INSERT INTO rule_subsections (
          section_id,
          name,
          slug,
          active,
          sort_order
        )
        VALUES (%s,%s,%s,true,%s)
        """,
        (
            section_id,
            name,
            slugify(name),
            sort_order,
        ),
    )

    return RedirectResponse(
        url="/rules?msg=Subsection+created",
        status_code=303,
    )


@app.post("/rules/subsection/{subsection_id}/update")
def rules_subsection_update(
    subsection_id: int,
    name: str = Form(...),
    sort_order: int = Form(100),
    active: str | None = Form(None),
):
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Subsection name required",
        )

    execute(
        """
        UPDATE rule_subsections
        SET
          name=%s,
          slug=%s,
          active=%s,
          sort_order=%s,
          updated_at=now()
        WHERE id=%s
        """,
        (
            name,
            slugify(name),
            active is not None,
            sort_order,
            subsection_id,
        ),
    )

    execute(
        """
        UPDATE watch_items
        SET
          subcategory=%s,
          updated_at=now()
        WHERE rule_subsection_id=%s
        """,
        (name, subsection_id),
    )

    return RedirectResponse(
        url="/rules?msg=Subsection+updated",
        status_code=303,
    )
