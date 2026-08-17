-- Fresh-install schema for synthesis log ingestion v2.
SET client_encoding = 'UTF8';
CREATE SCHEMA IF NOT EXISTS synth_log;
SET search_path TO synth_log, public;

DO $$
BEGIN
    EXECUTE format(
        'COMMENT ON DATABASE %I IS %L',
        current_database(),
        'Oligonucleotide synthesis execution logs with immutable source files and versioned parsing.'
    );
END
$$;

CREATE TYPE synth_log.run_status AS ENUM ('RUNNING', 'COMPLETED', 'FAILED', 'UNKNOWN');
CREATE TYPE synth_log.ingest_status AS ENUM ('PENDING', 'SUCCEEDED', 'FAILED');
CREATE TYPE synth_log.file_role AS ENUM ('SYNTHESIS_LOG', 'HPLC', 'MASS_SPECTROMETRY');
CREATE TYPE synth_log.file_blob_status AS ENUM ('PRESENT', 'MISSING');
CREATE TYPE synth_log.issue_severity AS ENUM ('INFO', 'WARNING', 'ERROR');
CREATE TYPE synth_log.step_type AS ENUM (
    'DEBLOCK', 'COUPLING', 'CAPPING', 'OXIDIZE', 'WASH',
    'SULFURIZE', 'ALT_WASH', 'OTHER'
);
CREATE TYPE synth_log.aux_phase AS ENUM ('INITIALIZATION', 'FINALIZATION');

CREATE TABLE synth_log.schema_migration (
    version         TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    applied_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE synth_log.source_file (
    file_id             BIGSERIAL PRIMARY KEY,
    file_role           synth_log.file_role NOT NULL,
    original_file_name  TEXT NOT NULL,
    content_type        TEXT NOT NULL DEFAULT 'text/plain',
    byte_size           BIGINT CHECK (byte_size IS NULL OR byte_size >= 0),
    sha256              CHAR(64),
    fast_hash           CHAR(16),
    detected_encoding   TEXT,
    blob_status         synth_log.file_blob_status NOT NULL DEFAULT 'PRESENT',
    uploaded_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT source_file_present_ck CHECK (
        blob_status <> 'PRESENT' OR (sha256 IS NOT NULL AND byte_size IS NOT NULL)
    )
);

CREATE UNIQUE INDEX source_file_sha256_uk
    ON synth_log.source_file (file_role, sha256)
    WHERE sha256 IS NOT NULL;
CREATE INDEX source_file_fast_hash_idx
    ON synth_log.source_file (fast_hash)
    WHERE fast_hash IS NOT NULL;

CREATE TABLE synth_log.source_file_blob (
    file_id         BIGINT PRIMARY KEY REFERENCES synth_log.source_file(file_id) ON DELETE CASCADE,
    content         BYTEA NOT NULL,
    CONSTRAINT source_file_blob_nonempty_ck CHECK (octet_length(content) > 0)
);

CREATE TABLE synth_log.synthesis_run (
    run_id                  BIGSERIAL PRIMARY KEY,
    run_code                TEXT NOT NULL,
    channel_no              INTEGER NOT NULL CHECK (channel_no > 0),
    attempt_no              INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    device                  TEXT NOT NULL DEFAULT 'MM12',
    current_version_id      BIGINT,
    started_at              TIMESTAMP WITHOUT TIME ZONE,
    completed_at            TIMESTAMP WITHOUT TIME ZONE,
    status                  synth_log.run_status NOT NULL DEFAULT 'UNKNOWN',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT synthesis_run_natural_uk UNIQUE (run_code, channel_no, attempt_no)
);

CREATE TABLE synth_log.run_log_version (
    version_id              BIGSERIAL PRIMARY KEY,
    run_id                  BIGINT REFERENCES synth_log.synthesis_run(run_id) ON DELETE CASCADE,
    version_no              INTEGER CHECK (version_no IS NULL OR version_no > 0),
    source_file_id          BIGINT NOT NULL REFERENCES synth_log.source_file(file_id),
    uploaded_file_name      TEXT NOT NULL,
    parser_version          TEXT NOT NULL,
    ingest_status           synth_log.ingest_status NOT NULL DEFAULT 'PENDING',
    error_summary           TEXT,
    recipe_file_name        TEXT,
    observed_sequence_3to5  TEXT,
    started_at              TIMESTAMP WITHOUT TIME ZONE,
    completed_at            TIMESTAMP WITHOUT TIME ZONE,
    run_status              synth_log.run_status NOT NULL DEFAULT 'UNKNOWN',
    cycle_count             INTEGER NOT NULL DEFAULT 0 CHECK (cycle_count >= 0),
    event_count             INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    operation_count         INTEGER NOT NULL DEFAULT 0 CHECK (operation_count >= 0),
    aux_event_count         INTEGER NOT NULL DEFAULT 0 CHECK (aux_event_count >= 0),
    issue_count             INTEGER NOT NULL DEFAULT 0 CHECK (issue_count >= 0),
    imported_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT run_log_version_number_ck CHECK (
        (run_id IS NULL AND version_no IS NULL) OR
        (run_id IS NOT NULL AND version_no IS NOT NULL)
    ),
    CONSTRAINT run_log_version_run_no_uk UNIQUE (run_id, version_no),
    CONSTRAINT run_log_version_parse_uk UNIQUE (run_id, source_file_id, parser_version)
);

ALTER TABLE synth_log.synthesis_run
    ADD CONSTRAINT synthesis_run_current_version_fk
    FOREIGN KEY (current_version_id)
    REFERENCES synth_log.run_log_version(version_id)
    ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;

DROP TRIGGER IF EXISTS synthesis_run_current_version_guard ON synth_log.synthesis_run;
CREATE OR REPLACE FUNCTION synth_log.validate_current_log_version()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.current_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM synth_log.run_log_version v
        WHERE v.version_id = NEW.current_version_id
          AND v.run_id = NEW.run_id
          AND v.ingest_status = 'SUCCEEDED'
    ) THEN
        RAISE EXCEPTION
            'current_version_id % must be a SUCCEEDED version of run_id %',
            NEW.current_version_id, NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER synthesis_run_current_version_guard
