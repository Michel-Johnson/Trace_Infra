"""Immutable derived artifacts: bounded uploads, typed lineage and scoped reads."""

import base64
import binascii
from typing import Annotated, Literal

from fastapi import Header, HTTPException, Request, Response
from pydantic import ConfigDict, Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool
from typing_extensions import TypedDict

from trace_hunter.artifacts.service import MAX_CONTENT_BYTES, MAX_INPUTS, MIME, TOKEN_PATTERN
from trace_hunter.content import ContentCorruption
from trace_hunter.traces.service import MAX_REVISION
from .access import require_project
from .traces import Count, Digest, ProjectPath, RunPath, Timestamp, TraceDTO, TraceRouteError, document_bytes

ArtifactToken = Annotated[str, Field(pattern="^" + TOKEN_PATTERN + "$", max_length=128)]
ResourceId = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\x00]+$")]
ProducerValue = Annotated[str, Field(max_length=256)] | None
MediaType = Annotated[str, Field(min_length=3, max_length=255, pattern="^" + MIME.pattern.removesuffix(r"\Z") + "$",
                               description="Printable ASCII MIME type/subtype; optional parameters are preserved.")]


class ArtifactTraceReference(TraceDTO):
    kind: Literal["trace_revision"]
    id: ResourceId
    revision: Annotated[int, Field(strict=True, ge=1, le=MAX_REVISION)]
    digest: Digest


class ArtifactSelectionReference(TraceDTO):
    kind: Literal["selection_snapshot"]
    id: ResourceId
    revision: None
    digest: Digest


class ArtifactReference(TraceDTO):
    kind: Literal["artifact"]
    id: ResourceId
    revision: None
    digest: Digest


ResourceReference = Annotated[
    ArtifactTraceReference | ArtifactSelectionReference | ArtifactReference,
    Field(discriminator="kind"),
]


class ArtifactInput(TraceDTO):
    role: ArtifactToken
    ref: ResourceReference


class ArtifactProducerClaim(TraceDTO):
    name: ProducerValue
    version: ProducerValue


class ArtifactContentInput(TraceDTO):
    media_type: MediaType
    encoding: Literal["base64"]
    data: Annotated[str, Field(json_schema_extra={"contentEncoding": "base64"},
        description="Canonical standard Base64; decoded content is limited to 8 MiB.")]


class ArtifactPublishRequest(TraceDTO):
    content: ArtifactContentInput
    artifact_type: ArtifactToken
    inputs: Annotated[list[ArtifactInput], Field(max_length=MAX_INPUTS)]
    producer_claim: ArtifactProducerClaim
    config: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


class ArtifactContentReference(TraceDTO):
    digest: Digest
    size_bytes: Count
    media_type: MediaType


class ArtifactSubmitter(TraceDTO):
    kind: Literal["unknown", "operator", "service"]
    principal_id: str | None


class ArtifactIdentity(TraceDTO):
    schema_version: Literal["trace-hunter/artifact/1"]
    kind: Literal["artifact"]
    project_id: str
    artifact_id: str
    revision: None
    ref: ArtifactReference
    artifact_type: ArtifactToken
    content: ArtifactContentReference
    descriptor_digest: Digest
    producer_claim: ArtifactProducerClaim
    config_digest: Digest
    provenance: Literal["declared"]
    schema_validation: Literal["envelope_only"]
    submitted_by: ArtifactSubmitter
    created_at: Timestamp


class ArtifactDescriptor(ArtifactIdentity):
    inputs: list[ArtifactInput]
    config: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


class ArtifactSummary(ArtifactIdentity):
    input_count: Count


class ArtifactAccepted(TraceDTO):
    created: bool
    artifact: ArtifactDescriptor


class ArtifactQueryFilters(TypedDict, total=False):
    # Optional keys preserve omitted versus explicit null producer claims. Other
    # filters remain non-nullable without inventing placeholder default values.
    __pydantic_config__ = ConfigDict(extra="forbid")
    artifact_type: Annotated[list[ArtifactToken], Field(min_length=1, max_length=50)]
    producer_name: ProducerValue
    producer_version: ProducerValue
    config_digest: Digest
    input_ref: ResourceReference


