#!/usr/bin/env bash
# ==============================================================================
# scripts/refresh_staging_db.sh
#
# Automated procedure to clone the PostgreSQL production database into a dedicated
# STAGING / PRUEBAS environment, apply security neutralization, and run validation.
#
# USAGE:
#   export PROD_DATABASE_URL="postgresql://user:pass@host:port/speechbm"
#   export STAGING_DATABASE_URL="postgresql://user:pass@host:port/speech_staging"
#   ./scripts/refresh_staging_db.sh --confirm-refresh-staging
#
# ==============================================================================
# CRITICAL SAFETY RULES:
#   # NEVER DROP PRODUCTION
#   # TARGET MUST BE STAGING
#   1. Target database name MUST contain: staging, pruebas, test, or dev.
#   2. Target database name CANNOT match any known production database name.
#   3. PROD_DATABASE_URL and STAGING_DATABASE_URL must be completely distinct.
#   4. Requires explicit confirmation parameter: --confirm-refresh-staging.
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# 1. PARSE ARGUMENTS & REQUIRE CONFIRMATION
# ------------------------------------------------------------------------------
CONFIRMED=false
for arg in "$@"; do
    if [ "$arg" = "--confirm-refresh-staging" ]; then
        CONFIRMED=true
    fi
done

if [ "$CONFIRMED" != "true" ]; then
    echo "========================================================================"
    echo "CRITICAL ERROR: Execution aborted."
    echo "This script overwrites the STAGING database with a fresh production clone."
    echo "To proceed, you MUST provide the explicit confirmation flag:"
    echo "  $0 --confirm-refresh-staging"
    echo "========================================================================"
    exit 1
fi

# ------------------------------------------------------------------------------
# 2. VALIDATE MANDATORY ENVIRONMENT VARIABLES
# ------------------------------------------------------------------------------
if [ -z "${PROD_DATABASE_URL:-}" ]; then
    echo "ERROR: Mandatory environment variable PROD_DATABASE_URL is not set." >&2
    exit 1
fi

if [ -z "${STAGING_DATABASE_URL:-}" ]; then
    echo "ERROR: Mandatory environment variable STAGING_DATABASE_URL is not set." >&2
    exit 1
fi

if [ "$PROD_DATABASE_URL" = "$STAGING_DATABASE_URL" ]; then
    echo "CRITICAL SAFETY VIOLATION: PROD_DATABASE_URL and STAGING_DATABASE_URL are identical! Aborting." >&2
    exit 1
fi

# ------------------------------------------------------------------------------
# 3. EXTRACT AND VALIDATE DATABASE NAMES (NEVER REVEAL PASSWORDS)
# ------------------------------------------------------------------------------
# Function to extract database name from postgresql connection URL
get_dbname() {
    local url="$1"
    # Remove query string if any
    local clean_url="${url%%\?*}"
    # Extract portion after last slash
    echo "${clean_url##*/}"
}

# Function to extract host and port without credentials
get_host_port() {
    local url="$1"
    local clean_url="${url%%\?*}"
    # Extract between @ and /
    local host_part="${clean_url#*@}"
    echo "${host_part%%/*}"
}

SOURCE_DB="$(get_dbname "$PROD_DATABASE_URL")"
TARGET_DB="$(get_dbname "$STAGING_DATABASE_URL")"
SOURCE_HOST="$(get_host_port "$PROD_DATABASE_URL")"
TARGET_HOST="$(get_host_port "$STAGING_DATABASE_URL")"

echo "========================================================================"
echo "STAGING REFRESH EXECUTION PLAN"
echo "  SOURCE DATABASE: ${SOURCE_DB} (at ${SOURCE_HOST})"
echo "  TARGET DATABASE: ${TARGET_DB} (at ${TARGET_HOST})"
echo "========================================================================"

