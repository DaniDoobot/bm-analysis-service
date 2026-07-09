-- Migration: v011_training_call_attempts.sql
-- Create table for tracking attempts count per call_sid manually

CREATE TABLE IF NOT EXISTS public.bm_training_call_attempts (
    call_sid TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
