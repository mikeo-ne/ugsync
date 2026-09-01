-- KLA-Sync catalog onboarding API support.
--
-- Stores the response for each idempotent onboarding request so a retried POST
-- with the same Idempotency-Key returns the original result instead of creating
-- a duplicate catalog. The onboarding API authenticates with a server-side
-- bearer token; this table is never exposed to browser clients.

BEGIN;

CREATE TABLE onboarding_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key UUID NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed')),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX onboarding_requests_created_idx ON onboarding_requests (created_at DESC);

COMMIT;