BEFORE INSERT OR UPDATE OF current_version_id ON synth_log.synthesis_run
FOR EACH ROW EXECUTE FUNCTION synth_log.validate_current_log_version();

CREATE INDEX synthesis_run_started_idx
    ON synth_log.synthesis_run (started_at DESC, run_id DESC);
CREATE INDEX synthesis_run_current_version_idx
    ON synth_log.synthesis_run (current_version_id)
    WHERE current_version_id IS NOT NULL;
CREATE INDEX run_log_version_run_imported_idx
    ON synth_log.run_log_version (run_id, imported_at DESC, version_id DESC);
CREATE INDEX run_log_version_source_idx
    ON synth_log.run_log_version (source_file_id);
CREATE INDEX run_log_version_status_idx
    ON synth_log.run_log_version (ingest_status, imported_at DESC);

CREATE TABLE synth_log.drain_profile (
    profile_id          BIGSERIAL PRIMARY KEY,
    pulse_sequence      JSONB NOT NULL,
    profile_hash        CHAR(32) GENERATED ALWAYS AS (md5(pulse_sequence::text)) STORED,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT drain_profile_seq_uk UNIQUE (pulse_sequence),
    CONSTRAINT drain_profile_seq_ck CHECK (jsonb_typeof(pulse_sequence) = 'array')
);

CREATE INDEX drain_profile_hash_idx ON synth_log.drain_profile (profile_hash);

