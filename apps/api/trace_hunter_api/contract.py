"""Generate the core trace API contract from registered transport models."""

from fastapi import FastAPI
from fastapi.responses import Response

from .access import install as install_access
from .aggregates import install as install_aggregates
from .query import install as install_query
from .search import install as install_search
from .spans import install as install_spans
from .traces import install as install_traces
from .imports import install as install_imports
from .observability import install as install_observability
from .tasks import install as install_tasks
from .advanced import install as install_advanced
from .batch_operations import install as install_batch_operations
from .uploads import install as install_uploads
from .otlp import install as install_otlp
from .agent import install as install_agent


SCOPES = {
    "appendTraceRevision": "traces:write", "rebuildTraceIndex": "traces:index",
    "getTraceRevision": "traces:read", "getTraceRevisionContent": "traces:read",
    "listTraceRevisions": "traces:read", "getProject": "traces:read",
    "queryTraceRevisions": "traces:read", "aggregateTraceRevisions": "traces:read",
    "querySpans": "traces:read", "searchTraceContent": "conditional", "listEvaluationSearchTerms": "traces:read",
    "promoteEvaluationSearchTerm": "traces:write",
    "demoteEvaluationSearchTerm": "traces:write",
    "getTraceQueryCapabilities": "authenticated", "getSpanQueryCapabilities": "authenticated",
    "getTraceSearchCapabilities": "authenticated",
    "getAdapterImportCapabilities": "authenticated",
    "getProjectObservability": "traces:read",
    "getTaskCapabilities": "authenticated", "createTask": "traces:write",
    "listTasks": "traces:read", "getTask": "traces:read", "watchTask": "traces:read",
    "updateTask": "traces:write", "cancelTask": "traces:write",
    "queryObjects": "conditional", "queryTraceMetrics": "conditional",
    "getAdvancedQueryCapabilities": "authenticated",
    "createEvidenceExport": "traces:search:analysis", "readEvidenceExport": "traces:search:analysis",
    "retryTask": "traces:write",
    "createTraceUpload": "traces:write", "getTraceUpload": "traces:read",
    "putTraceUploadPart": "traces:write", "completeTraceUpload": "traces:write",
    "deleteTraceUpload": "traces:write",
    "exportOtlpTraces": "traces:write",
    "getHealth": "authenticated", "getPublishedOpenApi": "authenticated",
    "downloadSkillArchive": "authenticated", "getSkillManifest": "authenticated",
    "getCanonicalTraceSchema": "authenticated", "listTraceFormats": "authenticated",
    "getTraceFormatSchema": "authenticated", "listTraceExamples": "authenticated",
    "getTraceExample": "authenticated",
    "getAgentCapabilities": "authenticated", "createAgentSession": "traces:read",
    "listAgentSessions": "authenticated", "getAgentSession": "authenticated",
    "sendAgentMessage": "traces:read", "cancelAgentTurn": "authenticated",
    "uploadAgentAttachment": "traces:write", "readAgentAttachment": "worker-only",
    "archiveAgentTurn": "worker-only", "watchAgentEvents": "authenticated",
    "listAgentEvents": "authenticated", "getAgentTraceSourceContent": "traces:read",
    "storeAgentRecorderSource": "worker-only",
    "recoverAgentWorker": "worker-only", "claimAgentTurn": "worker-only",
    "appendAgentWorkerEvent": "worker-only", "getAgentCancellation": "worker-only",
    "finishAgentTurn": "worker-only", "attachAgentTask": "worker-only",
    "listAgentWorkerAttachments": "worker-only",
}


def install_metadata_contract(app):
    @app.get("/api/health", operation_id="getHealth", tags=["Platform"])
    def health(): return {}

    @app.get("/api/openapi.json", operation_id="getPublishedOpenApi", tags=["Platform"])
    def openapi_document(): return {}

    @app.get("/api/skills/archive", operation_id="downloadSkillArchive", tags=["Skills"],
             response_class=Response, responses={200: {"content": {"application/zip": {}}}})
    def skill_archive(): return Response()

    @app.get("/api/skills/manifest", operation_id="getSkillManifest", tags=["Skills"])
    def skill_manifest(): return {}

    @app.get("/api/schema", operation_id="getCanonicalTraceSchema", tags=["Trace formats"])
    def trace_schema(): return {}

    @app.get("/api/v1/trace-formats", operation_id="listTraceFormats", tags=["Trace formats"])
    def trace_formats(): return {}

    @app.get("/api/v1/trace-formats/{profile_id}", operation_id="getTraceFormatSchema",
             tags=["Trace formats"])
    def trace_format_schema(profile_id: str): return {}

    @app.get("/api/examples", operation_id="listTraceExamples", tags=["Trace formats"])
    def examples(): return []

    @app.get("/api/examples/{name}", operation_id="getTraceExample", tags=["Trace formats"])
    def example(name: str): return {}


def trace_revision_contract():
    app = FastAPI(title="Trace Hunter core API", openapi_version="3.1.0")
    install_metadata_contract(app)
    install_traces(app, None, None, None)
    install_access(app, None, None)
    install_query(app, None, None)
    install_spans(app, None, None)
    install_search(app, None, None)
    install_aggregates(app, None)
    install_imports(app, None, None)
    install_uploads(app, None, None)
    install_otlp(app, None, None)
    install_agent(app, None, None)
    install_tasks(app, None, None, None, None)
    install_advanced(app, None, None)
    install_batch_operations(app, None, None)
    install_observability(app, None, None)
    contract = app.openapi()
    for path in contract["paths"].values():
        for method, operation in path.items():
            if method == "post":
                operation["responses"]["403"] = {
                    "description": "Request origin or project scope does not allow this operation.",
                    "content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/TraceRouteError"}}},
                }
            scope = SCOPES.get(operation["operationId"])
            operation["security"] = [{"OperatorBasic": []}]
            operation["x-required-scope"] = scope or "operator"
            operation["x-operator-only"] = True
            if operation["operationId"] == "searchTraceContent":
                operation["x-conditional-scopes"] = {
                    "analysis": ["traces:read", "traces:search:analysis"],
                    "model_context": ["traces:model_context"],
                }
            if operation["operationId"] in ("queryObjects", "queryTraceMetrics"):
                operation["x-conditional-scopes"] = {
                    "analysis": ["traces:read", "traces:search:analysis"],
                    "model_context": ["traces:model_context"],
                }
    schemes = contract.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["OperatorBasic"] = {
        "type": "http", "scheme": "basic",
        "description": "Verified by the configured gateway and trusted peer boundary.",
    }
    schemes["EvaluatorBearer"] = {
        "type": "http", "scheme": "bearer",
        "description": "Evaluator-only process token; grants model-context queries only.",
    }
    evaluator_operations = {"getTraceQueryCapabilities", "getSpanQueryCapabilities",
                            "getTraceSearchCapabilities", "getObjectAnalysisCapabilities",
                            "searchTraceContent", "analyzeObjects", "resolveObjectRunSet",
                            "analyzeObjectFunnel", "getAnalysisResult", "queryObjects",
                            "queryTraceMetrics", "queryObjectLineage", "querySessions",
                            "querySessionTimeline", "getAdvancedQueryCapabilities"}
    for path in contract["paths"].values():
        for operation in path.values():
            if isinstance(operation, dict) and operation.get("operationId") in evaluator_operations:
                operation["security"] = [{"OperatorBasic": []}, {"EvaluatorBearer": []}]
    return contract
