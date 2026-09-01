-- KLA-Sync core PostgreSQL schema
--
-- This migration is intentionally PostgreSQL-first and portable to Supabase
-- Postgres. Apply it with a role allowed to create the listed extensions.
-- It stores wallet references only as encrypted ciphertext/HMACs; raw MSISDNs,
-- registry credentials, and telecom secrets do not belong in this database.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TYPE kla_organization_type AS ENUM (
    'cmo', 'label', 'publisher', 'venue_operator', 'broadcaster', 'technology_partner', 'other'
);
CREATE TYPE kla_party_kind AS ENUM ('individual', 'organization');
CREATE TYPE kla_release_type AS ENUM ('single', 'ep', 'album', 'compilation', 'other');
CREATE TYPE kla_right_type AS ENUM ('master', 'composition', 'performance', 'mechanical');
CREATE TYPE kla_split_sheet_status AS ENUM ('draft', 'active', 'superseded', 'retired');
CREATE TYPE kla_source_type AS ENUM ('fm_stream', 'online_stream', 'venue_edge', 'vehicle_edge');
CREATE TYPE kla_location_type AS ENUM ('radio_station', 'nightclub', 'bar', 'restaurant', 'taxi', 'other');
CREATE TYPE kla_capture_policy AS ENUM ('hashes_only', 'encrypted_audio');
CREATE TYPE kla_capture_status AS ENUM ('received', 'processed', 'quarantined', 'expired');
CREATE TYPE kla_detection_status AS ENUM ('candidate', 'verified', 'rejected', 'disputed', 'expired');
CREATE TYPE kla_royalty_run_status AS ENUM ('draft', 'review', 'approved', 'paid', 'cancelled');
CREATE TYPE kla_wallet_provider AS ENUM ('mtn_momo', 'airtel_money');
CREATE TYPE kla_payment_account_status AS ENUM ('pending_verification', 'active', 'suspended', 'closed');
CREATE TYPE kla_payout_status AS ENUM ('queued', 'submitted', 'pending', 'paid', 'failed', 'reversed', 'held');
CREATE TYPE kla_registry_state AS ENUM ('found', 'not_found', 'ambiguous', 'unavailable', 'error');

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name TEXT NOT NULL CHECK (length(trim(legal_name)) > 0),
    trading_name TEXT,
    organization_type kla_organization_type NOT NULL,
    registration_number TEXT,
    country_code CHAR(2) NOT NULL DEFAULT 'UG' CHECK (country_code ~ '^[A-Z]{2}$'),
    contact_email CITEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX organizations_registration_number_uniq
    ON organizations (country_code, registration_number)
    WHERE registration_number IS NOT NULL;

CREATE TABLE rights_parties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    party_kind kla_party_kind NOT NULL,
    organization_id UUID UNIQUE REFERENCES organizations(id) ON DELETE RESTRICT,
    legal_name TEXT NOT NULL CHECK (length(trim(legal_name)) > 0),
    stage_or_trading_name TEXT,
    tax_reference_ciphertext BYTEA,
    tax_reference_hmac TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (party_kind = 'organization' AND organization_id IS NOT NULL)
        OR (party_kind = 'individual' AND organization_id IS NULL)
    )
);
CREATE UNIQUE INDEX rights_parties_tax_reference_hmac_uniq
    ON rights_parties (tax_reference_hmac)
    WHERE tax_reference_hmac IS NOT NULL;

CREATE TABLE catalogs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_organization_id, name)
);

CREATE TABLE artists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    stage_name CITEXT NOT NULL CHECK (length(trim(stage_name::text)) > 0),
    country_code CHAR(2) NOT NULL DEFAULT 'UG' CHECK (country_code ~ '^[A-Z]{2}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (party_id, stage_name)
);

CREATE TABLE music_works (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    alternate_titles TEXT[] NOT NULL DEFAULT '{}',
    iswc VARCHAR(15),
    language_code CHAR(3) NOT NULL DEFAULT 'und' CHECK (language_code ~ '^[a-z]{3}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (iswc IS NULL OR (iswc = upper(iswc) AND iswc ~ '^T-[0-9]{3}[.][0-9]{3}[.][0-9]{3}-[0-9]$'))
);
CREATE UNIQUE INDEX music_works_iswc_uniq ON music_works (iswc) WHERE iswc IS NOT NULL;
CREATE INDEX music_works_catalog_title_idx ON music_works (catalog_id, title);

CREATE TABLE releases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    release_type kla_release_type NOT NULL DEFAULT 'single',
    upc_ean TEXT,
    original_release_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (catalog_id, upc_ean)
);

