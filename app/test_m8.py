"""M8判分语义与故障反例；不访问真实模型/高德。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx
import openai

from app.config import Settings
from app.m8 import make_report, no_network
from app.m8_cases import calibration_ids, digest, independently_has_solution, suite
from app.m8_evidence import bind_claims
from app.m8_eval import run_case
from app.m8_metrics import cost_summary, grade, summarize
from app.m8_trace import (AuditedClient, BudgetStop, CALL_RESERVE_YUAN, SpendLedger, Trace,
                          cost_yuan, valid_usage)


def usage(p=100,c=20):
    return {"prompt_tokens":p,"completion_tokens":c,"total_tokens":p+c}


class FakeClient:
    def __init__(self, outputs):
        self.outputs=iter(outputs)
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.calls=0
    def create(self,**kwargs):
        self.calls+=1
        result=next(self.outputs)
        if isinstance(result,BaseException): raise result
        return result
    def close(self): pass


def response(action="expand", reported=None):
    u=usage() if reported is None else reported
    return SimpleNamespace(model="qwen3.7-flash-2026-07-15",
        usage=SimpleNamespace(model_dump=lambda **kwargs:u),
        choices=[SimpleNamespace(finish_reason="tool_calls",message=SimpleNamespace(tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name="choose_next_action",
                 arguments=json.dumps({"action":action,"reason_code":"few_candidates","question_code":"none"}))) ]))])


class M8Semantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=suite()
        cls.case=deepcopy(cls.data["dev"][0])
        cls.events=[]
        with no_network():
            cls.row=bind_claims(run_case(cls.case,"V0","scripted","t1",sink=cls.events.append))

    def summary(self, rows=None, events=None):
        manifest={"trials":[{"trial_id":"t1","case_id":self.case["id"],"policy":"V0","mode":"scripted"}]}
        return summarize(manifest,[self.case],deepcopy([self.row] if rows is None else rows),
                         deepcopy(self.events if events is None else events))

    def test_suite_counts_and_groups(self):
        self.assertEqual([len(self.data[s]) for s in ("dev","holdout")],[50,200])
        self.assertEqual({g:sum(c["group"]==g for c in self.data["holdout"])
                          for g in ("planning","refusal","abnormal","memory")},
                         {"planning":80,"refusal":40,"abnormal":40,"memory":40})
        ids=[c["id"] for s in ("dev","holdout") for c in self.data[s]]
        self.assertEqual(len(ids),len(set(ids)))

    def test_calibration_is_fixed_and_stratified(self):
        ids=calibration_ids(self.data["dev"])
        self.assertEqual(len(ids),20)
        self.assertEqual(len(set(ids)),20)
        self.assertTrue(any(self.case["id"]==x for x in ids))

    def test_finite_truth_not_production_evaluator(self):
        for split in ("dev","holdout"):
            for case in self.data[split]:
                if case["runner"]=="planner":
                    self.assertEqual(case["known_has_solution"],independently_has_solution(case))

    def test_correct_recommendation(self):
        scored=grade(deepcopy(self.row),self.case)
        self.assertTrue(scored["passed"])
        self.assertEqual(scored["citation"]["required"],8*len(self.row["result"]["recommendations"]))

    def test_swap_role_evidence_fails(self):
        row=deepcopy(self.row)
        a=next(c for c in row["claims"] if c["role"]=="a" and c["field"]=="duration_seconds")
        b=next(c for c in row["claims"] if c["role"]=="b" and c["field"]=="duration_seconds")
        a["evidence_ids"]=b["evidence_ids"]
        self.assertIn("evidence_integrity",grade(row,self.case)["hard_violations"])

    def test_delete_citations_accuracy_na_coverage_zero(self):
        row=deepcopy(self.row)
        row["claims"]=[]
        result=grade(row,self.case)
        self.assertEqual(result["citation"]["cited"],0)
        self.assertEqual(result["citation"]["covered"],0)
        self.assertGreater(result["citation"]["required"],0)
        self.assertFalse(result["passed"])

    def test_tampered_output_and_evidence_still_fail(self):
        row=deepcopy(self.row)
        claim=next(c for c in row["claims"] if c["role"]=="a" and c["field"]=="duration_seconds")
        claim["value"]=1
        for e in row["telemetry"]["evidence"]:
            if e["evidence_id"] in claim["evidence_ids"]: e["value"]=1
        self.assertIn("evidence_integrity",grade(row,self.case)["hard_violations"])

    def test_cross_run_evidence_fails(self):
        row=deepcopy(self.row)
        row["telemetry"]["evidence"][0]["run_id"]="other_run"
        self.assertFalse(grade(row,self.case)["passed"])

    def test_route_unknown_is_not_zero(self):
        row=deepcopy(self.row)
        row["result"]["recommendations"][0]["a"]["selected_route"]["duration_seconds"]=None
        self.assertIn("hard_constraint_a_duration_seconds",grade(row,self.case)["hard_violations"])

    def test_stale_conditions(self):
        row=deepcopy(self.row);row["result"]["constraints_version"]=0
        self.assertIn("stale_constraints_version",grade(row,self.case)["hard_violations"])

    def test_duplicate_results_not_success(self):
        s=self.summary(rows=[self.row,self.row])
        self.assertFalse(s["measurement_complete"])
        self.assertEqual(s["by_policy_group"]["V0:main"]["acceptance"]["numerator"],0)

    def test_missing_result_stays_in_denominator(self):
        s=self.summary(rows=[])
        self.assertEqual(s["by_policy_group"]["V0:main"]["acceptance"]["denominator"],1)
        self.assertFalse(s["measurement_complete"])

    def test_missing_event_does_not_lower_cost(self):
        row=deepcopy(self.row)
        row["telemetry"]["tools"].pop()
        s=self.summary(rows=[row])
        self.assertFalse(s["measurement_complete"])
        self.assertIsNone(s["by_policy_group"]["V0:main"]["cost"]["tool_calls_mean"])

    def test_crashed_model_attempt_is_unknown(self):
        events=deepcopy(self.events)+[{"event":"llm_start","trial_id":"t1","attempt_id":"lost","usage":None,"status":"started"}]
        s=self.summary(rows=[],events=events)
        cost=s["by_policy_group"]["V0:main"]["cost"]
        self.assertEqual(cost["usage_coverage"]["denominator"],1)
        self.assertEqual(cost["usage_coverage"]["numerator"],0)
        self.assertIsNone(cost["token_mean"]["total_tokens"])

    def test_missing_usage_unknown_not_zero(self):
        row=deepcopy(self.row)
        row["telemetry"]["llm"]=[{"attempt_id":"missing","usage":None}]
        cost=cost_summary([row],{"t1"})
        self.assertIsNone(cost["token_mean"]["total_tokens"])
        self.assertEqual(cost["usage_coverage"]["value"],0)

    def test_no_model_call_is_actual_zero(self):
        cost=cost_summary([self.row],{"t1"})
        self.assertEqual(cost["token_mean"]["total_tokens"],0)
        self.assertIsNone(cost["usage_coverage"]["value"])

    def test_usage_sum_mismatch_rejected(self):
        self.assertFalse(valid_usage({"prompt_tokens":1,"completion_tokens":2,"total_tokens":4}))
        self.assertFalse(valid_usage({"prompt_tokens":True,"completion_tokens":2,"total_tokens":3}))

    def test_always_refuse_exposes_false_refusals(self):
        row=deepcopy(self.row);row["actual_outcome"]="refused"
        s=self.summary(rows=[row])
        g=s["by_policy_group"]["V0:main"]
        self.assertEqual(g["false_refusal_rate"]["value"],1)
        self.assertEqual(g["refusal_precision"]["value"],0)

    def test_missing_refusal_support_is_not_faked(self):
        case=next(c for c in self.data["dev"] if c.get("expected_refusal") is True)
        with no_network(): row=run_case(case,"V0","scripted","refusal")
        self.assertEqual(row["actual_outcome"],"needs_clarification")
        self.assertFalse(grade(row,case)["passed"])

    def test_simulated_http_retry_separate_layer(self):
        case=deepcopy(self.case)
        case["fault"]={"tool":"search_meeting_places","kind":"retry_once"}
        with no_network(): row=bind_claims(run_case(case,"V0","scripted","retry"))
        self.assertEqual(len(row["telemetry"]["simulated_http"]),len(row["telemetry"]["tools"])+1)
        self.assertTrue(row["checks"]["counter_consistency"])
        self.assertTrue(grade(row,case)["passed"])

    def test_partial_budget_keeps_only_complete_plans(self):
        case=next(c for c in self.data["dev"] if c["id"]=="dev-E05-1")
        with no_network(): row=bind_claims(run_case(case,"V0","scripted","partial"))
        self.assertEqual(row["actual_outcome"],"recommended")
        self.assertEqual(row["reason"],"budget_exhausted:map_http")
        self.assertEqual(len(row["result"]["recommendations"]),1)
        self.assertTrue(grade(row,case)["passed"])

    def test_audited_real_client_boundary_with_test_double(self):
        trace=Trace("t","r")
        client=AuditedClient(FakeClient([response()]),trace)
        with no_network(): client.create(model="qwen3.7-flash",messages=[],max_tokens=512)
        self.assertEqual(len(trace.llm),1)
        self.assertEqual(trace.llm[0]["usage"]["total_tokens"],120)
        self.assertEqual(trace.llm[0]["returned_model"],"qwen3.7-flash-2026-07-15")

    def test_error_attempt_not_dropped(self):
        trace=Trace("t","r")
        error=openai.APIConnectionError(request=httpx.Request("POST","https://example.invalid"))
        client=AuditedClient(FakeClient([error,response()]),trace)
        with no_network():
            with self.assertRaises(openai.APIConnectionError): client.create(messages=[],max_tokens=512)
            client.create(messages=[],max_tokens=512)
        self.assertEqual(len(trace.llm),2)
        self.assertIsNone(trace.llm[0]["usage"])
        self.assertTrue(valid_usage(trace.llm[1]["usage"]))

    def test_money_guard_prevents_request(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger=SpendLedger(temp,"b",batch_limit=CALL_RESERVE_YUAN/2,total_limit=1)
            fake=FakeClient([response()])
            client=AuditedClient(fake,Trace("t","r"),ledger)
            with self.assertRaises(BudgetStop): client.create(messages=[],max_tokens=512)
            self.assertEqual(fake.calls,0)

    def test_missing_usage_keeps_money_reserved(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger=SpendLedger(temp,"b")
            ledger.reserve("a");ledger.settle("a",None)
            self.assertAlmostEqual(ledger.spent(),CALL_RESERVE_YUAN)
            ledger.settle("a",usage())
            self.assertAlmostEqual(ledger.spent(),cost_yuan(usage()))

    def test_input_bound_prevents_large_request(self):
        fake=FakeClient([response()])
        client=AuditedClient(fake,Trace("t","r"))
        with self.assertRaises(BudgetStop):
            client.create(messages=[{"role":"user","content":"中"*15000}],max_tokens=512)
        self.assertEqual(fake.calls,0)

    def test_existing_langgraph_uses_audited_model_boundary(self):
        case=next(c for c in self.data["dev"] if c["id"]=="dev-P04-1")
        events=[]
        settings=Settings(llm_model="qwen3.7-flash",llm_api_key="unit_test_placeholder")
        with no_network():
            row=bind_claims(run_case(case,"V0","llm","model_path",settings=settings,
                client_factory=lambda:FakeClient([response()]),sink=events.append))
        self.assertEqual(row["actual_outcome"],"recommended")
        self.assertEqual(len(row["telemetry"]["llm"]),1)
        self.assertTrue(row["checks"]["counter_consistency"])
        self.assertTrue(grade(row,case)["passed"])
        self.assertEqual(row["telemetry"]["llm"][0]["usage"]["total_tokens"],120)

    def test_empty_usage_propagates_from_existing_model_path(self):
        case=next(c for c in self.data["dev"] if c["id"]=="dev-P04-1")
        broken=response();broken.usage=None
        settings=Settings(llm_model="qwen3.7-flash",llm_api_key="unit_test_placeholder")
        with no_network():
            row=run_case(case,"V0","llm","empty_usage",settings=settings,
                         client_factory=lambda:FakeClient([broken]))
        self.assertEqual(len(row["telemetry"]["llm"]),1)
        cost=cost_summary([row],{"empty_usage"})
        self.assertEqual(cost["usage_coverage"]["value"],0)
        self.assertIsNone(cost["token_mean"]["total_tokens"])

    def test_rule_memory_is_zero_token_and_matches_expected(self):
        cases=[c for c in self.data["dev"] if c["group"]=="memory"]
        for case in cases:
            with no_network(): row=bind_claims(run_case(case,"V0","scripted",case["id"]))
            self.assertTrue(grade(row,case)["passed"])
            self.assertEqual(row["telemetry"]["llm"],[])


if __name__=="__main__": unittest.main()
