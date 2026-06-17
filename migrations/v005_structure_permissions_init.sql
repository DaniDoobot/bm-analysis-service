-- Migration 5: Structure Permissions Initial Schema
-- Adds owner_user_id columns as nullable and creates the permissions & audit tables.

-- 1. Add owner_user_id as nullable to bm_prompt_base_structures and bm_prompts
ALTER TABLE bm_prompt_base_structures ADD COLUMN IF NOT EXISTS owner_user_id INTEGER NULL;
ALTER TABLE bm_prompts ADD COLUMN IF NOT EXISTS owner_user_id INTEGER NULL;

-- 2. Create Foreign Key Constraints (RESTRICT to prevent deleting users owning resources)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_bm_prompt_base_structures_owner_user_id'
    ) THEN
        ALTER TABLE bm_prompt_base_structures ADD CONSTRAINT fk_bm_prompt_base_structures_owner_user_id
        FOREIGN KEY (owner_user_id) REFERENCES bm_users(user_id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_bm_prompts_owner_user_id'
    ) THEN
        ALTER TABLE bm_prompts ADD CONSTRAINT fk_bm_prompts_owner_user_id
        FOREIGN KEY (owner_user_id) REFERENCES bm_users(user_id) ON DELETE RESTRICT;
    END IF;
END $$;

-- 3. Create Structure Permissions Table
CREATE TABLE IF NOT EXISTS bm_structure_permissions (
    permission_id SERIAL PRIMARY KEY,
    structure_type VARCHAR(20) NOT NULL CHECK (structure_type IN ('base', 'specific')),
    structure_id BIGINT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES bm_users(user_id) ON DELETE CASCADE,
    permission_level VARCHAR(20) NOT NULL CHECK (permission_level IN ('view', 'use', 'edit')),
    granted_by_user_id INTEGER REFERENCES bm_users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_structure_user UNIQUE (structure_type, structure_id, user_id)
);

-- 4. Create indexes for permissions lookup
CREATE INDEX IF NOT EXISTS idx_struct_perm_user ON bm_structure_permissions(user_id);
CREATE INDEX IF NOT EXISTS idx_struct_perm_lookup ON bm_structure_permissions(structure_type, structure_id);

-- 5. Create Structure Permissions Audit Table
CREATE TABLE IF NOT EXISTS bm_structure_permissions_audit (
    audit_id SERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES bm_users(user_id) ON DELETE SET NULL,
    action VARCHAR(30) NOT NULL, -- 'grant', 'modify', 'revoke', 'transfer', 'create', 'duplicate', 'delete'
    structure_type VARCHAR(20) NOT NULL,
    structure_id BIGINT NOT NULL,
    affected_user_id INTEGER REFERENCES bm_users(user_id) ON DELETE SET NULL,
    previous_permission VARCHAR(20),
    new_permission VARCHAR(20),
    details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 6. Create indexes for audit
CREATE INDEX IF NOT EXISTS idx_struct_perm_audit_lookup ON bm_structure_permissions_audit(structure_type, structure_id);