CREATE TABLE synth_log.drain_profile_segment (
    segment_id      BIGSERIAL PRIMARY KEY,
    profile_id      BIGINT NOT NULL REFERENCES synth_log.drain_profile(profile_id) ON DELETE CASCADE,
    segment_no      INTEGER NOT NULL CHECK (segment_no > 0),
    drain_value     NUMERIC(12, 3) NOT NULL CHECK (drain_value >= 0),
    wait_ms         INTEGER NOT NULL CHECK (wait_ms >= 0),
    CONSTRAINT drain_profile_segment_uk UNIQUE (profile_id, segment_no)
);

CREATE OR REPLACE FUNCTION synth_log.upsert_drain_profile(p_sequence JSONB)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_id BIGINT;
BEGIN
    IF jsonb_typeof(p_sequence) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'pulse_sequence must be a JSON array of [drain, wait] pairs';
    END IF;

    INSERT INTO synth_log.drain_profile (pulse_sequence)
    VALUES (p_sequence)
    ON CONFLICT (pulse_sequence) DO UPDATE
        SET last_seen_at = CURRENT_TIMESTAMP
    RETURNING profile_id INTO v_id;

    INSERT INTO synth_log.drain_profile_segment (
        profile_id, segment_no, drain_value, wait_ms
    )
    SELECT
        v_id,
        ord::integer,
        (seg->>0)::numeric,
        (seg->>1)::integer
    FROM jsonb_array_elements(p_sequence) WITH ORDINALITY AS t(seg, ord)
    ON CONFLICT (profile_id, segment_no) DO NOTHING;

    RETURN v_id;
END;
$$;

CREATE TABLE synth_log.run_aux_event (
    aux_event_id            BIGSERIAL PRIMARY KEY,
    version_id              BIGINT NOT NULL REFERENCES synth_log.run_log_version(version_id) ON DELETE CASCADE,
    phase                   synth_log.aux_phase NOT NULL,
    event_no                INTEGER NOT NULL CHECK (event_no > 0),
    reagent_code            TEXT NOT NULL,
    injection_volumes_ul    NUMERIC(12, 3)[] NOT NULL,
    drain_profile_id        BIGINT NOT NULL REFERENCES synth_log.drain_profile(profile_id),
    pulse_code              TEXT NOT NULL,
    pulse_sequence          JSONB NOT NULL,
    temperature_c           NUMERIC(6, 2),
    humidity_percent        NUMERIC(6, 2),
    event_time              TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    source_line_no          INTEGER,
    raw_line_text           TEXT,
    CONSTRAINT run_aux_event_uk UNIQUE (version_id, phase, event_no),
    CONSTRAINT run_aux_event_volumes_ck CHECK (cardinality(injection_volumes_ul) > 0),
    CONSTRAINT run_aux_event_seq_ck CHECK (jsonb_typeof(pulse_sequence) = 'array')
);

CREATE TABLE synth_log.synthesis_cycle (
    cycle_id           BIGSERIAL PRIMARY KEY,
    version_id         BIGINT NOT NULL REFERENCES synth_log.run_log_version(version_id) ON DELETE CASCADE,
    cycle_no           INTEGER NOT NULL CHECK (cycle_no > 0),
    monomer_code       TEXT NOT NULL,
    inner_step_count   INTEGER NOT NULL DEFAULT 0 CHECK (inner_step_count >= 0),
    event_count        INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    ami_event_count    INTEGER NOT NULL DEFAULT 0 CHECK (ami_event_count >= 0),
    operation_count    INTEGER NOT NULL DEFAULT 0 CHECK (operation_count >= 0),
    started_at         TIMESTAMP WITHOUT TIME ZONE,
    completed_at       TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT synthesis_cycle_version_no_uk UNIQUE (version_id, cycle_no)
);

CREATE INDEX synthesis_cycle_version_order_idx
    ON synth_log.synthesis_cycle (version_id, cycle_no);

