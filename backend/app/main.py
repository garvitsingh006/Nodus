from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel

from backend.app.agent.discovery import run_scoped_investigation
from backend.app.config import Settings
from backend.app.gateway.config import GatewayConfig
from backend.app.gateway.gateway import ModelGateway
from backend.app.gateway.gemini_client import GeminiProviderClient
from backend.app.governance.access_requests import (
    approve_request, create_access_request, deny_request, get_authorization_scope,
    list_pending_requests, list_requests_for_employee, revoke_grant,
)
from backend.app.governance.identity import (
    NEXUS_TENANT_ID, NEXUS_WORKSPACE_ID, authorized_source_ids_for, isolated_demo_mode_enabled,
    nexus_identity_summary, principal_authorizes_action, resolve_demo_principal,
)
from backend.app.governance.store import GovernanceStore
from backend.app.graph.authority import AuthorityRule
from backend.app.graph.driver import Neo4jStore
from backend.app.graph.models import SourceRecordInput
from backend.app.graph.repository import GraphRepository
from backend.app.ingestion.local_json import LocalImport, normalize
from backend.app.ingestion.github_public import GitHubImport, fetch_public_pull_requests, normalize_pull_request
from backend.app.security import Principal
from backend.app.skills.catalog import build_production_skill_registry
from backend.app.tools import register_all_tools
from backend.app.tools.executor import ToolExecutor
from backend.app.tools.registry import ToolDependencies, ToolRegistry

# Both NEXUS deployment identifiers are fixed in governance.identity.
# principal_from_token never accepts a request-supplied tenant or workspace.


def principal_from_token(token: str, governance_store: GovernanceStore | None = None) -> Principal | None:
    try:
        item = json.loads(os.getenv("GRAPH_HACKS_API_PRINCIPALS_JSON", "{}"))[token]
        found = Principal(
            subject=str(item["subject"]), tenant_id=str(item["tenant_id"]),
            workspaces=tuple(str(x) for x in item["workspaces"]),
            permissions=frozenset(str(x) for x in item["permissions"]),
            source_ids=tuple(str(x) for x in item.get("source_ids", [])),
        )
        return found
    except (KeyError, ValueError, TypeError):
        pass
    demo_mode = isolated_demo_mode_enabled()
    if governance_store is not None and demo_mode:
        return resolve_demo_principal(token, governance_store, tenant_id=NEXUS_TENANT_ID,
                                      workspace_id=NEXUS_WORKSPACE_ID, demo_mode_enabled=demo_mode)
    return None


