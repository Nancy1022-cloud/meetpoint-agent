"""M10各对照臂复用同一冻结用例、工具环境、M9证据和原M8判分契约。"""

import tempfile
import time
from pathlib import Path
from uuid import uuid4

from openai import OpenAI

from app.m1_models import BudgetLimits, CreateTask, Requirements, StartRun
from app.m1_store import StateError
from app.m2_models import MapToolError
from app.m3_baseline import error_reason, finish_planning
from app.m4_models import DecisionError
from app.m4_synthetic import ScriptedDecider
from app.m5_checkpoint import inspect_checkpoint, saver_for
from app.m5_store import MemoryStore
from app.m7_eval import USER
from app.m8_eval import ObservedMap, normalized_outcome, run_case as run_m8
from app.m8_evidence import bind_claims
from app.m8_trace import AuditedClient, BudgetStop, Trace
from app.m9_eval import run_case as run_m9
from app.m9_evidence import build_view
from app.m10_decider import M10LLMDecider, RuleDecider
from app.m10_planner import M10Planner


POLICIES = ("B0", "RULE", "V1", "V2", "V2_OBS")


class EndpointObservedMap(ObservedMap):
    def _call(self, name, fn, *args, **kwargs):
        context = ({"role": args[0], "candidate_id": args[1]} if name == "get_transit_routes" else {})
        self.active_tool = {"attempt_id": uuid4().hex, "name": name, "status": "started", **context}
        try:
            result = fn(*args, **kwargs)
            self.active_tool["status"] = "returned"
            return result
        except Exception as exc:
            self.active_tool.update(status="error", error=type(exc).__name__)
            if not self.active_tool.get("started"):
                row = {"name": name, "error": type(exc).__name__}
                self.trace.rejected.append(row)
                self.trace.emit("rejected_action", **row)
            raise
        finally:
            if self.active_tool.get("started"):
                self.trace.emit("tool_end", **self.active_tool)


def present(row, case):
    bind_claims(row)
    row["presentation"] = build_view(row, case.get("requirements"))
    if not row.get("result"):
        row["presentation"].update(outcome=row["actual_outcome"], stop_kind=row["reason"])
    return row


def run_case(case, policy, mode, trial_id, settings=None, ledger=None, sink=None, client_factory=None):
    if policy not in POLICIES:
        raise ValueError("未知M10策略")
    if case["runner"] != "planner" or policy == "V1":
        if policy not in ("V1", "V2", "V2_OBS"):
            raise ValueError("B0/RULE只对规划任务运行，不对拒答和Memory重复计分")
        row = run_m9(case, "V1", mode, trial_id, settings, ledger, sink, client_factory)
        row["policy"] = policy
        return row
    if policy == "B0":
        row = run_m8(case, "B0", mode, trial_id, settings, ledger, sink, client_factory)
        return present(row, case)
    began, trace = time.perf_counter(), Trace(trial_id, uuid4().hex, sink)
    row = {"trial_id": trial_id, "run_id": trace.run_id, "case_id": case["id"],
           "group": case["group"], "family": case["family"], "policy": policy, "mode": mode,
           "started": True, "actual_outcome": "execution_error", "reason": None, "result": None,
           "memory_actual": None, "error": None, "checks": {},
           "decision_source": "deterministic_rule" if policy == "RULE" else (
               "real_llm_with_explicit_fast_paths" if mode == "llm" else "scripted_test_double")}
    trace.emit("trial_start", case_id=case["id"])
    try:
        with tempfile.TemporaryDirectory(prefix="m10_plan_") as temp:
            store = MemoryStore(Path(temp) / "state.sqlite3")
            task = store.create_task(CreateTask(user_id=USER,
                requirements=Requirements.model_validate(case["requirements"])))
            run = store.start_run(task["session_id"], StartRun(user_id=USER, expected_version=1),
                BudgetLimits.model_validate(case["limits"]),
                execution_mode="m4_llm_check" if mode == "llm" and policy != "RULE" else "m4_offline")
            row["planner_run_id"] = run["run_id"]
            tools = EndpointObservedMap(store, run["run_id"], case, trace)
            audited, planner, decider, reason = None, None, None, "api_error"
            try:
                if policy == "RULE":
                    decider = RuleDecider()
                elif mode == "llm":
                    delegate = client_factory() if client_factory else OpenAI(
                        api_key=settings.llm_api_key.get_secret_value(), base_url=settings.llm_base_url,
                        max_retries=0, timeout=settings.llm_timeout_seconds)
                    audited = AuditedClient(delegate, trace, ledger)
                    decider = M10LLMDecider(settings, store, USER, run["run_id"], client=audited)
                else:
                    decider = ScriptedDecider(store, run["run_id"])
                with saver_for(store) as saver:
                    planner = M10Planner(tools, task, decider, saver,
                        short_circuit=policy != "V2_OBS", fast_paths=policy != "V2_OBS",
                        choose_origin=lambda role, *args: None if case.get("origin_ambiguous") == role else 1)
                    reason = planner.run()
                row["checks"]["checkpoint_boundary"] = inspect_checkpoint(
                    store, USER, run["run_id"])["persistent_field_boundary_passed"]
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
            finished = finish_planning(store, USER, run["run_id"], reason)
            result = planner.summary(finished["stop_reason"]) if planner else None
            row.update(result=result, reason=finished["stop_reason"], counters=finished["counters"],
                       limits=finished["limits"], controller_events=getattr(planner, "trajectory", []),
                       actual_outcome=normalized_outcome(result) if result else "tool_unavailable")
            observed_llm = (0 if policy == "RULE" else len(trace.llm) if mode == "llm"
                            else len(getattr(decider, "observations", [])))
            row["checks"]["counter_consistency"] = (finished["counters"]["map_http"] == len(trace.http)
                and finished["counters"]["llm_calls"] == observed_llm)
    except BudgetStop as exc:
        row.update(actual_outcome="measurement_stopped", error=str(exc), reason="money_or_request_guard")
        trace.complete = False
    except Exception as exc:
        row["error"] = type(exc).__name__
        trace.complete = False
    row.update(telemetry=trace.export(), elapsed_seconds=round(time.perf_counter() - began, 6))
    trace.emit("trial_end", case_id=case["id"], actual_outcome=row["actual_outcome"])
    return present(row, case)
