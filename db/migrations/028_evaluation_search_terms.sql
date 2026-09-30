-- Derived, rebuildable exact-substring postings for a small curated vocabulary.
CREATE TABLE trace_search_hot_terms (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    term TEXT NOT NULL CHECK (char_length(term) BETWEEN 2 AND 128),
    search_count BIGINT NOT NULL DEFAULT 0 CHECK (search_count >= 0),
    total_latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (total_latency_ms >= 0),
    last_total_count BIGINT,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_searched_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ,
    PRIMARY KEY (project_id,term)
);
CREATE INDEX trace_search_hot_terms_rank
    ON trace_search_hot_terms(project_id,active,search_count DESC,last_searched_at DESC);

CREATE TABLE trace_search_hot_postings (
    project_id TEXT NOT NULL,
    term TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    document_ordinal INTEGER NOT NULL,
    object_kind TEXT NOT NULL,
    field TEXT NOT NULL,
    span_id TEXT,
    PRIMARY KEY (project_id,term,run_id,revision,projector_version,document_ordinal),
    FOREIGN KEY (project_id,term) REFERENCES trace_search_hot_terms(project_id,term) ON DELETE CASCADE
);
CREATE INDEX trace_search_hot_postings_page
    ON trace_search_hot_postings(project_id,term,projector_version,run_id COLLATE "C",revision,document_ordinal);
CREATE INDEX trace_search_hot_postings_document
    ON trace_search_hot_postings(project_id,run_id,revision,projector_version);
