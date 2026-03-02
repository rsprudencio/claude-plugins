"""End-to-end tests against real PostgreSQL + pgvector.

These tests verify SQL contracts (casts, triggers, JSONB operators) that
the InMemoryDB mock cannot catch. They require E2E_POSTGRES_URL to be set
and skip gracefully when it is absent.
"""