CREATE TABLE synth_log.synthesis_inner_step (
    inner_step_id       BIGSERIAL PRIMARY KEY,
    cycle_id            BIGINT NOT NULL REFERENCES synth_log.synthesis_cycle(cycle_id) ON DELETE CASCADE,
    step_order          INTEGER NOT NULL CHECK (step_order > 0),
    reagent_code        TEXT NOT NULL,
    step_occurrence     INTEGER NOT NULL CHECK (step_occurrence > 0),
    event_count         INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    operation_count     INTEGER NOT NULL DEFAULT 0 CHECK (operation_count >= 0),
    pulse_code          TEXT NOT NULL,
    pulse_sequence      JSONB NOT NULL,
    CONSTRAINT synthesis_inner_step_uk UNIQUE (cycle_id, step_order),
    CONSTRAINT synthesis_inner_step_seq_ck CHECK (jsonb_typeof(pulse_sequence) = 'array')
);

CREATE INDEX synthesis_inner_step_reagent_idx
    ON synth_log.synthesis_inner_step (cycle_id, reagent_code, step_occurrence);

CREATE TABLE synth_log.synthesis_event (
    event_id                BIGSERIAL PRIMARY KEY,
    inner_step_id           BIGINT NOT NULL REFERENCES synth_log.synthesis_inner_step(inner_step_id) ON DELETE CASCADE,
    drain_profile_id        BIGINT NOT NULL REFERENCES synth_log.drain_profile(profile_id),
    event_no                INTEGER NOT NULL CHECK (event_no > 0),
    inner_event_no          INTEGER NOT NULL CHECK (inner_event_no > 0),
    global_event_no         INTEGER NOT NULL CHECK (global_event_no > 0),
    injection_volumes_ul    NUMERIC(12, 3)[] NOT NULL,
    operation_count         INTEGER NOT NULL DEFAULT 0 CHECK (operation_count >= 0),
    temperature_c           NUMERIC(6, 2),
    humidity_percent        NUMERIC(6, 2),
    event_time              TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    source_line_no          INTEGER,
    raw_line_text           TEXT,
    pulse_code              TEXT NOT NULL,
    pulse_sequence          JSONB NOT NULL,
    CONSTRAINT synthesis_event_inner_no_uk UNIQUE (inner_step_id, inner_event_no),
    CONSTRAINT synthesis_event_volumes_ck CHECK (cardinality(injection_volumes_ul) > 0),
    CONSTRAINT synthesis_event_seq_ck CHECK (jsonb_typeof(pulse_sequence) = 'array')
);

CREATE INDEX synthesis_event_step_order_idx
    ON synth_log.synthesis_event (inner_step_id, event_no);
CREATE INDEX synthesis_event_profile_idx
    ON synth_log.synthesis_event (drain_profile_id);

CREATE TABLE synth_log.synthesis_event_segment (
    segment_id      BIGSERIAL PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES synth_log.synthesis_event(event_id) ON DELETE CASCADE,
    segment_no      INTEGER NOT NULL CHECK (segment_no > 0),
    drain_value     NUMERIC(12, 3) NOT NULL CHECK (drain_value >= 0),
    wait_ms         INTEGER NOT NULL CHECK (wait_ms >= 0),
    CONSTRAINT synthesis_event_segment_uk UNIQUE (event_id, segment_no)
);