CREATE TABLE recordings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    isrc VARCHAR(12),
    duration_seconds NUMERIC(10, 3) NOT NULL CHECK (duration_seconds > 0),
    explicit BOOLEAN NOT NULL DEFAULT false,
    audio_sha256 CHAR(64),
    fingerprint_schema_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (isrc IS NULL OR (isrc = upper(isrc) AND isrc ~ '^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$')),
    CHECK (audio_sha256 IS NULL OR audio_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX recordings_isrc_uniq ON recordings (isrc) WHERE isrc IS NOT NULL;
CREATE UNIQUE INDEX recordings_audio_sha256_uniq ON recordings (audio_sha256) WHERE audio_sha256 IS NOT NULL;
CREATE INDEX recordings_catalog_title_idx ON recordings (catalog_id, title);

CREATE TABLE release_recordings (
    release_id UUID NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    recording_id UUID NOT NULL REFERENCES recordings(id) ON DELETE RESTRICT,
    disc_number SMALLINT NOT NULL DEFAULT 1 CHECK (disc_number > 0),
    track_number SMALLINT NOT NULL CHECK (track_number > 0),
    PRIMARY KEY (release_id, recording_id),
    UNIQUE (release_id, disc_number, track_number)
);

CREATE TABLE recording_artists (
    recording_id UUID NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE RESTRICT,
    artist_role TEXT NOT NULL CHECK (artist_role IN ('primary', 'featured', 'remixer', 'producer')),
    display_order SMALLINT NOT NULL DEFAULT 1 CHECK (display_order > 0),
    PRIMARY KEY (recording_id, artist_id, artist_role)
);

CREATE TABLE work_contributors (
    work_id UUID NOT NULL REFERENCES music_works(id) ON DELETE CASCADE,
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    contributor_role TEXT NOT NULL CHECK (contributor_role IN ('composer', 'lyricist', 'arranger', 'publisher', 'administrator')),
    display_order SMALLINT NOT NULL DEFAULT 1 CHECK (display_order > 0),
    PRIMARY KEY (work_id, party_id, contributor_role)
);

CREATE TABLE recording_works (
    recording_id UUID NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    work_id UUID NOT NULL REFERENCES music_works(id) ON DELETE RESTRICT,
    is_primary_work BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (recording_id, work_id)
);
CREATE UNIQUE INDEX recording_primary_work_uniq ON recording_works (recording_id) WHERE is_primary_work;

-- Each sheet controls one asset/right category. A master sheet is tied to a
-- sound recording; composition/performance/mechanical sheets are tied to a work.
CREATE TABLE split_sheets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    recording_id UUID REFERENCES recordings(id) ON DELETE RESTRICT,
    work_id UUID REFERENCES music_works(id) ON DELETE RESTRICT,
    right_type kla_right_type NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status kla_split_sheet_status NOT NULL DEFAULT 'draft',
    valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_to DATE,
    source_document_key TEXT,
    approved_at TIMESTAMPTZ,
    approved_by_party_id UUID REFERENCES rights_parties(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((recording_id IS NOT NULL)::integer + (work_id IS NOT NULL)::integer = 1),
    CHECK (
        (right_type = 'master' AND recording_id IS NOT NULL)
        OR (right_type <> 'master' AND work_id IS NOT NULL)
    ),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK (status <> 'active' OR approved_at IS NOT NULL)
);
CREATE UNIQUE INDEX split_sheet_recording_version_uniq
    ON split_sheets (recording_id, right_type, version) WHERE recording_id IS NOT NULL;
CREATE UNIQUE INDEX split_sheet_work_version_uniq
    ON split_sheets (work_id, right_type, version) WHERE work_id IS NOT NULL;
CREATE INDEX split_sheets_active_lookup_idx
    ON split_sheets (recording_id, work_id, right_type, valid_from, valid_to)
    WHERE status = 'active';

CREATE TABLE split_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    split_sheet_id UUID NOT NULL REFERENCES split_sheets(id) ON DELETE CASCADE,
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (length(trim(role)) > 0),
    share_basis_points INTEGER NOT NULL CHECK (share_basis_points > 0 AND share_basis_points <= 10000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (split_sheet_id, party_id, role)
);
CREATE INDEX split_lines_sheet_idx ON split_lines (split_sheet_id);

-- Defer the 100% check so a draft can be assembled in one transaction, then
-- activated atomically. An active sheet can never commit at 99.99% or 100.01%.
CREATE FUNCTION kla_assert_active_split_total()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_sheet_id UUID;
    active_status kla_split_sheet_status;
    total_basis_points INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'split_lines' THEN
        IF TG_OP = 'DELETE' THEN
            affected_sheet_id := OLD.split_sheet_id;
        ELSE
            affected_sheet_id := NEW.split_sheet_id;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        affected_sheet_id := OLD.id;
    ELSE
        affected_sheet_id := NEW.id;
    END IF;

    SELECT status INTO active_status FROM split_sheets WHERE id = affected_sheet_id;
    IF active_status = 'active' THEN
        SELECT COALESCE(SUM(share_basis_points), 0)
          INTO total_basis_points
          FROM split_lines
         WHERE split_sheet_id = affected_sheet_id;
        IF total_basis_points <> 10000 THEN
            RAISE EXCEPTION
                'active split sheet % must total 10000 basis points, got %',
                affected_sheet_id, total_basis_points
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER split_lines_total_check
AFTER INSERT OR UPDATE OR DELETE ON split_lines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_active_split_total();

CREATE CONSTRAINT TRIGGER split_sheets_total_check
AFTER INSERT OR UPDATE ON split_sheets
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_active_split_total();

-- Registry payloads are kept as hashed/immutable provenance; access to actual
-- URSB data is governed by an approved data-sharing agreement and retention policy.
CREATE TABLE external_registry_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    external_record_id TEXT,
    work_id UUID REFERENCES music_works(id) ON DELETE CASCADE,
    recording_id UUID REFERENCES recordings(id) ON DELETE CASCADE,
    state kla_registry_state NOT NULL,
    source_payload_sha256 CHAR(64),
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((work_id IS NOT NULL)::integer + (recording_id IS NOT NULL)::integer = 1),
    CHECK (source_payload_sha256 IS NULL OR source_payload_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX external_registry_record_uniq
    ON external_registry_records (provider, external_record_id)
    WHERE external_record_id IS NOT NULL;
CREATE INDEX external_registry_work_idx ON external_registry_records (work_id, provider);
CREATE INDEX external_registry_recording_idx ON external_registry_records (recording_id, provider);

CREATE TABLE monitoring_locations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    location_type kla_location_type NOT NULL,
    region TEXT NOT NULL CHECK (length(trim(region)) > 0),
    district TEXT,
    latitude NUMERIC(9, 6),
    longitude NUMERIC(9, 6),
    timezone_name TEXT NOT NULL DEFAULT 'Africa/Kampala',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);
CREATE INDEX monitoring_locations_region_idx ON monitoring_locations (region, location_type);

CREATE TABLE monitoring_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id UUID NOT NULL REFERENCES monitoring_locations(id) ON DELETE RESTRICT,
    source_type kla_source_type NOT NULL,
    source_code TEXT NOT NULL UNIQUE CHECK (length(trim(source_code)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    stream_secret_reference TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX monitoring_sources_location_idx ON monitoring_sources (location_id, source_type) WHERE is_active;

CREATE TABLE edge_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES monitoring_sources(id) ON DELETE RESTRICT,
    hardware_serial TEXT NOT NULL UNIQUE,
    device_public_key TEXT NOT NULL,
    software_version TEXT,
    last_seen_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX edge_nodes_source_idx ON edge_nodes (source_id) WHERE is_active;

CREATE TABLE capture_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_chunk_id UUID NOT NULL UNIQUE,
    source_id UUID NOT NULL REFERENCES monitoring_sources(id) ON DELETE RESTRICT,
    edge_node_id UUID REFERENCES edge_nodes(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    capture_policy kla_capture_policy NOT NULL DEFAULT 'hashes_only',
    encrypted_object_key TEXT,
    fingerprint_schema_id TEXT NOT NULL,
    status kla_capture_status NOT NULL DEFAULT 'received',
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ended_at > started_at),
    CHECK (
        (capture_policy = 'hashes_only' AND encrypted_object_key IS NULL)
        OR (capture_policy = 'encrypted_audio')
    )
);
CREATE INDEX capture_chunks_source_time_idx ON capture_chunks (source_id, started_at, ended_at);
CREATE INDEX capture_chunks_pending_idx ON capture_chunks (received_at) WHERE status = 'received';

CREATE TABLE detection_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key UUID NOT NULL UNIQUE,
    capture_chunk_id UUID NOT NULL REFERENCES capture_chunks(id) ON DELETE RESTRICT,
    source_id UUID NOT NULL REFERENCES monitoring_sources(id) ON DELETE RESTRICT,
    recording_id UUID NOT NULL REFERENCES recordings(id) ON DELETE RESTRICT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    duration_seconds NUMERIC(10, 3) NOT NULL CHECK (duration_seconds > 0),
    matcher_version TEXT NOT NULL,
    fingerprint_schema_id TEXT NOT NULL,
    matched_hash_count INTEGER NOT NULL CHECK (matched_hash_count >= 0),
    match_confidence NUMERIC(6, 5) NOT NULL CHECK (match_confidence >= 0 AND match_confidence <= 1),
    reference_per_query_tempo_scale NUMERIC(7, 5),
    status kla_detection_status NOT NULL DEFAULT 'candidate',
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ended_at > started_at),
    CHECK (reference_per_query_tempo_scale IS NULL OR reference_per_query_tempo_scale BETWEEN 0.80 AND 1.20)
);
CREATE INDEX detection_events_approval_idx
    ON detection_events (status, started_at) WHERE status IN ('candidate', 'verified', 'disputed');
