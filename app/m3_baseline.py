"""固定 B0 搜索策略；地图数据只存在本次对象中，不写持久状态。"""

from collections import Counter

from app.m1_models import Requirements
from app.m1_store import StateError
from app.m2_models import MapToolError
from app.m3_core import evaluate_candidate, rank_candidates


B0_RADII = (1500, 3000, 5000)
FEASIBLE_TARGET = 3


class B0Planner:
    def __init__(self, tools, task, on_event=None):
        self.tools, self.task = tools, task
        self.requirements = Requirements.model_validate(task["requirements"])
        self.client = tools.client
        self.store, self.user_id = self.client.store, self.client.user_id
        self.run_id = self.client.run_id
        run = self.current()
        if task["constraints_version"] != run["constraints_version"]:
            raise StateError("stale_run", "任务快照与运行条件版本不一致。")
        self.evaluations, self.rounds = [], []
        self.incomplete_candidate_id = None
        self.on_event = on_event or (lambda event: None)

    def current(self):
        return self.store.assert_run_current(self.user_id, self.run_id)

    def http_available(self):
        return self.current()["remaining"]["map_http"] > 0

    def run(self):
        center = self.tools.midpoint()
        for radius in B0_RADII:
            current = self.current()
            if current["remaining"]["search_rounds"] == 0:
                return "budget_exhausted:search_rounds"
            if not self.http_available():
                return "budget_exhausted:map_http"
            self.on_event({"action": "search", "radius_meters": radius})
            search = self.tools.search_meeting_places(center, radius=radius)
            self.current()
            row = {
                "round": len(self.rounds) + 1, "radius_meters": radius,
                "search_status": search.status, "accepted_count": len(search.places),
                "evaluated_count": 0, "feasible_count": 0,
                "rejected_incomplete": search.rejected_incomplete,
            }
            self.rounds.append(row)
            for place in search.places:
                self.incomplete_candidate_id = place.poi_id
                self.on_event({"action": "query_both_routes", "candidate_name": place.name})
                if not self.http_available():
                    return "budget_exhausted:map_http"
                a = self.tools.get_transit_routes("a", place.poi_id)
                self.current()
                if not self.http_available():
                    return "budget_exhausted:map_http"
                b = self.tools.get_transit_routes("b", place.poi_id)
                self.current()
                item = evaluate_candidate(place, a, b, self.requirements)
                self.evaluations.append(item)
                self.incomplete_candidate_id = None
                row["evaluated_count"] += 1
                row["feasible_count"] += int(item["status"] == "feasible")
                # 统计的是已评估可行候选数；不是 Pareto 筛选后的卡片数。
                if sum(i["status"] == "feasible" for i in self.evaluations) >= FEASIBLE_TARGET:
                    return "feasible_target_reached"
        # 第3轮仍可完成路线评估；没有第4轮。完整无重试至多35发HTTP。
        return "budget_exhausted:search_rounds"

    def summary(self, termination_reason):
        ranked = rank_candidates(self.evaluations, self.requirements.preference)
        counts = dict(Counter(item["status"] for item in self.evaluations))
        if ranked["feasible_count"]:
            outcome = "feasible_found"
        elif termination_reason in ("needs_clarification", "no_place", "manual_stop", "api_error"):
            outcome = termination_reason
        elif not self.rounds:
            outcome = "not_evaluated"
        elif (self.incomplete_candidate_id is not None
              or counts.get("data_missing")
              or any(r["search_status"] == "data_missing" or r["rejected_incomplete"]
                     for r in self.rounds)):
            outcome = "data_missing"
        elif counts.get("constraints_unsatisfied"):
            outcome = "constraints_unsatisfied"
        elif counts.get("no_route"):
            outcome = "no_route"
        else:
            outcome = "no_candidates"
        return {
            "policy": "B0", "preference": self.requirements.preference,
            "constraints_version": self.task["constraints_version"],
            "outcome": outcome, "termination_reason": termination_reason,
            "evaluated_count": len(self.evaluations), "counts_by_status": counts,
            "incomplete_candidate_id": self.incomplete_candidate_id,
            **ranked, "rounds": self.rounds, "candidate_checks": self.evaluations,
            "scope_note": "结果仅覆盖本次实际查询和评估的候选，不证明全城最优或无解。",
        }


def finish_planning(store, user_id, run_id, reason):
    """发布前核对版本；M1已记录的预算停止、条件变更等状态不可覆盖。"""
    run = store.get_run(user_id, run_id)
    task = store.get_task(user_id, run["session_id"])
    if task["constraints_version"] != run["constraints_version"] or run["status"] == "superseded":
        raise StateError("stale_run", "条件已变化，本次旧方案禁止发布。")
    if run["status"] == "running":
        return store.finish_run(user_id, run_id, reason)
    if run["status"] == "stopped" and str(run["stop_reason"]).startswith("budget_exhausted:"):
        return run
    raise StateError("run_not_running", "本次运行已由其他操作结束，禁止发布其方案。")


def error_reason(exc):
    if isinstance(exc, StateError) and exc.code == "budget_exhausted":
        return "budget_exhausted:" + exc.details["resource"]
    if isinstance(exc, MapToolError) and exc.code == "data_missing":
        return "data_missing"
    return "api_error"