"""8个开发用合成场景；不包含高德响应，也不发起真实网络请求。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.m1_models import BudgetLimits, CreateTask, Requirements, Reservation, StartRun
from app.m1_store import StateStore
from app.m2_models import Place, PlaceResult, TransitResult, TransitRoute
from app.m3_baseline import B0Planner, finish_planning


def route(route_id="r1", seconds=1800, walk=500, transfers=1, **extra):
    return TransitRoute(
        route_id=route_id, duration_seconds=seconds, walk_meters=walk,
        transfers=transfers, boardings=None if transfers is None else transfers + 1,
        **extra,
    )


def ok(*routes):
    return TransitResult(status="ok", routes=list(routes) or [route()])


def place(poi_id):
    return Place(poi_id=poi_id, name="合成商场_" + poi_id,
                 location="116.400000,39.900000", citycode="010", typecode="060101")


def cases():
    good = lambda: (ok(route()), ok(route()))
    return {
        "constraints": {
            "title": "先查硬约束，再选个人路线",
            "rounds": {1500: ["good", "bad"]},
            "pairs": {
                "good": (ok(route("a_fast_but_far", 1200, 1600),
                            route("a_valid", 2500, 800), route("a_late", 2800, 100)),
                         ok(route("b_valid", 2400, 500))),
                "bad": (ok(route()), ok(route("b_too_many_transfers", 1200, 100, 2))),
            },
        },
        "boundary_unknown": {
            "title": "上限取等号；未知值仍为未知",
            "rounds": {1500: ["boundary", "unknown"]},
            "pairs": {
                "boundary": (ok(route("a_boundary", 2700, 1500, 1)),
                             ok(route("b_boundary", 2700, 1500, 1))),
                "unknown": (TransitResult(status="data_missing", routes=[route(
                    "a_unknown", seconds=None, missing_fields=["duration_seconds"],
                )]), ok(route())),
            },
        },
        "pareto": {
            "title": "被支配候选被剔除",
            "rounds": {1500: ["weak", "strong"]},
            "pairs": {
                "weak": (ok(route(seconds=2400, walk=800)), ok(route(seconds=2400, walk=800))),
                "strong": (ok(route(seconds=1800, walk=500)), ok(route(seconds=1800, walk=500))),
            },
        },
        "preference": {
            "title": "均衡优先与总耗时优先的结果不同",
            "rounds": {1500: ["balanced", "fast_total"]},
            "pairs": {
                "balanced": (ok(route(seconds=2100)), ok(route(seconds=2100))),
                "fast_total": (ok(route(seconds=1200)), ok(route(seconds=2400))),
            },
        },
        "tie": {
            "title": "平局按固定ID排序，结果不随输入顺序改变",
            "rounds": {1500: ["tie_b", "tie_a"]},
            "pairs": {
                "tie_b": good(),
                "tie_a": (ok(route("z_equal"), route("a_equal")), ok(route())),
            },
        },
        "expansion": {
            "title": "1500空、3000失败、5000找到；跨轮去重",
            "rounds": {1500: [], 3000: ["too_late"], 5000: ["too_late", "outer_good"]},
            "pairs": {
                "too_late": (ok(route(seconds=2800)), ok(route())), "outer_good": good(),
            },
        },
        "no_route_missing": {
            "title": "无路线与数据缺失分别保留",
            "rounds": {1500: ["no_route", "missing"]},
            "pairs": {
                "no_route": (TransitResult(status="no_route"), ok(route())),
                "missing": (TransitResult(status="data_missing", routes=[route(
                    "missing_walk", walk=None, missing_fields=["walk_meters"],
                )]), ok(route())),
            },
        },
        "budget_partial": {
            "title": "6次预算：保留完整候选，舍弃只查到A的候选",
            "max_http": 6,
            "rounds": {1500: ["complete_first", "partial_second"]},
            "pairs": {"complete_first": good(), "partial_second": good()},
        },
    }


class SyntheticTools:
    """在统一工具接口处注入合成结果；共用M1预算，计数明确标注为模拟。"""
    def __init__(self, store, run_id, case):
        self.client = SimpleNamespace(store=store, run_id=run_id, user_id="demo_user",
                                      simulated_http_attempts=0)
        self.case, self.candidates, self.origins = case, {}, {}

    def charge(self, resource):
        self.client.store.reserve(self.client.user_id, self.client.run_id,
                                  Reservation(resource=resource))
        self.client.simulated_http_attempts += 1

    def prepare_origins(self):
        for role in ("a", "b"):
            self.charge("map_http")
            self.origins[role] = place("synthetic_origin_" + role)

    def midpoint(self):
        if set(self.origins) != {"a", "b"}:
            raise ValueError("先准备双方合成起点。")
        return "116.400000,39.900000"

    def search_meeting_places(self, center, radius=1500):
        self.charge("search")
        allowance = self.client.store.assert_run_current(
            self.client.user_id, self.client.run_id,
        )["remaining"]["candidates_in_round"]
        fresh = []
        for candidate_id in self.case["rounds"].get(radius, []):
            if candidate_id not in self.candidates and candidate_id not in [p.poi_id for p in fresh]:
                fresh.append(place(candidate_id))
        fresh = fresh[:allowance]
        if fresh:
            self.client.store.reserve(self.client.user_id, self.client.run_id,
                                      Reservation(resource="candidates", count=len(fresh)))
            self.candidates.update({p.poi_id: p for p in fresh})
        return PlaceResult(status="ok" if fresh else "no_candidates", places=fresh)

    def get_transit_routes(self, role, candidate_id):
        if role not in self.origins or candidate_id not in self.candidates:
            raise ValueError("未登记的合成端点。")
        self.charge("map_http")
        return self.case["pairs"][candidate_id][0 if role == "a" else 1]


def verify_case(case_id, result, run, simulated_attempts):
    """预先写明每例的关键预期；失败直接抛错，不只打印一个成功标签。"""
    assert run["counters"]["llm_calls"] == 0
    assert run["counters"]["map_http"] == simulated_attempts
    assert simulated_attempts <= run["limits"]["max_map_http"]
    assert run["counters"]["search_rounds"] <= 3
    checks = {i["candidate"]["poi_id"]: i for i in result["candidate_checks"]}
    ids = result["pareto_candidate_ids"]
    if case_id == "constraints":
        assert ids == ["good"]
        assert checks["good"]["a"]["selected_route"]["route_id"] == "a_valid"
        assert checks["bad"]["status"] == "constraints_unsatisfied"
    elif case_id == "boundary_unknown":
        assert ids == ["boundary"]
        assert checks["unknown"]["status"] == "data_missing"
        assert checks["unknown"]["a"]["route_checks"][0]["constraints"][0]["passed"] is None
    elif case_id == "pareto":
        assert ids == ["strong"]
        assert result["dominated"] == [{"candidate_id": "weak", "dominated_by": ["strong"]}]
    elif case_id == "preference":
        assert ids == (["balanced", "fast_total"] if result["preference"] == "balanced"
                       else ["fast_total", "balanced"])
    elif case_id == "tie":
        assert ids == ["tie_a", "tie_b"]
        assert checks["tie_a"]["a"]["selected_route"]["route_id"] == "a_equal"
    elif case_id == "expansion":
        assert ids == ["outer_good"] and result["evaluated_count"] == 2
        assert [r["radius_meters"] for r in result["rounds"]] == [1500, 3000, 5000]
        assert run["counters"]["candidates_total"] == 2 and simulated_attempts == 9
    elif case_id == "no_route_missing":
        assert ids == [] and result["outcome"] == "data_missing"
        assert checks["no_route"]["status"] == "no_route"
        assert checks["missing"]["status"] == "data_missing"
    elif case_id == "budget_partial":
        assert ids == ["complete_first"] and result["evaluated_count"] == 1
        assert result["incomplete_candidate_id"] == "partial_second"
        assert result["termination_reason"] == "budget_exhausted:map_http"
        assert run["status"] == "stopped" and simulated_attempts == 6


def run_case(case_id, preference="balanced"):
    case = cases()[case_id]
    with tempfile.TemporaryDirectory(prefix="meetpoint_m3_") as temp:
        store = StateStore(Path(temp) / "synthetic.sqlite3")
        task = store.create_task(CreateTask(user_id="demo_user", requirements=Requirements.model_validate({
            "person_a": {"origin_text": "合成起点A"}, "person_b": {"origin_text": "合成起点B"},
            "preference": preference,
        })))
        run = store.start_run(task["session_id"], StartRun(user_id="demo_user", expected_version=1),
                              BudgetLimits(max_map_http=case.get("max_http", 40)),
                              execution_mode="m3_offline")
        tools = SyntheticTools(store, run["run_id"], case)
        tools.prepare_origins()
        planner = B0Planner(tools, task)
        reason = planner.run()
        finished = finish_planning(store, "demo_user", run["run_id"], reason)
        result = planner.summary(finished["stop_reason"])
        verify_case(case_id, result, finished, tools.client.simulated_http_attempts)
        return {"case_id": case_id, "title": case["title"], "case_checks_passed": True,
                "execution_mode": "m3_offline", "real_http_attempts": 0,
                "simulated_http_attempts": tools.client.simulated_http_attempts,
                "result": result, "run": finished}