CREATE INDEX detection_events_recording_time_idx ON detection_events (recording_id, started_at);
CREATE INDEX detection_events_source_time_idx ON detection_events (source_id, started_at);

-- A source weight represents measured/reviewed reach or venue class, and is
-- versioned so prior royalty runs remain reproducible.
CREATE TABLE source_weights (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES monitoring_sources(id) ON DELETE RESTRICT,
    weight NUMERIC(12, 6) NOT NULL CHECK (weight >= 0),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    valid_from DATE NOT NULL,
    valid_to DATE,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
ALTER TABLE source_weights ADD CONSTRAINT source_weights_no_overlap
    EXCLUDE USING gist (
        source_id WITH =,
        daterange(valid_from, COALESCE(valid_to + 1, 'infinity'::date), '[)') WITH &&
    );
CREATE INDEX source_weights_lookup_idx ON source_weights (source_id, valid_from, valid_to);

CREATE TABLE tariff_rates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    right_type kla_right_type NOT NULL,
    source_type kla_source_type,
    currency_code CHAR(3) NOT NULL DEFAULT 'UGX' CHECK (currency_code = 'UGX'),
    base_rate_ugx NUMERIC(18, 4) NOT NULL CHECK (base_rate_ugx >= 0),
    valid_from DATE NOT NULL,
    valid_to DATE,
    policy_reference TEXT NOT NULL CHECK (length(trim(policy_reference)) > 0),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX tariff_rates_lookup_idx
    ON tariff_rates (catalog_id, right_type, source_type, valid_from, valid_to);

CREATE TABLE royalty_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID NOT NULL REFERENCES catalogs(id) ON DELETE RESTRICT,
    period_started_at TIMESTAMPTZ NOT NULL,
    period_ended_at TIMESTAMPTZ NOT NULL,
    calculation_version TEXT NOT NULL,
    status kla_royalty_run_status NOT NULL DEFAULT 'draft',
    created_by_actor_id UUID,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (period_ended_at > period_started_at),
    CHECK (status NOT IN ('approved', 'paid') OR approved_at IS NOT NULL),
    UNIQUE (catalog_id, period_started_at, period_ended_at, calculation_version)
);

