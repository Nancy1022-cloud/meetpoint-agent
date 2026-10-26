"""M5接口只操作本地记忆；不会自动发起地图或模型请求。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.m1_models import UserId
from app.m1_store import StateError
from app.m5_models import (
    DeletePreferences, MemoryMessage, MemoryPatch, NewMemoryTask,
    RecoverRequest, SetPreferences,
)
from app.m5_store import MemoryStore

router = APIRouter(prefix="/api/m5", tags=["M5 Memory"])


def call(method, *args):
    try:
        return getattr(MemoryStore(), method)(*args)
    except StateError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": exc.message, **exc.details,
        }) from None


@router.get("/health")
def health():
    return {"status": "ok", "milestone": "M5", "memory_schema_version": 1,
            "storage": call("inspect"), "parser": "explicit_grammar_v1"}


@router.post("/tasks", status_code=201)
def create(request: NewMemoryTask):
    return call("create_memory_task", request)


@router.get("/tasks/{session_id}")
def get(session_id: str, user_id: Annotated[UserId, Query()]):
    return call("get_memory", user_id, session_id)


@router.patch("/tasks/{session_id}")
def patch(session_id: str, request: MemoryPatch):
    return call("patch_memory_task", session_id, request)


@router.post("/tasks/{session_id}/message")
def message(session_id: str, request: MemoryMessage):
    return call("apply_message", session_id, request)


@router.post("/tasks/{session_id}/recover")
def recover(session_id: str, request: RecoverRequest):
    return call("recover_task", session_id, request)


@router.get("/tasks/{session_id}/events")
def events(session_id: str, user_id: Annotated[UserId, Query()]):
    return call("events", user_id, session_id)


@router.get("/preferences")
def read(user_id: Annotated[UserId, Query()]):
    return call("read_preferences", user_id)


@router.put("/preferences")
def save(request: SetPreferences):
    return call("save_preferences", request)


@router.delete("/preferences")
def delete(request: DeletePreferences):
    return call("delete_preferences", request)