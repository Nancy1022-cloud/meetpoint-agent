"""用同一合成地图适配器运行原M3 B0与原M5 B1；不接触真实地图。"""

import math
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from app.m1_models import BudgetLimits, CreateTask, Requirements, Reservation, StartRun
from app.m1_store import StateError
from app.m2_models import MapToolError, Place, PlaceResult, TransitResult, TransitRoute
from app.m3_baseline import B0Planner, error_reason, finish_planning
from app.m3_core import evaluate_candidate, rank_candidates
from app.m4_models import DecisionError
from app.m4_synthetic import ScriptedDecider
from app.m5_checkpoint import MemoryPlanner, inspect_checkpoint, saver_for
from app.m5_store import MemoryStore


USER = "demo_user"


def place_for(item):
    return Place(poi_id=item["id"], name="自建商场_" + item["id"],
                 location=f'{116.4 + item["x"]/100000:.6f},39.900000',
                 citycode="010", typecode="060101")


def transit_for(values):
    if not values:
        return TransitResult(status="no_route")
    routes = []
    for i, value in enumerate(values):
        fields = {"duration_seconds": value["seconds"], "walk_meters": value["walk"],
                  "transfers": value["transfers"]}
        routes.append(TransitRoute(route_id="synthetic_" + str(i), **fields,
            boardings=None if value["transfers"] is None else value["transfers"] + 1,
            missing_fields=[k for k, v in fields.items() if v is None]))
    return TransitResult(status="ok" if all(r.complete for r in routes) else "data_missing",
                         routes=routes)


class FiniteMap:
    def __init__(self, store, rid, case):
        self.client = SimpleNamespace(store=store, user_id=USER, run_id=rid,
                                      simulated_http_attempts=0)
        self.case, self.origins, self.candidates, self.resolutions = case, {}, {}, {}
        self.items = {item["id"]: item for item in case["candidates"]}
        if len(self.items) != len(case["candidates"]):
            raise ValueError("合成候选ID重复")

    def charge(self, resource):
        self.client.store.reserve(USER, self.client.run_id, Reservation(resource=resource))
        self.client.simulated_http_attempts += 1

    def resolve_place(self, role, query):
        self.charge("map_http")
        p = Place(poi_id="synthetic_origin_" + role, name=query,
                  location=("116.300000" if role == "a" else "116.500000") + ",39.900000",
                  citycode="010")
        self.resolutions[role] = {p.poi_id: p}
        return PlaceResult(status="ok", places=[p])

    def confirm_origin(self, role, poi_id):
        self.origins[role] = self.resolutions[role][poi_id]
        return self.origins[role]

    def midpoint(self):
        if set(self.origins) != {"a", "b"}:
            raise ValueError("起点未确认")
        return "116.400000,39.900000"

    def search_meeting_places(self, center, radius=1500):
        self.charge("search")
        lng, lat = map(float, center.split(","))
        cx, cy = (lng-116.4)*100000, (lat-39.9)*111000
        distance = lambda item: math.hypot(item["x"]-cx, cy)
        ordered = sorted(self.items.values(), key=lambda item: (distance(item), item["id"]))
        allowance = self.client.store.assert_run_current(USER, self.client.run_id)["remaining"]["candidates_in_round"]
        fresh = [place_for(i) for i in ordered
                 if distance(i) <= radius + .01 and i["id"] not in self.candidates][:allowance]
        if fresh:
            self.client.store.reserve(USER, self.client.run_id,
                                      Reservation(resource="candidates", count=len(fresh)))
            self.candidates.update({p.poi_id: p for p in fresh})
        return PlaceResult(status="ok" if fresh else "no_candidates", places=fresh)

    def get_transit_routes(self, role, candidate_id):
        if role not in self.origins or candidate_id not in self.candidates:
            raise MapToolError("unverified_endpoint", "自建端点未经本次搜索验证")
        self.charge("map_http")
        return transit_for(self.items[candidate_id][role])


def oracle(case):
    req = Requirements.model_validate(case["requirements"])
    result = rank_candidates([evaluate_candidate(place_for(i), transit_for(i["a"]),
        transit_for(i["b"]), req) for i in case["candidates"]], req.preference)
    has_feasible = result["feasible_count"] > 0
    if has_feasible != case["expected_has_feasible"]:
        raise ValueError(case["id"] + "的预先登记真值与全集检查不一致")
    return {"has_feasible": has_feasible, "finite_candidate_count": len(case["candidates"]),
            "best_metrics": result["recommendations"][0]["metrics"] if has_feasible else None}


