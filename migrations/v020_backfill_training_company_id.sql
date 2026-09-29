-- v020_backfill_training_company_id.sql
-- Deterministic, safe, and idempotent backfill for training records with company_id IS NULL.
-- Resolves company_id from public.bm_users ONLY when hubspot_owner_id maps unequivocally
-- to exactly one non-null company_id (COUNT(DISTINCT company_id) = 1).
-- Does NOT alter columns to NOT NULL.

-- 1. Backfill public.bm_training_agent_reports
WITH unique_owner_companies AS (
    SELECT 
        hubspot_owner_id, 
        MIN(company_id) AS resolved_company_id
    FROM public.bm_users
    WHERE hubspot_owner_id IS NOT NULL 
      AND company_id IS NOT NULL
    GROUP BY hubspot_owner_id
    HAVING COUNT(DISTINCT company_id) = 1
)
UPDATE public.bm_training_agent_reports r
SET company_id = uoc.resolved_company_id,
    updated_at = NOW()
FROM unique_owner_companies uoc
WHERE r.hubspot_owner_id = uoc.hubspot_owner_id
  AND r.company_id IS NULL;

-- 2. Backfill public.bm_training_agent_settings
WITH unique_owner_companies AS (
    SELECT 
        hubspot_owner_id, 
        MIN(company_id) AS resolved_company_id
    FROM public.bm_users
    WHERE hubspot_owner_id IS NOT NULL 
      AND company_id IS NOT NULL
    GROUP BY hubspot_owner_id
    HAVING COUNT(DISTINCT company_id) = 1
)
UPDATE public.bm_training_agent_settings s
SET company_id = uoc.resolved_company_id,
    updated_at = NOW()
FROM unique_owner_companies uoc
WHERE s.hubspot_owner_id = uoc.hubspot_owner_id
  AND s.company_id IS NULL;

-- 3. Backfill public.bm_training_knowledge_documents from repaired reports
UPDATE public.bm_training_knowledge_documents kd
SET company_id = r.company_id,
    metadata_json = jsonb_set(kd.metadata_json, '{company_id}', to_jsonb(r.company_id)),
    updated_at = NOW()
FROM public.bm_training_agent_reports r
WHERE kd.cycle_id = r.training_report_id
  AND kd.company_id IS NULL
  AND r.company_id IS NOT NULL;
