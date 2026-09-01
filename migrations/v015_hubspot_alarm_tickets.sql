-- Migration: v015_hubspot_alarm_tickets.sql
-- Adds HubSpot contact and alarm ticket tracking columns to bm_mass_evaluation_results.

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS hubspot_contact_id TEXT NULL;

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS hubspot_ticket_id TEXT NULL;

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS hubspot_ticket_status TEXT NULL;

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS hubspot_ticket_created_at TIMESTAMPTZ NULL;

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS hubspot_ticket_error TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_mass_eval_results_contact_id 
ON public.bm_mass_evaluation_results (hubspot_contact_id);

CREATE INDEX IF NOT EXISTS idx_mass_eval_results_ticket_status 
ON public.bm_mass_evaluation_results (hubspot_ticket_status);

CREATE INDEX IF NOT EXISTS idx_mass_eval_results_ticket_id 
ON public.bm_mass_evaluation_results (hubspot_ticket_id);
