"""开发用自建场景。脚本决策器仅测试执行闭环；真实模型另用llm-check验收。"""

import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.m1_models import BudgetLimits, CreateTask, Requirements, Reservation, StartRun
from app.m1_store import StateStore
from app.m2_models import MapToolError, Place, PlaceResult, TransitResult
from app.m3_baseline import finish_planning
from app.m3_synthetic import ok, place, route
from app.m4_agent import B1Planner


CASE_NAMES = ("a_time", "b_time", "empty", "missing", "no_route", "map_budget",
              "llm_budget", "clarify", "invalid_action")


def demo_requirements():
    return Requirements.model_validate({
        "person_a": {"origin_text": "合成起点A"},
        "person_b": {"origin_text": "合成起点B"},
    })


class SyntheticTools:
    def __init__(self, store, run_id, case="a_time"):
        self.client = SimpleNamespace(store=store, user_id="demo_user", run_id=run_id,
                                      simulated_http_attempts=0)
        self.case, self.origins, self.candidates = case, {}, {}
        self.resolutions, self.calls = {}, []
        self.pairs = {}

    def charge(self, resource):
        self.client.store.reserve(self.client.user_id, self.client.run_id, Reservation(resource=resource))
        self.client.simulated_http_attempts += 1

    def resolve_place(self, role, query):
        self.charge("map_http")
        lng = "116.300000" if role == "a" else "116.500000"
        p = Place(poi_id="origin_" + role, name="合成起点" + role,
                  location=lng + ",39.900000", citycode="010")
        choices = [p]
        if self.case == "clarify" and role == "a":
            choices.append(p.model_copy(update={"poi_id": "origin_other", "name": "合成歧义地标"}))
        self.resolutions[role] = {p.poi_id: p for p in choices}
        return PlaceResult(status="ok", places=choices)

    def confirm_origin(self, role, poi_id):
        p = self.resolutions[role][poi_id]
        self.origins[role] = p
        return p

    def search_meeting_places(self, center, radius=1500):
        self.charge("search")
        self.calls.append((center, radius))
        region = "expand" if radius > 1500 else (
            "toward_a" if center.startswith("116.350000,") else
            "toward_b" if center.startswith("116.450000,") else "middle"
        )
        if region == "middle":
            ids = [] if self.case == "empty" else ["first_bad"]
            failed_a = self.case != "b_time"
            bad = ok(route("SYNTHETIC_BAD_ROUTE", seconds=3600))
            pair = (bad, ok()) if failed_a else (ok(), bad)
            if self.case == "missing":
                pair = (TransitResult(status="data_missing"), ok())
            elif self.case == "no_route":
                pair = (TransitResult(status="no_route"), ok())
            self.pairs["first_bad"] = pair
        else:
            desired = "toward_b" if self.case == "b_time" else "toward_a"
            success = region == desired or (self.case in ("empty", "missing", "no_route")
                                             and region == "expand")
            ids = ["found_1", "found_2", "found_3"] if success else []
            for index, candidate_id in enumerate(ids):
                self.pairs[candidate_id] = (ok(route(seconds=1800 + 60*index)),
                                             ok(route(seconds=1800 - 60*index)))
        allowance = self.client.store.assert_run_current(
            self.client.user_id, self.client.run_id,
        )["remaining"]["candidates_in_round"]
        fresh = [place(i) for i in ids if i not in self.candidates][:allowance]
        if fresh:
            self.client.store.reserve(self.client.user_id, self.client.run_id,
                                      Reservation(resource="candidates", count=len(fresh)))
            self.candidates.update({p.poi_id: p for p in fresh})
        return PlaceResult(status="ok" if fresh else "no_candidates", places=fresh)

    def get_transit_routes(self, role, candidate_id):
        if role not in self.origins or candidate_id not in self.candidates:
            raise MapToolError("unverified_endpoint", "合成候选未验证。")
        self.charge("map_http")
        return self.pairs[candidate_id][0 if role == "a" else 1]


