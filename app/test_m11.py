"""Offline correctness contracts; never call the real model or Amap."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.m1_store import StateError
from app.m5_models import MemoryPatch
from app.m5_store import flatten
from app.m8_trace import Trace, BudgetStop, SpendLedger, read_jsonl
from app.m11_models import Proposal, Operation, clarification
from app.m11_parse import validated, rule_proposal, LLMParser, changes
from app.m11_scope import scope_decision
from app.m11_store import ProposalStore
from app.m11_usage import MemoryClient, usage_summary
from app.m11_cases import development_cases
from app.m11_eval import run_case, grade, summarize, ORIGINS
from app.m11_api import router

def proposal(operations):
    return Proposal(intent="change",operations=[Operation(**v) for v in operations],question_code="none")

def op(path="person_b.max_duration_seconds",value=3600,scope="current",evidence="他最多一小时"):
    return {"scope":scope,"path":path,"value":value,"evidence":evidence}

class FakeClient:
    def __init__(self,data=None,usage=True,error=None):
        self.data=data or proposal([op()]).model_dump();self.usage=usage;self.error=error;self.requests=[]
        self.chat=NS(completions=NS(create=self.create));self.closed=False
    def create(self,**kwargs):
        self.requests.append(kwargs)
        if self.error:raise self.error
        return NS(model="fake-model",usage=NS(model_dump=lambda **kw:{"prompt_tokens":200,"completion_tokens":40,"total_tokens":240}) if self.usage else None,
            choices=[NS(finish_reason="tool_calls",message=NS(tool_calls=[NS(function=NS(name="propose_memory_change",arguments=json.dumps(self.data,ensure_ascii=False)))]))])
    def close(self):self.closed=True

class ParseTests(unittest.TestCase):
    def test_role_and_units(self):
        p=validated(proposal([op()]),"他最多一小时")
        self.assertEqual(changes(p)[0],{"person_b":{"max_duration_seconds":3600}})
    def test_half_hour(self):
        self.assertEqual(changes(validated(proposal([op(value=1800,evidence="他半小时")]),"他半小时"))[0]["person_b"]["max_duration_seconds"],1800)
    def test_hour_and_half(self):
        validated(proposal([op(value=5400,evidence="他一个半小时")]),"他一个半小时")
    def test_decimal_kilometres(self):
        validated(proposal([op(path="person_a.max_walk_meters",value=1500,evidence="我1.5公里")]),"我1.5公里")
    def test_evidence_must_come_from_message(self):
        with self.assertRaises(ValueError):validated(proposal([op()]),"我最多30分钟")
    def test_role_missing(self):
        with self.assertRaises(ValueError):validated(proposal([op(evidence="最多一小时")]),"最多一小时")
    def test_value_not_mentioned(self):
        with self.assertRaises(ValueError):validated(proposal([op(value=3000)]),"他最多一小时")
    def test_default_requires_long_term_intent(self):
        with self.assertRaises(ValueError):validated(proposal([op(path="person_a.max_duration_seconds",scope="defaults",evidence="我一小时")]),"我一小时")
    def test_negative_default(self):
        text="我一小时，别记成默认"
        with self.assertRaises(ValueError):validated(proposal([op(path="person_a.max_duration_seconds",scope="defaults",evidence=text)]),text)
    def test_friend_defaults_rejected(self):
        with self.assertRaises(ValueError):validated(proposal([op(scope="defaults",evidence="以后默认他一小时")]),"以后默认他一小时")
    def test_implicit_owner_default(self):
        validated(proposal([op(path="person_a.max_transfers",value=0,scope="defaults",evidence="以后默认最多换乘零次")]),"以后默认最多换乘零次")
    def test_origin_cannot_be_default(self):
        with self.assertRaises(ValueError):changes(proposal([op(path="person_a.origin_text",value="北苑站",scope="defaults",evidence="以后默认我从北苑站出发")]))
    def test_conflict_rejected(self):
        with self.assertRaises(ValueError):changes(proposal([op(),op(value=1800)]))
    def test_boolean_number_rejected(self):
        with self.assertRaises(ValidationError):proposal([op(value=True)])
    def test_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):proposal([op(path="person_a.user_id")])
    def test_no_write_in_clarification(self):
        with self.assertRaises(ValidationError):Proposal(intent="clarify",operations=[Operation(**op())],question_code="role")
    def test_controlled_grammar_baseline(self):
        self.assertEqual(changes(rule_proposal("我这次接受60分钟"))[0],{"person_a":{"max_duration_seconds":3600}})

class ScopeTests(unittest.TestCase):
    def test_drive_synonym(self):self.assertEqual(scope_decision("把我的出行方式改成驾车。")["decision"],"refuse")
    def test_negative_drive(self):self.assertEqual(scope_decision("不要改成驾车，我这次接受60分钟")["decision"],"continue")
    def test_reported_drive(self):self.assertEqual(scope_decision("他说改成驾车")["decision"],"continue")
    def test_quoted_drive(self):self.assertEqual(scope_decision("他说‘改成驾车’，我这次接受60分钟")["decision"],"continue")
    def test_hypothetical_drive(self):self.assertEqual(scope_decision("如果改成驾车会怎样？")["decision"],"continue")

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.time=100.
        self.store=ProposalStore(Path(self.temp.name)/"state.db",clock=lambda:self.time,ttl=10)
        self.m=self.store.create_memory_task({"user_id":"u",**ORIGINS});self.sid=self.m["task"]["session_id"]
        self.req={"user_id":"u","expected_version":1,"expected_preferences_revision":0,"text":"他最多一小时","parser":"llm"}
        self.confirm_req={k:self.req[k] for k in ("user_id","expected_version","expected_preferences_revision")}
    def tearDown(self):self.temp.cleanup()
    def preview(self,p=None):return self.store.preview(self.sid,self.req,lambda text,memory:p or proposal([op()]))
    def memory(self):return self.store.get_memory("u",self.sid)
    def confirm(self,p):return self.store.confirm(self.sid,p["proposal_id"],self.confirm_req)
    def test_preview_does_not_write(self):
        before=self.memory();p=self.preview();after=self.memory()
        self.assertEqual(p["status"],"awaiting_confirmation");self.assertEqual(before,after)
    def test_only_b_changed(self):
        p=self.preview();result=self.confirm(p)
        self.assertEqual(result["task"]["requirements"]["person_b"]["max_duration_seconds"],3600)
        self.assertEqual(result["task"]["requirements"]["person_a"],self.m["task"]["requirements"]["person_a"])
        self.assertEqual(result["preferences"]["values"],{});self.assertTrue(result["replan_required"])
    def test_default_not_current(self):
        p=self.preview(proposal([op(path="person_a.max_transfers",scope="defaults",value=0)]))
        result=self.confirm(p)
        self.assertEqual(result["task"]["requirements"],self.m["task"]["requirements"])
        self.assertEqual(result["preferences"]["values"],{"max_transfers":0});self.assertFalse(result["replan_required"])
    def test_new_session_inherits_only_a(self):
        self.test_default_not_current();m=self.store.create_memory_task({"user_id":"u",**ORIGINS})
        self.assertEqual(m["task"]["requirements"]["person_a"]["max_transfers"],0)
        self.assertEqual(m["task"]["requirements"]["person_b"]["max_transfers"],1)
    def test_delete_default_keeps_current(self):
        self.test_default_not_current()
        self.store.delete_preferences({"user_id":"u","expected_revision":1,"keys":["max_transfers"]})
        self.assertEqual(self.store.read_preferences("u")["values"],{})
        self.assertEqual(self.memory()["task"]["requirements"],self.m["task"]["requirements"])
    def test_cancel(self):
        p=self.preview();self.store.cancel(self.sid,p["proposal_id"],"u")
        with self.assertRaises(StateError):self.confirm(p)
        self.assertEqual(self.memory()["task"]["constraints_version"],1)
    def test_expired(self):
        p=self.preview();self.time=110.
        with self.assertRaises(StateError) as e:self.confirm(p)
        self.assertEqual(e.exception.code,"proposal_expired")
    def test_idempotent_confirm(self):
        p=self.preview();self.confirm(p);r=self.confirm(p)
        self.assertEqual(r["status"],"already_applied");self.assertEqual(r["task"]["constraints_version"],2)
    def test_stale_constraints(self):
        p=self.preview();self.store.patch_memory_task(self.sid,{"user_id":"u","expected_version":1,"changes":{"person_a":{"max_transfers":2}}})
        with self.assertRaises(StateError):self.confirm(p)
        self.assertEqual(self.memory()["task"]["requirements"]["person_b"]["max_duration_seconds"],2700)
    def test_stale_defaults(self):
        p=self.preview();self.store.save_preferences({"user_id":"u","expected_revision":0,"values":{"max_walk_meters":800}})
        with self.assertRaises(StateError):self.confirm(p)
        self.assertEqual(self.memory()["task"]["constraints_version"],1)
    def test_cross_user(self):
        p=self.preview()
        with self.assertRaises(StateError) as e:self.store.confirm(self.sid,p["proposal_id"],{**self.confirm_req,"user_id":"other"})
        self.assertEqual(e.exception.status,404)
    def test_cross_session(self):
        p=self.preview();sid=self.store.create_memory_task({"user_id":"u",**ORIGINS})["task"]["session_id"]
        with self.assertRaises(StateError):self.store.confirm(sid,p["proposal_id"],self.confirm_req)
    def test_model_response_race(self):
        def parser(text,memory):
            self.store.patch_memory_task(self.sid,{"user_id":"u","expected_version":1,"changes":{"person_a":{"max_transfers":2}}})
            return proposal([op()])
        with self.assertRaises(StateError):self.store.preview(self.sid,self.req,parser)
        self.assertEqual(self.store.history("u",self.sid),[])
    def test_defaults_and_current_rollback_together(self):
        p=self.preview(proposal([op(),op(path="person_a.max_transfers",scope="defaults",value=0)]))
        with patch.object(self.store,"_patch",side_effect=RuntimeError("forced database failure")):
            with self.assertRaises(RuntimeError):self.confirm(p)
        self.assertEqual(self.store.read_preferences("u")["values"],{})
        self.assertEqual(self.store.history("u",self.sid)[0]["status"],"pending")
    def test_two_concurrent_confirmations(self):
        a=self.preview();b=self.preview(proposal([op(value=1800)]))
        def apply(p):
            try:self.confirm(p);return "ok"
            except StateError:return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(apply,[a,b]))
        self.assertCountEqual(answers,["ok","stale"])
    def test_refusal_no_parser_or_write(self):
        req={**self.req,"text":"把出行方式改成驾车"}
        result=self.store.preview(self.sid,req,lambda *a:(_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(result["status"],"refused");self.assertEqual(self.memory()["task"]["constraints_version"],1)
    def test_raw_text_not_persisted(self):
        self.req["text"]="unique_raw_sentence_9876";p=self.preview()
        with self.store.connection() as conn:
            text="".join(str(tuple(r)) for r in conn.execute("SELECT * FROM m11_proposals"))
        self.assertNotIn("unique_raw_sentence_9876",text)

class ModelTests(unittest.TestCase):
    def test_schema_and_usage(self):
        trace=Trace("t","r");fake=FakeClient();client=MemoryClient(fake,trace)
        p=LLMParser(Settings(llm_model="qwen3.7-flash"),client)("他最多一小时",{"task":{"requirements":ORIGINS},"preferences":{"values":{}}})
        self.assertEqual(p.intent,"change");self.assertEqual(usage_summary(trace.llm)["tokens"]["total_tokens"],240)
        self.assertFalse(fake.requests[0]["extra_body"]["enable_thinking"])
        self.assertEqual(trace.llm[0]["purpose"],"memory_parse")
    def test_missing_usage_unknown(self):
        trace=Trace("t","r");MemoryClient(FakeClient(usage=False),trace).create(max_tokens=512)
        self.assertIsNone(usage_summary(trace.llm)["tokens"])
    def test_failed_request_counted(self):
        trace=Trace("t","r")
        with self.assertRaises(RuntimeError):MemoryClient(FakeClient(error=RuntimeError()),trace).create(max_tokens=512)
        self.assertEqual(len(trace.llm),1);self.assertFalse(usage_summary(trace.llm)["usage_complete"])
    def test_budget_stops_before_model(self):
        class Ledger:
            def reserve(self,*args):raise BudgetStop("limit")
        f=FakeClient()
        with self.assertRaises(BudgetStop):MemoryClient(f,Trace("t","r"),Ledger()).create(max_tokens=512)
        self.assertEqual(f.requests,[])
    def test_invalid_model_value_clarifies(self):
        client=MemoryClient(FakeClient(data=proposal([op(value=3000)]).model_dump()),Trace("t","r"))
        p=LLMParser(Settings(),client)("他最多一小时",{"task":{"requirements":ORIGINS},"preferences":{"values":{}}})
        self.assertEqual(p.intent,"clarify")
    def test_no_answer_in_prompt(self):
        f=FakeClient();LLMParser(Settings(),f)("他最多一小时",{"task":{"requirements":ORIGINS,"user_id":"secret_id"},"preferences":{"values":{}},"expected":"oracle"})
        text=json.dumps(f.requests,ensure_ascii=False)
        self.assertNotIn("secret_id",text);self.assertNotIn("oracle",text)

class EvaluationTests(unittest.TestCase):
    def test_all_cases_have_unique_ids(self):
        cases=development_cases();self.assertEqual(len(cases),36);self.assertEqual(len({c["id"] for c in cases}),36)
    def test_grade_rebuilds_expected(self):
        c=development_cases()[0];r=run_case(c,"RULE","t")
        self.assertEqual(grade(r,c),[])
        r["turns"][0]["actual"]["current"]["person_b.max_transfers"]=5;r["turns"][0]["current_correct"]=True
        self.assertIn("turn_0:current",grade(r,c))
    def test_wrong_proposal_is_confirmed_then_fails(self):
        c=development_cases()[0];r=run_case(c,"NL","t",scripted_parser=lambda *args:proposal([op()]))
        self.assertEqual(r["turns"][0]["status"],"applied");self.assertTrue(grade(r,c))
    def test_missing_planned_trial_fails(self):
        c=development_cases()[0];m={"plan":[{"trial_id":"t","case_id":c["id"],"policy":"NL"}],"policies":["NL"],"batch_id":"b","mode":"llm","stage":"dev"}
        s=summarize(m,[c],[])
        self.assertFalse(s["measurement_complete"]);self.assertEqual(s["policies"]["NL"]["automatic_acceptance_rate"],0)
    def test_scripted_not_model_success(self):
        c=development_cases()[2];r=run_case(c,"NL","t")
        self.assertEqual(r["attempts"],[]);self.assertTrue(grade(r,c))

class APITests(unittest.TestCase):
    def test_preview_confirm_and_history(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{"MEETPOINT_DB_PATH":str(Path(temp)/"state.db")}):
            store=ProposalStore();m=store.create_memory_task({"user_id":"u",**ORIGINS});sid=m["task"]["session_id"]
            app=FastAPI();app.include_router(router);client=TestClient(app)
            req={"user_id":"u","expected_version":1,"expected_preferences_revision":0,"text":"我这次接受60分钟","parser":"rule"}
            p=client.post(f"/api/m11/tasks/{sid}/preview",json=req)
            self.assertEqual(p.status_code,200);pid=p.json()["proposal_id"]
            self.assertEqual(store.get_memory("u",sid)["task"]["constraints_version"],1)
            r=client.post(f"/api/m11/tasks/{sid}/proposals/{pid}/confirm",json={k:req[k] for k in ("user_id","expected_version","expected_preferences_revision")})
            self.assertEqual(r.json()["task"]["constraints_version"],2)
            self.assertEqual(len(client.get(f"/api/m11/tasks/{sid}/proposals?user_id=u").json()),1)
    def test_invalid_actor_rejected(self):
        app=FastAPI();app.include_router(router);client=TestClient(app)
        r=client.post("/api/m11/tasks/x/preview",json={"user_id":"bad user","expected_version":1,"expected_preferences_revision":0,"text":"x"})
        self.assertEqual(r.status_code,422)

class InstallerTests(unittest.TestCase):
    def test_idempotent_and_rollback(self):
        import apply_m11_changes as installer
        root=Path(__file__).resolve().parents[1]
        manifest=json.loads((root/"M11_patch_manifest.json").read_text(encoding="utf-8-sig"))
        with tempfile.TemporaryDirectory() as temp:
            dst=Path(temp)
            for name in [*manifest["files"],"app/main.py","app/m6_runtime.py","M11_patch_manifest.json"]:
                target=dst/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((root/name).read_bytes())
            for name,spec in manifest["files"].items():
                target=dst/name;text=target.read_text(encoding="utf-8-sig")
                if installer.sha(text)==spec["after"]:
                    for rep in reversed(spec["replacements"]):text=text.replace(rep["new"],rep["old"])
                target.write_text(text,encoding="utf-8")
            p=dst/"app/main.py";p.write_text('from fastapi import FastAPI\napp = FastAPI()\n',encoding="utf-8")
            original={n:(dst/n).read_bytes() for n in [*manifest["files"],"app/main.py"]}
            installer.apply(dst);installed={n:(dst/n).read_bytes() for n in original}
            installer.apply(dst);self.assertEqual(installed,{n:(dst/n).read_bytes() for n in original})
            installer.apply(dst,True);self.assertEqual(original,{n:(dst/n).read_bytes() for n in original})

if __name__=="__main__":unittest.main()