def verify_recommendations(result, case):
    """再独立核对来源和三项硬约束；不只相信status标签。"""
    items = {i["id"]: i for i in case["candidates"]}
    req = Requirements.model_validate(case["requirements"])
    violations = []
    for item in result["recommendations"]:
        cid = item["candidate"]["poi_id"]
        if cid not in items or item["status"] != "feasible":
            violations.append("invalid_source_or_status")
            continue
        for role in ("a", "b"):
            selected = item[role]["selected_route"]
            limits = getattr(req, "person_" + role)
            if selected is None:
                violations.append("missing_selected_route")
                continue
            values = {"seconds": selected["duration_seconds"], "walk": selected["walk_meters"],
                      "transfers": selected["transfers"]}
            if values not in items[cid][role]:
                violations.append("route_not_from_fixture")
            for field, limit in (("duration_seconds", limits.max_duration_seconds),
                                 ("walk_meters", limits.max_walk_meters), ("transfers", limits.max_transfers)):
                if selected[field] is None or selected[field] > limit:
                    violations.append(role + "." + field)
    return violations


def run_one(case, policy, *, repeat=1, decider_factory=None):
    started = time.perf_counter()
    truth = oracle(case)
    with tempfile.TemporaryDirectory(prefix="m7_plan_") as temp:
        store = MemoryStore(Path(temp) / "state.sqlite3")
        task = store.create_task(CreateTask(user_id=USER,
                                          requirements=Requirements.model_validate(case["requirements"])))
        mode = "m3_offline" if policy == "B0" else "m4_llm_check" if decider_factory else "m4_offline"
        run = store.start_run(task["session_id"], StartRun(user_id=USER, expected_version=1),
                             BudgetLimits.model_validate(case["limits"]), execution_mode=mode)
        rid, tools = run["run_id"], FiniteMap(store, run["run_id"], case)
        decider, audit, error = None, None, None
        if policy == "B0":
            planner = B0Planner(tools, task)
        elif policy == "B1":
            # 工厂在这里才初始化真实模型，离线默认不会创建任何网络客户端。
            decider = decider_factory(store, rid) if decider_factory else ScriptedDecider(store, rid)
        else:
            raise ValueError("policy必须为B0或B1")
        try:
            if policy == "B0":
                for role in ("a", "b"):
                    choices = tools.resolve_place(role, task["requirements"]["person_"+role]["origin_text"])
                    tools.confirm_origin(role, choices.places[0].poi_id)
                reason = planner.run()
            else:
                with saver_for(store) as saver:
                    planner = MemoryPlanner(tools, task, decider, saver, choose_origin=lambda *args: 1)
                    reason = planner.run()
                audit = inspect_checkpoint(store, USER, rid)
                error = planner.error
        except (StateError, MapToolError, DecisionError) as exc:
            if isinstance(exc, StateError) and exc.code != "budget_exhausted":
                raise
            reason = "llm_error" if isinstance(exc, DecisionError) else error_reason(exc)
            error = {"code": exc.code}
        finally:
            if decider_factory and decider is not None:
                decider.close()
        finished = finish_planning(store, USER, rid, reason)
        result = planner.summary(finished["stop_reason"])
        model = decider.metadata() if decider_factory and decider is not None else {
            "real_llm_attempts": 0, "prompt_tokens_reported": 0, "completion_tokens_reported": 0,
            "simulated_decisions": len(decider.observations) if decider else 0}
        counters, limits = finished["counters"], finished["limits"]
        count_ok = counters["map_http"] == tools.client.simulated_http_attempts
        count_ok = count_ok and counters["llm_calls"] == (model["real_llm_attempts"]
                    if decider_factory else model.get("simulated_decisions", 0))
        budget_ok = all(counters[k] <= limits[v] for k, v in (
            ("map_http", "max_map_http"), ("llm_calls", "max_llm_calls"),
            ("search_rounds", "max_search_rounds"), ("candidates_in_round", "max_candidates_per_round")))
        budget_ok = budget_ok and counters["candidates_total"] <= counters["search_rounds"] * limits["max_candidates_per_round"]
        return {"record_type": "m7_planning_control", "data_source": "self_authored_synthetic",
                "case_id": case["id"], "title": case["title"], "repeat": repeat, "policy": policy,
                "decision_source": "deterministic_B0" if policy == "B0" else
                                   "real_llm" if decider_factory else "scripted_test_double",
                "completed_plan": bool(result["recommendations"]), "oracle": truth,
                "outcome": result["outcome"], "stop_reason": result["termination_reason"],
                "top_metrics": result["recommendations"][0]["metrics"] if result["recommendations"] else None,
                "recommendation_count": len(result["recommendations"]),
                "recommendation_violations": verify_recommendations(result, case),
                "invalid_recommendations": sum(bool(verify_recommendations({"recommendations": [i]}, case))
                                               for i in result["recommendations"]),
                "evaluated_count": result["evaluated_count"],
                "counts_by_status": result["counts_by_status"], "counters": counters, "limits": limits,
                "budget_ok": budget_ok, "counter_consistency": count_ok,
                "checkpoint_boundary_passed": audit["persistent_field_boundary_passed"] if audit else None,
                "real_map_http_attempts": 0, "simulated_map_http": tools.client.simulated_http_attempts,
                "elapsed_seconds_synthetic_tools": round(time.perf_counter()-started, 6),
                "model": model, "error_code": error["code"] if error else None,
                "rounds": result["rounds"], "decisions": [e["decision"] for e in
                    getattr(planner, "trajectory", []) if e["step"] == "decision"]}


