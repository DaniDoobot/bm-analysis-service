-- v022_trainer_chat_persistence.sql
-- Migration script to create tables for AI Tutor chat persistence.
-- Non-destructive, idempotent, with strict foreign keys and tenant isolation.

-- 1. Create Trainer Chats Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_chats (
    chat_id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT,
    owner_user_id INTEGER NOT NULL REFERENCES public.bm_users(user_id) ON DELETE CASCADE,
    target_agent_id TEXT NULL,
    title TEXT NOT NULL DEFAULT 'Nueva conversación',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_chats_owner_updated 
    ON public.bm_trainer_chats(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_chats_company 
    ON public.bm_trainer_chats(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_chats_target_agent 
    ON public.bm_trainer_chats(owner_user_id, target_agent_id);

-- 2. Create Trainer Chat Messages Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_chat_messages (
    message_id SERIAL PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES public.bm_trainer_chats(chat_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    input_type TEXT NOT NULL DEFAULT 'text' CHECK (input_type IN ('text', 'audio')),
    metadata_json JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_chat_messages_chat_created 
    ON public.bm_trainer_chat_messages(chat_id, created_at ASC);
