"""HTTP transport only. Protocol and analysis live in the shared Python core."""
import json
import os
from io import BytesIO
from contextlib import asynccontextmanager
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from trace_hunter.protocol import ROOT, SCHEMA, InvalidTrace
from trace_hunter.skill_manifest import build_skill_manifest, skill_files
from trace_hunter.trace_formats import describe_formats, profile_schema
from trace_hunter.storage import Store, Conflict
from trace_hunter.traces.index import TraceIndex
from trace_hunter.traces.lookup import LookupUnavailable
from trace_hunter.content import ContentCorruption, ContentLimitExceeded
from .traces import install as install_traces
from trace_hunter.access import Projects
from .access import install as install_access
from trace_hunter.query import SpanQuery, TraceQuery, TraceSearch, AdvancedQuery
from .query import install as install_queries
from .spans import install as install_span_queries
from .search import install as install_search
from trace_hunter.query.aggregate import TraceAggregates
from .aggregates import install as install_aggregates
from trace_hunter.import_jobs import ImportJobs
from trace_hunter.auto_imports import AutoImports
from .imports import install as install_imports
from trace_hunter.query import ProjectObservability
from .observability import install as install_observability
from trace_hunter.tasks import TaskRegistry
from .tasks import install as install_tasks
from .advanced import install as install_advanced
from trace_hunter.batch_operations import BatchOperations
from .batch_operations import install as install_batch_operations
from trace_hunter.uploads import Uploads
from .uploads import install as install_uploads
from trace_hunter.otlp import OtlpIngestor
from .otlp import install as install_otlp
from .query_cache import ServerQueryCache, install as install_query_cache
from .agent import install as install_agent


def reply(payload, status=200):
    return JSONResponse(payload, status_code=status, headers={'Cache-Control': 'no-store'})


