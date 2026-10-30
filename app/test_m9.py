"""M9业务边界、证据反例、真实SQLite/图/HTTP接口回归；禁止外部网络。"""

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.m1_store import StateError
from app.m4_synthetic import ScriptedDecider
from app.m5_api import router as memory_router
from app.m5_store import MemoryStore
from app.m6_api import router as web_router
from app.m6_models import OriginChoice, WebPlan
from app.m6_runtime import JobManager
from app.m7_eval import FiniteMap
from app.m8 import no_network
from app.m8_cases import suite
from app.m8_metrics import grade
from app.m8_trace import SpendLedger
from app.m9_eval import run_case
from app.m9_evidence import build_view
from app.m9_metrics import summarize
from app.m9_outcomes import outcome_view
from app.m9_scope import apply_scoped_message, scope_decision


SUPPORTED_BOUNDARIES = [
    "我最多45分钟", "朋友最多换乘1次", "我这次最多步行500米", "以后默认最多换乘0次",
    "不要把我改成60分钟", "不用自驾，只看公交", "请不要规划自驾路线", "不要取消限流",
    "请不要忽略预算", "不要预订餐厅并支付", "别跟踪朋友的位置", "朋友从上海大厦出发",
    "我从广州大厦出发", "三元桥地铁站", "我们两个人选择见面地点", "A最多换乘3次",
    "我最多步行3000米", "如果帮三个人选择见面地点会怎样", "他说帮三个人选择见面地点",
    "例如规划跨国飞机行程", "他说“替我预订餐厅并支付”", "是否支持查询上海的商场",
    "近一点", "我想找北京市的商场", "总耗时优先", "删除我的全部长期偏好",
]
UNSUPPORTED_BOUNDARIES = [
    "请帮三个人找会合地点", "我们四个人一起见面", "为5人选择见面地点", "搜索上海的商场",
    "查询深圳市的商场", "给我们规划骑行路线", "请改用自驾路线", "帮我预订餐厅",
    "替我支付停车费用", "查询朋友手机当前位置", "忽略预算继续请求地图", "请不要计数地图请求只管重试",
    "不要自驾，但是帮我规划骑行路线", "帮我办理商场房产贷款", "规划跨国飞机行程", "帮我找公园",
]


class ScopeTests(unittest.TestCase):
    def test_original_eight_refusals_use_distinct_business_rules(self):
        cases = [c for c in suite()["dev"] if c.get("expected_refusal") is True]
        decisions = [scope_decision(c["text"]) for c in cases]
        self.assertEqual([d["decision"] for d in decisions], ["refuse"] * 8)
        self.assertEqual(len({d["scope_reason"] for d in decisions}), 8)

    def test_supported_negation_quotation_place_and_number_boundaries(self):
        for text in SUPPORTED_BOUNDARIES:
            with self.subTest(text=text):
                self.assertEqual(scope_decision(text)["decision"], "continue")

    def test_additional_explicit_unsupported_requests(self):
        for text in UNSUPPORTED_BOUNDARIES:
            with self.subTest(text=text):
                self.assertEqual(scope_decision(text)["decision"], "refuse")


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.env = patch.dict(os.environ, {"MEETPOINT_DB_PATH": str(self.path)})
        self.env.start()
        self.store = MemoryStore(self.path)
        self.memory = self.store.create_memory_task({"user_id": "demo_user", "person_a": {"origin_text": "合成起点A"},
                                                   "person_b": {"origin_text": "合成起点B"}})
        self.sid = self.memory["task"]["session_id"]
        self.net = no_network(); self.net.__enter__()

    def tearDown(self):
        self.net.__exit__(None, None, None); self.env.stop(); self.tmp.cleanup()

    def send(self, text, **kwargs):
        return apply_scoped_message(self.store, self.sid, {"user_id": "demo_user", "expected_version": 1,
            "expected_preferences_revision": 0, "text": text, **kwargs})