def summarize(rows):
    summary, paired = {}, []
    for policy in ("B0", "B1"):
        selected = [r for r in rows if r["policy"] == policy]
        eligible = [r for r in selected if r["oracle"]["has_feasible"]]
        summary[policy] = {"total_runs": len(selected), "eligible_runs_with_known_solution": len(eligible),
            "successful_eligible_runs": sum(r["completed_plan"] for r in eligible),
            "success_rate": sum(r["completed_plan"] for r in eligible)/len(eligible) if eligible else None,
            "recommendations_checked": sum(r["recommendation_count"] for r in selected),
            "recommendation_violations": sum(len(r["recommendation_violations"]) for r in selected),
            "invalid_recommendations": sum(r["invalid_recommendations"] for r in selected),
            "hard_constraint_violation_rate": (sum(r["invalid_recommendations"] for r in selected)
                /sum(r["recommendation_count"] for r in selected)) if any(r["recommendation_count"] for r in selected) else None,
            "budget_failures": sum(not r["budget_ok"] for r in selected),
            "counter_failures": sum(not r["counter_consistency"] for r in selected),
            "simulated_map_http_total": sum(r["simulated_map_http"] for r in selected),
            "real_llm_attempts_total": sum(r["model"]["real_llm_attempts"] for r in selected),
            "prompt_tokens_reported": sum(r["model"]["prompt_tokens_reported"] for r in selected),
            "completion_tokens_reported": sum(r["model"]["completion_tokens_reported"] for r in selected),
            "map_http_mean_per_run": sum(r["simulated_map_http"] for r in selected)/len(selected) if selected else None,
            "synthetic_tools_elapsed_mean_seconds": sum(r["elapsed_seconds_synthetic_tools"] for r in selected)/len(selected) if selected else None}
    baseline = {r["case_id"]: r for r in rows if r["policy"] == "B0"}
    for r in rows:
        if r["policy"] != "B1":
            continue
        b = baseline[r["case_id"]]
        both = r["completed_plan"] and b["completed_plan"]
        paired.append({"case_id": r["case_id"], "repeat": r["repeat"],
            "B0_completed": b["completed_plan"], "B1_completed": r["completed_plan"],
            "both_completed": both, "map_http_delta_B1_minus_B0": r["simulated_map_http"]-b["simulated_map_http"],
            "llm_attempts_delta_B1_minus_B0": r["model"]["real_llm_attempts"]-b["model"]["real_llm_attempts"],
            "elapsed_delta_synthetic_tools_seconds": r["elapsed_seconds_synthetic_tools"]-b["elapsed_seconds_synthetic_tools"],
            "quality_delta_B1_minus_B0": {k: r["top_metrics"][k]-b["top_metrics"][k]
                for k in ("max_duration_seconds", "gap_seconds", "total_duration_seconds")} if both else None})
    repetitions = []
    for cid in baseline:
        group = [r for r in rows if r["case_id"] == cid and r["policy"] == "B1"]
        repetitions.append({"case_id": cid, "B1_completed_runs": sum(r["completed_plan"] for r in group),
                            "B1_total_runs": len(group), "map_http_min": min(r["simulated_map_http"] for r in group),
                            "map_http_max": max(r["simulated_map_http"] for r in group),
                            "stop_reasons": [r["stop_reason"] for r in group]})
    return {"by_policy": summary, "B1_per_task_repetitions": repetitions, "paired_quality_coverage": {
        "both_completed": sum(r["both_completed"] for r in paired), "all_pairs": len(paired)},
        "paired_deltas": paired,
        "note": "B0每任务一次；B1每任务保留每次重复。质量差仅比较双方都交付的配对。"
                "负值表示B1在该指标更小；合成工具计数和耗时不等同真实接口成本。"}