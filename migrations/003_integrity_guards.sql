-- KLA-Sync cross-table integrity guards.
-- Apply after 001_core_schema.sql. These trigger checks protect relationships
-- that PostgreSQL CHECK constraints cannot express across tables.

BEGIN;

CREATE FUNCTION kla_assert_release_recording_catalog()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    release_catalog_id UUID;
    recording_catalog_id UUID;
BEGIN
    SELECT catalog_id INTO release_catalog_id FROM releases WHERE id = NEW.release_id;
    SELECT catalog_id INTO recording_catalog_id FROM recordings WHERE id = NEW.recording_id;
    IF release_catalog_id IS DISTINCT FROM recording_catalog_id THEN
        RAISE EXCEPTION 'release % and recording % must belong to the same catalog', NEW.release_id, NEW.recording_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER release_recordings_catalog_check
BEFORE INSERT OR UPDATE OF release_id, recording_id ON release_recordings
FOR EACH ROW EXECUTE FUNCTION kla_assert_release_recording_catalog();

CREATE FUNCTION kla_assert_split_sheet_asset_catalog()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    asset_catalog_id UUID;
BEGIN
    IF NEW.recording_id IS NOT NULL THEN
        SELECT catalog_id INTO asset_catalog_id FROM recordings WHERE id = NEW.recording_id;
    ELSIF NEW.work_id IS NOT NULL THEN
        SELECT catalog_id INTO asset_catalog_id FROM music_works WHERE id = NEW.work_id;
    ELSE
        RETURN NEW; -- The table-level check reports the missing asset clearly.
    END IF;
    IF asset_catalog_id IS DISTINCT FROM NEW.catalog_id THEN
        RAISE EXCEPTION 'split sheet asset must belong to sheet catalog %', NEW.catalog_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER split_sheets_asset_catalog_check
BEFORE INSERT OR UPDATE OF catalog_id, recording_id, work_id ON split_sheets
FOR EACH ROW EXECUTE FUNCTION kla_assert_split_sheet_asset_catalog();

CREATE FUNCTION kla_assert_recording_work_catalog()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    recording_catalog_id UUID;
    work_catalog_id UUID;
BEGIN
    SELECT catalog_id INTO recording_catalog_id FROM recordings WHERE id = NEW.recording_id;
    SELECT catalog_id INTO work_catalog_id FROM music_works WHERE id = NEW.work_id;
    IF recording_catalog_id IS DISTINCT FROM work_catalog_id THEN
        RAISE EXCEPTION 'recording % and work % must belong to the same catalog', NEW.recording_id, NEW.work_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER recording_works_catalog_check
BEFORE INSERT OR UPDATE OF recording_id, work_id ON recording_works
FOR EACH ROW EXECUTE FUNCTION kla_assert_recording_work_catalog();

CREATE FUNCTION kla_assert_capture_source_link()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    node_source_id UUID;
BEGIN
    IF NEW.edge_node_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT source_id INTO node_source_id FROM edge_nodes WHERE id = NEW.edge_node_id;
    IF node_source_id IS DISTINCT FROM NEW.source_id THEN
        RAISE EXCEPTION 'capture source % does not match edge node source %', NEW.source_id, node_source_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER capture_chunks_source_check
BEFORE INSERT OR UPDATE OF edge_node_id, source_id ON capture_chunks
FOR EACH ROW EXECUTE FUNCTION kla_assert_capture_source_link();

CREATE FUNCTION kla_assert_detection_capture_link()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    capture_source_id UUID;
    capture_started_at TIMESTAMPTZ;
    capture_ended_at TIMESTAMPTZ;
BEGIN
    SELECT source_id, started_at, ended_at
      INTO capture_source_id, capture_started_at, capture_ended_at
      FROM capture_chunks
     WHERE id = NEW.capture_chunk_id;
    IF capture_source_id IS DISTINCT FROM NEW.source_id THEN
        RAISE EXCEPTION 'detection source must equal its capture source'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.started_at < capture_started_at OR NEW.ended_at > capture_ended_at THEN
        RAISE EXCEPTION 'detection time range must remain within its capture chunk'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER detection_events_capture_check
BEFORE INSERT OR UPDATE OF capture_chunk_id, source_id, started_at, ended_at ON detection_events
FOR EACH ROW EXECUTE FUNCTION kla_assert_detection_capture_link();

CREATE FUNCTION kla_assert_royalty_usage_links()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    run_catalog_id UUID;
    detection_recording_id UUID;
    recording_catalog_id UUID;
    detection_status_value kla_detection_status;
    sheet_catalog_id UUID;
    sheet_right_type kla_right_type;
    sheet_recording_id UUID;
    sheet_work_id UUID;
BEGIN
    SELECT catalog_id INTO run_catalog_id FROM royalty_runs WHERE id = NEW.royalty_run_id;
    SELECT detection.recording_id, recording.catalog_id, detection.status
      INTO detection_recording_id, recording_catalog_id, detection_status_value
      FROM detection_events detection
      JOIN recordings recording ON recording.id = detection.recording_id
     WHERE detection.id = NEW.detection_event_id;
    SELECT catalog_id, right_type, recording_id, work_id
      INTO sheet_catalog_id, sheet_right_type, sheet_recording_id, sheet_work_id
      FROM split_sheets
     WHERE id = NEW.split_sheet_id;

    IF detection_status_value IS DISTINCT FROM 'verified' THEN
        RAISE EXCEPTION 'only verified detection events may enter royalty calculations'
            USING ERRCODE = 'check_violation';
    END IF;
    IF run_catalog_id IS DISTINCT FROM recording_catalog_id
       OR run_catalog_id IS DISTINCT FROM sheet_catalog_id THEN
        RAISE EXCEPTION 'royalty run, detection recording, and split sheet must share one catalog'
            USING ERRCODE = 'check_violation';
    END IF;
    IF sheet_right_type IS DISTINCT FROM NEW.right_type THEN
        RAISE EXCEPTION 'royalty usage right type must match the split sheet right type'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.right_type = 'master' AND sheet_recording_id IS DISTINCT FROM detection_recording_id THEN
        RAISE EXCEPTION 'master royalty split sheet must belong to the matched recording'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.right_type <> 'master' AND NOT EXISTS (
        SELECT 1
          FROM recording_works link
         WHERE link.recording_id = detection_recording_id
           AND link.work_id = sheet_work_id
    ) THEN
        RAISE EXCEPTION 'non-master royalty split sheet work must be linked to the matched recording'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER royalty_usage_items_link_check
