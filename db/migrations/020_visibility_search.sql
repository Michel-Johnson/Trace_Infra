-- Availability-aware evaluation admission and rebuildable content search.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE trace_index_records ADD COLUMN context_id TEXT;
ALTER TABLE trace_index_records ADD COLUMN visibility_status TEXT
    CHECK(visibility_status IS NULL OR visibility_status IN ('pass','fail','unknown'));
ALTER TABLE trace_index_records ADD COLUMN visibility_issues TEXT;
CREATE INDEX trace_index_records_visibility
    ON trace_index_records(project_id,projector_version,visibility_status,context_id);

CREATE TABLE trace_search_documents (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    document_ordinal INTEGER NOT NULL CHECK(document_ordinal >= 0),
    object_kind TEXT NOT NULL CHECK(object_kind IN ('span','message','context','tool_call')),
    object_id TEXT NOT NULL,
    span_id TEXT,
    field TEXT NOT NULL,
    text TEXT NOT NULL,
    text_state TEXT NOT NULL CHECK(text_state IN ('exact','truncated')),
    source_refs TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,document_ordinal),
    FOREIGN KEY(project_id,run_id,revision,projector_version)
        REFERENCES trace_indexes(project_id,run_id,revision,projector_version)
);
CREATE INDEX trace_search_documents_identity
    ON trace_search_documents(project_id,projector_version,run_id,revision,object_kind,field);
CREATE INDEX trace_search_documents_trigram
    ON trace_search_documents USING GIN(text gin_trgm_ops);

CREATE TABLE trace_context_messages (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    context_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(project_id,run_id,revision,projector_version,context_id,message_id),
    FOREIGN KEY(project_id,run_id,revision,projector_version)
        REFERENCES trace_indexes(project_id,run_id,revision,projector_version)
);
CREATE INDEX trace_context_messages_lookup
    ON trace_context_messages(project_id,run_id,revision,projector_version,context_id,position);
