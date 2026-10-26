import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.m1_models import BudgetLimits, CreateTask, Requirements, StartRun, UpdateTask
from app.m1_store import StateError, StateStore
from app.m2_client import MapClient, RateGate
from app.m2_models import MapToolError, TransitResult
from app.m3_baseline import B0Planner, error_reason, finish_planning
from app.m3_core import choose_route, evaluate_candidate, rank_candidates
from app.m3_synthetic import SyntheticTools, cases, ok, place, route, run_case


def requirements(**changes):
    data = {"person_a": {"origin_text": "合成起点A"},
            "person_b": {"origin_text": "合成起点B"}, **changes}
    return Requirements.model_validate(data)


def candidate(candidate_id, a=1800, b=1800, walk=500):
    return evaluate_candidate(place(candidate_id), ok(route(seconds=a, walk=walk)),
                              ok(route(seconds=b, walk=walk)), requirements())


class CoreTests(unittest.TestCase):
    def test_each_person_uses_own_limits(self):
        req = requirements(person_a={"origin_text": "A", "max_duration_seconds": 3600})
        item = evaluate_candidate(place("own"), ok(route(seconds=3000)),
                                  ok(route(seconds=3000)), req)
        self.assertEqual(item["a"]["status"], "feasible")
        self.assertEqual(item["b"]["status"], "constraints_unsatisfied")
        self.assertIsNone(item["metrics"])

    def test_missing_route_can_never_win_over_valid_route(self):
        r = choose_route(ok(route("unknown", seconds=None), route("valid", 2400)),
                         requirements().person_a)
        self.assertEqual(r["selected_route"]["route_id"], "valid")

    def test_incomplete_alternative_prevents_claim_all_routes_fail(self):
        r = choose_route(ok(route(seconds=2800), route("unknown", walk=None)),
                         requirements().person_a)
        self.assertEqual(r["status"], "data_missing")
        self.assertIsNone(r["selected_route"])

    def test_route_issues_prevent_eligibility(self):
        r = choose_route(ok(route(issues=["walking_total_mismatch"])), requirements().person_a)
        self.assertEqual(r["status"], "data_missing")

    def test_result_issues_prevent_eligibility(self):
        r = choose_route(TransitResult(status="ok", routes=[route()], issues=["synthetic_issue"]),
                         requirements().person_a)
        self.assertEqual(r["status"], "data_missing")

    def test_contradictory_no_route_is_data_missing(self):
        r = choose_route(TransitResult(status="no_route", routes=[route()]), requirements().person_a)
        self.assertTrue(r["inconsistent_result"])
        self.assertIsNone(r["selected_route"])

    def test_zero_walk_and_transfers_are_valid_known_values(self):
        r = choose_route(ok(route(walk=0, transfers=0)), requirements().person_a)
        self.assertEqual(r["status"], "feasible")

    def test_route_order_is_duration_walk_transfers_then_id(self):
        routes = [route("slow", 2000, 0, 0), route("far", 1800, 800, 0),
                  route("transfer", 1800, 500, 1), route("z", 1800, 500, 0),
                  route("a", 1800, 500, 0)]
        for items in (routes, list(reversed(routes))):
            result = choose_route(ok(*items), requirements().person_a)
            self.assertEqual(result["selected_route"]["route_id"], "a")

    def test_equal_metrics_do_not_dominate_each_other(self):
        ranked = rank_candidates([candidate("b"), candidate("a")], "balanced")
        self.assertEqual(ranked["pareto_candidate_ids"], ["a", "b"])
        self.assertEqual(ranked["dominated"], [])

    def test_longest_time_precedes_time_gap_in_balanced(self):
        ranked = rank_candidates([candidate("small_gap", 1800, 2400),
                                  candidate("small_max", 600, 2100)], "balanced")
        self.assertEqual(ranked["recommendations"][0]["candidate"]["poi_id"], "small_max")

    def test_bad_candidate_never_enters_pareto(self):
        bad = candidate("bad", a=3000)
        self.assertEqual(rank_candidates([bad], "balanced")["recommendations"], [])

    def test_unknown_preference_and_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            rank_candidates([], "invented")
        with self.assertRaises(ValueError):
            rank_candidates([candidate("same"), candidate("same")], "balanced")


