-- Destructive cutover after 021 has copied the core facts and projections.
DO $$
BEGIN
    IF (SELECT count(*) FROM trace_revisions) != (SELECT count(*) FROM core_trace_revisions) THEN
        RAISE EXCEPTION 'core revision count does not match legacy revisions';
    END IF;
    IF EXISTS (
        SELECT project_id,run_id,revision,content_digest FROM trace_revisions
        EXCEPT SELECT project_id,run_id,revision,content_digest FROM core_trace_revisions
    ) THEN
        RAISE EXCEPTION 'core revision digests do not match legacy revisions';
    END IF;
    IF (SELECT count(*) FROM trace_index_records WHERE projector_version='trace-index/3') !=
       (SELECT count(*) FROM core_trace_objects WHERE projector_version='trace-index/3' AND object_kind='span') THEN
        RAISE EXCEPTION 'core span projection count does not match trace-index/3';
    END IF;
    IF (SELECT count(*) FROM trace_search_documents) !=
       (SELECT count(*) FROM core_trace_search_documents) THEN
        RAISE EXCEPTION 'core search projection count does not match';
    END IF;
END $$;

DROP TABLE IF EXISTS service_credentials,service_principals,
    notification_consumers,notifications,notification_heads,
    external_action_receipts,external_action_events,external_action_executions,external_actions,
    invocation_remote_results,invocation_task_execution_grants,invocation_task_grants,
    runtime_advertised_operations,runtime_probes,runtime_binding_requests,runtime_bindings,runtime_heads,
    invocation_events,invocation_outputs,invocation_results,invocation_claim_requests,invocation_attempts,
    invocation_requests,invocation_inputs,invocations,operation_versions,
    artifact_requests,artifact_inputs,artifacts,selection_members,selection_requests,selection_snapshots,
    collection_classifications,plugin_artifacts,plugin_attempts,plugin_invocations,plugin_batches,
    plugin_handlers,extension_versions,evaluation_results,evaluation_attempts,evaluation_jobs,
    evaluation_batches,plugin_versions,collection_cases,case_definitions,collections,
    trace_context_messages,trace_search_documents,trace_index_records,trace_indexes,
    trace_revision_lookup,trace_revision_requests,trace_revisions,trace_heads,analyses,runs CASCADE;

ALTER TABLE core_traces RENAME TO traces;
ALTER TABLE core_trace_revisions RENAME TO trace_revisions;
ALTER TABLE core_trace_objects RENAME TO trace_objects;
ALTER TABLE core_trace_edges RENAME TO trace_edges;
ALTER TABLE core_trace_search_documents RENAME TO trace_search_documents;
ALTER TABLE core_analysis_results RENAME TO analysis_results;

CREATE INDEX traces_query ON traces(project_id,substr(query_id_hex,1,128));
CREATE INDEX trace_revisions_digest ON trace_revisions(project_id,content_digest);
CREATE INDEX trace_objects_kind ON trace_objects(project_id,projector_version,kind,status);
CREATE INDEX trace_objects_skill ON trace_objects(project_id,projector_version,skill_name,skill_action,status);
CREATE INDEX trace_objects_duration ON trace_objects(project_id,projector_version,duration_ms);
CREATE INDEX trace_objects_visibility ON trace_objects(project_id,projector_version,visibility_status,context_id);
CREATE INDEX trace_edges_lookup ON trace_edges(project_id,run_id,revision,projector_version,relation,source_id);
CREATE INDEX trace_search_documents_identity ON trace_search_documents(project_id,projector_version,run_id,revision,object_kind,field);
CREATE INDEX trace_search_documents_trigram ON trace_search_documents USING GIN(text gin_trgm_ops);