def principal(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    parts = authorization.split(" ", 1) if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Bearer token required")
    governance_store = getattr(request.app.state, "governance_store", None)
    found = principal_from_token(parts[1].strip(), governance_store)
    if found is None:
        raise HTTPException(401, "Invalid credentials")
    return found


def require(permission: str):
    def dependency(user: Principal = Depends(principal)) -> Principal:
        if not user.permits(permission):
            raise HTTPException(403, "Permission denied")
        return user
    return dependency


def load_authority_rules() -> tuple[AuthorityRule, ...]:
    """Deterministic source/scope authority rules (R07), configured the same
    way as principals: an opaque JSON env var, never a reasoning service.
    Absent or invalid configuration means no rules apply — every conflict
    stays unresolved rather than guessed."""
    try:
        raw = json.loads(os.getenv("GRAPH_HACKS_AUTHORITY_RULES_JSON", "[]"))
        return tuple(AuthorityRule(**item) for item in raw)
    except (ValueError, TypeError):
        return ()


def repository(request: Request, user: Principal, workspace_id: str) -> GraphRepository:
    if workspace_id not in user.workspaces:
        raise HTTPException(404, "Workspace not available")
    is_admin = "ADMIN" in user.permissions
    return GraphRepository(request.app.state.graph_store, user.tenant_id, workspace_id,
                           user.subject, is_admin,
                           user.permits("READ_EVIDENCE"),
                           authority_rules=request.app.state.authority_rules,
                           authorized_source_ids=authorized_source_ids_for(user))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    store = Neo4jStore(settings)
    try:
        store.start()
        app.state.graph_store = store
        app.state.authority_rules = load_authority_rules()
        # Postgres/governance is optional: NEXUS demo-token resolution is
        # simply unavailable without it, exactly as before this feature
        # existed — it was never a hard requirement for the app to start.
        app.state.governance_store = None
        if settings.postgres_dsn:
            governance_store = GovernanceStore(settings.postgres_dsn)
            governance_store.start()
            app.state.governance_store = governance_store

        # Stage 11 (ND-11A): the deterministic tool/skill layer needs no
        # external credential and is always wired up; the real Gemini model
        # gateway is optional (only needed to actually drive an
        # investigation) and degrades to 503 at the endpoint, never a failed
        # app startup, when no API key is configured.
        tool_registry = ToolRegistry()
        register_all_tools(tool_registry)
        app.state.tool_registry = tool_registry
        app.state.skill_registry = build_production_skill_registry(tool_registry=tool_registry)
        app.state.tool_executor = ToolExecutor(tool_registry, ToolDependencies(
            graph_store=store, authority_rules=app.state.authority_rules,
            postgres_store=app.state.governance_store, governance_store=app.state.governance_store))
        app.state.model_gateway = None
        try:
            gateway_config = GatewayConfig.from_env()
            if gateway_config.api_key:
                app.state.model_gateway = ModelGateway(
                    GeminiProviderClient(gateway_config.api_key), gateway_config.routing,
                    gateway_config.pricing, gateway_config.retry_policy)
        except Exception:
            app.state.model_gateway = None  # never block API startup over optional model configuration
        yield
    finally:
        store.close()


app = FastAPI(title="NODUS Graph API", version="1.1.0", lifespan=lifespan)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@app.middleware("http")
async def request_id(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID", "")
    request.state.request_id = incoming if _REQUEST_ID.fullmatch(incoming) else str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


def error_response(request: Request, status: int, code: str, message: str,
                   details=None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "message": message,
        "request_id": getattr(request.state, "request_id", ""),
        "details": details,
    }})


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 422: "INVALID_REQUEST", 503: "DATABASE_UNAVAILABLE"}.get(exc.status_code, "REQUEST_FAILED")
    return error_response(request, exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
    return error_response(request, 422, "INVALID_REQUEST", "Request validation failed", details)


@app.exception_handler(ServiceUnavailable)
async def unavailable(request: Request, _exc: ServiceUnavailable):
    return error_response(request, 503, "DATABASE_UNAVAILABLE", "Graph database unavailable")


@app.exception_handler(Neo4jError)
async def database_error(request: Request, exc: Neo4jError):
    if "TransactionTimedOut" in str(getattr(exc, "code", "")):
        return error_response(request, 504, "QUERY_TIMEOUT", "Graph query timed out")
    return error_response(request, 503, "DATABASE_ERROR", "Graph database query failed")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _exc: Exception):
    return error_response(request, 500, "INTERNAL_ERROR", "An unexpected server error occurred")


@app.get("/health")
def health(request: Request):
    try:
        request.app.state.graph_store.read(lambda tx: tx.run("RETURN 1 AS ok").single()["ok"])
        return {"ready": True}
    except (Neo4jError, ServiceUnavailable) as exc:
        raise HTTPException(503, "Graph database unavailable") from exc


@app.get("/api/v1/session")
def session(user: Principal = Depends(principal)):
    return {"subject": user.subject, "tenant_id": user.tenant_id,
            "permissions": sorted(user.permissions)}


@app.get("/api/v1/workspaces")
def workspaces(user: Principal = Depends(require("READ_GRAPH"))):
    return {"items": [{"id": item, "name": item} for item in user.workspaces]}


@app.post("/api/v1/workspaces/{workspace_id}/records")
def ingest(workspace_id: str, record: SourceRecordInput, request: Request,
           user: Principal = Depends(require("MANAGE_INGESTION"))):
    repo = repository(request, user, workspace_id)
    if not user.permits_source(record.source_id):
        raise HTTPException(403, "Source is not authorized")
    try:
        return repo.ingest(record)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/workspaces/{workspace_id}/imports/json")
def import_json(workspace_id: str, document: LocalImport, request: Request,
                user: Principal = Depends(require("MANAGE_INGESTION"))):
    repo = repository(request, user, workspace_id)
    if not user.permits_source(document.source_id):
        raise HTTPException(403, "Source is not authorized")
    results = [repo.ingest(normalize(document.source_id, record)) for record in document.records]
    retracted = [
        record_id for record_id in document.deleted_record_ids
        if repo.retract_record(document.source_id, record_id)
    ]
    return {"source_id": document.source_id, "imported": results, "retracted": retracted}


