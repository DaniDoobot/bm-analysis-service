-- ==============================================================================
-- Migration: v029_prompt_snapshots_deduplication.sql
-- Module: Mass Evaluations / Prompt Deduplication (Content-Addressed Storage)
--
-- PURPOSE:
-- 1. Create bm_prompt_snapshots table to store unique prompt texts once.
-- 2. Add nullable prompt_snapshot_id to bm_mass_evaluation_results with FK.
-- 3. Create index on prompt_snapshot_id.
-- 4. Add foreign key fk_bm_mass_eval_res_prompt_snapshot.
-- 5. Ensure legacy prompt_snapshot column allows NULL values for new evaluations.
-- 6. Backfill distinct historical prompt snapshots without loading full text in RAM.
-- 7. Link existing mass evaluation records to their corresponding snapshot.
-- 8. Historical records PRESERVE their prompt_snapshot text (no data loss).
--
-- NOTE:
-- In Phase A, the legacy column `prompt_snapshot` is PRESERVED for zero-downtime
-- compatibility and transitional safety. It will be removed in Phase B once validated.
-- ==============================================================================

BEGIN;

-- 1. Create bm_prompt_snapshots table (UNIQUE constraint automatically provides required B-tree index)
CREATE TABLE IF NOT EXISTS public.bm_prompt_snapshots (
    id BIGSERIAL PRIMARY KEY,
    hash_sha256 VARCHAR(64) NOT NULL,
    prompt_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_bm_prompt_snapshots_hash UNIQUE (hash_sha256)
);

-- 2. Add nullable prompt_snapshot_id to bm_mass_evaluation_results
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public'
          AND table_name = 'bm_mass_evaluation_results' 
          AND column_name = 'prompt_snapshot_id'
    ) THEN
        ALTER TABLE public.bm_mass_evaluation_results 
        ADD COLUMN prompt_snapshot_id BIGINT NULL;
    END IF;
END $$;

-- 3. Create index on prompt_snapshot_id foreign key
CREATE INDEX IF NOT EXISTS idx_bm_mass_eval_res_prompt_snapshot_id 
ON public.bm_mass_evaluation_results (prompt_snapshot_id);

-- 4. Add foreign key constraint if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_schema = 'public'
          AND table_name = 'bm_mass_evaluation_results'
          AND constraint_name = 'fk_bm_mass_eval_res_prompt_snapshot'
    ) THEN
        ALTER TABLE public.bm_mass_evaluation_results
        ADD CONSTRAINT fk_bm_mass_eval_res_prompt_snapshot
        FOREIGN KEY (prompt_snapshot_id) REFERENCES public.bm_prompt_snapshots(id)
        ON DELETE SET NULL;
    END IF;
END $$;

-- 5. Ensure legacy column allows NULL for new evaluations (safe if already nullable)
ALTER TABLE public.bm_mass_evaluation_results 
ALTER COLUMN prompt_snapshot DROP NOT NULL;

-- 6. Backfill: Insert distinct historical snapshots into bm_prompt_snapshots
-- Uses native PostgreSQL sha256 function on UTF-8 bytes to compute deterministic hex hash.
INSERT INTO public.bm_prompt_snapshots (hash_sha256, prompt_text, created_at)
SELECT 
    encode(sha256(convert_to(prompt_snapshot, 'UTF8')), 'hex') AS hash_sha256,
    prompt_snapshot AS prompt_text,
    MIN(created_at) AS created_at
FROM public.bm_mass_evaluation_results
WHERE prompt_snapshot IS NOT NULL 
  AND prompt_snapshot != ''
GROUP BY prompt_snapshot
ON CONFLICT (hash_sha256) DO NOTHING;

-- 7. Backfill: Link historical records in bm_mass_evaluation_results to their snapshot
UPDATE public.bm_mass_evaluation_results r
SET prompt_snapshot_id = s.id
FROM public.bm_prompt_snapshots s
WHERE r.prompt_snapshot_id IS NULL
  AND r.prompt_snapshot IS NOT NULL
  AND r.prompt_snapshot != ''
  AND s.hash_sha256 = encode(sha256(convert_to(r.prompt_snapshot, 'UTF8')), 'hex');

COMMIT;

-- 8. Verification Query (Can be checked post-execution):
-- SELECT count(*) AS unlinked_historical_rows
-- FROM public.bm_mass_evaluation_results
-- WHERE prompt_snapshot IS NOT NULL 
--   AND prompt_snapshot != '' 
--   AND prompt_snapshot_id IS NULL;
