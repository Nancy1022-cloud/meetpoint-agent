"""Explicit preview requests alone can invoke Qwen; loading the page is free."""
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Query
from app.config import get_settings
from app.m1_store import StateError
from app.m8_trace import Trace, SpendLedger, BudgetStop, append_json, exclusive_run
from app.m11_models import PreviewRequest, ConfirmRequest, Actor
from app.m11_parse import rule_proposal, LLMParser
from app.m11_store import ProposalStore
from app.m11_usage import MemoryClient, usage_summary

router=APIRouter(prefix="/api/m11",tags=["M11 Memory"])

def call(method,*args):
    try:return getattr(ProposalStore(),method)(*args)
    except StateError as exc:
        raise HTTPException(status_code=exc.status,detail={"code":exc.code,"message":exc.message,**exc.details}) from None

@router.get("/health")
def health():
    return {"status":"ok","milestone":"M11","confirmation_required":True,"proposal_ttl_seconds":600,
        "roles":{"A":"我","B":"朋友/对方"},"map_requests":0,"raw_chat_persisted":False}

@router.post("/tasks/{session_id}/preview")
def preview(session_id:str,request:PreviewRequest):
    if request.parser=="rule":return call("preview",session_id,request,rule_proposal)
    settings=get_settings()
    if not settings.llm_api_key.get_secret_value().strip():
        raise HTTPException(422,detail={"code":"missing_llm_key","message":"请先填写已有的LLM_API_KEY。"})
    from openai import OpenAI
    trace=Trace(uuid4().hex,uuid4().hex,lambda r:append_json(Path("data/m11/v1/web_events.jsonl"),r))
    client=None
    try:
        with exclusive_run("data/m8/v1"):
            ledger=SpendLedger("data/m8/v1","m11_web",batch_limit=.5,total_limit=5.)
            delegate=OpenAI(api_key=settings.llm_api_key.get_secret_value(),base_url=settings.llm_base_url,
                            timeout=settings.llm_timeout_seconds,max_retries=0)
            client=MemoryClient(delegate,trace,ledger)
            result=call("preview",session_id,request,LLMParser(settings,client))
            return {**result,"model_usage":usage_summary(trace.llm)}
    except BudgetStop as exc:
        raise HTTPException(409,detail={"code":"memory_model_budget","message":"模型预算或请求上限已触发；预览未写入，请查看费用记录。"}) from None
    except (BlockingIOError,PermissionError):
        raise HTTPException(409,detail={"code":"model_ledger_busy","message":"另一批评测或解析正在使用共用账本，请稍后重试。"}) from None
    except HTTPException:raise
    except Exception:
        raise HTTPException(502,detail={"code":"memory_model_error","message":"本次模型请求未完成；未应用修改，失败尝试已保留。"}) from None
    finally:
        if client:client.close()

@router.post("/tasks/{session_id}/proposals/{proposal_id}/confirm")
def confirm(session_id:str,proposal_id:str,request:ConfirmRequest):
    return call("confirm",session_id,proposal_id,request)

@router.post("/tasks/{session_id}/proposals/{proposal_id}/cancel")
def cancel(session_id:str,proposal_id:str,request:Actor):
    return call("cancel",session_id,proposal_id,request.user_id)

@router.get("/tasks/{session_id}/proposals")
def history(session_id:str,user_id:str=Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")):
    return call("history",user_id,session_id)
