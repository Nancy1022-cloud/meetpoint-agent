"""零外部请求：真实SQLite/LangGraph + 自建工具；假客户端usage仅用于验证采集逻辑。"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from app.config import Settings
from app.m5_store import MemoryStore
from app.m6_models import OriginChoice, WebPlan
from app.m6_runtime import JobManager, WebPlanner
from app.m7_eval import FiniteMap
from app.m8 import no_network
from app.m8_cases import suite
from app.m8_metrics import grade
from app.m8_trace import CALL_RESERVE_YUAN, SpendLedger
from app.m10 import acceptance, select_cases
from app.m8_cases import digest
from app.m8 import dump
from app.m10_decider import M10LLMDecider, ORIGINAL_PROMPT, PromptClient, RuleDecider, SYSTEM_PROMPT
from app.m10_eval import run_case
from app.m10_metrics import comparison, short_circuit_errors, summarize
from app.m10_planner import M10Planner
from apply_m10_changes import apply, sha


DEV = suite()["dev"]


def case(cid="dev-P04-1"):
    return deepcopy(next(c for c in DEV if c["id"] == cid))


class FakeClient:
    def __init__(self, actions=("expand",), usage=True):
        self.actions, self.usage = iter(actions), usage
        self.calls, self.closed = [], False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        action = next(self.actions, "finish")
        args = {"action": action, "reason_code": "explore_other_area", "question_code": "none"}
        function = SimpleNamespace(name="choose_next_action", arguments=json.dumps(args))
        usage = SimpleNamespace(model_dump=lambda **_: {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        return SimpleNamespace(model="qwen3.7-flash", usage=usage if self.usage else None,
            choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
                tool_calls=[SimpleNamespace(function=function)]))])

    def close(self):
        self.closed = True


def execute(c=None, policy="V2", fake=None, ledger=None):
    c = c or case()
    events = []
    settings = Settings(_env_file=None, llm_model="qwen3.7-flash", llm_api_key="unit_test_placeholder")
    with no_network():
        row = run_case(c, policy, "llm" if fake else "scripted", c["id"] + ":" + policy,
                       settings=settings, ledger=ledger, sink=events.append,
                       client_factory=(lambda: fake) if fake else None)
    row["repeat"] = 1
    return row, events


def manifest(rows):
    return {"trials": [{k: r[k] for k in ("trial_id", "case_id", "policy", "mode", "repeat")} for r in rows],
            "model": "qwen3.7-flash" if any(r["telemetry"]["llm"] for r in rows) else "not_called",
            "primary_policy": "V2" if any(r["policy"] == "V2" for r in rows) else rows[0]["policy"],
            "batch_id": "unit_test_not_real_usage"}


class PlannerTests(unittest.TestCase):
    def test_short_circuit_known_no_route(self):
        row, _ = execute()
        self.assertTrue(grade(row, case())["passed"])
        self.assertEqual(row["result"]["short_circuits"][0]["proof_status"], "no_route")
        self.assertFalse(short_circuit_errors(row, case()))
        self.assertEqual(len(row["telemetry"]["tools"]), 11)

    def test_short_circuit_known_constraint_failure(self):
        c = case("dev-P02-1")
        row, _ = execute(c)
        self.assertEqual(row["result"]["short_circuits"][0]["proof_status"], "constraints_unsatisfied")
        self.assertFalse(short_circuit_errors(row, c))

    def test_missing_route_data_must_not_short_circuit(self):
        c = case()
        c["candidates"][0]["a"] = [{"seconds": None, "walk": 500, "transfers": 1}]
        row, _ = execute(c)
        self.assertFalse(row["result"]["short_circuits"])
        self.assertTrue(any(t.get("role") == "b" and t.get("candidate_id") == "empty_route"
                            for t in row["telemetry"]["tools"]))

    def test_alternative_feasible_route_must_not_short_circuit(self):
        c = case("dev-P08-1")
        row, _ = execute(c)
        self.assertTrue(row["result"]["recommendations"])
        self.assertFalse(row["result"]["short_circuits"])

    def test_unqueried_side_is_not_tool_returned_missing_data(self):
        row, _ = execute()
        first = row["result"]["candidate_checks"][0]
        self.assertTrue(first["b"]["not_queried"])
        decision = next(e for e in row["controller_events"] if e["step"] == "decision")
        self.assertNotIn("data_missing", decision["observation"]["observed_history"]["sides"]["b"]["status_counts"])

    def test_empty_search_fast_path_uses_zero_simulated_model_calls(self):
        c = case("dev-P05-1")
        row, _ = execute(c)
        self.assertEqual(row["result"]["fast_path_decisions"], 2)
        self.assertEqual(row["counters"]["llm_calls"], 0)
        self.assertTrue(grade(row, c)["passed"])

    def test_known_constraint_failure_does_not_force_expansion(self):
        c = case("dev-P10-1")
        row, _ = execute(c, "RULE")
        self.assertEqual([r["action"] for r in row["result"]["rounds"]], ["middle", "toward_a", "toward_b"])
        self.assertEqual(row["result"]["fast_path_decisions"], 0)
        self.assertTrue(grade(row, c)["passed"])

    def test_rule_uses_no_llm_budget_or_usage(self):
        row, _ = execute(policy="RULE")
        self.assertEqual(row["counters"]["llm_calls"], 0)
        self.assertEqual(row["telemetry"]["llm"], [])
        self.assertTrue(all(e["decision_source"] == "deterministic_rule" for e in row["controller_events"]
                            if e["step"] == "decision"))

    def test_new_model_path_can_recover_after_empty_direction(self):
        fake = FakeClient(("toward_a", "toward_b"))
        row, _ = execute(fake=fake)
        self.assertTrue(grade(row, case())["passed"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(row["result"]["rounds"][-1]["action"], "expand")
        self.assertTrue(fake.closed)

    def test_v1_preserves_original_search_and_full_route_queries(self):
        fake = FakeClient(("toward_a", "toward_b"))
        row, events = execute(policy="V1", fake=fake)
        self.assertEqual(row["actual_outcome"], "budget_exhausted")
        self.assertEqual(fake.calls[0]["messages"][0]["content"], ORIGINAL_PROMPT)
        self.assertNotIn("short_circuits", row["result"])
        stats = summarize(manifest([row]), [case()], [row], events)["optimization_counters"]["V1"]
        self.assertEqual(stats["decision_sources"]["real_llm"], 2)

    def test_observation_only_ablation_has_no_fast_path_or_short_circuit(self):
        row, _ = execute(policy="V2_OBS")
        self.assertEqual(row["result"]["short_circuits"], [])
        self.assertEqual(row["result"]["fast_path_decisions"], 0)
        self.assertEqual(len(row["telemetry"]["tools"]), 12)

    def test_observation_contains_history_but_no_unseen_truth(self):
        fake = FakeClient(("toward_a", "toward_b"))
        row, _ = execute(case("dev-P02-1"), fake=fake)
        obs = json.loads(fake.calls[0]["messages"][1]["content"])
        self.assertIn("observed_history", obs)
        self.assertIn("action_options", obs)
        text = json.dumps(obs)
        for forbidden in ("known_has_solution", "expected_outcomes", "west0", "candidate_id", "location", "dev-P02"):
            self.assertNotIn(forbidden, text)

    def test_checkpoint_boundary_survives_short_circuit(self):
        row, _ = execute()
        self.assertTrue(row["checks"]["checkpoint_boundary"])

    def test_http_budget_can_stop_without_publishing_partial_card(self):
        c = case()
        c["limits"]["max_map_http"] = 3
        row, _ = execute(c)
        self.assertEqual(row["counters"]["map_http"], 3)
        self.assertFalse(row["result"]["recommendations"])
        self.assertEqual(row["reason"], "budget_exhausted:map_http")

    def test_invalid_model_action_is_repaired_once_then_stops(self):
        fake = FakeClient(("change_constraints", "change_constraints"))
        row, _ = execute(fake=fake)
        self.assertEqual(row["reason"], "model_output_invalid")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(row["result"]["rounds"]), 1)

    def test_map_retry_count_is_separate_from_search_round(self):
        c = case()
        c["fault"] = {"tool": "search_meeting_places", "kind": "retry_once"}
        row, _ = execute(c)
        self.assertEqual(row["counters"]["search_rounds"], 2)
        self.assertEqual(row["counters"]["map_http"], len(row["telemetry"]["simulated_http"]))
        self.assertEqual(len(row["telemetry"]["simulated_http"]), len(row["telemetry"]["tools"]) + 1)


class ProtocolTests(unittest.TestCase):
    def test_new_prompt_reaches_audited_boundary(self):
        fake = FakeClient()
        row, _ = execute(fake=fake)
        self.assertEqual(fake.calls[0]["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(row["telemetry"]["llm"][0]["usage"]["total_tokens"], 110)

    def test_prompt_adapter_refuses_changed_parent_contract(self):
        with self.assertRaises(ValueError):
            PromptClient(FakeClient()).create(messages=[{"role": "system", "content": "changed"}])

    def test_false_short_circuit_proof_is_rejected(self):
        c = case()
        row, _ = execute(c)
        c["candidates"][0]["a"] = [{"seconds": 1200, "walk": 300, "transfers": 0}]
        self.assertIn("short_circuit_invalid_proof", short_circuit_errors(row, c))

    def test_deleted_short_circuit_record_is_rejected(self):
        row, _ = execute()
        row["result"]["short_circuits"] = []
        self.assertIn("short_circuit_records_mismatch", short_circuit_errors(row, case()))

    def test_fabricated_b_query_is_rejected(self):
        row, _ = execute()
        row["telemetry"]["tools"].append({"name": "get_transit_routes", "candidate_id": "empty_route", "role": "b", "status": "returned"})
        self.assertIn("short_circuit_tool_trace_mismatch", short_circuit_errors(row, case()))

    def test_raw_rows_not_mutated_by_report(self):
        row, events = execute()
        before = deepcopy(row)
        result = summarize(manifest([row]), [case()], [row], events)
        self.assertTrue(result["measurement_complete"])
        self.assertEqual(before, row)

    def test_missing_results_keep_planned_denominator(self):
        row, events = execute()
        m = manifest([row])
        m["trials"].append({**m["trials"][0], "trial_id": "missing", "repeat": 2})
        result = summarize(m, [case()], [row], events)
        self.assertFalse(result["measurement_complete"])
        self.assertEqual(result["by_policy_group"]["V2:planning"]["acceptance"]["denominator"], 2)

    def test_duplicate_results_fail_integrity(self):
        row, events = execute()
        self.assertFalse(summarize(manifest([row]), [case()], [row, deepcopy(row)], events)["measurement_complete"])

    def test_trial_end_is_checked(self):
        row, events = execute()
        events = [e for e in events if e["event"] != "trial_end"]
        self.assertFalse(summarize(manifest([row]), [case()], [row], events)["hard_checks_passed"])

    def test_missing_usage_remains_unknown_and_reservation_is_kept(self):
        fake = FakeClient(usage=False)
        with tempfile.TemporaryDirectory() as temp:
            ledger = SpendLedger(temp, "fake_test")
            row, events = execute(fake=fake, ledger=ledger)
            result = summarize(manifest([row]), [case()], [row], events)
            cost = result["by_policy_group"]["V2:planning"]["cost"]
            self.assertFalse(result["measurement_complete"])
            self.assertIsNone(cost["token_mean"]["total_tokens"])
            self.assertAlmostEqual(ledger.spent(), CALL_RESERVE_YUAN)

    def test_planning_baseline_does_not_hide_candidate_quality(self):
        c = case("dev-P02-1")
        a, ae = execute(c, "B0")
        b, be = execute(c)
        result = summarize(manifest([a, b]), [c], [a, b], ae + be)
        self.assertFalse(result["all_arms_quality_targets_passed"])
        self.assertTrue(result["quality_targets_passed"])

    def test_paired_report_retains_gains_and_quality_coverage(self):
        a, ae = execute(policy="V1", fake=FakeClient(("toward_a", "toward_b")))
        b, be = execute(fake=FakeClient(("toward_a", "toward_b")))
        m = manifest([a, b])
        data = comparison(m, [case()], [a, b], summarize(m, [case()], [a, b], ae + be))
        pair = data["comparisons"]["V1->V2"]
        self.assertEqual(pair["right_only_pass"], 1)
        self.assertEqual(pair["both_delivered_pairs"], 0)

    def test_model_mismatch_is_not_silently_pooled(self):
        row, events = execute(fake=FakeClient())
        m = manifest([row]); m["model"] = "different-model"
        self.assertFalse(summarize(m, [case()], [row], events)["hard_checks_passed"])

    def test_endpoint_context_tampering_is_rejected(self):
        row, events = execute()
        tool = next(t for t in row["telemetry"]["tools"] if t.get("role") == "a")
        tool["candidate_id"] = "changed_endpoint"
        self.assertFalse(summarize(manifest([row]), [case()], [row], events)["hard_checks_passed"])

    def test_paired_token_delta_is_unknown_when_usage_missing(self):
        a, ae = execute(policy="V1", fake=FakeClient())
        b, be = execute(fake=FakeClient(usage=False))
        m = manifest([a, b]); s = summarize(m, [case()], [a, b], ae + be)
        paired = comparison(m, [case()], [a, b], s)["comparisons"]["V1->V2"]
        self.assertIsNone(paired["all_complete_pairs_mean_delta"]["token_delta_right_minus_left"])

    def test_pilot_uses_only_one_dev_variant_per_planning_family(self):
        selected = select_cases({"dev": DEV, "holdout": [{"id": "must_not_run"}]}, "pilot", ["planning"])
        self.assertEqual(len(selected), 10)
        self.assertEqual(len({c["family"] for c in selected}), 10)
        self.assertTrue(all(c["id"].startswith("dev-") for c in selected))

    def test_acceptance_draft_does_not_invent_manual_checks(self):
        row, events = execute()
        m = {**manifest([row]), "milestone": "M10", "stage": "dev", "mode": "scripted",
             "selected_all_dev": False, "source_digest": "unit_test_source",
             "selected_cases_sha256": digest([case()])}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); batch = root / "batch"
            dump(batch / "manifest.json", m); dump(batch / "cases.json", [case()])
            (batch / "manifest.sha256").write_text(digest(m))
            (batch / "results.jsonl").write_text(json.dumps(row) + "\n")
            (batch / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            checks = root / "checks"; checks.mkdir()
            (checks / "m10_tests.txt").write_text("Ran 2 tests in 0.100s\n\nOK\n", encoding="utf-16")
            out = root / "auto.md"
            acceptance(SimpleNamespace(batch=str(batch), checks_dir=str(checks), out=str(out)))
            text = out.read_text()
            self.assertIn("未提供，未核验", text)
            self.assertIn("日志测试数2，含最终OK且无FAILED：True", text)
            self.assertIn("网页人工现场检查：未由本程序核验", text)
            self.assertIn("完整实模开发复测门禁：False", text)

    def test_refusal_and_memory_keep_m9_path(self):
        for c in (next(c for c in DEV if c["group"] == "refusal"), next(c for c in DEV if c["group"] == "memory")):
            row, _ = execute(deepcopy(c))
            self.assertTrue(grade(row, c)["passed"])
            self.assertEqual(row["telemetry"]["llm"], [])


class InstallTests(unittest.TestCase):
    def test_install_idempotent_crlf_bom_and_rollback(self):
        root = Path(__file__).resolve().parents[1]
        spec = json.loads((root / "M10_patch_manifest.json").read_text())
        original = (root / "app/m6_runtime.py").read_text(encoding="utf-8-sig")
        for rep in spec["files"]["app/m6_runtime.py"]["replacements"]:
            original = original.replace(rep["new"], rep["old"])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp); (dest / "app").mkdir()
            (dest / "M10_patch_manifest.json").write_text(json.dumps(spec))
            p = dest / "app/m6_runtime.py"
            p.write_bytes(("\ufeff" + original.replace("\n", "\r\n")).encode())
            apply(dest); apply(dest)
            self.assertIn("M10_INTEGRATION_V1", p.read_text())
            apply(dest, True)
            self.assertEqual(sha(p.read_text()), spec["files"]["app/m6_runtime.py"]["before"])

    def test_unexpected_source_change_is_rejected_without_writing(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp); (dest / "app").mkdir()
            (dest / "M10_patch_manifest.json").write_bytes((root / "M10_patch_manifest.json").read_bytes())
            p = dest / "app/m6_runtime.py"; p.write_text("# M9_INTEGRATION_V1\n# user modifications\n")
            before = p.read_bytes()
            with self.assertRaises(ValueError): apply(dest)
            self.assertEqual(p.read_bytes(), before)


class WebTests(unittest.TestCase):
    def test_production_web_planner_is_m10(self):
        self.assertTrue(issubclass(WebPlanner, M10Planner))

    def test_web_short_circuit_preserves_evidence_and_checkpoint_boundary(self):
        c = case()
        @contextmanager
        def resources(store, request, rid):
            tools, decider = FiniteMap(store, rid, c), RuleDecider()
            decider.metadata = lambda: {"real_llm_attempts": 0}
            yield tools.client, decider, tools
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = MemoryStore(path)
            task = store.create_memory_task({"user_id": "demo_user", "person_a": {"origin_text": "起点A"},
                                             "person_b": {"origin_text": "起点B"}})["task"]
            manager = JobManager(store_factory=lambda: MemoryStore(path), resources_factory=resources)
            try:
                request = WebPlan(user_id="demo_user", expected_version=1, departure_date="2026-10-29", departure_time="10:00")
                rid = manager.start(task["session_id"], request)["run_id"]
                deadline, seen = time.monotonic() + 5, set()
                with no_network():
                    while time.monotonic() < deadline:
                        data = manager.snapshot("demo_user", rid)
                        if not data["working"]: break
                        pending = data["pending"]
                        if pending and pending["prompt_id"] not in seen:
                            seen.add(pending["prompt_id"])
                            manager.choose(rid, OriginChoice(user_id="demo_user", prompt_id=pending["prompt_id"], index=1))
                        time.sleep(.005)
                self.assertFalse(data["working"])
                self.assertIsNone(data["error"])
                self.assertTrue(data["result"]["m9_presentation"]["evidence_valid"])
                self.assertTrue(data["result"]["short_circuits"])
                self.assertTrue(data["checkpoint_audit"]["persistent_field_boundary_passed"])
                for card in data["result"]["m9_presentation"]["cards"]:
                    self.assertEqual(len(card["claims"]), 8)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
