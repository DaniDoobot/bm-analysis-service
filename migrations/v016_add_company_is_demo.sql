-- Migration: v016_add_company_is_demo.sql
-- Adds security isolation flag is_demo to bm_companies table.
-- Existing companies default to FALSE.
-- Downgrade: ALTER TABLE public.bm_companies DROP COLUMN is_demo;

ALTER TABLE public.bm_companies
ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_bm_companies_is_demo
ON public.bm_companies (is_demo);
