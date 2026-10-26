"""M4执行边界检查。所有地图、模型传输均由自建数据或MockTransport提供。"""

import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI

from app.config import Settings
from app.m1_models import BudgetLimits, CreateTask, StartRun, UpdateTask
from app.m1_store import StateError, StateStore
from app.m2_client import MapClient
from app.m2_models import MapToolError, PlaceResult
from app.m2_tools import MapTools
from app.m3_baseline import finish_planning
from app.m4_agent import B1Planner
from app.m4_decider import LLMDecider
from app.m4_models import DecisionError
from app.m4_synthetic import CASE_NAMES, ScriptedDecider, SyntheticTools, demo_requirements, run_case


def decision(action="toward_a", reason="a_burden", question="none"):
    return {"action": action, "reason_code": reason, "question_code": question}


def response_payload(output=None):
    return {"id": "synthetic-model-response", "object": "chat.completion", "created": 1,
            "model": "synthetic-qwen", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant",
                         "content": None, "tool_calls": [{"id": "synthetic-call", "type": "function",
                         "function": {"name": "choose_next_action",
                                      "arguments": json.dumps(output or decision())}}]}}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.task = self.store.create_task(CreateTask(user_id="demo_user", requirements=demo_requirements()))

    def setup_planner(self, case="a_time", outputs=None, limits=None, choose_origin=lambda *args: 1):
        self.run = self.store.start_run(self.task["session_id"], StartRun(user_id="demo_user", expected_version=1),
                                        limits=limits, execution_mode="m4_offline")
        self.tools = SyntheticTools(self.store, self.run["run_id"], case)
        self.decider = ScriptedDecider(self.store, self.run["run_id"], outputs)
        self.planner = B1Planner(self.tools, self.task, self.decider, choose_origin=choose_origin)
        return self.planner

    def counts(self):
        return self.store.get_run("demo_user", self.run["run_id"])["counters"]


