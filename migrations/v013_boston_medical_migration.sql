-- Migration: v013_boston_medical_migration.sql
-- Backfill existing data to the default Boston Medical tenant and default Front team.

DO $$
DECLARE
    v_company_id INTEGER;
    v_front_service_id INTEGER;
    v_team_id INTEGER;
BEGIN
    -- 1. Insert Default Company 'Boston Medical' if not exists
    INSERT INTO public.bm_companies (company_name, company_key, is_active, created_at, updated_at)
    VALUES ('Boston Medical', 'boston-medical', TRUE, NOW(), NOW())
    ON CONFLICT (company_key) DO NOTHING;

    SELECT company_id INTO v_company_id
    FROM public.bm_companies
    WHERE company_key = 'boston-medical';

    -- 2. Resolve Front Service dynamically
    SELECT service_id INTO v_front_service_id
    FROM public.bm_services
    WHERE LOWER(service_key) = 'front' OR LOWER(service_name) = 'front'
    LIMIT 1;

    IF v_front_service_id IS NULL THEN
        RAISE EXCEPTION 'El servicio Front no fue encontrado. La migración no puede continuar.';
    END IF;

    -- 3. Insert Default Team 'Equipo Front Principal' if not exists
    INSERT INTO public.bm_teams (team_name, company_id, service_id, created_at, updated_at)
    VALUES ('Equipo Front Principal', v_company_id, v_front_service_id, NOW(), NOW())
    ON CONFLICT (service_id, team_name) DO NOTHING;

    SELECT team_id INTO v_team_id
    FROM public.bm_teams
    WHERE service_id = v_front_service_id AND team_name = 'Equipo Front Principal';

    -- 4. Associate existing AGENT users to the default team
    INSERT INTO public.bm_agent_teams (user_id, team_id, created_at)
    SELECT user_id, v_team_id, NOW()
    FROM public.bm_users
    WHERE LOWER(role) IN ('agent', 'agente')
    ON CONFLICT (user_id, team_id) DO NOTHING;

    -- 5. Backfill company_id to existing records (only where company_id is NULL)
    UPDATE public.bm_services SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_users SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_prompts SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_prompt_base_structures SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_typologies SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_analyses SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_call_analysis_current SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_mass_evaluation_jobs SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_mass_evaluation_runs SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_mass_evaluation_results SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_training_agent_settings SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_training_runs SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_training_agent_reports SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_trainer_simulations SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_trainer_evaluation_configs SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_trainer_sessions SET company_id = v_company_id WHERE company_id IS NULL;
    UPDATE public.bm_training_evaluation_prompts SET company_id = v_company_id WHERE company_id IS NULL;

    -- 6. Backfill service_id to existing records (only where service_id is NULL, assigning Front)
    UPDATE public.bm_analyses SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_call_analysis_current SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_mass_evaluation_jobs SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_mass_evaluation_runs SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_mass_evaluation_results SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_training_runs SET service_id = v_front_service_id WHERE service_id IS NULL;
    UPDATE public.bm_training_agent_reports SET service_id = v_front_service_id WHERE service_id IS NULL;

    -- 7. Ensure cycle_mode is set to 'automatic' if NULL or empty
    UPDATE public.bm_training_agent_reports 
    SET cycle_mode = 'automatic' 
    WHERE cycle_mode IS NULL OR cycle_mode = '';

END $$;
