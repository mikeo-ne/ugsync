-- KLA-Sync Supabase-specific membership and row-level security baseline.
--
-- @requires supabase
--
-- Apply only after 001_core_schema.sql in a Supabase project, where auth.users
-- and auth.uid() exist. Server-side workers should use a tightly controlled
-- service-role connection; browser clients must never receive that key. The
-- migration runner skips this file on plain PostgreSQL; enable it with
-- `kla-sync migrate --require supabase` when targeting Supabase.

BEGIN;

CREATE TYPE kla_catalog_member_role AS ENUM ('viewer', 'catalog_editor', 'finance_reviewer', 'catalog_admin');

CREATE TABLE catalog_members (
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    member_role kla_catalog_member_role NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_id, user_id)
);
CREATE INDEX catalog_members_user_idx ON catalog_members (user_id, catalog_id);

CREATE FUNCTION can_access_catalog(target_catalog_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM catalog_members membership
        WHERE membership.catalog_id = target_catalog_id
          AND membership.user_id = auth.uid()
    );
$$;

CREATE FUNCTION can_manage_catalog(target_catalog_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM catalog_members membership
        WHERE membership.catalog_id = target_catalog_id
          AND membership.user_id = auth.uid()
          AND membership.member_role IN ('catalog_editor', 'catalog_admin')
    );
$$;

CREATE FUNCTION is_catalog_admin(target_catalog_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM catalog_members membership
        WHERE membership.catalog_id = target_catalog_id
          AND membership.user_id = auth.uid()
          AND membership.member_role = 'catalog_admin'
    );
$$;

ALTER TABLE catalogs ENABLE ROW LEVEL SECURITY;
ALTER TABLE music_works ENABLE ROW LEVEL SECURITY;
ALTER TABLE releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE release_recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE recording_artists ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_contributors ENABLE ROW LEVEL SECURITY;
ALTER TABLE recording_works ENABLE ROW LEVEL SECURITY;
ALTER TABLE split_sheets ENABLE ROW LEVEL SECURITY;
ALTER TABLE split_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE tariff_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE royalty_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE royalty_usage_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE royalty_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_members ENABLE ROW LEVEL SECURITY;

CREATE POLICY catalogs_select_member ON catalogs
    FOR SELECT USING (can_access_catalog(id));
CREATE POLICY catalogs_update_admin ON catalogs
    FOR UPDATE USING (is_catalog_admin(id)) WITH CHECK (is_catalog_admin(id));

CREATE POLICY catalog_members_select_member ON catalog_members
    FOR SELECT USING (can_access_catalog(catalog_id));
CREATE POLICY catalog_members_manage_admin ON catalog_members
    FOR ALL USING (is_catalog_admin(catalog_id)) WITH CHECK (is_catalog_admin(catalog_id));

CREATE POLICY music_works_member_access ON music_works
    FOR ALL USING (can_access_catalog(catalog_id)) WITH CHECK (can_manage_catalog(catalog_id));
CREATE POLICY releases_member_access ON releases
    FOR ALL USING (can_access_catalog(catalog_id)) WITH CHECK (can_manage_catalog(catalog_id));
CREATE POLICY recordings_member_access ON recordings
    FOR ALL USING (can_access_catalog(catalog_id)) WITH CHECK (can_manage_catalog(catalog_id));

CREATE POLICY release_recordings_member_access ON release_recordings
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM releases release
            WHERE release.id = release_recordings.release_id
              AND can_access_catalog(release.catalog_id)
        )
        AND EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = release_recordings.recording_id
              AND can_access_catalog(recording.catalog_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM releases release
            WHERE release.id = release_recordings.release_id
              AND can_manage_catalog(release.catalog_id)
        )
        AND EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = release_recordings.recording_id
              AND can_manage_catalog(recording.catalog_id)
        )
    );

CREATE POLICY recording_artists_member_access ON recording_artists
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = recording_artists.recording_id
              AND can_access_catalog(recording.catalog_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = recording_artists.recording_id
              AND can_manage_catalog(recording.catalog_id)
        )
    );

CREATE POLICY work_contributors_member_access ON work_contributors
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM music_works work
            WHERE work.id = work_contributors.work_id
              AND can_access_catalog(work.catalog_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM music_works work
            WHERE work.id = work_contributors.work_id
              AND can_manage_catalog(work.catalog_id)
        )
    );

