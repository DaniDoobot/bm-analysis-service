#!/usr/bin/env python
"""
scripts/create_pruebas_db_from_production.py

Safe and isolated script to clone schema and base/config tables from the production database 
to a designated test database ('pruebas'), ensuring absolute exclusion of conversation records, 
patient data, audits, and other sensitive information.
"""
import argparse
import asyncio
import os
import sys
from typing import List

from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

sys.path.insert(0, os.path.abspath("."))

from app.db import Base
import app.models
from app.utils.security import hash_password

# 1. Configured Tables List
COPIED_TABLES = [
    "bm_users",
    "bm_services",
    "bm_typologies",
    "bm_training_agent_settings",
    "bm_training_scheduler_settings",
    "bm_prompts",
    "bm_prompt_versions",
    "bm_prompt_criteria",
    "bm_prompt_criterion_typologies",
    "bm_prompt_base_structures",
    "bm_training_evaluation_prompts",
    "bm_prompt_drafts",
]

EMPTY_TABLES = [
    "bm_password_reset_tokens",
    "bm_user_audits",
    "bm_structure_permissions",
    "bm_structure_permissions_audit",
    "bm_criteria_sync_logs",
    "bm_analyses",
    "bm_analysis_results",
    "bm_analysis_criterion_results",
    "bm_call_analysis_current",
    "bm_mass_evaluation_jobs",
    "bm_mass_evaluation_runs",
    "bm_mass_evaluation_results",
    "bm_mass_evaluation_criterion_results",
    "bm_mass_analysis_automations",
    "bm_mass_analysis_automation_runs",
    "bm_training_runs",
    "bm_training_agent_reports",
    "bm_training_simulation_prompts",
    "bm_training_completion_status",
    "bm_training_call_sessions",
    "bm_training_call_evaluations",
]

DEFAULT_TEST_PASSWORD = "SpeechPruebas2026!"


def _make_async_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    url = raw_url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
            break
    return url


async def reset_postgresql_sequences(conn, tables: List[str]):
    """Update primary key serial sequences to avoid constraint collisions on future inserts."""
    print("Resetting primary key sequences...")
    for table in tables:
        # Resolve ID column name (usually table_id or id)
        res_cols = await conn.execute(text(f"""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'public' 
              AND table_name = '{table}' 
              AND column_name IN ('id', '{table.replace("bm_", "")[:-1]}_id', 'prompt_id', 'criterion_id', 'session_id');
        """))
        col_row = res_cols.fetchone()
        if not col_row:
            continue
        col = col_row[0]
        
        # Check if column has a serial sequence
        res_seq = await conn.execute(text(f"SELECT pg_get_serial_sequence('{table}', '{col}');"))
        seq = res_seq.scalar()
        if seq:
            # Check max value in the table
            res_max = await conn.execute(text(f"SELECT MAX({col}) FROM {table};"))
            max_val = res_max.scalar()
            if max_val is not None:
                await conn.execute(text(f"SELECT setval('{seq}', {max_val});"))
                print(f"  Sequence reset for {table}.{col} to {max_val}")
            else:
                await conn.execute(text(f"SELECT setval('{seq}', 1, false);"))
                print(f"  Sequence reset for empty {table}.{col} to 1")