class SyntheticCaseTests(unittest.TestCase):
    def test_constraints(self):
        run_case("constraints")

    def test_boundary_unknown(self):
        run_case("boundary_unknown")

    def test_pareto(self):
        run_case("pareto")

    def test_preference(self):
        run_case("preference", "balanced")
        run_case("preference", "total_time")

    def test_tie(self):
        run_case("tie")

    def test_expansion(self):
        run_case("expansion")

    def test_no_route_missing(self):
        run_case("no_route_missing")

    def test_budget_partial(self):
        run_case("budget_partial")


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = StateStore(self.path / "state.sqlite3")
        self.task = self.store.create_task(CreateTask(user_id="demo_user", requirements=requirements()))

    def prepare(self, case, limits=None):
        run = self.store.start_run(self.task["session_id"], StartRun(
            user_id="demo_user", expected_version=1,
        ), limits=limits, execution_mode="m3_offline")
        tools = SyntheticTools(self.store, run["run_id"], case)
        tools.prepare_origins()
        return run, tools, B0Planner(tools, self.task)

    def test_stops_at_three_feasible_even_if_frontier_has_only_one(self):
        ids = ["strong", "middle", "weak", "unused4", "unused5"]
        case = {"rounds": {1500: ids}, "pairs": {
            key: (ok(route(seconds=1800 + i * 60)), ok(route(seconds=1800 + i * 60)))
            for i, key in enumerate(ids)
        }}
        run, tools, planner = self.prepare(case)
        reason = planner.run()
        result = planner.summary(reason)
        self.assertEqual(reason, "feasible_target_reached")
        self.assertEqual(result["evaluated_count"], 3)
        self.assertEqual(result["pareto_candidate_ids"], ["strong"])
        counts = self.store.get_run("demo_user", run["run_id"])["counters"]
        self.assertEqual((counts["candidates_total"], counts["map_http"], counts["search_rounds"]),
                         (5, 9, 1))

    def test_search_round_limit_uses_same_run_and_stops(self):
        run, tools, planner = self.prepare(cases()["expansion"], BudgetLimits(max_search_rounds=1))
        reason = planner.run()
        finished = finish_planning(self.store, "demo_user", run["run_id"], reason)
        self.assertEqual(finished["status"], "stopped")
        self.assertEqual(finished["stop_reason"], "budget_exhausted:search_rounds")
        self.assertEqual(finished["counters"]["map_http"], 3)
        self.assertEqual(self.store.inspect()["run_count"], 1)

    def test_condition_change_during_route_prevents_publication(self):
        run, tools, planner = self.prepare(cases()["constraints"])
        original = tools.get_transit_routes

        def changed(role, candidate_id):
            result = original(role, candidate_id)
            self.store.update_task(self.task["session_id"], UpdateTask.model_validate({
                "user_id": "demo_user", "expected_version": 1,
                "changes": {"person_a": {"max_transfers": 2}},
            }))
            return result

        with patch.object(tools, "get_transit_routes", side_effect=changed):
            with self.assertRaises(StateError) as caught:
                planner.run()
        self.assertEqual(caught.exception.code, "stale_run")
        self.assertEqual(len(planner.evaluations), 0)
        with self.assertRaises(StateError):
            finish_planning(self.store, "demo_user", run["run_id"], "api_error")
        self.assertEqual(self.store.get_run("demo_user", run["run_id"])["status"], "superseded")
        self.assertEqual(tools.client.simulated_http_attempts, 4)

    def test_existing_budget_stop_is_not_overwritten(self):
        run, tools, planner = self.prepare(cases()["constraints"], BudgetLimits(max_map_http=2))
        with self.assertRaises(StateError) as caught:
            tools.charge("map_http")
        reason = error_reason(caught.exception)
        finished = finish_planning(self.store, "demo_user", run["run_id"], reason)
        self.assertEqual(finished["status"], "stopped")
        self.assertEqual(finished["counters"]["map_http"], 2)

    def test_other_manual_finish_does_not_publish_old_run(self):
        run, tools, planner = self.prepare(cases()["constraints"])
        self.store.finish_run("demo_user", run["run_id"], "manual_stop")
        with self.assertRaises(StateError):
            finish_planning(self.store, "demo_user", run["run_id"], "api_error")

    def test_sqlite_retains_only_conditions_control_and_counts(self):
        run, tools, planner = self.prepare(cases()["constraints"])
        reason = planner.run()
        finish_planning(self.store, "demo_user", run["run_id"], reason)
        with self.store.connection() as conn:
            dump = "\n".join(conn.iterdump())
        for forbidden in ("合成商场", "116.400000", "a_fast_but_far", "selected_route", "candidate_checks"):
            self.assertNotIn(forbidden, dump)
        task = self.store.get_task("demo_user", self.task["session_id"])
        self.assertEqual(task["requirements"], self.task["requirements"])
        self.assertIsNone(task["active_run_id"])