CREATE POLICY recording_works_member_access ON recording_works
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = recording_works.recording_id
              AND can_access_catalog(recording.catalog_id)
        )
        AND EXISTS (
            SELECT 1 FROM music_works work
            WHERE work.id = recording_works.work_id
              AND can_access_catalog(work.catalog_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM recordings recording
            WHERE recording.id = recording_works.recording_id
              AND can_manage_catalog(recording.catalog_id)
        )
        AND EXISTS (
            SELECT 1 FROM music_works work
            WHERE work.id = recording_works.work_id
              AND can_manage_catalog(work.catalog_id)
        )
    );

-- Catalog editors can prepare drafts. Only a catalog administrator can make
-- an active/superseded rights-state transition through the browser role.
CREATE POLICY split_sheets_select_member ON split_sheets
    FOR SELECT USING (can_access_catalog(catalog_id));
CREATE POLICY split_sheets_insert_draft_editor ON split_sheets
    FOR INSERT WITH CHECK (can_manage_catalog(catalog_id) AND status = 'draft');
CREATE POLICY split_sheets_update_draft_editor ON split_sheets
    FOR UPDATE
    USING (can_manage_catalog(catalog_id) AND status = 'draft')
    WITH CHECK (can_manage_catalog(catalog_id) AND status = 'draft');
CREATE POLICY split_sheets_delete_draft_editor ON split_sheets
    FOR DELETE USING (can_manage_catalog(catalog_id) AND status = 'draft');
CREATE POLICY split_sheets_admin_manage ON split_sheets
    FOR ALL USING (is_catalog_admin(catalog_id)) WITH CHECK (is_catalog_admin(catalog_id));

CREATE POLICY split_lines_select_member ON split_lines
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM split_sheets sheet
            WHERE sheet.id = split_lines.split_sheet_id
              AND can_access_catalog(sheet.catalog_id)
        )
    );
CREATE POLICY split_lines_insert_draft_editor ON split_lines
    FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM split_sheets sheet
            WHERE sheet.id = split_lines.split_sheet_id
              AND sheet.status = 'draft'
              AND can_manage_catalog(sheet.catalog_id)
        )
    );
CREATE POLICY split_lines_update_draft_editor ON split_lines
    FOR UPDATE
    USING (
        EXISTS (
            SELECT 1 FROM split_sheets sheet
            WHERE sheet.id = split_lines.split_sheet_id
              AND sheet.status = 'draft'
              AND can_manage_catalog(sheet.catalog_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM split_sheets sheet
            WHERE sheet.id = split_lines.split_sheet_id
              AND sheet.status = 'draft'
              AND can_manage_catalog(sheet.catalog_id)
        )
    );
CREATE POLICY split_lines_delete_draft_editor ON split_lines
    FOR DELETE
    USING (
        EXISTS (
            SELECT 1 FROM split_sheets sheet
            WHERE sheet.id = split_lines.split_sheet_id
              AND sheet.status = 'draft'
              AND can_manage_catalog(sheet.catalog_id)
        )
    );

-- Financial values are readable in catalog scope but only server-side finance
-- workflows may create/change them. The service role is not exposed to clients.
CREATE POLICY tariff_rates_select_member ON tariff_rates
    FOR SELECT USING (can_access_catalog(catalog_id));
CREATE POLICY royalty_runs_select_member ON royalty_runs
    FOR SELECT USING (can_access_catalog(catalog_id));
CREATE POLICY audit_events_member_access ON audit_events
    FOR SELECT USING (catalog_id IS NOT NULL AND can_access_catalog(catalog_id));

CREATE POLICY royalty_usage_items_member_access ON royalty_usage_items
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM royalty_runs run
            WHERE run.id = royalty_usage_items.royalty_run_id
              AND can_access_catalog(run.catalog_id)
        )
    );
CREATE POLICY royalty_allocations_member_access ON royalty_allocations
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM royalty_usage_items item
            JOIN royalty_runs run ON run.id = item.royalty_run_id
            WHERE item.id = royalty_allocations.royalty_usage_item_id
              AND can_access_catalog(run.catalog_id)
        )
    );

-- Sensitive operational tables (capture chunks, detections, payment accounts,
-- payouts, and raw party PII) intentionally have no browser policies. Enable
-- RLS now so accidental future browser grants fail closed; expose reviewed,
-- redacted portal views/RPCs only after a threat-model review.
ALTER TABLE rights_parties ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE artists ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE capture_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE detection_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_weights ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_registry_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE payouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_allocation_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_outbox ENABLE ROW LEVEL SECURITY;

COMMIT;
