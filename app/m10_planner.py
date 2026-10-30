"""M10：累计观测、保守空搜索快路径、单侧不可行短路。地图数据仍只在本轮对象内。"""

from collections import Counter
from copy import deepcopy
import time

from app.m2_models import MapToolError, TransitResult
from app.m3_baseline import FEASIBLE_TARGET
from app.m3_core import choose_route, evaluate_candidate
from app.m4_agent import SEARCH_ACTIONS, failure_summary
from app.m5_checkpoint import MemoryPlanner


class M10Planner(MemoryPlanner):
    def __init__(self, *args, short_circuit=True, fast_paths=True, **kwargs):
        self.short_circuit, self.fast_paths = short_circuit, fast_paths
        self.short_circuits, self.fast_path_count = [], 0
        self._next_source = "shared_first_round"
        super().__init__(*args, **kwargs)

    def emit(self, event):
        if event.get("step") == "decision" and getattr(self.decider, "deterministic", False):
            event = {**event, "decision_source": "deterministic_rule"}
        super().emit(event)

    def observed_history(self):
        sides = {}
        for role in ("a", "b"):
            known = [i[role] for i in self.evaluations if not i[role].get("not_queried")]
            failures = Counter()
            for side in known:
                if side["status"] == "constraints_unsatisfied":
                    failures.update({c["field"] for route in side["route_checks"]
                                     for c in route["constraints"] if c["passed"] is False})
            sides[role] = {"status_counts": dict(Counter(i["status"] for i in known)),
                           "constraint_failures": dict(failures)}
        empty_streak = 0
        for r in reversed(self.rounds):
            if r["accepted_count"]:
                break
            empty_streak += 1
        return {"rounds": [{k: r[k] for k in ("round", "action", "radius_meters",
                    "accepted_count", "evaluated_count", "feasible_count")} for r in self.rounds],
                "empty_round_streak": empty_streak, "sides": sides}

    def observation(self, validation_feedback=None):
        data = super().observation(validation_feedback)
        data["observed_history"] = self.observed_history()
        data["action_options"] = [{"action": a, "radius_meters": self.search_spec(a)["radius_meters"]}
                                   for a in data["allowed_actions"] if a in SEARCH_ACTIONS]
        return data

    def decide(self, state):
        history = self.observed_history()
        # 仅查空且未观测到任何约束违规时，省去一次决定扩大覆盖的模型调用。
        # 已观测到约束违规时仍保留所有合法方向，避免一刀切expand导致其他任务退化。
        if (self.fast_paths and self.rounds and self.rounds[-1]["accepted_count"] == 0
                and not any(s["constraint_failures"] for s in history["sides"].values())
                and "expand" in self.legal_actions()):
            self.fast_path_count += 1
            self._next_source = "rule_empty_without_known_constraint_failure"
            decision = {"action": "expand", "reason_code": "few_candidates", "question_code": "none"}
            self.emit({"step": "decision", "observation": self.observation(), "decision": decision,
                       "reason": "查空且未观测到约束违规，扩大覆盖。",
                       "decision_source": self._next_source})
            return {"action": "expand"}
        self._next_source = "deterministic_rule" if getattr(self.decider, "deterministic", False) else (
            "scripted_test_double" if getattr(self.decider, "simulated", False) else "real_llm")
        result = super().decide(state)
        if self.trajectory and self.trajectory[-1].get("step") == "decision":
            self.trajectory[-1]["decision_source"] = self._next_source
        return result

    def search(self, state):
        started, before = time.perf_counter(), self.current()["counters"].copy()
        action = state["action"]
        run = self.current()
        if not run["remaining"]["search_rounds"]:
            return {"stop_reason": "budget_exhausted:search_rounds"}
        if not self.http_available():
            return {"stop_reason": "budget_exhausted:map_http"}
        if action not in SEARCH_ACTIONS or action not in self.legal_actions():
            return {"stop_reason": "model_output_invalid"}
        spec = self.search_spec(action)
        self.tried.append(spec)
        self.emit({"step": "execute", "action": action, "radius_meters": spec["radius_meters"],
                   "source": self._next_source})
        search = self.tools.search_meeting_places(spec["center"], radius=spec["radius_meters"])
        run = self.current()
        ids, seen = [p.poi_id for p in search.places], {i["candidate"]["poi_id"] for i in self.evaluations}
        if (len(ids) > run["limits"]["max_candidates_per_round"] or len(ids) != len(set(ids))
                or set(ids) & seen or any(self.tools.candidates.get(p.poi_id) != p for p in search.places)):
            raise MapToolError("unverified_candidates", "候选必须来自本次工具、数量合规且跨轮去重。")
        row = {"round": len(self.rounds) + 1, "action": action, "radius_meters": spec["radius_meters"],
               "search_status": search.status, "accepted_count": len(ids), "evaluated_count": 0,
               "feasible_count": 0, "rejected_incomplete": search.rejected_incomplete,
               "short_circuit_count": 0}
        self.rounds.append(row)
        start, reason = len(self.evaluations), ""
        for place in search.places:
            self.incomplete_candidate_id = place.poi_id
            if not self.http_available():
                reason = "budget_exhausted:map_http"
                break
            a = self.tools.get_transit_routes("a", place.poi_id)
            self.current()
            a_check = choose_route(a, self.requirements.person_a)
            skip = self.short_circuit and a_check["status"] in ("no_route", "constraints_unsatisfied")
            if skip:
                # 占位的未知侧明确标记为未查询；它不是工具返回的无路线或真实缺失数据。
                b = TransitResult(status="data_missing")
            else:
                if not self.http_available():
                    reason = "budget_exhausted:map_http"
                    break
                b = self.tools.get_transit_routes("b", place.poi_id)
                self.current()
            item = evaluate_candidate(place, a, b, self.requirements)
            if skip:
                item["b"]["not_queried"] = True
                item["b"]["skip_reason"] = "a_confirmed_infeasible"
                record = {"candidate_id": place.poi_id, "skipped_role": "b", "proof_role": "a",
                          "proof_status": a_check["status"], "round": row["round"]}
                self.short_circuits.append(record)
                self.emit({"step": "route_short_circuit", **record})
                row["short_circuit_count"] += 1
            self.evaluations.append(item)
            self.incomplete_candidate_id = None
            row["evaluated_count"] += 1
            row["feasible_count"] += int(item["status"] == "feasible")
            if sum(i["status"] == "feasible" for i in self.evaluations) >= FEASIBLE_TARGET:
                reason = "feasible_target_reached"
                break
        self.feedback = failure_summary(self.evaluations[start:], row)
        # 未查询的一侧不进入“实际工具返回数据缺失”的统计。
        for role in ("a", "b"):
            known = [i for i in self.evaluations[start:] if not i[role].get("not_queried")]
            self.feedback["sides"][role] = failure_summary(known, row)["sides"][role]
        after = self.current()["counters"]
        row["map_http_delta"] = after["map_http"] - before["map_http"]
        row["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        self.emit({"step": "tool_result", "round": row["round"], "action": action,
                   "feedback": deepcopy(self.feedback), "remaining": self.current()["remaining"]})
        return {"completed_rounds": len(self.rounds), "stop_reason": reason}

    def summary(self, termination_reason):
        data = super().summary(termination_reason)
        data.update(policy="M10", short_circuits=deepcopy(self.short_circuits),
                    fast_path_decisions=self.fast_path_count,
                    m10_options={"short_circuit": self.short_circuit, "fast_paths": self.fast_paths})
        return data
