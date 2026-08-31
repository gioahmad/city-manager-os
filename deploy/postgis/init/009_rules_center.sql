CREATE TABLE IF NOT EXISTS rule_sections (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    slug text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rule_subsections (
    id bigserial PRIMARY KEY,
    section_id bigint NOT NULL REFERENCES rule_sections(id) ON DELETE CASCADE,
    name text NOT NULL,
    slug text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(section_id, name),
    UNIQUE(section_id, slug)
);

ALTER TABLE watch_items
    ADD COLUMN IF NOT EXISTS rule_section_id bigint
        REFERENCES rule_sections(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS rule_subsection_id bigint
        REFERENCES rule_subsections(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_watch_items_rule_section
    ON watch_items(rule_section_id);

CREATE INDEX IF NOT EXISTS idx_watch_items_rule_subsection
    ON watch_items(rule_subsection_id);

CREATE INDEX IF NOT EXISTS idx_rule_subsections_section
    ON rule_subsections(section_id, active, sort_order);

INSERT INTO rule_sections(name, slug, sort_order)
VALUES
    ('Public Safety', 'public-safety', 10),
    ('Infrastructure', 'infrastructure', 20),
    ('Government', 'government', 30),
    ('Community', 'community', 40),
    ('Transportation', 'transportation', 50),
    ('Operations', 'operations', 60)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO rule_subsections(section_id, name, slug, sort_order)
SELECT s.id, x.name, x.slug, x.sort_order
FROM rule_sections s
JOIN (
    VALUES
      ('public-safety','Fire','fire',10),
      ('public-safety','Police','police',20),
      ('public-safety','EMS','ems',30),
      ('public-safety','OEM','oem',40),
      ('infrastructure','Roads','roads',10),
      ('infrastructure','Utilities','utilities',20),
      ('infrastructure','Gateway','gateway',30),
      ('infrastructure','Construction','construction',40),
      ('government','County','county',10),
      ('government','State','state',20),
      ('government','Federal','federal',30),
      ('government','Legislation','legislation',40),
      ('community','Events','events',10),
      ('community','Schools','schools',20),
      ('community','Health','health',30),
      ('community','Housing','housing',40),
      ('transportation','Traffic','traffic',10),
      ('transportation','Transit','transit',20),
      ('transportation','Waterfront','waterfront',30),
      ('operations','DPW','dpw',10),
      ('operations','Permits','permits',20),
      ('operations','Facilities','facilities',30)
) AS x(section_slug,name,slug,sort_order)
  ON s.slug=x.section_slug
ON CONFLICT (section_id, slug) DO NOTHING;
