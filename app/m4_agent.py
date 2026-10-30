"""固定图骨架，模型决定下一轮动作；工具数据只存在本次Planner对象中。"""

from collections import Counter
from copy import deepcopy

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.m1_store import StateError
from app.m2_models import MapToolError
from app.m3_baseline import B0Planner, FEASIBLE_TARGET, error_reason
from app.m3_core import evaluate_candidate
from app.m4_models import ControlState, Decision, DecisionError, QUESTION_TEXT, REASON_TEXT


SEARCH_ACTIONS = ("middle", "toward_a", "toward_b", "expand")


def failure_summary(items, row):
    sides = {}
    for role in ("a", "b"):
        violations = Counter()
        for item in items:
            side = item[role]
            if side["status"] == "constraints_unsatisfied":
                # 每个候选的同一字段最多记一次，不按备选路线数夸大失败。
                failed = {c["field"] for route in side["route_checks"]
                          for c in route["constraints"] if c["passed"] is False}
                violations.update(failed)
        sides[role] = {
            "status_counts": dict(Counter(i[role]["status"] for i in items)),
            "constraint_failures": dict(violations),
        }
    return {
        "search_status": row["search_status"], "accepted_count": row["accepted_count"],
        "evaluated_count": len(items),
        "candidate_status_counts": dict(Counter(i["status"] for i in items)),
        "rejected_incomplete": row["rejected_incomplete"], "sides": sides,
    }


