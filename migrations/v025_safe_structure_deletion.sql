-- v025_safe_structure_deletion.sql
-- Migration for Safe Deletion / Archival of Base Structures, Specific Structures, and Typologies.
-- Enables soft-delete / archiving preserving historical evaluations, analyses, and audit references.

-- 1. Ensure soft-delete columns on bm_prompt_base_structures
ALTER TABLE public.bm_prompt_base_structures
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS archived_by_email TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_bm_prompt_base_structures_active
    ON public.bm_prompt_base_structures(is_active) WHERE deleted_at IS NULL;

-- 2. Ensure soft-delete columns on bm_prompts (in case missing in any environment)
ALTER TABLE public.bm_prompts
    ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS archived_by_email TEXT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_bm_prompts_active_not_deleted
    ON public.bm_prompts(is_active) WHERE deleted_at IS NULL;
