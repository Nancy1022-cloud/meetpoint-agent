from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.m1_models import (
    CreateTask,
    FinishRun,
    Reservation,
    ReserveRequest,
    StartRun,
    UpdateTask,
    UserId,
)
from app.m1_store import StateError, StateStore


router = APIRouter(
    prefix="/api/m1",
    tags=["M1 条件与状态"],
)


def call_store(method, *args):
    try:
        return getattr(StateStore(), method)(*args)

    except StateError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={
                "code": exc.code,
                "message": exc.message,
                **exc.details,
            },
        ) from None


@router.get("/health")
def m1_health():
    return {
        "status": "ok",
        "milestone": "M1",
        "storage": call_store("inspect"),
        "note": "M1 接口只操作本机条件与预算，不发起外部请求。",
    }


@router.post("/tasks", status_code=201)
def create_task(request: CreateTask):
    return call_store("create_task", request)


@router.get("/tasks/{session_id}")
def get_task(
    session_id: str,
    user_id: Annotated[UserId, Query()],
):
    return call_store("get_task", user_id, session_id)


@router.patch("/tasks/{session_id}")
def update_task(session_id: str, request: UpdateTask):
    return call_store("update_task", session_id, request)


@router.post("/tasks/{session_id}/runs", status_code=201)
def start_run(session_id: str, request: StartRun):
    return call_store("start_run", session_id, request)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    user_id: Annotated[UserId, Query()],
):
    return call_store("get_run", user_id, run_id)


@router.post("/runs/{run_id}/reserve")
def reserve(run_id: str, request: ReserveRequest):
    reservation = Reservation.model_validate(
        request.model_dump(exclude={"user_id"})
    )

    return call_store(
        "reserve",
        request.user_id,
        run_id,
        reservation,
    )


@router.post("/runs/{run_id}/finish")
def finish(run_id: str, request: FinishRun):
    return call_store(
        "finish_run",
        request.user_id,
        run_id,
        request.reason,
    )