async def main():
    parser = argparse.ArgumentParser(description="Clone production configuration to a test database.")
    parser.add_argument(
        "--confirm-create-pruebas",
        action="store_true",
        help="Explicit confirmation parameter required to run the copy script.",
    )
    args = parser.parse_args()

    if not args.confirm_create_pruebas:
        print("CRITICAL ERROR: Execution aborted. You must supply the '--confirm-create-pruebas' flag to proceed.")
        sys.exit(1)

    # 1. Read and validate environment variables
    prod_url = os.environ.get("DATABASE_URL_PROD")
    pruebas_url = os.environ.get("DATABASE_URL_PRUEBAS")

    if not prod_url:
        print("CRITICAL ERROR: DATABASE_URL_PROD environment variable is not defined.")
        sys.exit(1)

    if not pruebas_url:
        print("CRITICAL ERROR: DATABASE_URL_PRUEBAS environment variable is not defined.")
        sys.exit(1)

    # 2. Strict safety checks
    if prod_url.strip() == pruebas_url.strip():
        print("CRITICAL SAFETY VIOLATION: Source and Destination databases are identical. Aborting for safety!")
        sys.exit(1)

    # Check for 'pruebas' in destination db name
    destination_dbname = pruebas_url.split("/")[-1].split("?")[0]
    if "pruebas" not in destination_dbname.lower():
        print(f"CRITICAL SAFETY VIOLATION: Destination database name '{destination_dbname}' does not contain the word 'pruebas'. Aborting!")
        sys.exit(1)

    # Convert URLs to asyncpg dialect
    async_prod_url = _make_async_url(prod_url)
    async_pruebas_url = _make_async_url(pruebas_url)

    print(f"Source Database (PROD): {prod_url.split('@')[-1]}")
    print(f"Destination Database (PRUEBAS): {pruebas_url.split('@')[-1]}")
    print("Starting environment setup...")

    prod_engine = create_async_engine(async_prod_url, echo=False)
    pruebas_engine = create_async_engine(async_pruebas_url, echo=False)

    try:
        # 3. Create schema and tables in the target database
        async with pruebas_engine.begin() as conn:
            print("\nRecreating 'bm_' tables in the target database...")
            # Drop existing bm_ tables to ensure a clean slate
            # We fetch all tables first
            res_tables = await conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name LIKE 'bm_%%';
            """))
            existing_bm_tables = [r[0] for r in res_tables.fetchall()]
            
            # Disable triggers/constraints for dropping in reverse dependency order
            if existing_bm_tables:
                print(f"  Dropping existing bm_ tables ({len(existing_bm_tables)} found)...")
                # Drop in cascade to clear constraints
                for table in reversed(COPIED_TABLES + EMPTY_TABLES):
                    if table in existing_bm_tables:
                        await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE;"))
            
            # Generate all tables defined in Metadata
            # Filter metadata to only include 'bm_' tables to prevent creating n8n tables in tests
            bm_tables_metadata = [t for t in Base.metadata.sorted_tables if t.name.startswith("bm_")]
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=bm_tables_metadata))
            print("  Table schemas created successfully.")

        # 4. Copy configuration data from production (Read-only on prod, Write on target)
        async with prod_engine.connect() as prod_conn:
            async with AsyncSession(pruebas_engine) as session_dest:
                print("\nMigrating configuration tables in strict order...")
                
                # Pre-calculate test password hash
                test_password_hash = hash_password(DEFAULT_TEST_PASSWORD)
                
                for table_name in COPIED_TABLES:
                    print(f"  Copying {table_name}...")
                    # Fetch all rows from production
                    res_prod = await prod_conn.execute(text(f"SELECT * FROM {table_name};"))
                    columns = res_prod.keys()
                    rows = res_prod.fetchall()
                    
                    if not rows:
                        print(f"    Table {table_name} is empty in production. Skipped.")
                        continue

                    # Insert rows into target in bulk
                    rows_to_insert = []
                    for row in rows:
                        row_dict = dict(zip(columns, row))
                        
                        # Apply security sanitization to user passwords
                        if table_name == "bm_users":
                            row_dict["password_hash"] = test_password_hash
                            row_dict["must_reset_password"] = True
                            
                        # Serialize list/dict fields as JSON strings for PostgreSQL JSON/JSONB compatibility in text-based executes
                        import json
                        for k, v in row_dict.items():
                            if isinstance(v, (list, dict)):
                                row_dict[k] = json.dumps(v)
                        rows_to_insert.append(row_dict)
                    
                    if rows_to_insert:
                        # Build insert query dynamically
                        col_names = ", ".join(rows_to_insert[0].keys())
                        placeholders = ", ".join([f":{k}" for k in rows_to_insert[0].keys()])
                        insert_query = text(f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders});")
                        await session_dest.execute(insert_query, rows_to_insert)
                    
                    await session_dest.flush()
                    print(f"    Copied {len(rows)} records successfully.")
                
                await session_dest.commit()
                print("All config tables migrated successfully.")

        # 5. Reset sequence indices in target
        async with pruebas_engine.begin() as conn:
            await reset_postgresql_sequences(conn, COPIED_TABLES)

        # 6. Verify setup and run counts
        print("\n=== RUN VERIFICATION REPORT ===")
        async with AsyncSession(pruebas_engine) as session_dest:
            all_ok = True
            
            # Verify config tables have rows
            print("Configuration Tables Count in Target:")
            for table_name in COPIED_TABLES:
                res_cnt = await session_dest.execute(text(f"SELECT COUNT(*) FROM {table_name};"))
                count = res_cnt.scalar()
                print(f"  - {table_name}: {count} rows")
                if count == 0 and table_name not in ["bm_prompt_drafts", "bm_prompt_sections", "bm_training_evaluation_prompts"]:
                    all_ok = False
            
            # Verify sensitive tables are empty
            print("\nExcluded/Sensitive Tables Count in Target (Must be 0):")
            for table_name in EMPTY_TABLES:
                res_cnt = await session_dest.execute(text(f"SELECT COUNT(*) FROM {table_name};"))
                count = res_cnt.scalar()
                print(f"  - {table_name}: {count} rows")
                if count != 0:
                    print(f"    [WARNING] Excluded table '{table_name}' contains {count} records! Verification failed.")
                    all_ok = False
            
            if all_ok:
                print("\n[VERIFICATION SUCCESS] The pruebas database has been populated safely and isolated completely.")
                print(f"All user accounts in the new environment have their password reset to: '{DEFAULT_TEST_PASSWORD}'")
            else:
                print("\n[VERIFICATION FAILED] Discrepancies detected during post-migration verification checks.")
                
    finally:
        await prod_engine.dispose()
        await pruebas_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
