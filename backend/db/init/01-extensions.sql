-- Runs once, when the database is first created.
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector: embedding columns + similarity search
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy text matching (supplier names, invoice numbers)
