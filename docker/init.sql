-- Jarvis pgvector schema initialization
-- Enables the pgvector extension for the jarvis database.
--
-- Table creation and indexing are handled by schema.py at Python startup,
-- which parameterizes embedding dimensions from config. This file only
-- ensures the extension is available before the MCP server starts.

CREATE EXTENSION IF NOT EXISTS vector;