-- Formula snapshot: gross = base_rate_ugx * source_weight * duration_ratio.
CREATE TABLE royalty_usage_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    royalty_run_id UUID NOT NULL REFERENCES royalty_runs(id) ON DELETE RESTRICT,
    detection_event_id UUID NOT NULL REFERENCES detection_events(id) ON DELETE RESTRICT,
    split_sheet_id UUID NOT NULL REFERENCES split_sheets(id) ON DELETE RESTRICT,
    right_type kla_right_type NOT NULL,
    base_rate_ugx NUMERIC(18, 4) NOT NULL CHECK (base_rate_ugx >= 0),
    source_weight NUMERIC(12, 6) NOT NULL CHECK (source_weight >= 0),
    duration_ratio NUMERIC(14, 8) NOT NULL CHECK (duration_ratio >= 0),
    gross_raw_ugx NUMERIC(20, 8) NOT NULL CHECK (gross_raw_ugx >= 0),
    gross_settled_ugx NUMERIC(20, 0) NOT NULL CHECK (gross_settled_ugx >= 0),
    formula_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (royalty_run_id, detection_event_id, right_type)
);
CREATE INDEX royalty_usage_items_run_idx ON royalty_usage_items (royalty_run_id, right_type);

CREATE TABLE royalty_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    royalty_usage_item_id UUID NOT NULL REFERENCES royalty_usage_items(id) ON DELETE RESTRICT,
    split_line_id UUID NOT NULL REFERENCES split_lines(id) ON DELETE RESTRICT,
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    role_snapshot TEXT NOT NULL,
    share_basis_points INTEGER NOT NULL CHECK (share_basis_points > 0 AND share_basis_points <= 10000),
    raw_amount_ugx NUMERIC(20, 8) NOT NULL CHECK (raw_amount_ugx >= 0),
    settled_amount_ugx NUMERIC(20, 0) NOT NULL CHECK (settled_amount_ugx >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (royalty_usage_item_id, split_line_id)
);
CREATE INDEX royalty_allocations_party_idx ON royalty_allocations (party_id, created_at);

