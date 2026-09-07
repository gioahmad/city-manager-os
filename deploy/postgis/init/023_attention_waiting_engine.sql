-- Attention / Waiting-On Engine v1.
-- Adds chase metadata to the existing issues source of truth.
-- No parallel task or dependency table is created.

ALTER TABLE issues
  ADD COLUMN IF NOT EXISTS
    waiting_on_since timestamptz,
  ADD COLUMN IF NOT EXISTS
    waiting_on_last_chased timestamptz,
  ADD COLUMN IF NOT EXISTS
    waiting_on_chase_count
      integer NOT NULL DEFAULT 0;

UPDATE issues
SET waiting_on_since =
    COALESCE(
      waiting_on_since,
      updated_at,
      created_at,
      now()
    )
WHERE NULLIF(
        btrim(
          COALESCE(
            waiting_on,
            ''
          )
        ),
        ''
      ) IS NOT NULL;

UPDATE issues
SET waiting_on_chase_count=0
WHERE waiting_on_chase_count < 0;

DO $$
BEGIN

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname =
      'issues_waiting_on_chase_count_nonnegative'
  )
  THEN

    ALTER TABLE issues
      ADD CONSTRAINT
        issues_waiting_on_chase_count_nonnegative
      CHECK (
        waiting_on_chase_count >= 0
      );

  END IF;

END
$$;


CREATE OR REPLACE FUNCTION
  cmos_sync_issue_waiting_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN

  IF NULLIF(
       btrim(
         COALESCE(
           NEW.waiting_on,
           ''
         )
       ),
       ''
     ) IS NULL
  THEN

    NEW.waiting_on := NULL;
    NEW.waiting_on_since := NULL;
    NEW.waiting_on_last_chased := NULL;
    NEW.waiting_on_chase_count := 0;

  ELSIF TG_OP = 'INSERT'
  THEN

    NEW.waiting_on_since :=
      COALESCE(
        NEW.waiting_on_since,
        now()
      );

    NEW.waiting_on_chase_count :=
      COALESCE(
        NEW.waiting_on_chase_count,
        0
      );

  ELSIF OLD.waiting_on
        IS DISTINCT FROM
        NEW.waiting_on
  THEN

    NEW.waiting_on_since := now();
    NEW.waiting_on_last_chased := NULL;
    NEW.waiting_on_chase_count := 0;

  END IF;

  RETURN NEW;

END
$$;


DROP TRIGGER IF EXISTS
  trg_cmos_sync_issue_waiting_state
ON issues;


CREATE TRIGGER
  trg_cmos_sync_issue_waiting_state
BEFORE INSERT OR UPDATE OF waiting_on
ON issues
FOR EACH ROW
EXECUTE FUNCTION
  cmos_sync_issue_waiting_state();


CREATE INDEX IF NOT EXISTS
  idx_issues_waiting_on_since_open
ON issues(
  waiting_on_since
)
WHERE
  status NOT IN (
    'RESOLVED',
    'CLOSED'
  )
  AND NULLIF(
        btrim(
          COALESCE(
            waiting_on,
            ''
          )
        ),
        ''
      ) IS NOT NULL;
