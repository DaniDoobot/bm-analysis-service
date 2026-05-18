"""
Schema audit script: compares PostgreSQL information_schema with SQLAlchemy ORM models.

Run with:
  cd /path/to/bm-analysis-service
  .venv/Scripts/python.exe -m app.utils.schema_audit

Prints a diff table showing columns where DB type != ORM type.
"""
import asyncio
import sys
from pathlib import Path

# Make sure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from app.db import get_engine


TABLES = [
    "bm_analyses",
    "bm_call_analysis_current",
    "bm_analysis_results",
    "bm_prompts",
    "bm_prompt_versions",
    "bm_prompt_criteria",
    "bm_prompt_drafts",
]

# ORM column type mapping (table → column → expected SA type label)
# Derived from reading all model files manually — update as models evolve.
ORM_TYPES: dict[str, dict[str, str]] = {
    "bm_analyses": {
        "analysis_id": "bigint",
        "analysis_type": "varchar",
        "call_id": "varchar",
        "hubspot_url": "text",
        "call_direction": "varchar",
        "call_timestamp": "timestamptz",
        "source": "varchar",
        "run_ts": "timestamptz",
        "fecha_eval": "timestamptz",
        "agente_telefonico": "varchar",
        "hubspot_owner_id": "varchar",
        "prompt_id": "bigint",
        "prompt_version_id": "bigint",
        "transcription": "text",
        "transcription_provider": "varchar",
        "transcription_model": "varchar",
        "model_provider": "varchar",
        "model_name": "varchar",
        "status": "varchar",
        "tipo_llamada": "varchar",
        "evaluacion_global": "numeric",
        "result": "jsonb",
        "payload": "jsonb",
        "error_message": "text",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
    "bm_call_analysis_current": {
        "call_id": "varchar",
        "analysis_type": "varchar",
        "latest_analysis_id": "bigint",
        "hubspot_url": "text",
        "call_direction": "varchar",
        "call_timestamp": "timestamptz",
        "source": "varchar",
        "fecha_eval": "timestamptz",
        "updated_at": "timestamptz",
        "agente_telefonico": "varchar",
        "hubspot_owner_id": "varchar",
        "prompt_id": "bigint",
        "prompt_version_id": "bigint",
        "status": "varchar",
        "tipo_llamada": "varchar",
        "evaluacion_global": "numeric",
        "result": "jsonb",
        "payload": "jsonb",
    },
    "bm_analysis_results": {
        "result_id": "bigint",
        "analysis_id": "bigint",
        "criterion_id": "bigint",
        "criterion_key": "varchar",
        "criterion_name": "varchar",
        "criterion_type": "varchar",
        "value_number": "numeric",
        "value_text": "text",
        "value_boolean": "bool",
        "value_category": "varchar",
        "feed": "text",
        "description": "text",
        "raw_value": "jsonb",
        "created_at": "timestamptz",
    },
    "bm_prompts": {
        "prompt_id": "bigint",
        "prompt_name": "varchar",
        "prompt_type": "varchar",
        "description": "text",
        "is_active": "bool",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
    "bm_prompt_versions": {
        "id": "bigint",
        "prompt_id": "bigint",
        "prompt_content": "text",
        "version_label": "varchar",
        "updated_by": "varchar",
        "updated_by_email": "varchar",
        "change_note": "text",
        "source": "varchar",
        "is_current": "bool",
        "created_at": "timestamptz",
    },
    "bm_prompt_criteria": {
        "criterion_id": "bigint",
        "prompt_id": "bigint",
        "criterion_key": "varchar",
        "criterion_name": "varchar",
        "criterion_description": "text",
        "criterion_type": "varchar",
        "output_key": "varchar",
        "feed_key": "varchar",
        "allowed_values": "jsonb",
        "applies_to_types": "jsonb",
        "order_index": "int4",
        "is_required": "bool",
        "is_active": "bool",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
    "bm_prompt_drafts": {
        "draft_id": "bigint",
        "prompt_id": "bigint",
        "draft_name": "varchar",
        "draft_data": "jsonb",
        "updated_by": "varchar",
        "updated_by_email": "varchar",
        "status": "varchar",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
}

# Normalise PG udt_name to a short label we can compare
_PG_TYPE_MAP = {
    "int8": "bigint",
    "int4": "int4",
    "int2": "int2",
    "float4": "float4",
    "float8": "float8",
    "numeric": "numeric",
    "bool": "bool",
    "text": "text",
    "varchar": "varchar",
    "bpchar": "varchar",
    "timestamptz": "timestamptz",
    "timestamp": "timestamp",
    "date": "date",
    "jsonb": "jsonb",
    "json": "json",
    "_text": "text[]",
    "_jsonb": "jsonb[]",
}


async def main() -> None:
    query = text("""
        SELECT table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_name = ANY(:tables)
        ORDER BY table_name, ordinal_position
    """)

    try:
        from app.db import get_engine
        engine = get_engine()
    except Exception as e:
        # Fallback: build engine from env
        import os
        from sqlalchemy.ext.asyncio import create_async_engine
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("ERROR: DATABASE_URL not set in environment.")
            return
        # ensure asyncpg
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://")
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
            
        engine = create_async_engine(db_url)

    mismatches = []
    col_rows = []

    async with engine.connect() as conn:
        result = await conn.execute(query, {"tables": TABLES})
        rows = result.fetchall()

    print(f"\n{'TABLE':<35} {'COLUMN':<30} {'PG TYPE':<15} {'ORM EXPECTS':<15} {'MATCH?'}")
    print("-" * 105)

    for row in rows:
        table, column, data_type, udt_name = row
        pg_label = _PG_TYPE_MAP.get(udt_name, udt_name)
        orm_expects = ORM_TYPES.get(table, {}).get(column, "?")
        match = "OK" if pg_label == orm_expects else "FAIL"
        if pg_label != orm_expects and orm_expects != "?":
            mismatches.append((table, column, pg_label, orm_expects))
        print(f"{table:<35} {column:<30} {pg_label:<15} {orm_expects:<15} {match}")

    print("\n")
    if mismatches:
        print(f"⚠️  {len(mismatches)} mismatches found:")
        for table, column, pg, orm in mismatches:
            print(f"   {table}.{column}: DB={pg}, ORM expects={orm}")
    else:
        print("✅  All mapped columns match PostgreSQL schema.")


if __name__ == "__main__":
    asyncio.run(main())