class LiveBoundaryTests(unittest.TestCase):
    """用真实M2客户端+MockTransport检查M3入口；全程没有网络请求。"""
    def test_offline_entry_needs_neither_credentials_nor_network(self):
        from app.m3 import main
        outputs = []
        with patch("sys.argv", ["m3", "offline", "--case", "all"]), \
                patch("app.m3.get_settings", side_effect=AssertionError("不应读取账号配置")), \
                patch("httpx.Client.request", side_effect=AssertionError("不应联网")), \
                patch("app.m3.show", side_effect=outputs.append):
            code = main()
        self.assertEqual(code, 0)
        self.assertEqual(outputs[-1]["offline_cases_passed"], 8)
        self.assertEqual(outputs[-1]["real_http_attempts"], 0)

    def exercise(self, max_http=40, fail_route_number=None):
        from app.m3 import main
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.sqlite3")
            task = store.create_task(CreateTask(user_id="demo_user", requirements=requirements()))
            outputs, seen, route_calls = [], [], []

            def poi(candidate_id):
                return {"id": candidate_id, "name": "合成商场_" + candidate_id,
                        "location": "116.400000,39.900000", "citycode": "010", "typecode": "060101"}

            def handler(request):
                seen.append(request.url.path)
                if request.url.path.endswith("/text"):
                    payload = {"pois": [poi("origin")]}
                elif request.url.path.endswith("/around"):
                    payload = {"pois": [poi("m" + str(i)) for i in range(5)]}
                else:
                    route_calls.append(1)
                    if len(route_calls) == fail_route_number:
                        return httpx.Response(200, json={"status": "0", "infocode": "10003"})
                    payload = {"route": {"transits": [{"cost": {"duration": "1800"},
                        "walking_distance": "500", "segments": [{"walking": {"distance": "500"},
                        "bus": {"buslines": [{"name": "合成线路"}]}}]}]}}
                return httpx.Response(200, json={"status": "1", "infocode": "10000", **payload})

            settings = Settings(amap_web_key="synthetic_not_a_real_key", _env_file=None)

            def client_factory(_settings, _store, user_id, run_id):
                return MapClient(settings, store, user_id, run_id,
                                 transport=httpx.MockTransport(handler),
                                 gate=RateGate(Path(temp) / "gate.sqlite3", 0))

            argv = ["m3", "live", "--session-id", task["session_id"], "--expected-version", "1",
                    "--date", "2026-10-26", "--time", "10:00", "--max-map-http", str(max_http)]
            with patch("sys.argv", argv), patch("app.m3.get_settings", return_value=settings), \
                    patch("app.m3.StateStore", return_value=store), \
                    patch("app.m3.MapClient", side_effect=client_factory), \
                    patch("app.m3.show", side_effect=outputs.append), \
                    patch("builtins.input", return_value="1"), redirect_stdout(io.StringIO()):
                code = main()
            return code, outputs[-1], len(seen)

    def test_live_entry_uses_m2_tools_without_llm(self):
        code, final, calls = self.exercise()
        self.assertEqual(code, 0)
        self.assertEqual(final["run"]["execution_mode"], "m3_live")
        self.assertEqual(final["result"]["feasible_count"], 3)
        self.assertEqual(final["run"]["stop_reason"], "feasible_target_reached")
        self.assertEqual((calls, final["run"]["counters"]["map_http"]), (9, 9))
        self.assertEqual(final["run"]["counters"]["llm_calls"], 0)

    def test_live_budget_one_denies_second_origin_request(self):
        code, final, calls = self.exercise(max_http=1)
        self.assertEqual((code, calls), (1, 1))
        self.assertEqual(final["run"]["status"], "stopped")
        self.assertEqual(final["run"]["stop_reason"], "budget_exhausted:map_http")
        self.assertEqual(final["result"]["recommendations"], [])

    def test_api_error_retains_only_complete_valid_prefix(self):
        code, final, calls = self.exercise(fail_route_number=4)
        self.assertEqual(code, 1)
        self.assertEqual(final["error"]["code"], "amap_quota_exhausted")
        self.assertEqual(final["run"]["stop_reason"], "api_error")
        self.assertEqual(final["result"]["outcome"], "feasible_found")
        self.assertEqual(final["result"]["evaluated_count"], 1)
        self.assertEqual(final["result"]["incomplete_candidate_id"], "m1")
        self.assertEqual(len(final["result"]["recommendations"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)