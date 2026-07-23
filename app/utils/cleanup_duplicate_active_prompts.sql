-- ============================================================
-- CLEANUP: Duplicated active prompts per (company_id, service_id, prompt_type)
-- ============================================================
-- DO NOT EXECUTE IN PRODUCTION WITHOUT EXPLICIT REVIEW
-- Run the DRY-RUN section first to verify the affected rows.
-- ============================================================


-- 1. DRY-RUN: Detect duplicated active prompts
SELECT
    p.company_id,
    p.service_id,
    p.prompt_type,
    COUNT(*) AS active_count,
    STRING_AGG(p.prompt_id::text, '', '' ORDER BY p.prompt_id) AS prompt_ids,
    STRING_AGG(p.prompt_name, '' | '' ORDER BY p.prompt_id) AS prompt_names
FROM bm_prompts p
WHERE p.is_active = TRUE
  AND p.is_archived = FALSE
  AND p.deleted_at IS NULL
GROUP BY p.company_id, p.service_id, p.prompt_type
HAVING COUNT(*) > 1
ORDER BY p.company_id, p.service_id, p.prompt_type;


-- 2. DRY-RUN: Detail view of duplicated prompts
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
  AND (p.company_id, COALESCE(p.service_id, -1), p.prompt_type) IN (
    SELECT p2.company_id, COALESCE(p2.service_id, -1), p2.prompt_type
    FROM bm_prompts p2
    WHERE p2.is_active = TRUE
      AND p2.is_archived = FALSE
      AND p2.deleted_at IS NULL
    GROUP BY p2.company_id, COALESCE(p2.service_id, -1), p2.prompt_type
    HAVING COUNT(*) > 1
  )
ORDER BY p.company_id, p.service_id, p.prompt_type, p.updated_at DESC;


-- 3. REPAIR (confirm before running!)
-- Keeps only the most recently updated prompt active per group.
UPDATE bm_prompts
SET is_active = FALSE
WHERE is_active = TRUE
  AND is_archived = FALSE
  AND deleted_at IS NULL
  AND prompt_id NOT IN (
    SELECT DISTINCT ON (
        p.company_id,
        COALESCE(p.service_id, -1),
        p.prompt_type
    )
        p.prompt_id
    FROM bm_prompts p
    WHERE p.is_active = TRUE
      AND p.is_archived = FALSE
      AND p.deleted_at IS NULL
    ORDER BY
        p.company_id,
        COALESCE(p.service_id, -1),
        p.prompt_type,
        p.updated_at DESC NULLS LAST,
        p.prompt_id DESC
  )
  AND (company_id, COALESCE(service_id, -1), prompt_type) IN (
    SELECT p2.company_id, COALESCE(p2.service_id, -1), p2.prompt_type
    FROM bm_prompts p2
    WHERE p2.is_active = TRUE
      AND p2.is_archived = FALSE
      AND p2.deleted_at IS NULL
    GROUP BY p2.company_id, COALESCE(p2.service_id, -1), p2.prompt_type
    HAVING COUNT(*) > 1
  );


-- 4. Partial unique index (run REPAIR first):
-- CREATE UNIQUE INDEX IF NOT EXISTS uix_prompts_active_per_tenant_service_type
--     ON bm_prompts (
--         COALESCE(company_id, -1),
--         COALESCE(service_id, -1),
--         prompt_type
--     )
--     WHERE is_active = TRUE
--       AND is_archived = FALSE
--       AND deleted_at IS NULL;
