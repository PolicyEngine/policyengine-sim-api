# PolicyEngine Stage 12 persistence

Temporary SQLAlchemy Core mappings and transactional persistence for the two
Stage 12 comparison tables. `policyengine-api` remains the sole owner of the
SQLModel definitions, Alembic revisions, and live schema. This package performs
DML only and is removed with the temporary comparison path during Stage 14.
