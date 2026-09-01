-- KLA-Sync reviewer/dispute dashboard support.
--
-- Disputes challenge a candidate or verified detection. While a dispute is
-- open the affected amount is held by the royalty pipeline (it never reaches a
-- payout batch); resolution moves the detection to rejected (upheld) or
-- verified (dismissed). At most one open dispute may exist per detection.
-- This migration is portable core PostgreSQL. The optional reviewer-role enum
-- extension below applies only where the Supabase membership enum exists.

BEGIN;

CREATE TABLE detection_disputes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detection_event_id UUID NOT NULL REFERENCES detection_events(id) ON DELETE RESTRICT,
    -- Portal user who raised it (Supabase auth.users in the managed deployment;
    -- a server-side identity otherwise). No hard FK to auth schema here so the
    -- core migration stays portable.
    raised_by_user_id UUID NOT NULL,
    -- Optional linked rights party (rights_parties) for rightsholder disputes.
    raised_by_party_id UUID REFERENCES rights_parties(id) ON DELETE SET NULL,
    reason TEXT NOT NULL CHECK (
        reason IN ('wrong_identity', 'wrong_duration', 'wrong_source',
                   'wrong_split', 'duplicate', 'other')
    ),
    detail TEXT NOT NULL CHECK (length(trim(detail)) >= 10 AND length(detail) <= 4000),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'upheld', 'dismissed')),
    resolved_by_user_id UUID,
    resolution_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    CHECK (
        (status = 'open' AND resolved_at IS NULL AND resolved_by_user_id IS NULL)
        OR (status IN ('upheld', 'dismissed') AND resolved_at IS NOT NULL
            AND resolved_by_user_id IS NOT NULL
            AND resolution_note IS NOT NULL AND length(trim(resolution_note)) >= 5)
    )
);

-- At most one open dispute per detection; resolved ones are retained for audit.
CREATE UNIQUE INDEX detection_disputes_one_open
    ON detection_disputes (detection_event_id)
    WHERE status = 'open';

CREATE INDEX detection_disputes_status_idx
    ON detection_disputes (status, created_at DESC);

CREATE INDEX detection_disputes_detection_idx
    ON detection_disputes (detection_event_id, created_at DESC);

-- On the Supabase-managed deployment, add the dedicated detection reviewer
-- role to the membership enum created by 002_supabase_rls.sql. Guarded so this
-- core migration is a no-op for the enum on plain PostgreSQL (where the type
-- does not exist).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'kla_catalog_member_role'
    ) AND NOT EXISTS (
        SELECT 1
          FROM pg_enum e
          JOIN pg_type t ON t.oid = e.enumtypid
         WHERE t.typname = 'kla_catalog_member_role'
           AND e.enumlabel = 'reviewer'
    ) THEN
        ALTER TYPE kla_catalog_member_role ADD VALUE 'reviewer';
    END IF;
END $$;

COMMIT;