def build_skill_archive(skills_directory: Path, cli_path: Path) -> bytes:
    manifest = build_skill_manifest(skills_directory, cli_path)
    skills = [skills_directory / item['name'] for item in manifest['skills']]
    archive_manifest = {
        **manifest,
        'format': 'trace-hunter-skill-bundle/1',
        'skills': [item['name'] for item in manifest['skills']],
        'skill_versions': manifest['skills'],
    }
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED, compresslevel=9) as archive:
        def write(name, content, mode=0o644):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = mode << 16
            archive.writestr(info, content)

        write('trace-hunter-client/manifest.json',
              (json.dumps(archive_manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
        for skill in skills:
            for relative, content in sorted(skill_files(skill, skills_directory, cli_path).items()):
                mode = 0o755 if relative == 'trace-hunter-cli/scripts/trace_hunter_cli.py' else 0o644
                write(f'trace-hunter-client/skills/{relative}', content, mode)
    return output.getvalue()


def create_app(store=None, allowed_origins=None):
    owns_store = store is None
    if store is None:
        location = os.environ.get('DATABASE_URL')
        if not location:
            raise RuntimeError('DATABASE_URL is required; use PostgreSQL for the platform or an explicit SQLite path for tests')
        pricing = os.environ.get('PRICING_FILE')
        store = Store(location, json.loads(Path(pricing).read_text()) if pricing else None)
    origins = set(allowed_origins if allowed_origins is not None else os.environ.get('ALLOWED_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(','))
    @asynccontextmanager
    async def lifespan(app):
        store.repository.rows('SELECT run_id FROM traces LIMIT 1')
        if store.repository.postgres:
            for row in store.repository.rows('SELECT project_id FROM projects ORDER BY project_id'):
                app.state.trace_search.warm(row['project_id'])
        try:
            yield
        finally:
            for name in ('batch_operations', 'uploads'):
                service = getattr(app.state, name, None)
                if service is not None:
                    service.close()
            if owns_store:
                store.close()

    app = FastAPI(title='Trace Hunter API', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.trace_index = TraceIndex(store.revisions)
    install_traces(app, store.revisions, app.state.trace_index, reply)
    app.state.tasks = TaskRegistry(store.content.root / "task-state")
    app.state.query_cache = ServerQueryCache.from_environment(store.repository)
    app.state.import_jobs = ImportJobs(store, app.state.trace_index, app.state.tasks,
                                       app.state.query_cache.invalidate_project)
    app.state.uploads = Uploads(store.content.root, app.state.import_jobs, store.content)
    app.state.otlp = OtlpIngestor(store, app.state.trace_index)
    app.state.advanced_query = AdvancedQuery(store.repository)
    app.state.batch_operations = BatchOperations(app.state.advanced_query, app.state.tasks,
                                                 store.content, store.repository)
    install_imports(app, app.state.import_jobs, reply)
    install_uploads(app, app.state.uploads, reply)
    install_otlp(app, app.state.otlp, reply)
    install_tasks(app, app.state.tasks, app.state.import_jobs, reply, app.state.batch_operations)
    install_agent(app, store, reply)
    if os.environ.get("TRACE_HUNTER_AGENT_ENABLED", "").lower() in ("1", "true", "yes"):
        app.state.uploads.auto_imports = AutoImports(app.state.agent_sessions, app.state.tasks,
                                                     store.repository)
    install_batch_operations(app, app.state.batch_operations, reply)

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request,error):
        details = []
        for issue in error.errors():
            value = issue.get('input')
            if isinstance(value, (dict, list, str)) and len(str(value)) > 256:
                value = '<omitted>'
            details.append({'field': '.'.join(str(item) for item in issue.get('loc', ())),
                            'code': issue.get('type', 'validation_error'),
                            'message': issue.get('msg', 'invalid value'), 'value': value,
                            'suggestion': (issue.get('ctx') or {}).get('expected')})
        return reply({'error':'请求参数不符合协议','details':details},422)

    @app.exception_handler(PermissionError)
    async def forbidden(request,error):
        return reply({'error':str(error),'details':[]},403)

    @app.exception_handler(ContentCorruption)
    async def corrupt_content(request, error):
        return reply({'error': '轨迹原件校验失败', 'details': []}, 500)

    @app.exception_handler(LookupUnavailable)
    async def missing_lookup(request, error):
        return reply({'error': '轨迹查询索引待修复', 'details': []}, 503)

    @app.exception_handler(ContentLimitExceeded)
    async def content_limit(request, error):
        return reply({'error': '内容超过允许的字节数', 'details': []}, 413)

    @app.middleware('http')
    async def origin_check(request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.headers.get('origin') and request.headers['origin'] not in origins:
            return reply({'error': '仅支持允许来源的请求'}, 403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    async def invalid(request, error):
        if isinstance(error, Conflict):
            return reply({'error': str(error), 'details': []}, 409)
        return reply({'error': '输入不符合协议' if isinstance(error, InvalidTrace) else str(error), 'details': getattr(error, 'errors', [])}, 422)
    for kind in (InvalidTrace, ValueError, TypeError):
        app.add_exception_handler(kind, invalid)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return reply({'error': str(error), 'details': []}, 404)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return reply({'error': str(error.detail), 'details': []}, error.status_code)

    @app.exception_handler(Exception)
    async def failed(request, error):
        # Exception details may contain database URLs or trace input; keep them out of responses.
        return reply({'error': '服务器处理失败', 'details': []}, 500)

    @app.get('/api/health')
    def health():
        store.repository.rows('SELECT run_id FROM traces LIMIT 1')
        return reply({'status': 'ok', 'database': 'postgresql' if store.repository.postgres else 'sqlite'})

    @app.get('/api/openapi.json')
    def openapi_document():
        return reply(json.loads((ROOT / 'contracts/openapi.json').read_text()))

    @app.get('/api/skills/archive')
    def skill_archive():
        content = build_skill_archive(ROOT / 'skills', ROOT / 'scripts' / 'trace_hunter_cli.py')
        return Response(content, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="trace-hunter-client.zip"',
            'Content-Length': str(len(content)),
        })

    @app.get('/api/skills/manifest')
    def skill_manifest():
        return reply(build_skill_manifest(ROOT / 'skills', ROOT / 'scripts' / 'trace_hunter_cli.py'))

    @app.get('/api/schema')
    def trace_schema():
        return reply(SCHEMA)

    @app.get('/api/v1/trace-formats')
    def trace_formats():
        return reply(describe_formats())

    @app.get('/api/v1/trace-formats/{profile_id}')
    def trace_format_schema(profile_id: str):
        return reply(profile_schema(profile_id))

    def example_paths():
        return {p.name: p for p in sorted((ROOT / 'examples').glob('*.trace.json'))}

    @app.get('/api/examples')
    def examples():
        result = []
        for name, path in example_paths().items():
            run = json.loads(path.read_text())['run']
            result.append({'name': name, 'title': ' · '.join(run[k] for k in ('harness', 'model', 'title'))})
        return reply(result)

    @app.get('/api/examples/{name}')
    def example(name: str):
        if name not in example_paths():
            raise KeyError('示例不存在')
        return reply(json.loads(example_paths()[name].read_text()))

    app.state.trace_query = TraceQuery(store.repository)
    install_queries(app, app.state.trace_query, reply)
    app.state.span_query = SpanQuery(store.repository)
    install_span_queries(app, app.state.span_query, reply)
    app.state.trace_search = TraceSearch(store.repository, version_provider=app.state.query_cache.data_version)
    install_search(app, app.state.trace_search, reply)
    app.state.trace_aggregates = TraceAggregates(store.repository)
    install_aggregates(app, app.state.trace_aggregates)
    install_advanced(app, app.state.advanced_query, reply)
    app.state.project_observability = ProjectObservability(store.repository)
    install_observability(app, app.state.project_observability, reply)
    install_query_cache(app, app.state.query_cache)
    # Install last so authentication wraps all project-scoped routes.
    install_access(app, Projects(store.repository), reply)
    return app
