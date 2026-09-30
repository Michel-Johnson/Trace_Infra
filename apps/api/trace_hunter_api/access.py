"""Stateless trusted-peer access for the eight-table core deployment."""

import hashlib
import hmac
import ipaddress
import os
from typing import Annotated

from fastapi import Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trace_hunter.access import Principal


Identifier = Annotated[str, Path(min_length=1, max_length=512)]


def current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "service_principal", None)
    if not isinstance(principal, Principal):
        raise PermissionError("Authenticated Trace Hunter access required")
    return principal


def require_project(request: Request, project_id: str, scope: str) -> Principal:
    principal = current_principal(request)
    principal.require(project_id, scope)
    return principal


def require_operator(request: Request) -> Principal:
    principal = current_principal(request)
    if not principal.is_operator:
        raise PermissionError("Trusted operator access required")
    return principal


def _networks(environ):
    value = environ.get("TRACE_HUNTER_OPERATOR_NETWORKS", "127.0.0.1/32,::1/128")
    try:
        return tuple(ipaddress.ip_network(item.strip(), strict=False)
                     for item in value.split(",") if item.strip())
    except ValueError:
        raise ValueError("TRACE_HUNTER_OPERATOR_NETWORKS must contain explicit trusted CIDRs") from None


class AccessDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectInput(AccessDTO):
    project_id: Annotated[str, Field(min_length=1, max_length=512)]
    name: Annotated[str, Field(min_length=1, max_length=256)]


class ProjectDescriptor(AccessDTO):
    project_id: str
    name: str
    created_at: str


class ProjectPage(AccessDTO):
    items: list[ProjectDescriptor]
    next_after: str | None


def install(app, projects, reply, task_access=None):
    del task_access
    networks = _networks(os.environ)
    evaluator_only = os.environ.get("TRACE_HUNTER_EVALUATOR_ONLY", "").lower() in ("1", "true", "yes")
    evaluator_digest = os.environ.get("TRACE_HUNTER_EVALUATOR_TOKEN_SHA256")
    evaluator_project = os.environ.get("TRACE_HUNTER_EVALUATOR_PROJECT")
    if evaluator_only:
        try:
            valid_digest = len(bytes.fromhex(evaluator_digest or "")) == 32
        except ValueError:
            valid_digest = False
        if not valid_digest or not evaluator_project:
            raise ValueError("Evaluator-only mode requires token SHA-256 and project")
    app.state.projects = projects

    @app.middleware("http")
    async def trusted_peer(request, call_next):
        path = request.url.path
        if path == "/api/v1" or path.startswith("/api/v1/") or path == "/v1/traces":
            try:
                peer = ipaddress.ip_address(request.client.host) if request.client else None
            except ValueError:
                peer = None
            if peer is None or not any(peer in network for network in networks):
                return reply({"error": "Trusted operator access required", "details": []}, 403)
            if evaluator_only:
                authorization = request.headers.get("authorization", "")
                scheme, separator, token = authorization.partition(" ")
                token = token if separator and scheme.lower() == "bearer" else ""
                digest = hashlib.sha256(token.encode()).hexdigest()
                if not token or not hmac.compare_digest(digest, evaluator_digest):
                    return reply({"error": "Evaluator authentication required", "details": []}, 403)
                request.state.service_principal = Principal.evaluator(evaluator_project)
            else:
                request.state.service_principal = Principal.operator()
        return await call_next(request)

    common = {"tags": ["Projects"]}

    @app.post("/api/v1/projects", status_code=201, response_model=ProjectDescriptor,
              operation_id="createProject", **common)
    def create_project(value: ProjectInput, request: Request):
        require_operator(request)
        return projects.create_project(value.project_id, value.name)

    @app.get("/api/v1/projects", response_model=ProjectPage, operation_id="listProjects", **common)
    def list_projects(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                      after: Annotated[str | None, Query(min_length=1, max_length=512)] = None):
        require_operator(request)
        return projects.list_projects(limit=limit, after=after)

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectDescriptor,
             operation_id="getProject", **common)
    def get_project(project_id: Identifier, request: Request):
        require_project(request, project_id, "traces:read")
        return projects.get_project(project_id)
