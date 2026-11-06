"""Thin HTTP boundaries for the five implemented field-session operations."""

import json
import sqlite3
from collections.abc import Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from oilfield_energy.workflows.field_initialization.api import (
    DispatchControlCycle,
    InitializeRun,
    PrepareControlCycle,
    PrepareResynchronization,
    QueryRunStatus,
    ReportExecution,
    RequestPccTarget,
    ResumeRun,
    StopRun,
    SubmitSnapshot,
    UpdatePlanningInputs,
    decode_json,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    DispatchControlCycleRequest,
    ExecutionResponse,
    InitializationFailure,
    InitializeRequest,
    InitializeResponse,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
    PrepareResynchronizationRequest,
    QueryRunStatusRequest,
    ReportExecutionRequest,
    RequestPccTargetRequest,
    RequestPccTargetResponse,
    ResumeRunRequest,
    StopRunRequest,
    SubmitSnapshotRequest,
    SubmitSnapshotResponse,
    UpdatePlanningRequest,
    UpdatePlanningResponse,
)


class StrictJsonRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            content = await request.body()
            if len(content) > 8 * 1024 * 1024:
                return JSONResponse(status_code=413, content={"code": "MESSAGE_TOO_LARGE"})
            try:
                decode_json(content)
            except (ValueError, InitializationFailure) as exc:
                return JSONResponse(
                    status_code=422,
                    content={"code": "INVALID_MESSAGE", "message": str(exc)},
                )
            return await handler(request)

        return guarded


router = APIRouter(route_class=StrictJsonRoute)


def _initializer(request: Request) -> InitializeRun:
    return cast(InitializeRun, request.app.state.field_initializer)


@router.post(
    "/api/field/single-microgrid/initialize",
    response_model=InitializeResponse,
    responses={
        409: {"description": "Initialization prerequisite or request conflict"},
        422: {"description": "Invalid input message"},
        413: {"description": "Message exceeds 8 MiB"},
        503: {"description": "Session persistence unavailable"},
    },
)
async def initialize(
    request: Request,
    body: InitializeRequest,
    service: Annotated[InitializeRun, Depends(_initializer)],
):
    try:
        result = await run_in_threadpool(service.execute, body)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    return JSONResponse(
        status_code=200 if result.payload.outcome == "accepted" else 409,
        content=json.loads(result.model_dump_json()),
    )


def _planning_updater(request: Request) -> UpdatePlanningInputs:
    return cast(UpdatePlanningInputs, request.app.state.planning_updater)


@router.post(
    "/api/field/single-microgrid/planning-inputs",
    response_model=UpdatePlanningResponse,
    responses={
        202: {
            "model": UpdatePlanningResponse,
            "description": "Validated and staged; not activated",
        },
        409: {
            "model": UpdatePlanningResponse,
            "description": "Rejected update or conflict",
        },
        413: {"description": "Message exceeds 8 MiB"},
        503: {"description": "Session persistence unavailable"},
    },
)
async def update_planning_inputs(
    body: UpdatePlanningRequest,
    service: Annotated[UpdatePlanningInputs, Depends(_planning_updater)],
):
    try:
        result = await run_in_threadpool(service.execute, body)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    status = {"activated": 200, "staged": 202, "rejected": 409}[
        result.payload.data.activation_status
    ]
    return JSONResponse(status_code=status, content=result.model_dump(mode="json"))


def _target_requester(request: Request) -> RequestPccTarget:
    return cast(RequestPccTarget, request.app.state.pcc_target_requester)


@router.post(
    "/api/field/single-microgrid/pcc-target",
    response_model=RequestPccTargetResponse,
    responses={
        409: {
            "model": RequestPccTargetResponse,
            "description": "Target rejected or request conflict",
        },
        413: {"description": "Message exceeds 8 MiB"},
        503: {"description": "Session persistence unavailable"},
    },
)
async def request_pcc_target(
    body: RequestPccTargetRequest,
    service: Annotated[RequestPccTarget, Depends(_target_requester)],
):
    try:
        result = await run_in_threadpool(service.execute, body)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    return JSONResponse(
        status_code=200 if result.payload.outcome == "accepted" else 409,
        content=result.model_dump(mode="json"),
    )


