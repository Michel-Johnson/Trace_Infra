-- Consolidate the platform into seven trace-domain tables plus the existing
-- projects/service identity tables.  The migration is transactional: legacy
-- tables are dropped only after their trace facts and derived projections have
-- been copied into the new model.

CREATE TABLE core_traces (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    run_id TEXT NOT NULL,
    latest_revision INTEGER NOT NULL CHECK(latest_revision >= 0),
    query_id_hex TEXT,
    env_id_hex TEXT,
    harness_hex TEXT,
    model_hex TEXT,
    status_hex TEXT,
    title_hex TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id)
);

CREATE TABLE core_trace_revisions (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    request_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=71),
    size_bytes BIGINT NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL,
    format_version TEXT NOT NULL,
    previous_revision INTEGER,
    derivation TEXT NOT NULL CHECK(derivation IN ('capture','supplement','correction','legacy_import')),
    metadata TEXT NOT NULL,
    query_id_hex TEXT,
    env_id_hex TEXT,
    harness_hex TEXT,
    model_hex TEXT,
    status_hex TEXT,
    projector_version TEXT,
    projection_state TEXT NOT NULL DEFAULT 'unindexed'
        CHECK(projection_state IN ('unindexed','complete','failed')),
    projection_error TEXT,
    projection_digest TEXT,
    indexed_at TEXT,
    record_count INTEGER,
    model_count INTEGER,
    model_batch_count INTEGER,
    tool_count INTEGER,
    agent_count INTEGER,
    wait_count INTEGER,
    other_count INTEGER,
    unknown_count INTEGER,
    capture_coverage TEXT,
    identity_basis TEXT NOT NULL DEFAULT 'source',
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision),
    UNIQUE(project_id,request_key),
    FOREIGN KEY(project_id,run_id) REFERENCES core_traces(project_id,run_id)
);

CREATE TABLE core_trace_objects (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    object_ordinal INTEGER NOT NULL CHECK(object_ordinal >= 0),
    object_kind TEXT NOT NULL CHECK(object_kind IN ('span','message','context','tool_call','annotation')),
    object_id TEXT NOT NULL,
    source_ordinal INTEGER,
    span_id TEXT,
    parent_id TEXT,
    kind TEXT,
    name TEXT,
    operation TEXT,
    status TEXT,
    agent_id TEXT,
    segment_id TEXT,
    turn_id TEXT,
    proposal_id TEXT,
    call_id TEXT,
    invocation_id TEXT,
    attempt INTEGER,
    context_id TEXT,
    visibility_status TEXT,
    visibility_issues TEXT,
    skill_name TEXT,
    skill_action TEXT,
    source_refs TEXT NOT NULL,
    stream_id TEXT,
    source_sequence BIGINT,
    clock_id TEXT,
    clock_kind TEXT,
    clock_origin_at TEXT,
    clock_uncertainty_ms DOUBLE PRECISION,
    start_ms DOUBLE PRECISION,
    end_ms DOUBLE PRECISION,
    duration_ms DOUBLE PRECISION,
    duration_basis TEXT,
    duration_scope TEXT,
    first_response_ms DOUBLE PRECISION,
    last_response_ms DOUBLE PRECISION,
    response_boundary TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,object_ordinal),
    UNIQUE(project_id,run_id,revision,projector_version,object_kind,object_id),
    FOREIGN KEY(project_id,run_id,revision) REFERENCES core_trace_revisions(project_id,run_id,revision)
);

CREATE TABLE core_trace_edges (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    edge_ordinal INTEGER NOT NULL CHECK(edge_ordinal >= 0),
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('parent','retry_of','proposal_of','context_message','invokes','depends_on')),
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    position INTEGER,
    payload TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,edge_ordinal),
    FOREIGN KEY(project_id,run_id,revision) REFERENCES core_trace_revisions(project_id,run_id,revision)
);

CREATE TABLE core_trace_search_documents (
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
    FOREIGN KEY(project_id,run_id,revision) REFERENCES core_trace_revisions(project_id,run_id,revision)
);

CREATE TABLE core_analysis_results (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,analyzer,analyzer_version,scope_digest),
    FOREIGN KEY(project_id,run_id,revision) REFERENCES core_trace_revisions(project_id,run_id,revision)
);

INSERT INTO core_traces(project_id,run_id,latest_revision,query_id_hex,env_id_hex,harness_hex,
    model_hex,status_hex,title_hex,created_at,updated_at)
SELECT h.project_id,h.run_id,h.latest_revision,l.query_id_hex,l.env_id_hex,l.harness_hex,
    l.model_hex,l.status_hex,CASE WHEN i.title IS NULL THEN NULL ELSE encode(convert_to(i.title,'UTF8'),'hex') END,
    MIN(r.created_at),MAX(r.created_at)
FROM trace_heads h JOIN trace_revisions r ON r.project_id=h.project_id AND r.run_id=h.run_id
LEFT JOIN trace_revision_lookup l ON l.project_id=r.project_id AND l.run_id=r.run_id AND l.revision=h.latest_revision
LEFT JOIN trace_indexes i ON i.project_id=r.project_id AND i.run_id=r.run_id
    AND i.revision=h.latest_revision AND i.projector_version='trace-index/3'