@app.post("/api/v1/workspaces/{workspace_id}/imports/github")
def import_github(workspace_id: str, source: GitHubImport, request: Request,
                  user: Principal = Depends(require("MANAGE_INGESTION"))):
    repo = repository(request, user, workspace_id)
    if not user.permits_source(source.source_id):
        raise HTTPException(403, "Source is not authorized")
    try:
        records = [normalize_pull_request(source.source_id, item)
                   for item in fetch_public_pull_requests(source)]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (RuntimeError, KeyError, TypeError) as exc:
        raise HTTPException(502, "GitHub source unavailable") from exc
    return {"source_id": source.source_id, "imported": [repo.ingest(record) for record in records]}


@app.delete("/api/v1/workspaces/{workspace_id}/records/{source_id}/{record_id:path}")
def retract(workspace_id: str, source_id: str, record_id: str, request: Request,
            user: Principal = Depends(require("MANAGE_INGESTION"))):
    repo = repository(request, user, workspace_id)
    if not user.permits_source(source_id):
        raise HTTPException(403, "Source is not authorized")
    if not repo.retract_record(source_id, record_id):
        raise HTTPException(404, "Source record not found")
    return {"retracted": True}


@app.get("/api/v1/workspaces/{workspace_id}/search")
def search(workspace_id: str, request: Request,
           q: str = Query(min_length=1, max_length=200),
           limit: int = Query(25, ge=1, le=100),
           offset: int = Query(0, ge=0, le=10000),
           user: Principal = Depends(require("READ_GRAPH"))):
    return repository(request, user, workspace_id).search(q, limit, offset)


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}")
def entity(workspace_id: str, entity_id: str, request: Request,
           user: Principal = Depends(require("READ_GRAPH"))):
    result = repository(request, user, workspace_id).get_entity(entity_id)
    if result is None:
        raise HTTPException(404, "Entity not found")
    return result


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}/neighborhood")
def neighborhood(workspace_id: str, entity_id: str, request: Request,
                 limit: int = Query(50, ge=1, le=100), history: bool = False,
                 user: Principal = Depends(require("READ_GRAPH"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity(entity_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.neighborhood(entity_id, limit, history)


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}/reverse-dependencies")
def reverse_dependencies(workspace_id: str, entity_id: str, request: Request,
                         limit: int = Query(50, ge=1, le=100),
                         user: Principal = Depends(require("READ_GRAPH"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity(entity_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.reverse_dependencies(entity_id, limit)


@app.get("/api/v1/workspaces/{workspace_id}/relationships/{relation_id}")
def relationship(workspace_id: str, relation_id: str, request: Request,
                 user: Principal = Depends(require("READ_GRAPH"))):
    result = repository(request, user, workspace_id).relationship(relation_id)
    if result is None:
        raise HTTPException(404, "Relationship not found")
    return result


@app.get("/api/v1/workspaces/{workspace_id}/paths")
def paths(workspace_id: str, request: Request, source_id: str, target_id: str,
          max_depth: int = Query(3, ge=1, le=4), limit: int = Query(10, ge=1, le=20),
          user: Principal = Depends(require("READ_GRAPH"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity(source_id) is None or repo.get_entity(target_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.paths(source_id, target_id, max_depth, limit)


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}/evidence")
def evidence(workspace_id: str, entity_id: str, request: Request,
             limit: int = Query(50, ge=1, le=100),
             user: Principal = Depends(require("READ_EVIDENCE"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity(entity_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.evidence(entity_id, limit)


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}/history")
def history(workspace_id: str, entity_id: str, request: Request,
            limit: int = Query(50, ge=1, le=100),
            user: Principal = Depends(require("READ_GRAPH"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity_history(entity_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.neighborhood(entity_id, limit, True)


@app.get("/api/v1/workspaces/{workspace_id}/entities/{entity_id}/assertions")
def assertions(workspace_id: str, entity_id: str, request: Request,
               limit: int = Query(50, ge=1, le=100),
               user: Principal = Depends(require("READ_EVIDENCE"))):
    repo = repository(request, user, workspace_id)
    if repo.get_entity_history(entity_id) is None:
        raise HTTPException(404, "Entity not found")
    return repo.assertions(entity_id, limit)


@app.get("/api/v1/workspaces/{workspace_id}/graph-revision")
def graph_revision(workspace_id: str, request: Request,
                   user: Principal = Depends(require("READ_GRAPH"))):
    return {"revision": repository(request, user, workspace_id).graph_revision()}


@app.get("/api/v1/workspaces/{workspace_id}/sources")
def sources(workspace_id: str, request: Request, limit: int = Query(50, ge=1, le=100),
            user: Principal = Depends(require("READ_EVIDENCE"))):
    return repository(request, user, workspace_id).sources(limit)


class InvestigationRequestBody(BaseModel):
    objective: str
    selected_source_ids: list[str] = []
    entity_id: str | None = None


@app.post("/api/v1/workspaces/{workspace_id}/investigations")
async def investigate(workspace_id: str, body: InvestigationRequestBody, request: Request,
                      user: Principal = Depends(require("EXECUTE_TOOL"))):
    """Stage 11 (ND-11A) scoped entry point: ``objective`` (free text) and
    ``selected_source_ids`` come from the request body, but the acting
    principal is always the server-derived, already-authenticated ``user``
    from ``Depends(require(...))`` — never a caller-supplied identity. The
    effective run scope (selected ∩ authorized) is computed by
    ``run_scoped_investigation`` itself; an empty scope comes back as a
    ``BLOCKED`` result rather than running unscoped."""
    if workspace_id not in user.workspaces:
        raise HTTPException(404, "Workspace not available")
    model_gateway = getattr(request.app.state, "model_gateway", None)
    if model_gateway is None:
        raise HTTPException(503, "Model gateway is not configured for this deployment")
    result = await run_scoped_investigation(
        objective=body.objective, selected_source_ids=tuple(body.selected_source_ids), principal=user,
        workspace_id=workspace_id, run_id=str(uuid.uuid4()), graph_store=request.app.state.graph_store,
        model_gateway=model_gateway, tool_registry=request.app.state.tool_registry,
        tool_executor=request.app.state.tool_executor, skill_registry=request.app.state.skill_registry,
        authority_rules=request.app.state.authority_rules, entity_id=body.entity_id,
    )
    return result.model_dump(mode="json")


@app.get("/api/v1/nexus/session")
def nexus_session(request: Request, user: Principal = Depends(principal)):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None or not user.subject.startswith("nexus:"):
        raise HTTPException(404, "Not a NEXUS demo identity")
    # Re-deriving from the bearer token (not trusting `user.subject` as a
    # lookup key) keeps this endpoint honest about what actually
    # authenticated the caller.
    authorization = request.headers.get("authorization", "")
    token = authorization.split(" ", 1)[1].strip() if " " in authorization else ""
    summary = nexus_identity_summary(token, governance_store, demo_mode_enabled=isolated_demo_mode_enabled())
    if summary is None:
        raise HTTPException(404, "Not a NEXUS demo identity")
    return summary


@app.get("/api/v1/nexus/sources")
def nexus_sources(request: Request, user: Principal = Depends(principal)):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        return {"items": []}
    if not user.source_ids:
        return {"items": []}
    rows = governance_store.read("""
        SELECT source_id, stage, classification, owner_project_id FROM nexus_sources
        WHERE source_id = ANY(%s)
        ORDER BY source_id
        """, (list(user.source_ids),))
    return {"items": rows}


_NEXUS_COMPANY_ID = "nexus"


class AccessRequestBody(BaseModel):
    resource_id: str
    requested_actions: list[str] = []
    justification: str
    requested_project_id: str | None = None
    requested_duration_hours: float | None = None


class ApproveAccessRequestBody(BaseModel):
    approved_resource_id: str | None = None
    approved_actions: list[str] | None = None
    approved_project_id: str | None = None
    approved_duration_hours: float | None = None


class DenyAccessRequestBody(BaseModel):
    reason: str | None = None


class RevokeGrantBody(BaseModel):
    reason: str | None = None


def _nexus_employee_id(user: Principal) -> str:
    return user.subject.removeprefix("nexus:")


@app.post("/api/v1/nexus/access-requests")
def nexus_create_access_request(body: AccessRequestBody, request: Request,
                                user: Principal = Depends(principal)):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        raise HTTPException(404, "NEXUS governance store not available")
    try:
        return create_access_request(
            governance_store, requester_employee_id=_nexus_employee_id(user),
            company_id=_NEXUS_COMPANY_ID, resource_id=body.resource_id,
            requested_actions=body.requested_actions, justification=body.justification,
            requested_project_id=body.requested_project_id,
            requested_duration_hours=body.requested_duration_hours)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/nexus/access-requests/mine")
def nexus_my_access_requests(request: Request, user: Principal = Depends(principal)):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        return {"items": []}
    items = list_requests_for_employee(
        governance_store, employee_id=_nexus_employee_id(user), company_id=_NEXUS_COMPANY_ID)
    return {"items": items}


@app.get("/api/v1/nexus/access-requests/pending")
def nexus_pending_access_requests(request: Request, user: Principal = Depends(require("ADMIN"))):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        return {"items": []}
    items = list_pending_requests(governance_store, company_id=_NEXUS_COMPANY_ID)
    return {"items": items}


@app.post("/api/v1/nexus/access-requests/{request_id}/approve")
def nexus_approve_access_request(request_id: str, body: ApproveAccessRequestBody, request: Request,
                                 user: Principal = Depends(require("ADMIN"))):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        raise HTTPException(404, "NEXUS governance store not available")
    try:
        return approve_request(
            governance_store, request_id=request_id, company_id=_NEXUS_COMPANY_ID,
            approver_employee_id=_nexus_employee_id(user),
            approved_resource_id=body.approved_resource_id, approved_actions=body.approved_actions,
            approved_project_id=body.approved_project_id,
            approved_duration_hours=body.approved_duration_hours)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/nexus/access-requests/{request_id}/deny")
def nexus_deny_access_request(request_id: str, body: DenyAccessRequestBody, request: Request,
                              user: Principal = Depends(require("ADMIN"))):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        raise HTTPException(404, "NEXUS governance store not available")
    try:
        return deny_request(
            governance_store, request_id=request_id, company_id=_NEXUS_COMPANY_ID,
            approver_employee_id=_nexus_employee_id(user), reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/nexus/grants/{grant_id}/revoke")
def nexus_revoke_grant(grant_id: str, body: RevokeGrantBody, request: Request,
                       user: Principal = Depends(require("ADMIN"))):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        raise HTTPException(404, "NEXUS governance store not available")
    try:
        return revoke_grant(
            governance_store, grant_id=grant_id, company_id=_NEXUS_COMPANY_ID,
            approver_employee_id=_nexus_employee_id(user), reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/nexus/authorization-scope")
def nexus_authorization_scope(request: Request, user: Principal = Depends(principal)):
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        raise HTTPException(404, "NEXUS governance store not available")
    return get_authorization_scope(
        governance_store, employee_id=_nexus_employee_id(user), company_id=_NEXUS_COMPANY_ID)


def _require_nexus_action(request: Request, user: Principal, resource_id: str, action: str) -> None:
    """Action-level authorization boundary (Stage 09 gap-fix), shared with the
    agent-tool execution path's equivalent decision
    (``backend.app.governance.identity.authorized_actions_for``). A source-id
    level grant (``permits_source``) is necessary but no longer sufficient: a
    grant approved for a narrower action set than ``action`` (e.g. READ-only)
    must not authorize this call. No-op for non-NEXUS principals or when no
    governance store is configured, preserving prior behavior exactly for
    every caller this change was never meant to affect."""
    if user.tenant_id != NEXUS_TENANT_ID or not user.subject.startswith("nexus:"):
        return
    governance_store = getattr(request.app.state, "governance_store", None)
    if governance_store is None:
        return
    if not principal_authorizes_action(user, resource_id, action, governance_store):
        raise HTTPException(403, "Action not authorized for this resource")


@app.get("/api/v1/workspaces/{workspace_id}/records/{source_id}/{record_id:path}")
def source_record(workspace_id: str, source_id: str, record_id: str, request: Request,
                  user: Principal = Depends(require("READ_EVIDENCE"))):
    _require_nexus_action(request, user, source_id, "READ")
    result = repository(request, user, workspace_id).source_record(source_id, record_id)
    if result is None:
        raise HTTPException(404, "Source record not found")
    return result
