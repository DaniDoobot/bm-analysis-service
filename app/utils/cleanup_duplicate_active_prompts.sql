-- ============================================================
-- CLEANUP: Duplicated active prompts per (service_id, prompt_type)
-- ============================================================
-- ⚠️  DO NOT EXECUTE IN PRODUCTION WITHOUT EXPLICIT REVIEW
-- Run the DRY-RUN section first to verify the affected rows.
-- ============================================================


-- ── 1. DRY-RUN: Detect functional duplicates by service_id + prompt_type ──────
-- Grouping strictly by service_id + prompt_type (ignoring company_id).
-- Detects coexisting tenant-specific (company_id=X) and legacy (company_id=NULL).

SELECT
    p.service_id,
    p.prompt_type,
    COUNT(*) AS active_count,
    STRING_AGG(p.prompt_id::text, ', ' ORDER BY p.prompt_id) AS prompt_ids,
    STRING_AGG(COALESCE(p.company_id::text, 'NULL'), ', ' ORDER BY p.prompt_id) AS company_ids,
    STRING_AGG(p.prompt_name, ' | ' ORDER BY p.prompt_id) AS prompt_names
FROM bm_prompts p
WHERE p.is_active = TRUE
  AND p.is_archived = FALSE
  AND p.deleted_at IS NULL
  AND p.service_id IS NOT NULL
GROUP BY p.service_id, p.prompt_type
HAVING COUNT(*) > 1
ORDER BY p.service_id, p.prompt_type;


-- ── 2. DRY-RUN: Detail view of duplicated prompts ────────────────────────────
SELECT
    p.prompt_id,
    p.prompt_name,
    p.prompt_type,
    p.company_id,
    p.service_id,
    p.is_active,
    p.is_archived,
    p.deleted_at,
    p.updated_at,
    p.created_at,
    p.created_by_email
FROM bm_prompts p
WHERE p.is_active = TRUE
  AND p.is_archived = FALSE
  AND p.deleted_at IS NULL
  AND p.service_id IS NOT NULL
  AND (p.service_id, p.prompt_type) IN (
    SELECT p2.service_id, p2.prompt_type
    FROM bm_prompts p2
    WHERE p2.is_active = TRUE
      AND p2.is_archived = FALSE
      AND p2.deleted_at IS NULL
      AND p2.service_id IS NOT NULL
    GROUP BY p2.service_id, p2.prompt_type
    HAVING COUNT(*) > 1
  )
ORDER BY p.service_id, p.prompt_type, p.updated_at DESC;


-- ── 3. REPAIR: Normalize company_id & deactivate older duplicates ─────────────
-- Step 3a: Normalize company_id from bm_services for prompts with NULL company_id
UPDATE bm_prompts p
SET company_id = s.company_id
FROM bm_services s
WHERE p.service_id = s.service_id
  AND p.company_id IS NULL;

-- Step 3b: Keep only the MOST RECENT prompt active per (service_id, prompt_type)
UPDATE bm_prompts
SET is_active = FALSE
WHERE is_active = TRUE
  AND is_archived = FALSE
  AND deleted_at IS NULL
  AND service_id IS NOT NULL
  AND prompt_id NOT IN (
    SELECT DISTINCT ON (p.service_id, p.prompt_type)
        p.prompt_id
    FROM bm_prompts p
    WHERE p.is_active = TRUE
      AND p.is_archived = FALSE
      AND p.deleted_at IS NULL
      AND p.service_id IS NOT NULL
    ORDER BY
        p.service_id,
        p.prompt_type,
        p.updated_at DESC NULLS LAST,
        p.prompt_id DESC
  )
  AND (service_id, prompt_type) IN (
    SELECT p2.service_id, p2.prompt_type
    FROM bm_prompts p2
    WHERE p2.is_active = TRUE
      AND p2.is_archived = FALSE
      AND p2.deleted_at IS NULL
      AND p2.service_id IS NOT NULL
    GROUP BY p2.service_id, p2.prompt_type
    HAVING COUNT(*) > 1
  );


-- ── 4. Partial unique indexes (PostgreSQL, run REPAIR first) ──────────────────
-- Rule A: Prompts with service_id (at most 1 active per service_id + prompt_type)
-- CREATE UNIQUE INDEX IF NOT EXISTS uix_prompts_active_per_service_type
--     ON bm_prompts (service_id, prompt_type)
--     WHERE is_active = TRUE
--       AND is_archived = FALSE
--       AND deleted_at IS NULL
--       AND service_id IS NOT NULL;

-- Rule B: Prompts without service_id (at most 1 active per company_id + prompt_type)
-- CREATE UNIQUE INDEX IF NOT EXISTS uix_prompts_active_per_company_type
--     ON bm_prompts (COALESCE(company_id, -1), prompt_type)
--     WHERE is_active = TRUE
--       AND is_archived = FALSE
--       AND deleted_at IS NULL
--       AND service_id IS NULL;
