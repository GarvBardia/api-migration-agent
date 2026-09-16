"""resize api_doc_chunks.embedding for a local embedding model (Step 10)

Revision ID: 0005_api_doc_chunks_embed_dim
Revises: 0004_migration_runs_pr_metadata
Create Date: 2026-08-27 00:00:00

**Naming note:** the first attempt at this migration used the longer id
`0005_api_doc_chunks_local_embedding_dim` (39 chars) and failed applying
--`alembic_version.version_num` is `VARCHAR(32)`, same as every other
migration file in this project already (implicitly) respects -- caught
immediately (the whole upgrade rolled back cleanly, transactional DDL, no
partial schema change), renamed to this 29-char id, and reapplied
successfully. Noted here rather than silently renaming, since it's the
kind of easy-to-repeat mistake worth a real trail.

Why this migration exists (read before applying):

`api_doc_chunks.embedding` was `vector(1536)`, sized for OpenAI's
`text-embedding-3-small`. No embedding API key (OpenAI or otherwise) is
configured in this environment -- rather than block Step 10 on that,
this project defaults to a free, locally-run embedding model
(`sentence-transformers`, `all-MiniLM-L6-v2`), consistent with the
cost-conscious choices made throughout (see e.g. Step 4's OpenRouter free
tier). `all-MiniLM-L6-v2` was actually loaded and run in this session to
confirm its real output dimension -- 384 -- rather than assuming it; see
CLAUDE.md's dated note for the full reasoning.

`vector` columns are fixed-dimension in pgvector: there is no in-place
"resize," the column has to be dropped and recreated at the new
dimension. This also means the IVFFlat index (built against the specific
1536-dim column) has to be dropped and rebuilt against the new one.

**Data note:** `api_doc_chunks` has no rows yet anywhere this migration has
been applied (Step 10 is the first thing to ever write to it), so there is
no real data to migrate/re-embed here -- this only needed to be a
schema-only change. If this repo ever accumulates real `api_doc_chunks`
rows under the 1536-dim column before this migration runs, they would be
destroyed by the column drop below; there's no re-embedding step in this
migration because there was nothing to re-embed at the time it was written.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0005_api_doc_chunks_embed_dim"
down_revision: Union[str, None] = "0004_migration_runs_pr_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_DIM = 384


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_api_doc_chunks_embedding")
    op.drop_column("api_doc_chunks", "embedding")
    op.add_column(
        "api_doc_chunks",
        sa.Column("embedding", Vector(NEW_DIM), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_api_doc_chunks_embedding ON api_doc_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_api_doc_chunks_embedding")
    op.drop_column("api_doc_chunks", "embedding")
    op.add_column(
        "api_doc_chunks",
        sa.Column("embedding", Vector(1536), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_api_doc_chunks_embedding ON api_doc_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