class ScriptedDecider:
    simulated = True

    def __init__(self, store, run_id, outputs=None):
        self.store, self.run_id = store, run_id
        self.outputs = None if outputs is None else iter(outputs)
        self.observations = []

    def __call__(self, observation):
        self.store.reserve("demo_user", self.run_id, Reservation(resource="llm"))
        self.observations.append(deepcopy(observation))
        if self.outputs is not None:
            return next(self.outputs)
        sides = observation["latest_tool_feedback"]["sides"]
        for role in ("a", "b"):
            action = "toward_" + role
            if sides[role]["constraint_failures"] and action in observation["allowed_actions"]:
                return {"action": action, "reason_code": role + "_burden", "question_code": "none"}
        action = "expand" if "expand" in observation["allowed_actions"] else "finish"
        return {"action": action, "reason_code": "few_candidates", "question_code": "none"}


def run_case(case, *, decider_factory=None, on_event=None):
    limits = BudgetLimits(max_map_http=4 if case == "map_budget" else 40,
                          max_llm_calls=1 if case == "llm_budget" else 12)
    with tempfile.TemporaryDirectory() as temporary:
        store = StateStore(Path(temporary) / "synthetic.sqlite3")
        task = store.create_task(CreateTask(user_id="demo_user", requirements=demo_requirements()))
        run = store.start_run(task["session_id"], StartRun(user_id="demo_user", expected_version=1),
                              limits=limits, execution_mode="m4_llm_check" if decider_factory
                              else "m4_offline")
        tools = SyntheticTools(store, run["run_id"], case)
        if decider_factory:
            decider = decider_factory(store, run["run_id"])
        else:
            invalid = [{"action": "change_constraints"}] * 2 if case == "invalid_action" else None
            decider = ScriptedDecider(store, run["run_id"], invalid)
        planner = B1Planner(tools, task, decider,
                            choose_origin=(lambda *args: None if case == "clarify" else 1),
                            on_event=on_event)
        try:
            reason = planner.run()
            finished = finish_planning(store, "demo_user", run["run_id"], reason)
            result = planner.summary(finished["stop_reason"])
            telemetry = decider.metadata() if decider_factory else {
                "real_llm_attempts": 0, "simulated_decisions": len(decider.observations),
            }
        finally:
            if decider_factory:
                decider.close()
        report = {"case": case, "data_source": "self_authored_synthetic",
                  "decision_source": "real_llm" if decider_factory else "scripted_test_double",
                  "result": result, "run": finished,
                  "real_map_http_attempts": 0,
                  "simulated_map_attempts": tools.client.simulated_http_attempts,
                  "model": telemetry, "error": planner.error}
        if not decider_factory:
            verify_case(report)
        return report


def verify_case(report):
    case, result, run = report["case"], report["result"], report["run"]
    assert report["real_map_http_attempts"] == report["model"]["real_llm_attempts"] == 0
    assert run["counters"]["map_http"] == report["simulated_map_attempts"] <= run["limits"]["max_map_http"]
    assert run["counters"]["llm_calls"] == report["model"]["simulated_decisions"]
    assert run["counters"]["search_rounds"] <= 3
    if case in ("a_time", "b_time", "empty", "missing", "no_route"):
        expected = "toward_a" if case == "a_time" else "toward_b" if case == "b_time" else "expand"
        assert [r["action"] for r in result["rounds"]] == ["middle", expected]
        assert result["feasible_count"] == 3
        assert result["termination_reason"] == "feasible_target_reached"
    elif case == "map_budget":
        assert result["termination_reason"] == "budget_exhausted:map_http"
        assert result["recommendations"] == [] and result["incomplete_candidate_id"] is not None
    elif case == "llm_budget":
        assert result["termination_reason"] == "budget_exhausted:llm_calls"
        assert len(result["rounds"]) == 1
    elif case == "clarify":
        assert result["termination_reason"] == "needs_clarification"
        assert len(result["rounds"]) == 0 and run["counters"]["llm_calls"] == 0
    elif case == "invalid_action":
        assert result["termination_reason"] == "model_output_invalid"
        assert run["counters"]["llm_calls"] == 2 and len(result["rounds"]) == 1
