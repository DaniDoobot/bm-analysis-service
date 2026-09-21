-- v018_training_knowledge_documents.sql
-- Migration script to create tables for Trainer Knowledge Layer Documents.

CREATE TABLE IF NOT EXISTS public.bm_training_knowledge_documents (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE CASCADE,
    hubspot_owner_id TEXT NOT NULL,
    service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL,
    team_id INTEGER NULL REFERENCES public.bm_teams(team_id) ON DELETE SET NULL,
    cycle_id INTEGER NOT NULL REFERENCES public.bm_training_agent_reports(training_report_id) ON DELETE CASCADE,
    simulation_id INTEGER NULL REFERENCES public.bm_training_simulation_prompts(simulation_prompt_id) ON DELETE SET NULL,
    evaluation_id INTEGER NULL REFERENCES public.bm_training_call_evaluations(evaluation_id) ON DELETE SET NULL,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_company_id ON public.bm_training_knowledge_documents(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_owner_id ON public.bm_training_knowledge_documents(hubspot_owner_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_service_id ON public.bm_training_knowledge_documents(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_team_id ON public.bm_training_knowledge_documents(team_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_cycle_id ON public.bm_training_knowledge_documents(cycle_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_sim_id ON public.bm_training_knowledge_documents(simulation_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_eval_id ON public.bm_training_knowledge_documents(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_bm_knowledge_docs_type ON public.bm_training_knowledge_documents(document_type);

-- Partial unique indexes for idempotency
CREATE UNIQUE INDEX IF NOT EXISTS uq_bm_knowledge_docs_simulation 
ON public.bm_training_knowledge_documents(cycle_id, simulation_id, document_type) 
WHERE simulation_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bm_knowledge_docs_cycle 
ON public.bm_training_knowledge_documents(cycle_id, document_type) 
WHERE simulation_id IS NULL;
