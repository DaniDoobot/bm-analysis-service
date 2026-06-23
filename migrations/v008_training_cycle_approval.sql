-- v008: Add approval audit fields to bm_training_agent_reports
-- Adds approved_at (timestamp) and approved_by_user_id (int) to support
-- the new two-phase cycle approval flow (pending_approval -> in_progress).
-- Safe: both columns are nullable with no default constraints.

ALTER TABLE bm_training_agent_reports
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS approved_by_user_id INTEGER NULL;

-- Create an index for querying pending_approval cycles efficiently
CREATE INDEX IF NOT EXISTS idx_training_report_pending_approval
    ON bm_training_agent_reports (hubspot_owner_id, status)
    WHERE status = 'pending_approval';