CREATE TABLE payment_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    provider kla_wallet_provider NOT NULL,
    account_reference_ciphertext BYTEA NOT NULL,
    account_reference_hmac CHAR(64) NOT NULL CHECK (account_reference_hmac ~ '^[0-9a-f]{64}$'),
    key_reference TEXT NOT NULL,
    display_last_four CHAR(4),
    status kla_payment_account_status NOT NULL DEFAULT 'pending_verification',
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'active' OR verified_at IS NOT NULL),
    UNIQUE (provider, account_reference_hmac)
);
CREATE INDEX payment_accounts_party_active_idx ON payment_accounts (party_id) WHERE status = 'active';

CREATE TABLE payout_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    royalty_run_id UUID NOT NULL REFERENCES royalty_runs(id) ON DELETE RESTRICT,
    provider kla_wallet_provider NOT NULL,
    currency_code CHAR(3) NOT NULL DEFAULT 'UGX' CHECK (currency_code = 'UGX'),
    status kla_payout_status NOT NULL DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (royalty_run_id, provider)
);

CREATE TABLE payouts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_batch_id UUID NOT NULL REFERENCES payout_batches(id) ON DELETE RESTRICT,
    party_id UUID NOT NULL REFERENCES rights_parties(id) ON DELETE RESTRICT,
    payment_account_id UUID NOT NULL REFERENCES payment_accounts(id) ON DELETE RESTRICT,
    amount_ugx NUMERIC(20, 0) NOT NULL CHECK (amount_ugx > 0),
    idempotency_key UUID NOT NULL UNIQUE,
    status kla_payout_status NOT NULL DEFAULT 'queued',
    provider_reference TEXT,
    failure_code TEXT,
    failure_message TEXT,
    submitted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payouts_batch_status_idx ON payouts (payout_batch_id, status, created_at);
CREATE INDEX payouts_party_idx ON payouts (party_id, created_at DESC);

CREATE TABLE payout_allocation_items (
    payout_id UUID NOT NULL REFERENCES payouts(id) ON DELETE RESTRICT,
    royalty_allocation_id UUID NOT NULL UNIQUE REFERENCES royalty_allocations(id) ON DELETE RESTRICT,
    amount_ugx NUMERIC(20, 0) NOT NULL CHECK (amount_ugx > 0),
    PRIMARY KEY (payout_id, royalty_allocation_id)
);

-- Transactional outbox: create it in the same transaction as payout status so
-- a worker can safely retry provider submission using the idempotency key.
CREATE TABLE payout_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_id UUID NOT NULL UNIQUE REFERENCES payouts(id) ON DELETE RESTRICT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payout_outbox_due_idx ON payout_outbox (next_attempt_at) WHERE delivered_at IS NULL;

-- Draft runs may be built incrementally. Once a run is approved or paid, every
-- usage item must reconcile exactly to its rounded allocation total. This is
-- deferred to permit a complete run to be written atomically in one transaction.
CREATE FUNCTION kla_assert_approved_run_allocation_totals()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_run_id UUID;
    affected_usage_item_id UUID;
    offending_usage_id UUID;