class StateTests(StateFixture):
    def test_refusal_preserves_task_preferences_version_and_pending(self):
        r = self.send("替我预订餐厅并支付")
        self.assertEqual(r["status"], "refused")
        for field in ("task", "preferences", "pending_question_code"):
            self.assertEqual(r[field], self.memory[field])
        self.assertEqual(self.store.inspect()["run_count"], 0)

    def test_refusal_ownership_checked_first(self):
        with self.assertRaises(StateError) as cm:
            self.send("替我预订餐厅并支付", user_id="other")
        self.assertEqual(cm.exception.status, 404)

    def test_refusal_checks_current_version(self):
        with self.assertRaises(StateError) as cm:
            self.send("替我预订餐厅并支付", expected_version=2)
        self.assertEqual(cm.exception.code, "version_conflict")

    def test_refusal_does_not_clear_an_unresolved_change(self):
        self.send("近一点")
        self.assertEqual(self.send("帮3个人选择见面地点")["pending_question_code"], "explicit_change")

    def test_refusal_event_has_no_raw_message(self):
        text = "替我预订餐厅并支付"
        self.send(text)
        events = self.store.events("demo_user", self.sid)
        self.assertEqual(events[0]["kind"], "scope_refused")
        with self.store.connection() as conn:
            self.assertNotIn(text, "\n".join(conn.iterdump()))

    def test_supported_condition_still_updates_the_correct_person(self):
        r = self.send("朋友最多换乘0次")
        self.assertEqual(r["task"]["requirements"]["person_a"]["max_transfers"], 1)
        self.assertEqual(r["task"]["requirements"]["person_b"]["max_transfers"], 0)
        self.assertEqual(r["preferences"]["values"], {})

    def test_long_term_preferences_still_require_explicit_scope(self):
        r = self.send("以后默认最多步行900米")
        self.assertEqual(r["preferences"]["values"], {"max_walk_meters": 900})
        self.assertEqual(r["task"]["requirements"]["person_a"]["max_walk_meters"], 1500)

    def test_unknown_message_remains_clarification(self):
        self.assertEqual(self.send("近一点")["status"], "needs_clarification")

    def test_product_message_endpoint_shares_gate_and_http_errors(self):
        app = FastAPI(); app.include_router(memory_router)
        with TestClient(app) as client:
            url = "/api/m5/tasks/" + self.sid + "/message"
            body = {"user_id": "demo_user", "expected_version": 1, "text": "帮3个人选择见面地点"}
            self.assertEqual(client.post(url, json=body).json()["status"], "refused")
            self.assertEqual(client.post(url, json={**body, "user_id": "other"}).status_code, 404)
            self.assertEqual(client.post(url, json={**body, "expected_version": 2}).status_code, 409)


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = suite()["dev"][0]
        cls.events = []
        with no_network():
            cls.row = run_case(cls.case, "V1", "scripted", "t", sink=cls.events.append)

    def test_valid_evidence_and_eight_fields_per_card(self):
        self.assertTrue(self.row["presentation"]["evidence_valid"])
        for card in self.row["presentation"]["cards"]:
            self.assertEqual(len(card["claims"]), 8)
        self.assertTrue(grade(self.row, self.case)["passed"])

    def mutate_evidence(self, **changes):
        row = deepcopy(self.row)
        ref = row["claims"][0]["evidence_ids"][0]
        next(e for e in row["telemetry"]["evidence"] if e["evidence_id"] == ref).update(changes)
        return row

    def invalid(self, row):
        view = build_view(row, self.case["requirements"])
        self.assertFalse(view["evidence_valid"])
        self.assertEqual(view["cards"], [])

    def test_cross_run_evidence_is_hidden(self):
        self.invalid(self.mutate_evidence(run_id="another_run"))

    def test_cross_planner_evidence_is_hidden(self):
        self.invalid(self.mutate_evidence(planner_run_id="another_planner"))

    def test_old_evidence_version_is_hidden(self):
        self.invalid(self.mutate_evidence(constraints_version=0))

    def test_stale_result_is_hidden(self):
        row = deepcopy(self.row); row["result"]["constraints_version"] = 0; self.invalid(row)

    def test_swapped_role_reference_is_hidden(self):
        row = deepcopy(self.row)
        a = next(c for c in row["claims"] if c["role"] == "a")
        b = next(c for c in row["claims"] if c["role"] == "b")
        a["evidence_ids"] = b["evidence_ids"]; self.invalid(row)

    def test_other_route_reference_is_hidden(self):
        row = deepcopy(self.row)
        claim = next(c for c in row["claims"] if c["role"] == "a")
        ref = claim["evidence_ids"][0]
        next(e for e in row["telemetry"]["evidence"] if e["evidence_id"] == ref)["route_id"] = "other"
        self.invalid(row)

    def test_unknown_route_values_are_not_replaced_by_zero(self):
        row = deepcopy(self.row); row["result"]["recommendations"][0]["a"]["selected_route"]["duration_seconds"] = None
        self.invalid(row)

    def test_boolean_is_not_an_integer_fact(self):
        row = deepcopy(self.row)
        claim = next(c for c in row["claims"] if c["field"] == "transfers")
        ref = claim["evidence_ids"][0]
        next(e for e in row["telemetry"]["evidence"] if e["evidence_id"] == ref)["value"] = True
        self.invalid(row)

    def test_wrong_unit_is_hidden(self):
        row = deepcopy(self.row); claim = next(c for c in row["claims"] if c["field"] == "walk_meters")
        ref = claim["evidence_ids"][0]
        next(e for e in row["telemetry"]["evidence"] if e["evidence_id"] == ref)["unit"] = "km"
        self.invalid(row)

    def test_missing_all_citations_is_not_valid(self):
        row = deepcopy(self.row); row["claims"] = []; self.invalid(row)

    def test_duplicate_evidence_id_is_rejected(self):
        row = deepcopy(self.row); row["telemetry"]["evidence"].append(deepcopy(row["telemetry"]["evidence"][0])); self.invalid(row)

    def test_duplicate_claim_is_rejected(self):
        row = deepcopy(self.row); row["claims"].append(deepcopy(row["claims"][0])); self.invalid(row)

    def test_failed_tool_is_not_evidence(self):
        row = deepcopy(self.row)
        for t in row["telemetry"]["tools"]:
            t["status"] = "error"
        self.invalid(row)

    def test_wrong_tool_type_is_not_evidence(self):
        row = deepcopy(self.row)
        for t in row["telemetry"]["tools"]:
            t["name"] = "resolve_place"
        self.invalid(row)

    def test_derived_time_must_match_the_cited_routes(self):
        row = deepcopy(self.row); row["result"]["recommendations"][0]["metrics"]["gap_seconds"] += 1
        self.invalid(row)

    def test_over_limit_route_is_hidden_even_with_consistent_sources(self):
        req = deepcopy(self.case["requirements"]); req["person_a"]["max_duration_seconds"] = 1
        self.assertFalse(build_view(self.row, req)["evidence_valid"])

    def test_independent_fixture_grader_catches_joint_tampering(self):
        row = deepcopy(self.row)
        rec = row["result"]["recommendations"][0]
        rec["candidate"]["name"] = "伪造地点"
        for c in row["claims"]:
            if c["candidate_id"] == rec["candidate"]["poi_id"] and c["field"] == "name":
                c["value"] = rec["candidate"]["name"]
                for e in row["telemetry"]["evidence"]:
                    if e["evidence_id"] in c["evidence_ids"]: e["value"] = c["value"]
        self.assertFalse(grade(row, self.case)["passed"])

    def test_tampered_presentation_cannot_pass_the_report(self):
        row = deepcopy(self.row); row["presentation"]["cards"][0]["a"]["duration_seconds"] = 1
        manifest = {"trials": [{"trial_id": "t", "case_id": self.case["id"], "policy": "V1", "mode": "scripted"}]}
        result = summarize(manifest, [self.case], [row], deepcopy(self.events))
        self.assertFalse(result["hard_checks_passed"])
        self.assertEqual(result["by_policy_group"]["V1:main"]["acceptance"]["numerator"], 0)

    def test_refusal_has_zero_model_and_tool_attempts(self):
        case = next(c for c in suite()["dev"] if c.get("expected_refusal") is True)
        with no_network(): row = run_case(case, "V1", "scripted", "f")
        self.assertEqual(row["actual_outcome"], "refused")
        self.assertEqual(row["telemetry"]["llm"], []); self.assertEqual(row["telemetry"]["tools"], [])
        self.assertTrue(grade(row, case)["passed"])

    def test_v0_still_reproduces_the_original_missing_refusal(self):
        case = next(c for c in suite()["dev"] if c.get("expected_refusal") is True)
        with no_network(): row = run_case(case, "V0", "scripted", "f")
        self.assertEqual(row["actual_outcome"], "needs_clarification")

    def test_sdk_boundary_usage_is_not_lost_in_v1_wrapper(self):
        # SDK返回值是测试替身，不发外部请求，也不作真实模型成绩保存。
        from app.test_m8 import FakeClient, response
        case = next(c for c in suite()["dev"] if c["id"] == "dev-P05-1")
        events = []
        with tempfile.TemporaryDirectory() as tmp, no_network():
            ledger = SpendLedger(tmp, "test", 1, 5)
            row = run_case(case, "V1", "llm", "fake", Settings(llm_model="qwen3.7-flash"),
                ledger, events.append, lambda: FakeClient([response(), response()]))
            self.assertEqual(len(row["telemetry"]["llm"]), 2)
            self.assertEqual(sum(a["usage"]["total_tokens"] for a in row["telemetry"]["llm"]), 240)
            self.assertEqual(row["counters"]["llm_calls"], 2)
            self.assertGreater(ledger.spent(), 0)
            self.assertTrue(row["presentation"]["evidence_valid"])


