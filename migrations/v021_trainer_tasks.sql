-- v021_trainer_tasks.sql
-- Migration script to create tables for Trainer Tasks functionality.
-- Non-destructive, idempotent, with strict foreign keys and audit preservation.

-- 1. Create Trainer Tasks Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_tasks (
    task_id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT,
    service_id INTEGER NOT NULL REFERENCES public.bm_services(service_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    description TEXT NULL,
    due_at TIMESTAMPTZ NULL,
    created_by_user_id INTEGER NULL REFERENCES public.bm_users(user_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_tasks_company ON public.bm_trainer_tasks(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_tasks_service ON public.bm_trainer_tasks(service_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_tasks_status ON public.bm_trainer_tasks(status);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_tasks_due_at ON public.bm_trainer_tasks(due_at);

-- 2. Create Trainer Task Items Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_task_items (
    task_item_id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES public.bm_trainer_tasks(task_id) ON DELETE RESTRICT,
    simulation_id INTEGER NOT NULL REFERENCES public.bm_trainer_simulations(simulation_id) ON DELETE RESTRICT,
    required_attempts INTEGER NOT NULL CHECK (required_attempts >= 1),
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trainer_task_item UNIQUE (task_id, simulation_id)
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_items_task ON public.bm_trainer_task_items(task_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_items_sim ON public.bm_trainer_task_items(simulation_id);

-- 3. Create Trainer Task Assignees Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_task_assignees (
    task_assignee_id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES public.bm_trainer_tasks(task_id) ON DELETE RESTRICT,
    company_id INTEGER NOT NULL REFERENCES public.bm_companies(company_id) ON DELETE RESTRICT,
    hubspot_owner_id TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trainer_task_assignee UNIQUE (task_id, hubspot_owner_id)
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_assignees_agent ON public.bm_trainer_task_assignees(hubspot_owner_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_assignees_company ON public.bm_trainer_task_assignees(company_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_assignees_status ON public.bm_trainer_task_assignees(status);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_task_assignees_task ON public.bm_trainer_task_assignees(task_id);

-- 4. Create Trainer Task Attempt Allocations Table
CREATE TABLE IF NOT EXISTS public.bm_trainer_task_attempt_allocations (
    allocation_id SERIAL PRIMARY KEY,
    task_assignee_id INTEGER NOT NULL REFERENCES public.bm_trainer_task_assignees(task_assignee_id) ON DELETE RESTRICT,
    task_item_id INTEGER NOT NULL REFERENCES public.bm_trainer_task_items(task_item_id) ON DELETE RESTRICT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES public.bm_trainer_sessions(session_id) ON DELETE RESTRICT,
    allocated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_trainer_allocations_assignee_item ON public.bm_trainer_task_attempt_allocations(task_assignee_id, task_item_id);
CREATE INDEX IF NOT EXISTS idx_bm_trainer_allocations_session ON public.bm_trainer_task_attempt_allocations(session_id);