class AgentTests(Fixture):
    def test_all_development_scenarios_check_predeclared_expectations(self):
        for case in CASE_NAMES:
            with self.subTest(case=case):
                run_case(case)

    def test_feedback_swap_changes_test_double_action_but_is_not_real_model_proof(self):
        reports = [run_case(c) for c in ("a_time", "b_time")]
        observed = [[r["action"] for r in p["result"]["rounds"]] for p in reports]
        self.assertEqual(observed, [["middle", "toward_a"], ["middle", "toward_b"]])
        self.assertTrue(all(p["decision_source"] == "scripted_test_double" for p in reports))

    def test_model_input_contains_feedback_history_legal_actions_and_budget_without_answer_label(self):
        p = self.setup_planner()
        p.run()
        obs = self.decider.observations[0]
        self.assertEqual(obs["latest_tool_feedback"]["sides"]["a"]["constraint_failures"], {"duration_seconds": 1})
        self.assertEqual(obs["attempted_actions"], [{"action": "middle", "radius_meters": 1500}])
        self.assertEqual(obs["remaining"]["llm_calls"], 12)
        self.assertNotIn("middle", obs["allowed_actions"])
        for label in ("expected", "case_id", "first_bad", "116.", "合成起点"):
            self.assertNotIn(label, json.dumps(obs, ensure_ascii=False))

    def test_model_can_choose_other_direction_instead_of_code_overriding_it(self):
        p = self.setup_planner(outputs=[decision("toward_b", "explore_other_area"), decision("finish", "enough_for_now")])
        self.assertEqual(p.run(), "agent_finished")
        self.assertEqual([r["action"] for r in p.rounds], ["middle", "toward_b"])

    def test_invalid_output_does_not_fall_back_to_fixed_b0(self):
        p = self.setup_planner(outputs=[{"action": "change_constraints", "seconds": 3600}] * 2)
        self.assertEqual(p.run(), "model_output_invalid")
        self.assertEqual((len(self.tools.calls), self.counts()["llm_calls"]), (1, 2))
        self.assertEqual(self.store.get_task("demo_user", self.task["session_id"])["constraints_version"], 1)

    def test_one_invalid_output_can_be_repaired_with_another_counted_call(self):
        p = self.setup_planner(outputs=[{"action": "bad"}, decision()])
        self.assertEqual(p.run(), "feasible_target_reached")
        self.assertEqual(self.counts()["llm_calls"], 2)
        self.assertEqual(self.decider.observations[1]["validation_feedback"], "previous_action_invalid")

    def test_arbitrary_coordinate_or_constraint_fields_are_rejected(self):
        p = self.setup_planner(outputs=[decision() | {"center": "1,1", "max_transfers": 5}] * 2)
        self.assertEqual(p.run(), "model_output_invalid")
        self.assertEqual(len(self.tools.calls), 1)

    def test_repeated_region_is_rejected_without_additional_map_call(self):
        p = self.setup_planner(outputs=[decision("middle", "few_candidates")] * 2)
        self.assertEqual(p.run(), "model_output_invalid")
        self.assertEqual(len(self.tools.calls), 1)

    def test_three_round_limit_preserves_third_round_and_denies_fourth(self):
        p = self.setup_planner(outputs=[decision("expand", "few_candidates")] * 2)
        self.assertEqual(p.run(), "budget_exhausted:search_rounds")
        self.assertEqual([r["radius_meters"] for r in p.rounds], [1500, 3000, 5000])
        self.assertEqual(self.counts()["search_rounds"], 3)

    def test_last_llm_budget_stops_search_and_does_not_reset_run(self):
        p = self.setup_planner(limits=BudgetLimits(max_llm_calls=1))
        reason = p.run()
        final = finish_planning(self.store, "demo_user", self.run["run_id"], reason)
        self.assertEqual((reason, final["status"]), ("budget_exhausted:llm_calls", "stopped"))
        self.assertEqual((len(self.tools.calls), self.counts()["llm_calls"]), (1, 1))
        self.assertEqual(self.store.inspect()["run_count"], 1)

    def test_map_budget_mid_candidate_never_publishes_one_sided_route(self):
        p = self.setup_planner(limits=BudgetLimits(max_map_http=4))
        reason = p.run()
        self.assertEqual(reason, "budget_exhausted:map_http")
        self.assertEqual(p.summary(reason)["recommendations"], [])
        self.assertEqual(self.counts()["map_http"], 4)
        self.assertEqual(len(self.decider.observations), 0)

    def test_ambiguity_without_confirmation_never_searches(self):
        p = self.setup_planner(case="clarify", choose_origin=lambda *args: None)
        self.assertEqual(p.run(), "needs_clarification")
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(self.counts()["llm_calls"], 0)

    def test_origin_confirmation_then_continues_in_same_run(self):
        seen = []
        def select(role, query, result):
            self.assertEqual(self.tools.calls, [])
            seen.append((role, len(result.places)))
            return 1
        p = self.setup_planner(case="clarify", choose_origin=select)
        self.assertEqual(p.run(), "feasible_target_reached")
        self.assertEqual(seen, [("a", 2), ("b", 1)])
        self.assertEqual(self.store.inspect()["run_count"], 1)

    def test_model_asks_user_without_changing_constraints(self):
        p = self.setup_planner(outputs=[decision("ask_user", "need_user_input", "duration")])
        self.assertEqual(p.run(), "needs_clarification")
        self.assertIn("时间上限", p.pending_user)
        self.assertEqual(self.store.get_task("demo_user", self.task["session_id"])["requirements"], self.task["requirements"])

    def test_question_must_have_observed_failure_evidence(self):
        p = self.setup_planner(outputs=[decision("ask_user", "need_user_input", "walking")] * 2)
        self.assertEqual(p.run(), "model_output_invalid")
        self.assertIsNone(p.pending_user)

    def test_version_change_while_model_is_running_blocks_next_map_and_publication(self):
        p = self.setup_planner()
        original = self.decider
        def changing(obs):
            value = original(obs)
            self.store.update_task(self.task["session_id"], UpdateTask.model_validate({
                "user_id": "demo_user", "expected_version": 1,
                "changes": {"person_a": {"max_duration_seconds": 3600}},
            }))
            return value
        p.decider = changing
        with self.assertRaises(StateError) as caught:
            p.run()
        self.assertEqual(caught.exception.code, "stale_run")
        self.assertEqual(len(self.tools.calls), 1)
        with self.assertRaises(StateError):
            finish_planning(self.store, "demo_user", self.run["run_id"], "agent_finished")

    def test_manual_stop_during_model_blocks_next_map_and_publication(self):
        p = self.setup_planner()
        def stopping(obs):
            self.store.finish_run("demo_user", self.run["run_id"], "manual_stop")
            return decision()
        p.decider = stopping
        with self.assertRaises(StateError):
            p.run()
        self.assertEqual(len(self.tools.calls), 1)
        with self.assertRaises(StateError):
            finish_planning(self.store, "demo_user", self.run["run_id"], "agent_finished")

    def test_unregistered_candidate_is_blocked_before_route_query(self):
        p = self.setup_planner()
        search = self.tools.search_meeting_places
        def tampered(*args, **kwargs):
            result = search(*args, **kwargs)
            self.tools.candidates.clear()
            return result
        self.tools.search_meeting_places = tampered
        self.assertEqual(p.run(), "api_error")
        self.assertEqual(self.counts()["map_http"], 3)

    def test_transient_tool_data_never_enters_sqlite_or_graph_state(self):
        p = self.setup_planner()
        reason = p.run()
        finish_planning(self.store, "demo_user", self.run["run_id"], reason)
        with self.store.connection() as conn:
            rows = list(conn.iterdump())
        persisted = "\n".join(rows)
        for value in ("first_bad", "SYNTHETIC_BAD_ROUTE", "found_1", "合成商场", "116.350000", "latest_tool_feedback"):
            self.assertNotIn(value, persisted)
        self.assertFalse(p.graph.checkpointer)
        self.assertEqual(set(p.graph.get_input_jsonschema()["properties"]),
                         {"run_id", "action", "completed_rounds", "stop_reason"})

    def test_trajectory_contains_feedback_action_new_result_and_stop(self):
        p = self.setup_planner()
        p.run()
        self.assertEqual([e["step"] for e in p.trajectory],
                         ["execute", "tool_result", "decision", "execute", "tool_result", "stop"])
        self.assertEqual(p.trajectory[1]["feedback"]["candidate_status_counts"], {"constraints_unsatisfied": 1})
        self.assertEqual(p.trajectory[4]["feedback"]["candidate_status_counts"], {"feasible": 3})