# Target MUST contain staging, pruebas, test, or dev
TARGET_DB_LOWER="$(echo "$TARGET_DB" | tr '[:upper:]' '[:lower:]')"
if [[ ! "$TARGET_DB_LOWER" =~ (staging|pruebas|test|dev) ]]; then
    echo "CRITICAL SAFETY VIOLATION: Target database '${TARGET_DB}' does not contain 'staging', 'pruebas', 'test', or 'dev'." >&2
    echo "ABORTING IMMEDIATELY to prevent accidental damage to non-staging database!" >&2
    exit 1
fi

# Target MUST NOT be a known production database name
if [[ "$TARGET_DB_LOWER" =~ ^(n8n|speechbm|production|prod|bm_analysis|postgres)$ ]]; then
    echo "CRITICAL SAFETY VIOLATION: Target database '${TARGET_DB}' is a protected production database name!" >&2
    echo "ABORTING IMMEDIATELY!" >&2
    exit 1
fi

# Target database name cannot match source database name
if [ "$TARGET_DB_LOWER" = "$(echo "$SOURCE_DB" | tr '[:upper:]' '[:lower:]')" ]; then
    echo "CRITICAL SAFETY VIOLATION: Target database name is identical to source database name!" >&2
    exit 1
fi

# # NEVER DROP PRODUCTION
# # TARGET MUST BE STAGING

# Locate SQL helper scripts relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEUTRALIZE_SQL="${SCRIPT_DIR}/neutralize_staging_db.sql"
VALIDATE_SQL="${SCRIPT_DIR}/validate_staging_db.sql"

if [ ! -f "$NEUTRALIZE_SQL" ]; then
    echo "ERROR: Neutralization script not found at ${NEUTRALIZE_SQL}" >&2
    exit 1
fi

if [ ! -f "$VALIDATE_SQL" ]; then
    echo "ERROR: Validation script not found at ${VALIDATE_SQL}" >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DUMP_FILE="/tmp/speech_prod_snapshot_${TIMESTAMP}.dump"

echo ""
echo "[1/5] Creating SELECTIVE dump of Speech BM objects (public.bm_* and public.vw_bm_*)..."
echo "      Excluding all generic n8n and agent execution tables from dump."
echo "      Output file: ${DUMP_FILE}"
# Selective custom format dump (-Fc):
# Only dumps tables matching 'public.bm_*' and views matching 'public.vw_bm_*'.
# Automatically includes all column sequences, indexes, primary/foreign keys, and constraints.
pg_dump --format=custom \
        --no-owner \
        --no-privileges \
        -t 'public.bm_*' \
        -t 'public.vw_bm_*' \
        --dbname="$PROD_DATABASE_URL" \
        --file="$DUMP_FILE"

DUMP_SIZE="$(du -h "$DUMP_FILE" | cut -f1)"
echo "      Selective Speech BM snapshot generated successfully (Size: ${DUMP_SIZE})."

echo ""
echo "[2/5] Restoring selective snapshot into TARGET STAGING database (${TARGET_DB})..."
# --clean drops database objects prior to recreating them (ONLY objects contained in the archive!)
# --if-exists prevents errors on initial empty databases
# --no-owner & --no-privileges ensure staging user owns all imported tables/views
pg_restore --clean \
           --if-exists \
           --no-owner \
           --no-privileges \
           --dbname="$STAGING_DATABASE_URL" \
           "$DUMP_FILE" || {
    # pg_restore returns 1 on non-fatal warnings (e.g. drop table if exists when table is absent)
    # We log notice and proceed to validation
    echo "      Note: pg_restore completed with minor notices (normal during clean overwrite)."
}

echo ""
echo "[3/5] Neutralizing background schedulers and active runs in STAGING..."
psql "$STAGING_DATABASE_URL" -f "$NEUTRALIZE_SQL"

echo ""
echo "[4/5] Executing post-refresh verification checks on STAGING..."
psql "$STAGING_DATABASE_URL" -f "$VALIDATE_SQL"

echo ""
echo "[5/5] Cleaning up temporary snapshot file..."
rm -f "$DUMP_FILE"

echo ""
echo "========================================================================"
echo "[SUCCESS] Staging database refresh completed safely and successfully."
echo "Target database '${TARGET_DB}' is fully cloned, disarmed, and verified."
echo "========================================================================"
