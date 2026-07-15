-- Migration: v012_multi_tenant_base.sql
-- Create base tables and add columns for multi-tenancy support.

-- 1. Create Companies Table
CREATE TABLE IF NOT EXISTS public.bm_companies (
    company_id SERIAL PRIMARY KEY,
    company_name TEXT UNIQUE NOT NULL,
    company_key TEXT UNIQUE NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Create Teams Table
CREATE TABLE IF NOT EXISTS public.bm_teams (
    team_id SERIAL PRIMARY KEY,
    team_name TEXT NOT NULL,
    company_id INTEGER NOT NULL REFERENCES public.bm_companies(company_id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_service_team_name UNIQUE (service_id, team_name)
);

-- 3. Create User-Services Association Table (for Responsable de Servicio)
CREATE TABLE IF NOT EXISTS public.bm_user_services (
    user_id INTEGER NOT NULL REFERENCES public.bm_users(user_id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, service_id)
);

-- 4. Create User-Teams Association Table (for Coordinador de Equipo)
CREATE TABLE IF NOT EXISTS public.bm_user_teams (
    user_id INTEGER NOT NULL REFERENCES public.bm_users(user_id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES public.bm_teams(team_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, team_id)
);

-- 5. Create Agent-Teams Association Table (for Agents)
CREATE TABLE IF NOT EXISTS public.bm_agent_teams (
    user_id INTEGER NOT NULL REFERENCES public.bm_users(user_id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES public.bm_teams(team_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, team_id)
);

-- 6. Add company_id Columns and FKs to Existing Tables (Idempotent / Additive)
ALTER TABLE public.bm_users ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_services ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT;
ALTER TABLE public.bm_prompts ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_prompt_base_structures ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_typologies ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE CASCADE;
ALTER TABLE public.bm_analyses ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_call_analysis_current ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_mass_evaluation_jobs ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_mass_evaluation_runs ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_mass_evaluation_results ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_training_agent_settings ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_training_runs ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_training_agent_reports ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;
ALTER TABLE public.bm_trainer_simulations ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT;
ALTER TABLE public.bm_trainer_evaluation_configs ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT;
ALTER TABLE public.bm_trainer_sessions ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT;
ALTER TABLE public.bm_training_evaluation_prompts ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL;

-- 7. Add service_id Columns and FKs where missing (Idempotent / Additive)
ALTER TABLE public.bm_analyses ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
ALTER TABLE public.bm_call_analysis_current ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
ALTER TABLE public.bm_mass_evaluation_jobs ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
ALTER TABLE public.bm_mass_evaluation_runs ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
-- bm_mass_evaluation_results already has service_id column, add FK
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_name = 'bm_mass_evaluation_results_service_id_fkey'
    ) THEN
        ALTER TABLE public.bm_mass_evaluation_results 
        ADD CONSTRAINT bm_mass_evaluation_results_service_id_fkey 
        FOREIGN KEY (service_id) REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
    END IF;
END $$;

ALTER TABLE public.bm_training_runs ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;
ALTER TABLE public.bm_training_agent_reports ADD COLUMN IF NOT EXISTS service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL;

-- 8. Add is_global Flag for Future Prompt Templates
ALTER TABLE public.bm_prompts ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.bm_prompt_base_structures ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;

-- 9. Add cycle_mode to TrainingAgentReport (Default 'automatic')
ALTER TABLE public.bm_training_agent_reports ADD COLUMN IF NOT EXISTS cycle_mode TEXT NOT NULL DEFAULT 'automatic';

-- 10. Add basic indexes for high-frequency queries
CREATE INDEX IF NOT EXISTS idx_bm_users_company_id ON public.bm_users(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_services_company_id ON public.bm_services(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_teams_company_id ON public.bm_teams(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_teams_service_id ON public.bm_teams(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_prompts_company_id ON public.bm_prompts(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_analyses_company_id ON public.bm_analyses(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_analyses_service_id ON public.bm_analyses(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_agent_reports_company_id ON public.bm_training_agent_reports(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_training_agent_reports_service_id ON public.bm_training_agent_reports(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_sessions_company_id ON public.bm_trainer_sessions(company_id);

-- 11. Add composite unique constraint for service_key per company
-- Note: Existing unique index uq_services_service_key on service_key will be kept in Fase 1
-- to avoid breaking existing code, and the composite index is added as new.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_name = 'uq_company_service_key'
    ) THEN
        ALTER TABLE public.bm_services ADD CONSTRAINT uq_company_service_key UNIQUE (company_id, service_key);
    END IF;
END $$;
