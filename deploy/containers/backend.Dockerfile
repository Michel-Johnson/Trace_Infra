# Supply the verified Python image digest from the release image lock.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app/apps/api \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY apps/api/requirements.lock /app/apps/api/requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: -r /app/apps/api/requirements.lock

COPY src/trace_hunter/ /app/src/trace_hunter/
COPY apps/api/trace_hunter_api/ /app/apps/api/trace_hunter_api/
COPY apps/api/run.py /app/apps/api/run.py
COPY schemas/ /app/schemas/
COPY contracts/schemas/ /app/contracts/schemas/
COPY contracts/imports/ /app/contracts/imports/
COPY contracts/openapi.json /app/contracts/openapi.json
COPY contracts/drafts/trace-v2/trace.schema.json /app/contracts/drafts/trace-v2/trace.schema.json
COPY db/migrations/ /app/db/migrations/
COPY plugins/worker/ /app/plugins/worker/
COPY plugins/official/ /app/plugins/official/
COPY plugins/extensions/ /app/plugins/extensions/
# Published package hashes also cover these browser files; the API verifies them.
COPY apps/web/src/plugins/renderers.tsx apps/web/src/plugins/renderers-1.1.tsx apps/web/src/plugins/slicers.ts /app/apps/web/src/plugins/
COPY apps/web/src/lib/duration-scale.ts /app/apps/web/src/lib/duration-scale.ts
COPY examples/ /app/examples/
COPY scripts/evaluation_worker.py scripts/invocation_worker.py scripts/migrate_database.py scripts/rebuild_span_indexes.py /app/scripts/
COPY deploy/containers/backend_entrypoint.py /app/deploy/containers/backend_entrypoint.py

RUN mkdir -p /var/lib/trace-hunter/content && chown -R 10001:10001 /var/lib/trace-hunter

USER 10001:10001
EXPOSE 8767
ENTRYPOINT ["python", "/app/deploy/containers/backend_entrypoint.py"]
CMD ["api"]

# Separate deployable roles share the same verified Backend layer.
FROM backend AS api
CMD ["api"]

FROM backend AS plugin
CMD ["worker"]

# Optional client tooling: carries the complete repository-relative skill wrappers.
FROM backend AS agent-tools
COPY scripts/evaluation_mcp.py scripts/trace_import.py scripts/prepare_doubao_corpus.py /app/scripts/
COPY scripts/prepare_doubao_message_csv.py /app/scripts/prepare_doubao_message_csv.py
COPY skills/ /app/skills/
ENTRYPOINT ["python"]
CMD ["/app/scripts/trace_import.py", "--help"]