GROUP BY h.project_id,h.run_id,h.latest_revision,l.query_id_hex,l.env_id_hex,l.harness_hex,
    l.model_hex,l.status_hex,i.title;

INSERT INTO core_trace_revisions(project_id,run_id,revision,request_key,request_fingerprint,
    content_digest,size_bytes,media_type,format_version,previous_revision,derivation,metadata,
    query_id_hex,env_id_hex,harness_hex,model_hex,status_hex,
    projector_version,projection_state,projection_error,projection_digest,indexed_at,
    record_count,model_count,model_batch_count,tool_count,agent_count,wait_count,other_count,
    unknown_count,capture_coverage,identity_basis,created_at)
SELECT r.project_id,r.run_id,r.revision,COALESCE(q.request_key,'migrated:'||r.run_id||':'||r.revision),
    COALESCE(q.fingerprint,r.content_digest),r.content_digest,r.size_bytes,r.media_type,r.format_version,
    r.previous_revision,r.derivation,r.metadata,l.query_id_hex,l.env_id_hex,l.harness_hex,l.model_hex,l.status_hex,
    i.projector_version,COALESCE(i.state,'unindexed'),
    i.error_code,i.projection_digest,i.indexed_at,i.record_count,i.model_count,i.model_batch_count,
    i.tool_count,i.agent_count,i.wait_count,i.other_count,i.unknown_count,i.capture_coverage,
    COALESCE(i.identity_basis,'source'),r.created_at
FROM trace_revisions r
LEFT JOIN trace_revision_requests q ON q.project_id=r.project_id AND q.run_id=r.run_id AND q.revision=r.revision
LEFT JOIN trace_revision_lookup l ON l.project_id=r.project_id AND l.run_id=r.run_id AND l.revision=r.revision
LEFT JOIN trace_indexes i ON i.project_id=r.project_id AND i.run_id=r.run_id
    AND i.revision=r.revision AND i.projector_version='trace-index/3';

INSERT INTO core_trace_objects(project_id,run_id,revision,projector_version,object_ordinal,
    object_kind,object_id,source_ordinal,span_id,parent_id,kind,name,operation,status,agent_id,
    segment_id,turn_id,proposal_id,call_id,invocation_id,attempt,context_id,visibility_status,
    visibility_issues,skill_name,skill_action,source_refs,stream_id,source_sequence,clock_id,
    clock_kind,clock_origin_at,clock_uncertainty_ms,start_ms,end_ms,duration_ms,duration_basis,
    duration_scope,first_response_ms,last_response_ms,response_boundary,payload)
SELECT project_id,run_id,revision,projector_version,source_ordinal,'span',span_id,source_ordinal,
    span_id,parent_id,kind,name,operation,status,agent_id,segment_id,turn_id,proposal_id,call_id,
    invocation_id,attempt,context_id,visibility_status,visibility_issues,skill_name,skill_action,
    COALESCE(source_refs,'[]'),stream_id,source_sequence,clock_id,clock_kind,clock_origin_at,
    clock_uncertainty_ms,start_ms,end_ms,duration_ms,duration_basis,duration_scope,
    first_response_ms,last_response_ms,response_boundary,'{}'
FROM trace_index_records WHERE projector_version='trace-index/3';

INSERT INTO core_trace_edges(project_id,run_id,revision,projector_version,edge_ordinal,
    source_kind,source_id,relation,target_kind,target_id,position,payload)
SELECT project_id,run_id,revision,projector_version,
    ROW_NUMBER() OVER(PARTITION BY project_id,run_id,revision,projector_version ORDER BY source_ordinal)-1,
    'span',span_id,'parent','span',parent_id,NULL,'{}'
FROM trace_index_records WHERE projector_version='trace-index/3' AND parent_id IS NOT NULL;

INSERT INTO core_trace_edges(project_id,run_id,revision,projector_version,edge_ordinal,
    source_kind,source_id,relation,target_kind,target_id,position,payload)
SELECT cm.project_id,cm.run_id,cm.revision,cm.projector_version,
    COALESCE((SELECT MAX(e.edge_ordinal)+1 FROM core_trace_edges e WHERE e.project_id=cm.project_id
      AND e.run_id=cm.run_id AND e.revision=cm.revision AND e.projector_version=cm.projector_version),0)
      + ROW_NUMBER() OVER(PARTITION BY cm.project_id,cm.run_id,cm.revision,cm.projector_version ORDER BY cm.context_id,cm.position)-1,
    'context',cm.context_id,'context_message','message',cm.message_id,cm.position,'{}'
FROM trace_context_messages cm WHERE cm.projector_version='trace-index/3';

INSERT INTO core_trace_search_documents SELECT * FROM trace_search_documents;

INSERT INTO core_analysis_results(project_id,run_id,revision,analyzer,analyzer_version,scope_digest,
    input_digest,payload,created_at)
SELECT 'default',r.id,1,'legacy-report',a.version,a.scope,a.digest,a.payload,r.imported_at
FROM analyses a JOIN runs r ON r.digest=a.digest
JOIN core_trace_revisions v ON v.project_id='default' AND v.run_id=r.id AND v.revision=1
ON CONFLICT DO NOTHING;

-- Cutover is intentionally separate in 022.  Operators can stop here, compare
-- the shadow core_* tables with the still-live legacy tables, and back up again.
