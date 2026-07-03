-- v010_trainer_module.sql
-- Migration script to create tables for the Trainer module.

-- 1. Create Trainer Evaluation Configs Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_evaluation_configs (
    config_id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE RESTRICT,
    speech_structure_id BIGINT NOT NULL REFERENCES public.bm_prompts(prompt_id) ON DELETE RESTRICT,
    extra_instructions TEXT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Create Trainer Simulations Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_simulations (
    simulation_id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE RESTRICT,
    evaluation_config_id INTEGER NULL REFERENCES public.bm_trainer_evaluation_configs(config_id) ON DELETE SET NULL,
    roleplay_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    objective TEXT NULL,
    difficulty TEXT NULL,
    created_by TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ NULL,
    archived_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_simulations_code ON public.bm_trainer_simulations(code);

-- 3. Create Trainer Simulation Versions Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_simulation_versions (
    version_id SERIAL PRIMARY KEY,
    simulation_id INTEGER NOT NULL REFERENCES public.bm_trainer_simulations(simulation_id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL,
    roleplay_prompt_snapshot TEXT NOT NULL,
    evaluation_config_snapshot JSONB NOT NULL,
    service_id INTEGER NOT NULL,
    evaluation_config_id INTEGER NOT NULL,
    created_by TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_bm_trainer_sim_version UNIQUE (simulation_id, version_number)
);

-- 4. Create Trainer Sessions Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_sessions (
    session_id SERIAL PRIMARY KEY,
    simulation_id INTEGER NOT NULL REFERENCES public.bm_trainer_simulations(simulation_id) ON DELETE RESTRICT,
    simulation_version_id INTEGER NULL REFERENCES public.bm_trainer_simulation_versions(version_id) ON DELETE SET NULL,
    agent_id TEXT NOT NULL,
    agent_code TEXT NOT NULL,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE RESTRICT,
    call_id TEXT NOT NULL,
    external_call_sid TEXT NULL,
    recording_url TEXT NULL,
    transcript TEXT NULL,
    duration_seconds INTEGER NULL,
    status TEXT NOT NULL DEFAULT 'started',
    evaluation_status TEXT NOT NULL DEFAULT 'started',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_sessions_agent_id ON public.bm_trainer_sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_sessions_call_id ON public.bm_trainer_sessions(call_id);

-- 5. Create Trainer Evaluations Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_evaluations (
    evaluation_id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES public.bm_trainer_sessions(session_id) ON DELETE RESTRICT,
    evaluation_config_id INTEGER NULL REFERENCES public.bm_trainer_evaluation_configs(config_id) ON DELETE SET NULL,
    prompt_snapshot TEXT NOT NULL,
    result_json JSONB NOT NULL,
    score NUMERIC(5,2) NULL,
    summary TEXT NULL,
    strengths JSONB NULL,
    improvement_points JSONB NULL,
    error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
