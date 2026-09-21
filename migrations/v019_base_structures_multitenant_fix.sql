-- v019_base_structures_multitenant_fix.sql
-- Migration script to fix prompt base structures multi-tenant scoping and backfill global catalog templates.

-- 1. Standard generic catalog templates must be global, active, and not bound to any company
UPDATE public.bm_prompt_base_structures
SET is_global = TRUE,
    company_id = NULL,
    is_active = TRUE
WHERE structure_key IN ('generic_customer_service', 'commercial_quality', 'blank');

-- 2. Ensure Boston Medical structures remain private to Boston Medical
UPDATE public.bm_prompt_base_structures
SET is_global = FALSE,
    company_id = (SELECT company_id FROM public.bm_companies WHERE company_key = 'boston-medical' LIMIT 1)
WHERE structure_key IN ('boston_medical_audio', 'boston_medical_appointment');

-- 3. Backfill base structures created by users belonging to Empresa Demo that were saved with company_id IS NULL
UPDATE public.bm_prompt_base_structures bs
SET company_id = u.company_id
FROM public.bm_users u
JOIN public.bm_companies c ON c.company_id = u.company_id
WHERE bs.owner_user_id = u.user_id
  AND c.company_key = 'empresa-demo'
  AND bs.company_id IS NULL
  AND bs.is_global = FALSE;

-- 4. Backfill any remaining company base structures where service_id is defined but company_id is NULL
UPDATE public.bm_prompt_base_structures bs
SET company_id = s.company_id
FROM public.bm_services s
WHERE bs.service_id = s.service_id
  AND bs.company_id IS NULL
  AND bs.is_global = FALSE;
