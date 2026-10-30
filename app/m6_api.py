"""网页与原M5接口同源；浏览器不接触API Key。"""
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from app.m1_models import UserId
from app.m1_store import StateError
from app.m6_models import Actor, OriginChoice, WebPlan
from app.m6_runtime import JobManager

router = APIRouter(tags=["M6 Web"])


@lru_cache
def manager():
    return JobManager()


def call(method, *args):
    try:
        return getattr(manager(), method)(*args)
    except StateError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": exc.message, **exc.details}) from None


@router.get("/demo", include_in_schema=False)
def page():
    return FileResponse(Path(__file__).parent / "static" / "meetpoint.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/m6/health")
def health():
    return {"status": "ok", "milestone": "M6", "worker_mode": "single_process_single_active_job",
            "origin_confirmation": "explicit_click", "result_storage": "memory_only",
            "result_ttl_seconds": 600, "requires_completed_m5": True}


@router.post("/api/m6/tasks/{session_id}/plan", status_code=202)
def plan(session_id: str, request: WebPlan):
    return call("start", session_id, request)


@router.get("/api/m6/jobs/{run_id}")
def job(run_id: str, user_id: Annotated[UserId, Query()]):
    return call("snapshot", user_id, run_id)


@router.post("/api/m6/jobs/{run_id}/choice")
def choice(run_id: str, request: OriginChoice):
    return call("choose", run_id, request)


@router.post("/api/m6/jobs/{run_id}/cancel")
def cancel(run_id: str, request: Actor):
    return call("cancel", request.user_id, run_id)