class AdapterTests(Fixture):
    def make_adapter(self, handler, limit=12):
        p = self.setup_planner(limits=BudgetLimits(max_llm_calls=limit))
        p.prepare({"stop_reason": ""})
        p.search({"action": "middle"})
        settings = Settings(llm_api_key="synthetic-not-a-real-key", llm_model="synthetic-configured-model")
        client = OpenAI(api_key="synthetic-not-a-real-key", base_url="https://synthetic.invalid/v1", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        adapter = LLMDecider(settings, self.store, "demo_user", self.run["run_id"], client=client, sleeper=lambda _: None)
        return adapter, p.observation()

    def test_forced_tool_call_and_returned_model_usage_are_recorded(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response_payload())
        adapter, obs = self.make_adapter(handler)
        self.assertEqual(adapter(obs), decision())
        body = requests[0]
        self.assertEqual(body["tool_choice"]["function"]["name"], "choose_next_action")
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(body["tools"][0]["function"]["parameters"]["properties"]["action"]["enum"], obs["allowed_actions"])
        self.assertEqual((self.counts()["llm_calls"], adapter.attempts_started), (1, 1))
        self.assertEqual(adapter.metadata()["returned_models"], ["synthetic-qwen"])
        self.assertEqual(adapter.metadata()["prompt_tokens_reported"], 10)

    def test_timeout_then_success_costs_two_model_attempts(self):
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(200, json=response_payload())
        adapter, obs = self.make_adapter(handler)
        adapter(obs)
        self.assertEqual((len(requests), self.counts()["llm_calls"], adapter.retries_started), (2, 2, 1))
        self.assertEqual(adapter.metadata()["usage_coverage"], {"attempts": 2, "responses_with_usage": 1})

    def test_400_is_not_retried_and_response_body_is_not_exposed(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(400, json={"error": {"message": "SYNTHETIC_PRIVATE_BODY", "type": "bad_request"}})
        adapter, obs = self.make_adapter(handler)
        with self.assertRaises(DecisionError) as caught:
            adapter(obs)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_BODY", str(caught.exception))

    def test_retry_is_denied_before_http_when_llm_budget_is_one(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {"message": "synthetic rate limit"}})
        adapter, obs = self.make_adapter(handler, limit=1)
        with self.assertRaises(StateError) as caught:
            adapter(obs)
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual((len(requests), self.counts()["llm_calls"]), (1, 1))

    def test_server_error_is_retried_at_most_once(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})
        adapter, obs = self.make_adapter(handler)
        with self.assertRaises(DecisionError):
            adapter(obs)
        self.assertEqual((len(requests), self.counts()["llm_calls"]), (2, 2))

    def test_multiple_tool_calls_are_invalid_and_not_executed(self):
        payload = response_payload()
        payload["choices"][0]["message"]["tool_calls"] *= 2
        adapter, obs = self.make_adapter(lambda req: httpx.Response(200, json=payload))
        self.assertEqual(adapter(obs), {})

    def test_model_return_after_condition_change_is_discarded(self):
        def handler(request):
            self.store.update_task(self.task["session_id"], UpdateTask.model_validate({
                "user_id": "demo_user", "expected_version": 1,
                "changes": {"person_b": {"max_duration_seconds": 3600}},
            }))
            return httpx.Response(200, json=response_payload())
        adapter, obs = self.make_adapter(handler)
        with self.assertRaises(StateError) as caught:
            adapter(obs)
        self.assertEqual(caught.exception.code, "stale_run")
        self.assertEqual(adapter.returned_models, [])