def _snapshot_submitter(request: Request) -> SubmitSnapshot:
    return cast(SubmitSnapshot, request.app.state.snapshot_submitter)


@router.post(
    "/api/field/single-microgrid/snapshots",
    response_model=SubmitSnapshotResponse,
    responses={
        409: {
            "model": SubmitSnapshotResponse,
            "description": "Snapshot assessment failed or request conflict",
        },
        413: {"description": "Message exceeds 8 MiB"},
        503: {"description": "Session persistence unavailable"},
    },
)
async def submit_snapshot(
    body: SubmitSnapshotRequest,
    service: Annotated[SubmitSnapshot, Depends(_snapshot_submitter)],
):
    try:
        result = await run_in_threadpool(service.execute, body)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    return JSONResponse(
        status_code=200 if result.payload.outcome == "accepted" else 409,
        content=result.model_dump(mode="json"),
    )


def _cycle_preparer(request: Request) -> PrepareControlCycle:
    return cast(PrepareControlCycle, request.app.state.control_cycle_preparer)


@router.post(
    "/api/field/single-microgrid/control-cycles/prepare",
    response_model=PrepareControlCycleResponse,
    responses={
        409: {
            "model": PrepareControlCycleResponse,
            "description": "Cycle withheld or request conflict",
        },
        413: {"description": "Message exceeds 8 MiB"},
        503: {"description": "Session persistence unavailable"},
    },
)
async def prepare_control_cycle(
    body: PrepareControlCycleRequest,
    service: Annotated[PrepareControlCycle, Depends(_cycle_preparer)],
) -> JSONResponse:
    try:
        result = await run_in_threadpool(service.execute, body)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    return JSONResponse(
        status_code=200 if result.payload.outcome == "accepted" else 409,
        content=result.model_dump(mode="json"),
    )


async def _execute_field(execute: Callable[[], ExecutionResponse]) -> JSONResponse:
    try:
        result = await run_in_threadpool(execute)
    except (OSError, sqlite3.Error):
        return JSONResponse(status_code=503, content={"code": "SESSION_STORE_UNAVAILABLE"})
    return JSONResponse(
        status_code={"accepted": 200, "pending": 202, "rejected": 409}[result.payload.outcome],
        content=result.model_dump(mode="json"),
    )


EXECUTION_RESPONSES: dict[int | str, dict[str, object]] = {
    202: {"model": ExecutionResponse, "description": "Recorded; cycle remains pending"},
    409: {"model": ExecutionResponse, "description": "Execution or session prerequisite rejected"},
    413: {"description": "Message exceeds 8 MiB"},
    503: {"description": "Session persistence unavailable"},
}


@router.post(
    "/api/field/single-microgrid/control-cycles/dispatch",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def dispatch_control_cycle(body: DispatchControlCycleRequest, request: Request):
    return await _execute_field(
        lambda: cast(DispatchControlCycle, request.app.state.field_dispatcher).execute(body)
    )


@router.post(
    "/api/field/single-microgrid/execution-reports",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def report_execution(body: ReportExecutionRequest, request: Request):
    return await _execute_field(
        lambda: cast(ReportExecution, request.app.state.execution_reporter).execute(body)
    )


@router.post(
    "/api/field/single-microgrid/status",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def query_run_status(body: QueryRunStatusRequest, request: Request):
    return await _execute_field(
        lambda: cast(QueryRunStatus, request.app.state.field_status_query).execute(body)
    )


@router.post(
    "/api/field/single-microgrid/stop",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def stop_run(body: StopRunRequest, request: Request):
    return await _execute_field(
        lambda: cast(StopRun, request.app.state.field_stopper).execute(body)
    )


@router.post(
    "/api/field/single-microgrid/resynchronizations/prepare",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def prepare_resynchronization(body: PrepareResynchronizationRequest, request: Request):
    return await _execute_field(
        lambda: cast(PrepareResynchronization, request.app.state.recovery_preparer).execute(body)
    )


@router.post(
    "/api/field/single-microgrid/resume",
    response_model=ExecutionResponse,
    responses=EXECUTION_RESPONSES,
)
async def resume_run(body: ResumeRunRequest, request: Request):
    return await _execute_field(
        lambda: cast(ResumeRun, request.app.state.run_resumer).execute(body)
    )