CREATE TABLE synth_log.log_parse_issue (
    issue_id         BIGSERIAL PRIMARY KEY,
    version_id       BIGINT NOT NULL REFERENCES synth_log.run_log_version(version_id) ON DELETE CASCADE,
    line_no          INTEGER CHECK (line_no IS NULL OR line_no > 0),
    severity         synth_log.issue_severity NOT NULL,
    issue_code       TEXT NOT NULL,
    message          TEXT NOT NULL,
    raw_line_text    TEXT,
    created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX log_parse_issue_version_idx
    ON synth_log.log_parse_issue (version_id, severity, line_no);

CREATE OR REPLACE FUNCTION synth_log.map_step_type(p_reagent TEXT)
RETURNS synth_log.step_type
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE upper(p_reagent)
        WHEN 'DEB' THEN 'DEBLOCK'::synth_log.step_type
        WHEN 'AMI' THEN 'COUPLING'::synth_log.step_type
        WHEN 'MODIFY' THEN 'COUPLING'::synth_log.step_type
        WHEN 'CAP' THEN 'CAPPING'::synth_log.step_type
        WHEN 'OXI' THEN 'OXIDIZE'::synth_log.step_type
        WHEN 'SUL' THEN 'SULFURIZE'::synth_log.step_type
        WHEN 'WASH' THEN 'WASH'::synth_log.step_type
        WHEN 'AUX1' THEN 'ALT_WASH'::synth_log.step_type
        WHEN 'AUX_1' THEN 'ALT_WASH'::synth_log.step_type
        ELSE 'OTHER'::synth_log.step_type
    END;
$$;

CREATE OR REPLACE VIEW synth_log.v_run_sequence AS
SELECT
    v.run_id,
    v.version_id,
    string_agg(c.monomer_code, '' ORDER BY c.cycle_no) AS sequence_3to5
FROM synth_log.run_log_version v
JOIN synth_log.synthesis_cycle c ON c.version_id = v.version_id
GROUP BY v.run_id, v.version_id;

CREATE OR REPLACE VIEW synth_log.v_synthesis_event AS
SELECT
    e.event_id,
    v.run_id,
    c.version_id,
    c.cycle_id,
    c.cycle_no,
    e.inner_step_id,
    e.drain_profile_id,
    e.event_no,
    e.inner_event_no,
    e.global_event_no,
    synth_log.map_step_type(s.reagent_code) AS step_type,
    s.reagent_code,
    s.step_occurrence,
    e.injection_volumes_ul,
    cardinality(e.injection_volumes_ul) AS injection_count,
    (SELECT COALESCE(sum(value), 0) FROM unnest(e.injection_volumes_ul) AS value) AS total_volume_ul,
    e.operation_count,
    e.temperature_c,
    e.humidity_percent,
    e.event_time,
    e.source_line_no,
    e.raw_line_text,
    e.pulse_code,
    e.pulse_sequence
FROM synth_log.synthesis_event e
JOIN synth_log.synthesis_inner_step s ON s.inner_step_id = e.inner_step_id
JOIN synth_log.synthesis_cycle c ON c.cycle_id = s.cycle_id
JOIN synth_log.run_log_version v ON v.version_id = c.version_id;

CREATE OR REPLACE VIEW synth_log.v_cycle_inner_shape AS
SELECT
    v.run_id,
    c.version_id,
    c.cycle_id,
    c.cycle_no,
    c.monomer_code,
    c.inner_step_count,
    c.event_count,
    c.operation_count,
    string_agg(s.reagent_code || '×' || s.event_count::text, '-' ORDER BY s.step_order) AS inner_shape,
    c.ami_event_count
FROM synth_log.synthesis_cycle c
JOIN synth_log.run_log_version v ON v.version_id = c.version_id
JOIN synth_log.synthesis_inner_step s ON s.cycle_id = c.cycle_id
GROUP BY v.run_id, c.version_id, c.cycle_id, c.cycle_no, c.monomer_code,
         c.inner_step_count, c.event_count, c.operation_count, c.ami_event_count;

CREATE OR REPLACE VIEW synth_log.v_drain_profile_usage AS
SELECT
    p.profile_id,
    p.profile_hash,
    p.pulse_sequence,
    jsonb_array_length(p.pulse_sequence) AS pulse_count,
    (
        SELECT COALESCE(sum(ps.wait_ms), 0)::bigint
        FROM synth_log.drain_profile_segment ps
        WHERE ps.profile_id = p.profile_id
    ) AS total_wait_ms,
    (
        SELECT count(*)
        FROM synth_log.synthesis_event e
        WHERE e.drain_profile_id = p.profile_id
    ) AS event_count,
    p.created_at,
    p.last_seen_at
FROM synth_log.drain_profile p;

CREATE OR REPLACE VIEW synth_log.v_current_run AS
SELECT
    r.run_id,
    r.run_code,
    r.channel_no,
    r.attempt_no,
    r.device,
    r.started_at,
    r.completed_at,
    r.status,
    r.current_version_id AS version_id,
    v.version_no,
    v.parser_version,
    v.recipe_file_name,
    v.observed_sequence_3to5 AS sequence_3to5,
    v.cycle_count,
    v.event_count,
    v.operation_count,
    v.aux_event_count,
    v.issue_count,
    v.imported_at,
    f.file_id AS source_file_id,
    f.original_file_name,
    f.sha256,
    f.blob_status
FROM synth_log.synthesis_run r
LEFT JOIN synth_log.run_log_version v ON v.version_id = r.current_version_id
LEFT JOIN synth_log.source_file f ON f.file_id = v.source_file_id;

-- Semantic comparison of two parsed versions. Equal profile_id values avoid segment scans.
CREATE OR REPLACE FUNCTION synth_log.compare_log_versions(
    p_left_version_id BIGINT,
    p_right_version_id BIGINT
)
RETURNS TABLE (
    change_type        TEXT,
    cycle_no           INTEGER,
    reagent_code       TEXT,
    step_occurrence    INTEGER,
    event_no           INTEGER,
    segment_no         INTEGER,
    field_name         TEXT,
    old_value          TEXT,
    new_value          TEXT,
    delta              NUMERIC,
    delta_percent      NUMERIC
)
LANGUAGE sql
STABLE
AS $$
    WITH left_events AS (
        SELECT c.cycle_no, s.reagent_code, s.step_occurrence,
               e.inner_event_no, e.event_no, e.event_id, e.drain_profile_id,
               e.operation_count, e.injection_volumes_ul
        FROM synth_log.synthesis_cycle c
        JOIN synth_log.synthesis_inner_step s ON s.cycle_id = c.cycle_id
        JOIN synth_log.synthesis_event e ON e.inner_step_id = s.inner_step_id
        WHERE c.version_id = p_left_version_id
    ), right_events AS (
        SELECT c.cycle_no, s.reagent_code, s.step_occurrence,
               e.inner_event_no, e.event_no, e.event_id, e.drain_profile_id,
               e.operation_count, e.injection_volumes_ul
        FROM synth_log.synthesis_cycle c
        JOIN synth_log.synthesis_inner_step s ON s.cycle_id = c.cycle_id
        JOIN synth_log.synthesis_event e ON e.inner_step_id = s.inner_step_id
        WHERE c.version_id = p_right_version_id
    ), event_pairs AS (
        SELECT
            COALESCE(l.cycle_no, r.cycle_no) AS cycle_no,
            COALESCE(l.reagent_code, r.reagent_code) AS reagent_code,
            COALESCE(l.step_occurrence, r.step_occurrence) AS step_occurrence,
            COALESCE(l.inner_event_no, r.inner_event_no) AS inner_event_no,
            COALESCE(l.event_no, r.event_no) AS event_no,
            l.event_id AS left_event_id,
            r.event_id AS right_event_id,
            l.drain_profile_id AS left_profile_id,
            r.drain_profile_id AS right_profile_id,
            l.operation_count AS left_operation_count,
            r.operation_count AS right_operation_count,
            l.injection_volumes_ul AS left_volumes,
            r.injection_volumes_ul AS right_volumes
        FROM left_events l
        FULL JOIN right_events r USING (cycle_no, reagent_code, step_occurrence, inner_event_no)
    ), step_counts AS (
        SELECT cycle_no, reagent_code, step_occurrence, count(*)::integer AS event_count
        FROM left_events GROUP BY cycle_no, reagent_code, step_occurrence
    ), right_step_counts AS (
        SELECT cycle_no, reagent_code, step_occurrence, count(*)::integer AS event_count
        FROM right_events GROUP BY cycle_no, reagent_code, step_occurrence
    ), event_count_diff AS (
        SELECT
            'MODIFIED'::text AS change_type,
            COALESCE(l.cycle_no, r.cycle_no) AS cycle_no,
            COALESCE(l.reagent_code, r.reagent_code) AS reagent_code,
            COALESCE(l.step_occurrence, r.step_occurrence) AS step_occurrence,
            NULL::integer AS event_no,
            NULL::integer AS segment_no,
            'event_count'::text AS field_name,
            COALESCE(l.event_count, 0)::text AS old_value,
            COALESCE(r.event_count, 0)::text AS new_value,
            (COALESCE(r.event_count, 0) - COALESCE(l.event_count, 0))::numeric AS delta,
            CASE WHEN COALESCE(l.event_count, 0) = 0 THEN NULL
                 ELSE round(100.0 * (COALESCE(r.event_count, 0) - l.event_count) / l.event_count, 3)
            END AS delta_percent
        FROM step_counts l
        FULL JOIN right_step_counts r USING (cycle_no, reagent_code, step_occurrence)
        WHERE COALESCE(l.event_count, 0) <> COALESCE(r.event_count, 0)
    ), event_presence_diff AS (
        SELECT
            CASE WHEN left_event_id IS NULL THEN 'ADDED' ELSE 'REMOVED' END::text AS change_type,
            cycle_no, reagent_code, step_occurrence, event_no, NULL::integer AS segment_no,
            'event_presence'::text AS field_name,
            CASE WHEN left_event_id IS NULL THEN NULL ELSE 'present' END::text AS old_value,
            CASE WHEN right_event_id IS NULL THEN NULL ELSE 'present' END::text AS new_value,
            NULL::numeric AS delta, NULL::numeric AS delta_percent
        FROM event_pairs
        WHERE left_event_id IS NULL OR right_event_id IS NULL
    ), operation_count_diff AS (
        SELECT
            'MODIFIED'::text AS change_type,
            cycle_no, reagent_code, step_occurrence, event_no, NULL::integer AS segment_no,
            'operation_count'::text AS field_name,
            left_operation_count::text AS old_value,
            right_operation_count::text AS new_value,
            (right_operation_count - left_operation_count)::numeric AS delta,
            CASE WHEN left_operation_count = 0 THEN NULL
                 ELSE round(100.0 * (right_operation_count - left_operation_count) / left_operation_count, 3)
            END AS delta_percent
        FROM event_pairs
        WHERE left_event_id IS NOT NULL AND right_event_id IS NOT NULL
          AND left_operation_count IS DISTINCT FROM right_operation_count
    ), volume_pairs AS (
        SELECT ep.*, n.volume_no,
               ep.left_volumes[n.volume_no]::numeric AS left_value,
               ep.right_volumes[n.volume_no]::numeric AS right_value
        FROM event_pairs ep
        CROSS JOIN LATERAL generate_series(
            1,
            greatest(cardinality(ep.left_volumes), cardinality(ep.right_volumes))
        ) AS n(volume_no)
        WHERE ep.left_event_id IS NOT NULL AND ep.right_event_id IS NOT NULL
    ), volume_diff AS (
        SELECT
            CASE WHEN left_value IS NULL THEN 'ADDED'
                 WHEN right_value IS NULL THEN 'REMOVED' ELSE 'MODIFIED' END::text AS change_type,
            cycle_no, reagent_code, step_occurrence, event_no, volume_no AS segment_no,
            'injection_volume_ul'::text AS field_name,
            left_value::text AS old_value, right_value::text AS new_value,
            (right_value - left_value)::numeric AS delta,
            CASE WHEN left_value IS NULL OR left_value = 0 OR right_value IS NULL THEN NULL
                 ELSE round(100.0 * (right_value - left_value) / left_value, 3)
            END AS delta_percent
        FROM volume_pairs
        WHERE left_value IS DISTINCT FROM right_value
    ), changed_profile_pairs AS (
        SELECT * FROM event_pairs
        WHERE left_event_id IS NOT NULL AND right_event_id IS NOT NULL
          AND left_profile_id IS DISTINCT FROM right_profile_id
    ), segment_pairs AS (
        SELECT ep.cycle_no, ep.reagent_code, ep.step_occurrence, ep.event_no,
               n.segment_no,
               ls.drain_value AS left_drain, rs.drain_value AS right_drain,
               ls.wait_ms AS left_wait, rs.wait_ms AS right_wait
        FROM changed_profile_pairs ep
        CROSS JOIN LATERAL generate_series(
            1,
            greatest(ep.left_operation_count, ep.right_operation_count)
        ) AS n(segment_no)
        LEFT JOIN synth_log.synthesis_event_segment ls
            ON ls.event_id = ep.left_event_id AND ls.segment_no = n.segment_no
        LEFT JOIN synth_log.synthesis_event_segment rs
            ON rs.event_id = ep.right_event_id AND rs.segment_no = n.segment_no
    ), segment_diff AS (
        SELECT
            CASE WHEN left_drain IS NULL THEN 'ADDED'
                 WHEN right_drain IS NULL THEN 'REMOVED' ELSE 'MODIFIED' END::text AS change_type,
            cycle_no, reagent_code, step_occurrence, event_no, segment_no,
            'drain_value'::text AS field_name,
            left_drain::text AS old_value, right_drain::text AS new_value,
            (right_drain - left_drain)::numeric AS delta,
            CASE WHEN left_drain IS NULL OR left_drain = 0 OR right_drain IS NULL THEN NULL
                 ELSE round(100.0 * (right_drain - left_drain) / left_drain, 3)
            END AS delta_percent
        FROM segment_pairs WHERE left_drain IS DISTINCT FROM right_drain
        UNION ALL
        SELECT
            CASE WHEN left_wait IS NULL THEN 'ADDED'
                 WHEN right_wait IS NULL THEN 'REMOVED' ELSE 'MODIFIED' END::text,
            cycle_no, reagent_code, step_occurrence, event_no, segment_no,
            'wait_ms'::text,
            left_wait::text, right_wait::text,
            (right_wait - left_wait)::numeric,
            CASE WHEN left_wait IS NULL OR left_wait = 0 OR right_wait IS NULL THEN NULL
                 ELSE round(100.0 * (right_wait - left_wait) / left_wait, 3)
            END
        FROM segment_pairs WHERE left_wait IS DISTINCT FROM right_wait
    )
    SELECT * FROM event_count_diff
    UNION ALL SELECT * FROM event_presence_diff
    UNION ALL SELECT * FROM operation_count_diff
    UNION ALL SELECT * FROM volume_diff
    UNION ALL SELECT * FROM segment_diff;
$$;

COMMENT ON TABLE synth_log.synthesis_run IS
    'Stable identity of one physical synthesis execution. Re-imports are stored in run_log_version.';
COMMENT ON TABLE synth_log.run_log_version IS
    'One uploaded/reparsed log version. Only a SUCCEEDED version may be referenced as current_version_id.';
COMMENT ON TABLE synth_log.source_file_blob IS
    'Raw bytes separated from metadata so list and analysis queries never fetch the blob.';
COMMENT ON FUNCTION synth_log.compare_log_versions(BIGINT, BIGINT) IS
    'Semantic version diff: event counts/presence, operation counts, injection volume, Drain and Wait.';

INSERT INTO synth_log.schema_migration (version, description)
VALUES ('001_log_ingestion', 'Current baseline: stable runs, log versions, source blobs and semantic diff');
