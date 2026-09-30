"""Pull durable notifications and explicitly report a consumer checkpoint."""
from typing import Annotated, Literal
from fastapi import Path, Query, Request
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool
from trace_hunter.notifications import MAX_SEQUENCE
from .access import require_project
from .execution import ExecutionActor
from .invocations import _body, _request_schema
from .traces import Digest, ProjectPath, Timestamp, TraceDTO, TraceRouteError

Sequence = Annotated[int, Field(strict=True, ge=0, le=MAX_SEQUENCE)]
ConsumerId = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]
ConsumerPath = Annotated[str, Path(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]


class Notification(TraceDTO):
    project_id: str
    sequence: Sequence
    event_type: Literal['invocation.requested', 'invocation.claimed', 'invocation.result.accepted', 'invocation.cancelled',
                        'invocation.retry_requested', 'invocation.lease_expired', 'external_action.prepared', 'external_action.started',
                        'external_action.reported', 'external_action.reconciled', 'external_action.abandoned', 'external_action.outcome_unknown']
    invocation_id: str
    attempt: int | None
    payload: dict[str, JsonValue]
    payload_digest: Digest
    recorded_at: Timestamp


class NotificationPage(TraceDTO):
    items: list[Notification]
    next_after: Sequence
    has_more: bool
    head: Sequence
    history_scope: Literal['recorded_events']


class NotificationConsumer(TraceDTO):
    project_id: str
    consumer_id: str
    owner: ExecutionActor
    acknowledged_sequence: Sequence
    created_at: Timestamp
    updated_at: Timestamp
    meaning: Literal['notification_progress_reported']


class ConsumerCreateRequest(TraceDTO):
    consumer_id: ConsumerId


class ConsumerAckRequest(TraceDTO):
    expected_after: Sequence
    through: Sequence


class ConsumerAck(TraceDTO):
    changed: bool
    consumer: NotificationConsumer


REQUEST_MODELS = (ConsumerCreateRequest, ConsumerAckRequest)


def install(app, notifications):
    common = {'tags':['Notifications'], 'responses':{c:{'model':TraceRouteError} for c in (401,403,404,409,413,415,422,500)}}
    root = '/api/v1/projects/{project_id}/notifications'

    @app.get(root, response_model=NotificationPage, operation_id='listNotifications', **common)
    def read(project_id: ProjectPath, request: Request, after: Annotated[int, Query(ge=0, le=MAX_SEQUENCE)]=0,
             limit: Annotated[int, Query(ge=1, le=100)]=100):
        require_project(request, project_id, 'notifications:read')
        return notifications.read(project_id, after=after, limit=limit)

    @app.post(root+'/consumers', response_model=NotificationConsumer, operation_id='createNotificationConsumer',
              openapi_extra=_request_schema(ConsumerCreateRequest), **common)
    async def create(project_id: ProjectPath, request: Request):
        actor = require_project(request, project_id, 'notifications:ack')
        body = await _body(request, ConsumerCreateRequest)
        return await run_in_threadpool(notifications.create_consumer, project_id, body.consumer_id, actor=actor)

    @app.get(root+'/consumers/{consumer_id}', response_model=NotificationConsumer, operation_id='getNotificationConsumer', **common)
    def get(project_id: ProjectPath, consumer_id: ConsumerPath, request: Request):
        actor = require_project(request, project_id, 'notifications:ack')
        return notifications.get_consumer(project_id, consumer_id, actor=actor)

    @app.post(root+'/consumers/{consumer_id}/ack', response_model=ConsumerAck, operation_id='ackNotificationConsumer',
              openapi_extra=_request_schema(ConsumerAckRequest), **common)
    async def ack(project_id: ProjectPath, consumer_id: ConsumerPath, request: Request):
        actor = require_project(request, project_id, 'notifications:ack')
        body = await _body(request, ConsumerAckRequest)
        return await run_in_threadpool(notifications.acknowledge, project_id, consumer_id, actor=actor, **body.model_dump())