BEGIN
    IF TG_TABLE_NAME = 'royalty_runs' THEN
        IF TG_OP = 'DELETE' THEN
            affected_run_id := OLD.id;
        ELSE
            affected_run_id := NEW.id;
        END IF;
    ELSIF TG_TABLE_NAME = 'royalty_usage_items' THEN
        IF TG_OP = 'DELETE' THEN
            affected_run_id := OLD.royalty_run_id;
        ELSE
            affected_run_id := NEW.royalty_run_id;
        END IF;
    ELSE
        IF TG_OP = 'DELETE' THEN
            affected_usage_item_id := OLD.royalty_usage_item_id;
        ELSE
            affected_usage_item_id := NEW.royalty_usage_item_id;
        END IF;
        SELECT item.royalty_run_id
          INTO affected_run_id
          FROM royalty_usage_items item
         WHERE item.id = affected_usage_item_id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM royalty_runs run
         WHERE run.id = affected_run_id
           AND run.status IN ('approved', 'paid')
    ) THEN
        SELECT item.id
          INTO offending_usage_id
          FROM royalty_usage_items item
          LEFT JOIN royalty_allocations allocation
            ON allocation.royalty_usage_item_id = item.id
         WHERE item.royalty_run_id = affected_run_id
         GROUP BY item.id, item.gross_settled_ugx
        HAVING COALESCE(SUM(allocation.settled_amount_ugx), 0) <> item.gross_settled_ugx
         LIMIT 1;
        IF offending_usage_id IS NOT NULL THEN
            RAISE EXCEPTION
                'approved royalty run % has unreconciled allocation for usage item %',
                affected_run_id, offending_usage_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER royalty_runs_allocation_total_check
AFTER INSERT OR UPDATE ON royalty_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_approved_run_allocation_totals();
CREATE CONSTRAINT TRIGGER royalty_usage_items_allocation_total_check
AFTER INSERT OR UPDATE OR DELETE ON royalty_usage_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_approved_run_allocation_totals();
CREATE CONSTRAINT TRIGGER royalty_allocations_total_check
AFTER INSERT OR UPDATE OR DELETE ON royalty_allocations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_approved_run_allocation_totals();

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_id UUID REFERENCES catalogs(id) ON DELETE SET NULL,
    actor_id UUID,
    action TEXT NOT NULL CHECK (length(trim(action)) > 0),
    entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
    entity_id UUID,
    request_id UUID,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    before_sha256 CHAR(64),
    after_sha256 CHAR(64),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (before_sha256 IS NULL OR before_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (after_sha256 IS NULL OR after_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX audit_events_catalog_time_idx ON audit_events (catalog_id, occurred_at DESC);
CREATE INDEX audit_events_entity_idx ON audit_events (entity_type, entity_id, occurred_at DESC);

CREATE FUNCTION kla_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER organizations_touch_updated_at BEFORE UPDATE ON organizations
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER rights_parties_touch_updated_at BEFORE UPDATE ON rights_parties
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER catalogs_touch_updated_at BEFORE UPDATE ON catalogs
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER artists_touch_updated_at BEFORE UPDATE ON artists
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER music_works_touch_updated_at BEFORE UPDATE ON music_works
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER releases_touch_updated_at BEFORE UPDATE ON releases
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER recordings_touch_updated_at BEFORE UPDATE ON recordings
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER split_sheets_touch_updated_at BEFORE UPDATE ON split_sheets
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER split_lines_touch_updated_at BEFORE UPDATE ON split_lines
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER monitoring_locations_touch_updated_at BEFORE UPDATE ON monitoring_locations
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER monitoring_sources_touch_updated_at BEFORE UPDATE ON monitoring_sources
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER edge_nodes_touch_updated_at BEFORE UPDATE ON edge_nodes
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER detection_events_touch_updated_at BEFORE UPDATE ON detection_events
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER royalty_runs_touch_updated_at BEFORE UPDATE ON royalty_runs
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER payment_accounts_touch_updated_at BEFORE UPDATE ON payment_accounts
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();
CREATE TRIGGER payouts_touch_updated_at BEFORE UPDATE ON payouts
FOR EACH ROW EXECUTE FUNCTION kla_touch_updated_at();

COMMIT;
