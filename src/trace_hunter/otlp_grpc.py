"""OTLP/gRPC companion listener sharing the API's storage services."""

from concurrent import futures


def start(ingestor, projects, *, address="127.0.0.1:4317"):
    import grpc
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

    class Service(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            metadata = dict(context.invocation_metadata())
            project_id = metadata.get("x-trace-project")
            if not project_id:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "x-trace-project metadata is required")
            try:
                projects.get_project(project_id)
                envelope = MessageToDict(request, preserving_proto_field_name=False)
                result = ingestor.ingest(project_id, envelope, request.SerializeToString(),
                                         media_type="application/x-protobuf")
                return trace_service_pb2.ExportTraceServiceResponse(
                    partial_success=trace_service_pb2.ExportTracePartialSuccess(
                        rejected_spans=result["rejected_spans"]))
            except KeyError:
                context.abort(grpc.StatusCode.NOT_FOUND, "project not found")
            except ValueError as error:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), server)
    if server.add_insecure_port(address) == 0:
        raise RuntimeError("Unable to bind OTLP gRPC listener " + address)
    server.start()
    return server