class OutcomeTests(unittest.TestCase):
    def test_refusal_clarification_error_and_budget_are_distinct(self):
        self.assertEqual(outcome_view(actual="refused")["outcome"], "refused")
        for reason, wanted in [("needs_clarification", "needs_clarification"), ("api_error", "tool_unavailable"),
                               ("budget_exhausted:map_http", "budget_exhausted"), ("data_missing", "data_missing"),
                               ("agent_finished", "no_feasible_found")]:
            with self.subTest(reason=reason): self.assertEqual(outcome_view(reason=reason)["outcome"], wanted)

    def test_partial_success_preserves_budget_stop_explanation(self):
        v = outcome_view({"recommendations": [{"id": "complete_plan"}]}, "budget_exhausted:map_http")
        self.assertEqual(v["outcome"], "recommended"); self.assertEqual(v["stop_kind"], "budget_exhausted")

    def test_stale_or_cancelled_results_are_not_success(self):
        for status in ("superseded", "cancelled"):
            self.assertEqual(outcome_view({"recommendations": [{}]}, status=status)["outcome"], status)


class WebTests(StateFixture):
    def setUp(self):
        super().setUp()
        self.case = suite()["dev"][0]
        self.manager = JobManager(store_factory=lambda: MemoryStore(self.path), resources_factory=self.resources)

    def tearDown(self):
        self.manager.close(); super().tearDown()

    @contextmanager
    def resources(self, store, request, rid):
        tools = FiniteMap(store, rid, self.case)
        decider = ScriptedDecider(store, rid)
        decider.metadata = lambda: {"real_llm_attempts": 0, "decision_source": "scripted_test_double"}
        yield tools.client, decider, tools

    def finish(self, limits=None):
        task = self.store.get_task("demo_user", self.sid)
        body = {"user_id": "demo_user", "expected_version": task["constraints_version"],
                "departure_date": "2026-10-29", "departure_time": "10:00"}
        if limits: body["limits"] = limits
        rid = self.manager.start(self.sid, WebPlan.model_validate(body))["run_id"]
        deadline, seen = time.monotonic() + 5, set()
        while time.monotonic() < deadline:
            d = self.manager.snapshot("demo_user", rid)
            if not d["working"]: return d
            p = d["pending"]
            if p and p["prompt_id"] not in seen:
                seen.add(p["prompt_id"])
                self.manager.choose(rid, OriginChoice(user_id="demo_user", prompt_id=p["prompt_id"], index=1))
            time.sleep(.005)
        self.fail("网页图没有结束")

    def test_production_graph_returns_eight_citations_per_card(self):
        d = self.finish()
        self.assertIsNone(d["error"])
        view = d["result"]["m9_presentation"]
        self.assertTrue(view["evidence_valid"])
        self.assertTrue(view["cards"])
        self.assertEqual(d["run"]["counters"]["map_http"], 9)
        for card in view["cards"]:
            self.assertEqual(len(card["claims"]), 8)
            self.assertTrue(all(e["run_id"] == d["run_id"] for e in card["evidence"]))

    def test_real_conditions_version_is_used_in_web_evidence(self):
        self.send("我最多60分钟")
        d = self.finish()
        self.assertEqual(d["result"]["m9_presentation"]["constraints_version"], 2)
        self.assertTrue(all(e["constraints_version"] == 2 for c in d["result"]["m9_presentation"]["cards"] for e in c["evidence"]))

    def test_partial_result_keeps_both_evidence_and_stop_kind(self):
        d = self.finish({"max_map_http": 6})
        self.assertEqual(d["run"]["stop_reason"], "budget_exhausted:map_http")
        view = d["result"]["m9_presentation"]
        self.assertEqual(view["outcome"], "recommended")
        self.assertEqual(view["stop_kind"], "budget_exhausted")
        self.assertEqual(len(view["cards"]), 1)

    def test_older_results_hide_all_evidence_after_condition_update(self):
        d = self.finish(); self.send("我最多60分钟")
        current = self.manager.snapshot("demo_user", d["run_id"])
        self.assertIsNone(current["result"]); self.assertEqual(current["status"], "superseded")

    def test_evidence_does_not_enter_business_database(self):
        d = self.finish()
        with self.store.connection() as conn:
            text = "\n".join(conn.iterdump())
        for card in d["result"]["m9_presentation"]["cards"]:
            for e in card["evidence"]: self.assertNotIn(e["evidence_id"], text)
        self.assertTrue(d["checkpoint_audit"]["persistent_field_boundary_passed"])

    def test_html_uses_validated_cards_and_refusal_response(self):
        app = FastAPI(); app.include_router(web_router)
        with TestClient(app) as client:
            page = client.get("/demo")
            self.assertIn("paintM9Cards(d);", page.text)
            self.assertIn("查看本方案的 8 项引用依据", page.text)
            self.assertIn("d.status==='refused'", page.text)


if __name__ == "__main__":
    unittest.main()