BEFORE INSERT OR UPDATE OF royalty_run_id, detection_event_id, split_sheet_id, right_type ON royalty_usage_items
FOR EACH ROW EXECUTE FUNCTION kla_assert_royalty_usage_links();

CREATE FUNCTION kla_assert_royalty_allocation_links()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    expected_sheet_id UUID;
    line_sheet_id UUID;
    line_party_id UUID;
    line_role TEXT;
    line_share INTEGER;
BEGIN
    SELECT split_sheet_id INTO expected_sheet_id
      FROM royalty_usage_items
     WHERE id = NEW.royalty_usage_item_id;
    SELECT split_sheet_id, party_id, role, share_basis_points
      INTO line_sheet_id, line_party_id, line_role, line_share
      FROM split_lines
     WHERE id = NEW.split_line_id;
    IF line_sheet_id IS DISTINCT FROM expected_sheet_id
       OR line_party_id IS DISTINCT FROM NEW.party_id
       OR line_role IS DISTINCT FROM NEW.role_snapshot
       OR line_share IS DISTINCT FROM NEW.share_basis_points THEN
        RAISE EXCEPTION 'royalty allocation must exactly snapshot a split line from its usage sheet'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER royalty_allocations_link_check
BEFORE INSERT OR UPDATE OF royalty_usage_item_id, split_line_id, party_id, role_snapshot, share_basis_points
ON royalty_allocations
FOR EACH ROW EXECUTE FUNCTION kla_assert_royalty_allocation_links();

CREATE FUNCTION kla_assert_payout_account_link()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    account_party_id UUID;
    account_provider kla_wallet_provider;
    account_status kla_payment_account_status;
    batch_provider kla_wallet_provider;
BEGIN
    SELECT party_id, provider, status
      INTO account_party_id, account_provider, account_status
      FROM payment_accounts
     WHERE id = NEW.payment_account_id;
    SELECT provider INTO batch_provider FROM payout_batches WHERE id = NEW.payout_batch_id;
    IF account_party_id IS DISTINCT FROM NEW.party_id
       OR account_provider IS DISTINCT FROM batch_provider
       OR account_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'payout requires active recipient account for the batch provider'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payouts_account_check
BEFORE INSERT OR UPDATE OF payout_batch_id, party_id, payment_account_id ON payouts
FOR EACH ROW EXECUTE FUNCTION kla_assert_payout_account_link();

CREATE FUNCTION kla_assert_payout_allocation_link()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    payout_party_id UUID;
    allocation_party_id UUID;
    allocation_amount NUMERIC(20, 0);
BEGIN
    SELECT party_id INTO payout_party_id FROM payouts WHERE id = NEW.payout_id;
    SELECT party_id, settled_amount_ugx
      INTO allocation_party_id, allocation_amount
      FROM royalty_allocations
     WHERE id = NEW.royalty_allocation_id;
    IF payout_party_id IS DISTINCT FROM allocation_party_id
       OR NEW.amount_ugx IS DISTINCT FROM allocation_amount THEN
        RAISE EXCEPTION 'payout allocation must pay its recipient and full settled allocation amount'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payout_allocation_items_link_check
BEFORE INSERT OR UPDATE OF payout_id, royalty_allocation_id, amount_ugx ON payout_allocation_items
FOR EACH ROW EXECUTE FUNCTION kla_assert_payout_allocation_link();

-- A queued payout can be assembled over several rows. Before external dispatch
-- (submitted/pending/paid), its amount must reconcile exactly to its allocations.
CREATE FUNCTION kla_assert_dispatched_payout_total()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_payout_id UUID;
    payout_amount NUMERIC(20, 0);
    payout_status_value kla_payout_status;
    allocation_total NUMERIC(20, 0);
BEGIN
    IF TG_TABLE_NAME = 'payouts' THEN
        IF TG_OP = 'DELETE' THEN
            affected_payout_id := OLD.id;
        ELSE
            affected_payout_id := NEW.id;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        affected_payout_id := OLD.payout_id;
    ELSE
        affected_payout_id := NEW.payout_id;
    END IF;
    SELECT amount_ugx, status
      INTO payout_amount, payout_status_value
      FROM payouts
     WHERE id = affected_payout_id;
    IF payout_status_value IN ('submitted', 'pending', 'paid') THEN
        SELECT COALESCE(SUM(amount_ugx), 0)
          INTO allocation_total
          FROM payout_allocation_items
         WHERE payout_id = affected_payout_id;
        IF allocation_total <> payout_amount THEN
            RAISE EXCEPTION 'dispatched payout % amount does not reconcile to allocations', affected_payout_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER payouts_total_check
AFTER INSERT OR UPDATE OR DELETE ON payouts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_dispatched_payout_total();
CREATE CONSTRAINT TRIGGER payout_allocation_items_total_check
AFTER INSERT OR UPDATE OR DELETE ON payout_allocation_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION kla_assert_dispatched_payout_total();

COMMIT;