class ArtifactQueryRequest(TraceDTO):
    filters: ArtifactQueryFilters = Field(default_factory=dict)
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class ArtifactQueryResult(TraceDTO):
    items: list[ArtifactSummary]
    next_cursor: str | None
    query_digest: Digest
    consistency: Literal["live_keyset"]


def content_bytes(encoded: str) -> bytes:
    """Decode only standard canonical Base64, with a bound before allocation."""
    if len(encoded) > 4 * ((MAX_CONTENT_BYTES + 2) // 3):
        raise HTTPException(413, "Artifact content exceeds 8 MiB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Artifact content requires canonical Base64") from None
    if len(raw) > MAX_CONTENT_BYTES:
        raise HTTPException(413, "Artifact content exceeds 8 MiB")
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise HTTPException(422, "Artifact content requires canonical Base64")
    return raw


def install(app, artifacts):
    root = "/api/v1/projects/{project_id}/artifacts"
    resource = root + "/{artifact_id}"
    errors = {status: {"model": TraceRouteError} for status in (401, 403, 404, 409, 413, 415, 422, 500)}

    @app.post(root, response_model=ArtifactAccepted, operation_id="publishArtifact", tags=["Artifacts"],
              responses={201: {"model": ArtifactAccepted}, **errors},
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
                  "$ref": "#/components/schemas/ArtifactPublishRequest",
                  "description": "At most 16 MiB of envelope bytes and 8 MiB of decoded content. Publishing stores declared results; it does not run analysis.",
              }}}}})
    async def publish(project_id: ProjectPath, request: Request, response: Response,
                      idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=512)]):
        actor = require_project(request, project_id, "artifacts:write")
        # A typed FastAPI Body would read the entire request before this bound.
        raw_envelope = await document_bytes(request)
        try:
            body = ArtifactPublishRequest.model_validate_json(raw_envelope)
        except ValidationError:
            raise HTTPException(422, "Artifact envelope does not match the protocol") from None
        for kind in {item.ref.kind for item in body.inputs}:
            actor.require(project_id, "artifacts:read" if kind == "artifact" else "traces:read")
        raw = content_bytes(body.content.data)
        value = body.model_dump(mode="json", exclude={"content"})
        result = await run_in_threadpool(artifacts.publish, project_id, raw,
            media_type=body.content.media_type, artifact_type=body.artifact_type,
            inputs=value["inputs"], producer_claim=value["producer_claim"],
            config=value["config"], metadata=value["metadata"], request_key=idempotency_key, actor=actor)
        response.status_code = 201 if result["created"] else 200
        return result

    @app.post(root + "/query", response_model=ArtifactQueryResult, operation_id="queryArtifacts",
              tags=["Artifacts"], responses=errors)
    def query(project_id: ProjectPath, body: ArtifactQueryRequest, request: Request):
        require_project(request, project_id, "artifacts:read")
        return artifacts.query(project_id, body.model_dump(exclude_unset=True))

    @app.get(resource, response_model=ArtifactDescriptor, operation_id="getArtifact",
             tags=["Artifacts"], responses=errors)
    def get(project_id: ProjectPath, artifact_id: RunPath, request: Request):
        require_project(request, project_id, "artifacts:read")
        return artifacts.get(project_id, artifact_id)

    @app.get(resource + "/content", response_class=Response, operation_id="getArtifactContent",
             tags=["Artifacts"], responses={200: {
                 "description": "Digest-verified original bytes, served as an attachment with the recorded MIME type.",
                 "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
                 "headers": {
                     "Cache-Control": {"schema": {"const": "no-store"}},
                     "Content-Disposition": {"schema": {"const": 'attachment; filename="artifact.bin"'}},
                     "X-Content-Type-Options": {"schema": {"const": "nosniff"}},
                 },
             }, **errors})
    def content(project_id: ProjectPath, artifact_id: RunPath, request: Request):
        require_project(request, project_id, "artifacts:read")
        try:
            descriptor = artifacts.get(project_id, artifact_id)
            raw = artifacts.read_content(project_id, artifact_id)
        except (ContentCorruption, OSError):
            raise HTTPException(500, "Artifact content unavailable or failed integrity validation") from None
        return Response(raw, headers={"Content-Type": descriptor["content"]["media_type"],
            "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="artifact.bin"',
            "X-Content-Type-Options": "nosniff"})