class RealAdapterIntegrationTests(Fixture):
    def integrated(self, *, map_budget=40, llm_status=200, first_feasible=False):
        from app.test_m2 import poi, success, transit
        self.run = self.store.start_run(self.task["session_id"], StartRun(user_id="demo_user", expected_version=1),
                                        limits=BudgetLimits(max_map_http=map_budget), execution_mode="m4_live")
        hits = {"map": [], "llm": []}
        route_count, search_count = 0, 0
        def map_handler(request):
            nonlocal route_count, search_count
            hits["map"].append(request)
            if request.url.path == "/v5/place/text":
                p = poi("origin_a" if len(hits["map"]) == 1 else "origin_b")
                p["location"] = "116.300000,39.900000" if len(hits["map"]) == 1 else "116.500000,39.900000"
                payload = success(pois=[p])
            elif request.url.path == "/v5/place/around":
                search_count += 1
                payload = success(pois=[poi("initial")] if search_count == 1 else [poi("next" + str(i)) for i in range(3)])
            else:
                route_count += 1
                seconds = 3600 if route_count == 1 and not first_feasible else 1800
                payload = transit(cost={"duration": str(seconds)})
            return httpx.Response(200, json=payload)
        def llm_handler(request):
            hits["llm"].append(request)
            return httpx.Response(llm_status, json=response_payload() if llm_status == 200
                                  else {"error": {"message": "synthetic unavailable"}})
        class Gate:
            @contextlib.contextmanager
            def slot(self):
                yield None
            def mark_started(self, conn):
                pass
        settings = Settings(amap_web_key="synthetic-map-key", llm_api_key="synthetic-model-key")
        client = MapClient(settings, self.store, "demo_user", self.run["run_id"],
                           transport=httpx.MockTransport(map_handler), gate=Gate())
        self.addCleanup(client.close)
        model_client = OpenAI(api_key="synthetic-model-key", base_url="https://synthetic.invalid/v1", max_retries=0,
                              http_client=httpx.Client(transport=httpx.MockTransport(llm_handler)))
        self.addCleanup(model_client.close)
        decider = LLMDecider(settings, self.store, "demo_user", self.run["run_id"],
                             client=model_client, sleeper=lambda _: None)
        p = B1Planner(MapTools(client, "2026-10-26", "10:00"), self.task, decider,
                       choose_origin=lambda *args: 1)
        reason = p.run()
        finish_planning(self.store, "demo_user", self.run["run_id"], reason)
        return p, reason, hits

    def test_full_graph_with_both_real_adapters_and_synthetic_http(self):
        p, reason, hits = self.integrated()
        self.assertEqual(reason, "feasible_target_reached")
        self.assertEqual((len(hits["map"]), len(hits["llm"])), (12, 1))
        self.assertEqual(self.counts()["map_http"], 12)
        self.assertEqual(self.counts()["llm_calls"], 1)
        self.assertEqual(hits["map"][5].url.params["location"], "116.350000,39.900000")
        self.assertTrue(all(i["status"] == "feasible" for i in p.summary(reason)["recommendations"]))

    def test_budget_stop_does_not_fall_through_to_next_graph_node(self):
        p, reason, hits = self.integrated(map_budget=3)
        self.assertEqual(reason, "budget_exhausted:map_http")
        self.assertEqual((len(hits["map"]), len(hits["llm"])), (3, 0))
        self.assertEqual(p.summary(reason)["recommendations"], [])

    def test_llm_service_error_keeps_only_complete_previously_verified_candidate(self):
        p, reason, hits = self.integrated(llm_status=503, first_feasible=True)
        self.assertEqual(reason, "llm_error")
        self.assertEqual((len(hits["map"]), len(hits["llm"])), (5, 2))
        self.assertEqual(p.summary(reason)["feasible_count"], 1)
        self.assertEqual(p.summary(reason)["outcome"], "feasible_found")


class CLITests(unittest.TestCase):
    def test_offline_entry_needs_no_credentials_or_network(self):
        from app.m4 import main
        output = io.StringIO()
        with patch("sys.argv", ["m4", "offline", "--case", "a_time"]), \
                patch("socket.create_connection", side_effect=AssertionError("Unexpected network")), \
                patch("app.m4.get_settings", side_effect=AssertionError("Unexpected credentials")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn('"offline_cases_passed": 1', output.getvalue())


if __name__ == "__main__":
    unittest.main()
