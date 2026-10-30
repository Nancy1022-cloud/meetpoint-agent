"""Actual model attempts, including failures; share M8's cumulative money ledger."""
import json
import time
from types import SimpleNamespace
from uuid import uuid4
from app.m8_trace import INPUT_CAP, OUTPUT_CAP, BudgetStop, valid_usage

class MemoryClient:
    def __init__(self,delegate,trace,ledger=None):
        self.delegate,self.trace,self.ledger=delegate,trace,ledger
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self,**kwargs):
        bound=len(json.dumps(kwargs,ensure_ascii=False).encode("utf-8"))+4096
        if bound>INPUT_CAP or kwargs.get("max_tokens",OUTPUT_CAP)>OUTPUT_CAP:
            raise BudgetStop("request_reservation_bound_exceeded")
        aid=uuid4().hex
        if self.ledger:self.ledger.reserve(aid)
        row={"attempt_id":aid,"purpose":"memory_parse","usage":None,"returned_model":None,
             "status":"started","error":None}
        self.trace.llm.append(row);self.trace.emit("llm_start",**row)
        started=time.perf_counter()
        try:
            response=self.delegate.chat.completions.create(**kwargs)
            u=getattr(response,"usage",None)
            row["usage"]=u.model_dump(exclude_none=True) if hasattr(u,"model_dump") else u
            row["returned_model"]=getattr(response,"model",None);row["status"]="returned"
            if self.ledger:self.ledger.settle(aid,row["usage"])
            if valid_usage(row["usage"]) and (row["usage"]["prompt_tokens"]>INPUT_CAP or row["usage"]["completion_tokens"]>OUTPUT_CAP):
                self.trace.complete=False
                raise BudgetStop("provider_usage_exceeds_reservation")
            return response
        except Exception as exc:
            row.update(status="error",error=type(exc).__name__,http_status=getattr(exc,"status_code",None))
            raise
        finally:
            row["elapsed_seconds"]=round(time.perf_counter()-started,6)
            self.trace.emit("llm_end",**row)

    def close(self):self.delegate.close()

def usage_summary(attempts):
    known=[a for a in attempts if valid_usage(a.get("usage"))]
    all_known=len(known)==len(attempts)
    totals={k:sum(a["usage"][k] for a in known) for k in ("prompt_tokens","completion_tokens","total_tokens")}
    return {"attempts":len(attempts),"observed_usage_attempts":len(known),
        "usage_coverage":len(known)/len(attempts) if attempts else None,
        "usage_complete":all_known,"tokens":totals if all_known else None,"known_tokens":totals}
