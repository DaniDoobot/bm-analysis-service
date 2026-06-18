-- Migration 7: User HubSpot Owner ID Uniqueness
-- Checks for duplicates and creates a partial unique index on hubspot_owner_id.

DO $$
DECLARE
    duplicate_count INTEGER;
BEGIN
    -- 1. Check for duplicates
    SELECT COUNT(*) INTO duplicate_count FROM (
        SELECT hubspot_owner_id 
        FROM bm_users 
        WHERE hubspot_owner_id IS NOT NULL 
        GROUP BY hubspot_owner_id 
        HAVING COUNT(*) > 1
    ) sub;

    IF duplicate_count > 0 THEN
        RAISE WARNING 'Cannot create unique index: found % duplicate hubspot_owner_id values in bm_users.', duplicate_count;
    ELSE
        -- 2. Create unique index
        IF NOT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'uq_idx_bm_users_hubspot_owner_id'
              AND n.nspname = 'public'
        ) THEN
            CREATE UNIQUE INDEX uq_idx_bm_users_hubspot_owner_id 
            ON bm_users (hubspot_owner_id) 
            WHERE hubspot_owner_id IS NOT NULL;
            RAISE NOTICE 'Unique index uq_idx_bm_users_hubspot_owner_id created successfully.';
        ELSE
            RAISE NOTICE 'Unique index uq_idx_bm_users_hubspot_owner_id already exists.';
        END IF;
    END IF;
END $$;
