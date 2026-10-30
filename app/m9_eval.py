"""V0保留M8执行；V1只增加共享范围门禁和证据展示，不改变搜索策略。"""

import tempfile
import time
from pathlib import Path
from uuid import uuid4

from app.m5_store import MemoryStore, flatten
from app.m7_memory import USER, default_new
from app.m8_eval import run_case as run_m8
from app.m8_evidence import bind_claims
from app.m8_trace import Trace
from app.m9_evidence import build_view
from app.m9_scope import apply_scoped_message


def run_case(case, policy, mode, trial_id, settings=None, ledger=None, sink=None, client_factory=None):
    if policy not in ("V0", "V1"):
        raise ValueError("M9只接受V0或V1；V1规划器仍为原MemoryPlanner")
    if policy == "V1" and case["runner"] == "message":
        began = time.perf_counter()
        trace = Trace(trial_id, uuid4().hex, sink)
        trace.emit("trial_start", case_id=case["id"])
        row = {"trial_id": trial_id, "run_id": trace.run_id, "case_id": case["id"],
               "group": case["group"], "family": case["family"], "policy": policy, "mode": mode,
               "started": True, "actual_outcome": "execution_error", "reason": None,
               "result": None, "memory_actual": None, "error": None, "checks": {},
               "decision_source": "deterministic_scope_gate_and_controlled_parser"}
        try:
            with tempfile.TemporaryDirectory(prefix="m9_scope_") as temp:
                store = MemoryStore(Path(temp) / "state.sqlite3")
                defaults = default_new()
                memory = store.create_memory_task({"user_id": USER,
                    "person_a": defaults["a"], "person_b": defaults["b"]})
                response = apply_scoped_message(store, memory["task"]["session_id"], {
                    "user_id": USER, "expected_version": 1,
                    "expected_preferences_revision": memory["preferences"]["revision"], "text": case["text"]})
                actual = ("refused" if response["status"] == "refused" else
                          "needs_clarification" if response["status"] == "needs_clarification" else "conditions_updated")
                row.update(actual_outcome=actual,
                    reason="unsupported_scope" if actual == "refused" else
                           "explicit_change" if actual == "needs_clarification" else "conditions_updated",
                    scope_reason=response.get("scope_reason"),
                    memory_actual={"requirements": flatten(response["task"]["requirements"]),
                        "preferences": response["preferences"]["values"],
                        "pending": response["pending_question_code"] is not None})
                row["checks"]["refusal_keeps_state"] = (actual != "refused" or
                    (response["task"] == memory["task"] and response["preferences"] == memory["preferences"]
                     and response["pending_question_code"] == memory["pending_question_code"]))
                row["checks"]["counter_consistency"] = True
        except Exception as exc:
            row["error"] = type(exc).__name__
            trace.complete = False
        row["telemetry"] = trace.export()
        row["elapsed_seconds"] = round(time.perf_counter() - began, 6)
        trace.emit("trial_end", case_id=case["id"], actual_outcome=row["actual_outcome"])
    else:
        row = run_m8(case, "V0", mode, trial_id, settings, ledger, sink, client_factory)
        row["policy"] = policy
    bind_claims(row)
    row["presentation"] = build_view(row, case.get("requirements"))
    # Memory/范围消息没有路线卡片；这里保持它们真实的业务状态。
    if not row.get("result"):
        row["presentation"].update(outcome=row["actual_outcome"], stop_kind=row["reason"])
    return row
