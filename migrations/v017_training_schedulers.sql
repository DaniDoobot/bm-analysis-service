-- v017_training_schedulers.sql
-- Migration script to create tables for Granular Training Schedulers.

-- 1. Create Training Schedulers Table
CREATE TABLE IF NOT EXISTS public.bm_training_schedulers (
    scheduler_id SERIAL PRIMARY KEY,
    company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE CASCADE,
    service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL,
    team_id INTEGER NULL REFERENCES public.bm_teams(team_id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    interval_days INTEGER NOT NULL DEFAULT 14,
    lookback_days INTEGER NOT NULL DEFAULT 14,
    last_run_at TIMESTAMPTZ NULL,
    next_run_at TIMESTAMPTZ NULL,
    last_status TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_training_schedulers_company_id ON public.bm_training_schedulers(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_schedulers_service_id ON public.bm_training_schedulers(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_schedulers_team_id ON public.bm_training_schedulers(team_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_schedulers_active_next ON public.bm_training_schedulers(is_active, next_run_at);

-- 2. Create Training Scheduler Agents Association Table
CREATE TABLE IF NOT EXISTS public.bm_training_scheduler_agents (
    id SERIAL PRIMARY KEY,
    scheduler_id INTEGER NOT NULL REFERENCES public.bm_training_schedulers(scheduler_id) ON DELETE CASCADE,
    hubspot_owner_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_scheduler_hubspot_owner UNIQUE (scheduler_id, hubspot_owner_id)
);

CREATE INDEX IF NOT EXISTS idx_bm_training_scheduler_agents_scheduler_id ON public.bm_training_scheduler_agents(scheduler_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_scheduler_agents_owner_id ON public.bm_training_scheduler_agents(hubspot_owner_id);