class B1Planner(B0Planner):
    def __init__(self, tools, task, decider, choose_origin=None, on_event=None):
        super().__init__(tools, task, on_event)
        self.decider, self.choose_origin = decider, choose_origin
        self.tried, self.trajectory = [], []
        self.feedback, self.pending_user, self.error = None, None, None
        self.graph = self.build_graph()

    def emit(self, event):
        self.trajectory.append(deepcopy(event))
        self.on_event(event)

    def search_spec(self, action):
        a = list(map(float, self.tools.origins["a"].location.split(",")))
        b = list(map(float, self.tools.origins["b"].location.split(",")))
        radius = 1500
        weight = {"middle": .5, "toward_a": .75, "toward_b": .25}.get(action, .5)
        if action == "expand":
            used = [s["radius_meters"] for s in self.tried if s["action"] in ("middle", "expand")]
            radius = next((r for r in (3000, 5000) if r > max(used, default=1500)), None)
            if radius is None:
                return None
        center = f"{weight*a[0]+(1-weight)*b[0]:.6f},{weight*a[1]+(1-weight)*b[1]:.6f}"
        return {"action": action, "center": center, "radius_meters": radius}

    def legal_actions(self):
        run = self.current()
        actions = []
        if run["remaining"]["map_http"] and run["remaining"]["search_rounds"]:
            used = {(s["center"], s["radius_meters"]) for s in self.tried}
            for action in SEARCH_ACTIONS:
                spec = self.search_spec(action)
                if spec and (spec["center"], spec["radius_meters"]) not in used:
                    actions.append(action)
        return actions + ["ask_user", "finish"]

    def observation(self, validation_feedback=None):
        run = self.current()
        return {
            "constraints_version": run["constraints_version"],
            "constraints": {
                role: {k: v for k, v in self.task["requirements"][f"person_{role}"].items()
                       if k != "origin_text"} for role in ("a", "b")
            },
            "preference": self.requirements.preference,
            "latest_tool_feedback": deepcopy(self.feedback),
            "feasible_count": sum(i["status"] == "feasible" for i in self.evaluations),
            "attempted_actions": [{"action": s["action"], "radius_meters": s["radius_meters"]}
                                  for s in self.tried],
            "remaining": run["remaining"], "allowed_actions": self.legal_actions(),
            "validation_feedback": validation_feedback,
        }

    def guarded(self, function):
        def node(state):
            if state["stop_reason"]:
                return {}
            try:
                self.current()
                return function(state)
            except StateError as exc:
                if exc.code != "budget_exhausted":
                    raise  # 旧版本、取消或越权：禁止继续和发布旧方案。
                return {"stop_reason": error_reason(exc)}
            except MapToolError as exc:
                self.error = exc.as_dict()
                return {"stop_reason": "needs_clarification" if exc.code == "needs_clarification"
                        else error_reason(exc)}
            except DecisionError as exc:
                self.error = {"code": exc.code, "message": exc.message}
                return {"stop_reason": "llm_error"}
        return node

    def prepare(self, state):
        for role in ("a", "b"):
            if role in self.tools.origins:
                continue
            if not self.http_available():
                return {"stop_reason": "budget_exhausted:map_http"}
            query = self.task["requirements"][f"person_{role}"]["origin_text"]
            result = self.tools.resolve_place(role, query)
            self.current()
            if result.status != "ok":
                return {"stop_reason": result.status}
            index = self.choose_origin(role, query, result) if self.choose_origin else None
            self.current()
            if type(index) is not int or not 1 <= index <= len(result.places):
                self.pending_user = f"请确认{role.upper()}的起点；尚未开始候选搜索。"
                return {"stop_reason": "needs_clarification"}
            self.tools.confirm_origin(role, result.places[index - 1].poi_id)
        return {"action": "middle"}

    def search(self, state):
        action = state["action"]
        run = self.current()
        if run["remaining"]["search_rounds"] == 0:
            return {"stop_reason": "budget_exhausted:search_rounds"}
        if not self.http_available():
            return {"stop_reason": "budget_exhausted:map_http"}
        if action not in SEARCH_ACTIONS or action not in self.legal_actions():
            return {"stop_reason": "model_output_invalid"}
        spec = self.search_spec(action)
        self.tried.append(spec)
        self.emit({"step": "execute", "action": action, "radius_meters": spec["radius_meters"],
                   "source": "shared_first_round" if len(self.tried) == 1 else "model"})
        search = self.tools.search_meeting_places(spec["center"], radius=spec["radius_meters"])
        run = self.current()
        if len(search.places) > run["limits"]["max_candidates_per_round"]:
            raise MapToolError("unverified_candidates", "候选数量超过本轮上限。")
        ids = [p.poi_id for p in search.places]
        seen = {i["candidate"]["poi_id"] for i in self.evaluations}
        if len(ids) != len(set(ids)) or set(ids) & seen or any(
            self.tools.candidates.get(p.poi_id) != p for p in search.places
        ):
            raise MapToolError("unverified_candidates", "候选必须来自本次工具并且跨轮去重。")
        row = {"round": len(self.rounds) + 1, "action": action,
               "radius_meters": spec["radius_meters"], "search_status": search.status,
               "accepted_count": len(search.places), "evaluated_count": 0,
               "feasible_count": 0, "rejected_incomplete": search.rejected_incomplete}
        self.rounds.append(row)
        start, reason = len(self.evaluations), ""
        for place in search.places:
            self.incomplete_candidate_id = place.poi_id
            if not self.http_available():
                reason = "budget_exhausted:map_http"
                break
            a = self.tools.get_transit_routes("a", place.poi_id)
            self.current()
            if not self.http_available():
                reason = "budget_exhausted:map_http"
                break
            b = self.tools.get_transit_routes("b", place.poi_id)
            self.current()
            item = evaluate_candidate(place, a, b, self.requirements)
            self.evaluations.append(item)
            self.incomplete_candidate_id = None
            row["evaluated_count"] += 1
            row["feasible_count"] += int(item["status"] == "feasible")
            if sum(i["status"] == "feasible" for i in self.evaluations) >= FEASIBLE_TARGET:
                reason = "feasible_target_reached"
                break
        self.feedback = failure_summary(self.evaluations[start:], row)
        self.emit({"step": "tool_result", "round": row["round"], "action": action,
                   "feedback": self.feedback, "remaining": self.current()["remaining"]})
        return {"completed_rounds": len(self.rounds), "stop_reason": reason}

    def observe(self, state):
        if state["stop_reason"]:
            return {}
        run = self.current()
        for resource in ("map_http", "search_rounds", "llm_calls"):
            if run["remaining"][resource] == 0:
                return {"stop_reason": "budget_exhausted:" + resource}
        if not any(a in SEARCH_ACTIONS for a in self.legal_actions()):
            return {"stop_reason": "no_legal_actions"}
        return {}

    def validate_decision(self, raw, observation):
        decision = Decision.model_validate(raw)
        if decision.action not in observation["allowed_actions"]:
            raise ValueError("illegal_action")
        question_field = {"duration": "duration_seconds", "transfers": "transfers",
                          "walking": "walk_meters"}.get(decision.question_code)
        if question_field and not any(
            observation["latest_tool_feedback"]["sides"][role]["constraint_failures"].get(question_field)
            for role in ("a", "b")
        ):
            raise ValueError("unsupported_question")
        return decision

    def decide(self, state):
        for attempt in range(2):
            if self.current()["remaining"]["llm_calls"] == 0:
                return {"stop_reason": "budget_exhausted:llm_calls"}
            observation = self.observation("previous_action_invalid" if attempt else None)
            raw = self.decider(deepcopy(observation))
            self.current()
            try:
                decision = self.validate_decision(raw, observation)
            except (ValidationError, ValueError, TypeError):
                self.emit({"step": "decision_rejected", "attempt": attempt + 1,
                           "reason": "invalid_schema_or_action"})
                continue
            self.emit({"step": "decision", "observation": observation,
                       "decision": decision.model_dump(), "reason": REASON_TEXT[decision.reason_code],
                       "decision_source": "scripted_test_double" if getattr(self.decider, "simulated", False)
                       else "real_llm"})
            if decision.action == "finish":
                return {"stop_reason": "agent_finished"}
            if decision.action == "ask_user":
                self.pending_user = QUESTION_TEXT[decision.question_code]
                return {"stop_reason": "needs_clarification"}
            # 达到模型上限后不再发起新的地图探索；最后一次合法模型输出仍可结束/追问。
            if self.current()["remaining"]["llm_calls"] == 0:
                return {"stop_reason": "budget_exhausted:llm_calls"}
            return {"action": decision.action}
        self.error = {"code": "model_output_invalid", "message": "模型两次未给出合法动作，本次安全结束。"}
        return {"stop_reason": "model_output_invalid"}

    def build_graph(self):
        graph = StateGraph(ControlState)
        for name in ("prepare", "search", "observe", "decide"):
            graph.add_node(name, self.guarded(getattr(self, name)))
        graph.add_edge(START, "prepare")
        graph.add_conditional_edges("prepare", lambda s: "end" if s["stop_reason"] else "search",
                                    {"end": END, "search": "search"})
        graph.add_edge("search", "observe")
        graph.add_conditional_edges("observe", lambda s: "end" if s["stop_reason"] else "decide",
                                    {"end": END, "decide": "decide"})
        graph.add_conditional_edges("decide", lambda s: "end" if s["stop_reason"] else "search",
                                    {"end": END, "search": "search"})
        # M4只在本次进程内暂停确认；M5再接入仅含安全控制字段的持久检查点。
        return graph.compile(checkpointer=False)

    def run(self):
        state = self.graph.invoke({"run_id": self.run_id, "action": "middle",
                                   "completed_rounds": 0, "stop_reason": ""},
                                  config={"recursion_limit": 40})
        self.emit({"step": "stop", "reason": state["stop_reason"],
                   "completed_rounds": state["completed_rounds"]})
        return state["stop_reason"]

    def summary(self, termination_reason):
        result = super().summary(termination_reason)
        result.update(policy="B1", pending_user=self.pending_user,
                      trajectory=self.trajectory, decision_protocol="m4_actions_v1")
        return result
