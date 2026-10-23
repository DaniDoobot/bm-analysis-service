"""Alembic migration: prompt_snapshots_deduplication

Revision ID: v029_prompt_snapshots
Revises: v028_gemini_3_1_flash_lite
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'v029_prompt_snapshots'
down_revision = 'v028_gemini_3_1_flash_lite'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create table bm_prompt_snapshots
    op.create_table(
        'bm_prompt_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('hash_sha256', sa.String(length=64), nullable=False),
        sa.Column('prompt_text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('hash_sha256', name='uq_bm_prompt_snapshots_hash'),
    )
    op.create_index('idx_bm_prompt_snapshots_hash', 'bm_prompt_snapshots', ['hash_sha256'])

    # 2. Add nullable prompt_snapshot_id to bm_mass_evaluation_results
    op.add_column(
        'bm_mass_evaluation_results',
        sa.Column('prompt_snapshot_id', sa.BigInteger(), nullable=True)
    )
    op.create_index(
        'idx_bm_mass_eval_res_prompt_snapshot_id',
        'bm_mass_evaluation_results',
        ['prompt_snapshot_id']
    )
    op.create_foreign_key(
        'fk_bm_mass_eval_res_prompt_snapshot',
        'bm_mass_evaluation_results',
        'bm_prompt_snapshots',
        ['prompt_snapshot_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # 3. Batch backfill using PostgreSQL native SHA256 aggregation
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == 'postgresql':
        op.execute("""
            INSERT INTO public.bm_prompt_snapshots (hash_sha256, prompt_text, created_at)
            SELECT 
                encode(sha256(convert_to(prompt_snapshot, 'UTF8')), 'hex') AS hash_sha256,
                prompt_snapshot AS prompt_text,
                MIN(created_at) AS created_at
            FROM public.bm_mass_evaluation_results
            WHERE prompt_snapshot IS NOT NULL 
              AND prompt_snapshot != ''
            GROUP BY prompt_snapshot
            ON CONFLICT (hash_sha256) DO NOTHING;
        """)
        op.execute("""
            UPDATE public.bm_mass_evaluation_results r
            SET prompt_snapshot_id = s.id
            FROM public.bm_prompt_snapshots s
            WHERE r.prompt_snapshot_id IS NULL
              AND r.prompt_snapshot IS NOT NULL
              AND r.prompt_snapshot != ''
              AND s.hash_sha256 = encode(sha256(convert_to(r.prompt_snapshot, 'UTF8')), 'hex');
        """)


def downgrade() -> None:
    op.drop_constraint('fk_bm_mass_eval_res_prompt_snapshot', 'bm_mass_evaluation_results', type_='foreignkey')
    op.drop_index('idx_bm_mass_eval_res_prompt_snapshot_id', table_name='bm_mass_evaluation_results')
    op.drop_column('bm_mass_evaluation_results', 'prompt_snapshot_id')
    op.drop_index('idx_bm_prompt_snapshots_hash', table_name='bm_prompt_snapshots')
    op.drop_table('bm_prompt_snapshots')
