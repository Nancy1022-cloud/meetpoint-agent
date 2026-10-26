"""M7评测器的语义检查；假模型调用只用于适配测试，不计入实模评测结果。"""

import json
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.m1_models import Requirements
from app.m4_decider import LLMDecider
from app.m7 import fee, freeze, verify, online_report
from app.m7_cases import suite
from app.m7_eval import oracle, run_one, summarize, verify_recommendations
from app.m7_memory import evaluate_memory
from app.m7_memory_llm import checked_response, expectation, evaluate_model_memory


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = suite()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")):
            cls.rows = [run_one(c, p) for c in cls.data["planning"] for p in ("B0", "B1")]

    def row(self, cid, policy):
        return next(r for r in self.rows if r["case_id"] == cid and r["policy"] == policy)

    def test_original_planners_respect_budgets_and_recommendations(self):
        for r in self.rows:
            with self.subTest(case=r["case_id"], policy=r["policy"]):
                self.assertTrue(r["budget_ok"] and r["counter_consistency"])
                self.assertEqual(r["recommendation_violations"], [])
                self.assertEqual(r["real_map_http_attempts"], 0)
                self.assertEqual(r["model"]["real_llm_attempts"], 0)
                if r["policy"] == "B1":
                    self.assertIs(r["checkpoint_boundary_passed"], True)

    def test_feedback_search_can_help(self):
        self.assertFalse(self.row("S02", "B0")["completed_plan"])
        self.assertTrue(self.row("S02", "B1")["completed_plan"])
        self.assertEqual(self.row("S02", "B1")["decisions"][0]["action"], "toward_a")

    def test_feedback_search_can_fail(self):
        self.assertTrue(self.row("S06", "B0")["completed_plan"])
        self.assertFalse(self.row("S06", "B1")["completed_plan"])

    def test_quality_deltas_do_not_hide_tradeoffs(self):
        pairs = summarize(self.rows)["paired_deltas"]
        delta = next(p for p in pairs if p["case_id"] == "S07")["quality_delta_B1_minus_B0"]
        self.assertLess(delta["max_duration_seconds"], 0)
        self.assertGreater(delta["gap_seconds"], 0)

    def test_no_solution_is_not_in_success_denominator(self):
        result = summarize(self.rows)
        self.assertEqual(result["by_policy"]["B0"]["eligible_runs_with_known_solution"], 9)
        self.assertEqual(len(result["paired_deltas"]), 12)
        self.assertIsNone(next(p for p in result["paired_deltas"] if p["case_id"] == "S06")
                          ["quality_delta_B1_minus_B0"])

    def test_partial_candidate_not_published(self):
        for p in ("B0", "B1"):
            r = self.row("S12", p)
            self.assertEqual(r["counters"]["map_http"], 6)
            self.assertEqual(r["recommendation_count"], 1)
            self.assertEqual(r["evaluated_count"], 1)

    def test_unknown_is_not_zero(self):
        self.assertFalse(self.row("S09", "B0")["completed_plan"])
        self.assertEqual(self.row("S09", "B1")["outcome"], "data_missing")
        self.assertEqual(self.row("S10", "B0")["outcome"], "no_route")

    def test_oracle_checks_authored_truth(self):
        case = deepcopy(self.data["planning"][0])
        case["expected_has_feasible"] = False
        with self.assertRaises(ValueError):
            oracle(case)

    def test_checker_rejects_fabricated_candidate(self):
        case = self.data["planning"][0]
        bad = {"recommendations": [{"candidate": {"poi_id": "made_up"}, "status": "feasible"}]}
        self.assertTrue(verify_recommendations(bad, case))

    def test_memory_scope_and_window_loss(self):
        result = evaluate_memory(self.data["memory"])
        self.assertEqual(result["summary"]["structured"]["passed"], 8)
        failed = [r for r in result["rows"] if r["policy"] == "recent_events_replay" and not r["passed"]]
        self.assertIn("R01", [r["case_id"] for r in failed])
        self.assertEqual(result["real_llm_attempts"], 0)

    def test_freeze_rejects_overwrite_and_data_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            freeze(temp)
            verify(temp)
            with self.assertRaises(ValueError):
                freeze(temp)
            f = Path(temp) / "frozen/cases.json"
            f.write_text(f.read_text() + " ")
            with self.assertRaises(ValueError):
                verify(temp)

    def test_freeze_rejects_changed_code(self):
        with tempfile.TemporaryDirectory() as temp:
            freeze(temp)
            with patch("app.m7.source_hashes", return_value={"changed.py": "changed"}):
                with self.assertRaises(ValueError):
                    verify(temp)

    def test_unknown_cost_is_not_zero(self):
        metadata = {"real_llm_attempts": 2, "prompt_tokens_reported": 100,
                    "completion_tokens_reported": 20,
                    "usage_coverage": {"responses_with_usage": 1}}
        self.assertIsNone(fee(metadata)["reported_usage_price_estimate"])
        self.assertFalse(fee(metadata, 1, 2)["complete_usage"])
        self.assertAlmostEqual(fee(metadata, 1, 2)["reported_usage_price_estimate"], .00014)
        metadata["usage_coverage"]["responses_with_usage"] = 0
        self.assertIsNone(fee(metadata, 1, 2)["reported_usage_price_estimate"])

    def test_existing_llm_decider_adapter_with_fake_client(self):
        # 只测试原真实SDK路径能接入评测器；此处没有真实API请求。
        def create(**kwargs):
            observation = json.loads(kwargs["messages"][1]["content"])
            action = "toward_a" if "toward_a" in observation["allowed_actions"] else "expand"
            function = SimpleNamespace(name="choose_next_action", arguments=json.dumps({
                "action": action, "reason_code": "a_burden", "question_code": "none"}))
            return SimpleNamespace(model="fake_test_model", usage=SimpleNamespace(model_dump=lambda **k: {
                "prompt_tokens": 100, "completion_tokens": 20}),
                choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
                    tool_calls=[SimpleNamespace(function=function)]))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        factory = lambda store, rid: LLMDecider(get_settings(), store, "demo_user", rid, client=client)
        row = run_one(self.data["planning"][1], "B1", decider_factory=factory)
        self.assertTrue(row["completed_plan"])
        self.assertEqual(row["model"]["returned_models"], ["fake_test_model"])
        self.assertTrue(row["counter_consistency"])

    def test_memory_model_rejects_unknown_or_excessive_input_tokens(self):
        expected = expectation(self.data["memory"][4])
        requirements = Requirements.model_validate({"person_a": {"origin_text": "合成起点A"},
                                                   "person_b": {"origin_text": "合成起点B"}}).model_dump()
        answer = {"requirements": requirements, "preferences": expected["preferences"], "pending": False}
        function = SimpleNamespace(name="emit_memory", arguments=json.dumps(answer))
        response = SimpleNamespace(model="fake", usage=None, choices=[SimpleNamespace(
            finish_reason="tool_calls", message=SimpleNamespace(tool_calls=[SimpleNamespace(function=function)]))])
        self.assertTrue(checked_response(response, expected)["matches_expected"])
        self.assertFalse(checked_response(response, expected)["passed"])
        response.usage = SimpleNamespace(model_dump=lambda **k: {"prompt_tokens": 2049})
        self.assertFalse(checked_response(response, expected)["passed"])
        response.usage = SimpleNamespace(model_dump=lambda **k: {"prompt_tokens": 2048})
        self.assertTrue(checked_response(response, expected)["passed"])

    def test_memory_model_keeps_two_arms_and_all_repeats(self):
        response = SimpleNamespace(model="fake", usage=None, choices=[])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response)))
        result = evaluate_model_memory(self.data["memory"][:1], get_settings(), repeats=2, client=client)
        self.assertEqual(len(result["rows"]), 4)
        self.assertEqual(result["summary"]["structured"]["total"], 2)
        self.assertEqual(result["summary"]["recent_events"]["total"], 2)
        self.assertEqual(result["summary"]["structured"]["passed"], 0)


if __name__ == "__main__":
    unittest.main()