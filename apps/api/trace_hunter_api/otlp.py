"""Standard OTLP/HTTP trace receiver."""

import json
from typing import Annotated

from fastapi import Header, Request
from fastapi.responses import JSONResponse, Response

from .access import require_project
from .traces import errors


async def _body(request, limit=16 * 1024 * 1024):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit: raise ValueError("OTLP request exceeds 16 MiB")
    return bytes(raw)


def install(app, ingestor, reply):
    @app.post("/v1/traces", operation_id="exportOtlpTraces", tags=["OTLP"],
              responses=errors(413, 415, 422, 500))
    async def export(request: Request,
                     project_id: Annotated[str, Header(alias="X-Trace-Project", min_length=1, max_length=512)]):
        require_project(request, project_id, "traces:write")
        raw = await _body(request)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type in ("application/json", "application/otlp+json"):
            try: envelope = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError): raise ValueError("OTLP JSON is invalid") from None
            result = ingestor.ingest(project_id, envelope, raw, media_type="application/json")
            response = JSONResponse({"partialSuccess": {"rejectedSpans": result["rejected_spans"]}},
                                    headers={"Cache-Control": "no-store"})
        elif media_type in ("application/x-protobuf", "application/protobuf"):
            try:
                from google.protobuf.json_format import MessageToDict
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                    ExportTraceServiceRequest, ExportTraceServiceResponse)
                request_message = ExportTraceServiceRequest.FromString(raw)
                envelope = MessageToDict(request_message, preserving_proto_field_name=False)
            except (ImportError, ValueError) as error:
                raise ValueError("OTLP protobuf is invalid or unavailable") from error
            result = ingestor.ingest(project_id, envelope, raw, media_type="application/x-protobuf")
            response = Response(ExportTraceServiceResponse().SerializeToString(),
                                media_type="application/x-protobuf", headers={"Cache-Control": "no-store"})
        else:
            return reply({"error": "OTLP requires application/json or application/x-protobuf", "details": []}, 415)
        response.headers["X-Trace-Hunter-Accepted-Traces"] = str(len(result["accepted_traces"]))
        response.headers["X-Trace-Hunter-Accepted-Spans"] = str(result["accepted_spans"])
        return response
