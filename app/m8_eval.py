"""M8适配现有生产规划器，不重写搜索策略；不创建高德客户端。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from openai import OpenAI

from app.m1_models import BudgetLimits, CreateTask, Requirements, Reservation, StartRun
from app.m1_store import StateError
from app.m2_models import MapToolError
from app.m3_baseline import B0Planner, error_reason, finish_planning
from app.m4_decider import LLMDecider
from app.m4_models import DecisionError
from app.m4_synthetic import ScriptedDecider
from app.m5_checkpoint import MemoryPlanner, inspect_checkpoint, saver_for
from app.m5_store import MemoryStore, flatten
from app.m7_eval import FiniteMap, USER
from app.m7_memory import default_new, replay
from app.m8_trace import AuditedClient, BudgetStop, Trace


class ObservedMap(FiniteMap):
    def __init__(self, store, rid, case, trace):
        super().__init__(store, rid, case)
        self.trace, self.active_tool, self.fault_used = trace, None, False

    def _http(self, resource, status):
        self.client.store.reserve(USER, self.client.run_id, Reservation(resource=resource))
        self.client.simulated_http_attempts += 1
        row = {"attempt_id": uuid4().hex, "tool_attempt_id": self.active_tool["attempt_id"],
               "status": status, "resource": resource}
        self.trace.http.append(row)
        self.trace.emit("simulated_http", **row)

    def charge(self, resource):
        # 先成功预扣，再登记实际进入执行的业务工具。拦截动作不算调用。
        fault = self.case.get("fault", {})
        hit = fault.get("tool") == self.active_tool["name"] and not self.fault_used
        if hit:
            self.fault_used = True
        self._http(resource, "transient_error" if hit and fault["kind"] == "retry_once"
                   else "permanent_error" if hit else "ok")
        if not self.active_tool.get("started"):
            self.active_tool["started"] = True
            self.trace.tools.append(self.active_tool)
            self.trace.emit("tool_start", **self.active_tool)
        if hit and fault["kind"] == "permanent":
            raise MapToolError("amap_api_error", "自建永久工具错误")
        if hit and fault["kind"] == "retry_once":
            # 同一逻辑工具的HTTP重试另扣请求，不能再扣一轮搜索。
            self._http("map_http", "ok")

    def _call(self, name, fn, *args, **kwargs):
        self.active_tool = {"attempt_id": uuid4().hex, "name": name, "status": "started"}
        try:
            result = fn(*args, **kwargs)
            self.active_tool["status"] = "returned"
            return result
        except Exception as exc:
            self.active_tool["status"] = "error"
            self.active_tool["error"] = type(exc).__name__
            if not self.active_tool.get("started"):
                row = {"name": name, "error": type(exc).__name__}
                self.trace.rejected.append(row)
                self.trace.emit("rejected_action", **row)
            raise
        finally:
            if self.active_tool.get("started"):
                self.trace.emit("tool_end", **self.active_tool)

    def _evidence(self, candidate_id, role, route_id, field, value, unit):
        row = {"evidence_id": uuid4().hex, "run_id": self.trace.run_id,
               "planner_run_id": self.client.run_id,
               "constraints_version": 1, "tool_attempt_id": self.active_tool["attempt_id"],
               "candidate_id": candidate_id, "role": role, "route_id": route_id,
               "field": field, "value": value, "unit": unit}
        self.trace.evidence.append(row)

    def resolve_place(self, role, query):
        result = self._call("resolve_place", super().resolve_place, role, query)
        if self.case.get("origin_ambiguous") == role:
            # 返回两个自建候选，真实触发未确认起点的分支。
            other = result.places[0].model_copy(update={"poi_id":"synthetic_other_"+role,
                                                       "name":"自建歧义起点"})
            result = result.model_copy(update={"places":result.places+[other]})
            self.resolutions[role][other.poi_id] = other
        return result

    def search_meeting_places(self, center, radius=1500):
        result = self._call("search_meeting_places", super().search_meeting_places, center, radius)
        for p in result.places:
            for field, value in (("poi_id", p.poi_id), ("name", p.name)):
                self._evidence(p.poi_id, None, None, field, value, "text")
        return result

    def get_transit_routes(self, role, candidate_id):
        result = self._call("get_transit_routes", super().get_transit_routes, role, candidate_id)
        for route in result.routes:
            for field, unit in (("duration_seconds","s"), ("walk_meters","m"), ("transfers","count")):
                self._evidence(candidate_id, role, route.route_id, field, getattr(route,field), unit)
        return result


def normalized_outcome(result):
    reason = result.get("termination_reason", "")
    if result.get("recommendations"):
        return "recommended"
    if reason == "needs_clarification":
        return "needs_clarification"
    if reason in ("api_error", "llm_error", "model_output_invalid", "data_missing") or result.get("outcome") == "data_missing":
        return "tool_unavailable"
    if reason.startswith("budget_exhausted:"):
        return "budget_exhausted"
    return "no_feasible_found"


def run_case(case, policy, mode, trial_id, settings=None, ledger=None, sink=None, client_factory=None):
    """所有策略共享同一结构化证据展示适配层；不声称旧网页已有引用。"""
    began = time.perf_counter()
    trace = Trace(trial_id, uuid4().hex, sink)
    row = {"trial_id":trial_id, "run_id":trace.run_id, "case_id":case["id"],
           "group":case["group"], "family":case["family"], "policy":policy, "mode":mode,
           "started":True, "actual_outcome":"execution_error", "reason":None,
           "result":None, "memory_actual":None, "error":None, "checks":{},
           "decision_source":"deterministic_B0" if policy == "B0" else
                "real_llm" if mode == "llm" else "scripted_test_double"}
    trace.emit("trial_start", case_id=case["id"])
    try:
        if case["runner"] in ("memory", "message"):
            events = case.get("events", [default_new(), {"op":"say", "text":case.get("text", "")}])
            row["memory_actual"] = replay(events)
            row["actual_outcome"] = "needs_clarification" if row["memory_actual"]["pending"] else "conditions_updated"
            row["reason"] = "explicit_change" if row["memory_actual"]["pending"] else "conditions_updated"
            row["decision_source"] = "production_controlled_parser"
            row["checks"]["counter_consistency"] = True
        else:
            with tempfile.TemporaryDirectory(prefix="m8_plan_") as temp:
                store = MemoryStore(Path(temp)/"state.sqlite3")
                task = store.create_task(CreateTask(user_id=USER,
                    requirements=Requirements.model_validate(case["requirements"])))
                run = store.start_run(task["session_id"], StartRun(user_id=USER, expected_version=1),
                    BudgetLimits.model_validate(case["limits"]),
                    execution_mode="m3_offline" if policy == "B0" else "m4_llm_check" if mode=="llm" else "m4_offline")
                row["planner_run_id"] = run["run_id"]
                tools = ObservedMap(store, run["run_id"], case, trace)
                decider, audited, planner = None, None, None
                reason = "api_error"
                try:
                    if policy == "B0":
                        planner = B0Planner(tools, task)
                        for role in ("a","b"):
                            choices = tools.resolve_place(role, task["requirements"]["person_"+role]["origin_text"])
                            if case.get("origin_ambiguous") == role:
                                reason = "needs_clarification"
                                break
                            tools.confirm_origin(role, choices.places[0].poi_id)
                        else:
                            reason = planner.run()
                    else:
                        if mode == "llm":
                            if client_factory:
                                delegate = client_factory()
                            else:
                                if not settings.llm_api_key.get_secret_value().strip():
                                    raise DecisionError("missing_llm_key", "未设置模型Key")
                                delegate = OpenAI(api_key=settings.llm_api_key.get_secret_value(),
                                    base_url=settings.llm_base_url, max_retries=0,
                                    timeout=settings.llm_timeout_seconds)
                            audited = AuditedClient(delegate, trace, ledger)
                            decider = LLMDecider(settings, store, USER, run["run_id"], client=audited)
                        else:
                            decider = ScriptedDecider(store, run["run_id"])
                        with saver_for(store) as saver:
                            planner = MemoryPlanner(tools, task, decider, saver,
                                choose_origin=lambda role,*args: None if case.get("origin_ambiguous")==role else 1)
                            reason = planner.run()
                        audit = inspect_checkpoint(store,USER,run["run_id"])
                        row["checks"]["checkpoint_boundary"] = audit["persistent_field_boundary_passed"]
                        if planner.error:
                            row["error"] = planner.error["code"]
                except BudgetStop:
                    raise
                except (StateError, MapToolError, DecisionError) as exc:
                    reason = "llm_error" if isinstance(exc, DecisionError) else error_reason(exc)
                    row["error"] = exc.code
                finally:
                    if audited:
                        audited.close()
                finished = finish_planning(store,USER,run["run_id"],reason)
                result = planner.summary(finished["stop_reason"]) if planner else None
                row.update(result=result, reason=finished["stop_reason"],
                           actual_outcome=normalized_outcome(result) if result else "tool_unavailable",
                           counters=finished["counters"], limits=finished["limits"])
                observed_llm = len(trace.llm) if mode=="llm" else len(getattr(decider,"observations",[]))
                row["checks"]["counter_consistency"] = (
                    finished["counters"]["map_http"] == len(trace.http)
                    and finished["counters"]["llm_calls"] == observed_llm)
    except BudgetStop as exc:
        row["actual_outcome"], row["error"], row["reason"] = "measurement_stopped", str(exc), "money_or_request_guard"
        trace.complete = False  # store预扣可能已发生，不能声称观测完整。
    except Exception as exc:
        row["error"] = type(exc).__name__  # 不输出可能带Key的原始错误正文。
        trace.complete = False
    row["telemetry"] = trace.export()
    row["elapsed_seconds"] = round(time.perf_counter()-began,6)
    trace.emit("trial_end", case_id=case["id"], actual_outcome=row["actual_outcome"])
    return row
