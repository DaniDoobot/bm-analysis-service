-- Migration 6: Structure Permissions Hardening Schema
-- Verifies that all records have owner_user_id assigned, and sets it to NOT NULL.

DO $$
DECLARE
    null_bases INTEGER;
    null_specifics INTEGER;
BEGIN
    -- 1. Check if any NULL owner_user_id exists
    SELECT COUNT(*) INTO null_bases FROM bm_prompt_base_structures WHERE owner_user_id IS NULL;
    SELECT COUNT(*) INTO null_specifics FROM bm_prompts WHERE owner_user_id IS NULL;

    IF null_bases > 0 OR null_specifics > 0 THEN
        RAISE EXCEPTION 'Cannot harden owner_user_id columns. There are % bases and % specific prompts without owner assigned. Perform the backfill first!', null_bases, null_specifics;
    END IF;

    -- 2. Set owner_user_id to NOT NULL
    ALTER TABLE bm_prompt_base_structures ALTER COLUMN owner_user_id SET NOT NULL;
    ALTER TABLE bm_prompts ALTER COLUMN owner_user_id SET NOT NULL;
    
    RAISE NOTICE 'owner_user_id columns hardened to NOT NULL successfully.';
END $$;
