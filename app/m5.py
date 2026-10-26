"""M5入口：查看持久化边界，或从已保存的条件发起新的真实规划。"""

import argparse
import json
import time
import tempfile
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.m1_models import BudgetLimits, StartRun
from app.m1_store import StateError
from app.m2 import CHINA_TZ, run_meta
from app.m2_client import MapClient
from app.m2_models import MapToolError
from app.m2_tools import MapTools
from app.m3 import compact_result
from app.m3_baseline import error_reason, finish_planning
from app.m4 import choose_origin, progress
from app.m4_decider import LLMDecider
from app.m4_models import DecisionError
from app.m5_checkpoint import MemoryPlanner, inspect_checkpoint, saver_for
from app.m5_store import MemoryStore


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def offline():
    from app.m4_synthetic import ScriptedDecider, SyntheticTools
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "demo.sqlite3")
        initial = store.create_memory_task({"user_id": "demo_user",
            "person_a": {"origin_text": "合成起点A"}, "person_b": {"origin_text": "合成起点B"}})
        sid = initial["task"]["session_id"]
        changed = store.apply_message(sid, {"user_id": "demo_user", "expected_version": 1,
            "text": "我这次接受60分钟，朋友仍然最多45分钟，其他不变"})
        assert changed["changed_fields"] == ["person_a.max_duration_seconds"]
        assert changed["task"]["constraints_version"] == 2
        saved = store.apply_message(sid, {"user_id": "demo_user", "expected_version": 2,
            "text": "以后默认最多换乘零次", "expected_preferences_revision": 0})
        assert saved["task"]["requirements"]["person_a"]["max_transfers"] == 1
        fresh = store.create_memory_task({"user_id": "demo_user", "person_a": {"origin_text": "合成起点A"},
            "person_b": {"origin_text": "合成起点B"}})
        assert fresh["task"]["requirements"]["person_a"]["max_transfers"] == 0
        recovered = MemoryStore(store.path).get_memory("demo_user", sid)
        assert recovered["task"]["requirements"]["person_a"]["max_duration_seconds"] == 3600
        # 原会话的明确条件保持；真实SQLite检查点，工具/决策器全部为合成替身。
        task = recovered["task"]
        run = store.start_run(sid, StartRun(user_id="demo_user", expected_version=2), execution_mode="m5_live")
        tools, decider = SyntheticTools(store, run["run_id"], "empty"), ScriptedDecider(store, run["run_id"])
        with saver_for(store) as saver:
            planner = MemoryPlanner(tools, task, decider, saver, choose_origin=lambda *args: 1)
            reason = planner.run()
        finished = finish_planning(store, "demo_user", run["run_id"], reason)
        audit = inspect_checkpoint(store, "demo_user", run["run_id"])
        assert audit["persistent_field_boundary_passed"]
        show({"data_source": "self_authored_synthetic", "decision_source": "scripted_test_double",
              "memory_checks_passed": True, "changed_fields": changed["changed_fields"],
              "same_session_A_seconds": 3600, "new_session_A_transfers": 0,
              "current_session_A_transfers": 1, "checkpoint_audit": audit,
              "simulated_counters": finished["counters"], "real_map_http_attempts": 0,
              "real_llm_attempts": 0, "note": "临时自建数据在退出后清理；不是M7效果评测。"})
    return 0


def live(args):
    store, run, client, decider, planner = MemoryStore(), None, None, None, None
    reason, error, started = "api_error", None, time.perf_counter()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        datetime.strptime(args.time, "%H:%M")
        task = store.get_task(args.user_id, args.session_id)
        settings = get_settings()
        run = store.start_run(args.session_id, StartRun(user_id=args.user_id,
            expected_version=args.expected_version), limits=BudgetLimits(
                max_map_http=args.max_map_http, max_llm_calls=args.max_llm_calls), execution_mode="m5_live")
        show({"meta": run_meta(), "execution_mode": "m5_live", "run_id": run["run_id"],
              "constraints_version": task["constraints_version"], "requirements": task["requirements"],
              "departure_date": args.date, "departure_time": args.time,
              "resume_policy": "read_confirmed_conditions_then_requery"})
        client = MapClient(settings, store, args.user_id, run["run_id"])
        decider = LLMDecider(settings, store, args.user_id, run["run_id"])
        with saver_for(store) as saver:
            planner = MemoryPlanner(MapTools(client, args.date, args.time), task, decider, saver,
                                    choose_origin=choose_origin, on_event=progress)
            reason = planner.run()
            error = planner.error
    except (KeyboardInterrupt, EOFError):
        reason, error = "manual_stop", {"code": "manual_stop", "message": "用户停止本次规划。"}
    except StateError as exc:
        reason, error = error_reason(exc), {"code": exc.code, "message": exc.message, **exc.details}
    except MapToolError as exc:
        reason, error = error_reason(exc), exc.as_dict()
    except DecisionError as exc:
        reason, error = "llm_error", {"code": exc.code, "message": exc.message}
    except ValueError:
        reason, error = "api_error", {"code": "invalid_input", "message": "请核对参数与配置。"}
    finally:
        if client:
            client.close()
        if decider:
            decider.close()
    finished, result = None, None
    if run:
        try:
            finished = finish_planning(store, args.user_id, run["run_id"], reason)
            if planner:
                result = planner.summary(finished["stop_reason"])
        except StateError as exc:
            error = {"code": exc.code, "message": exc.message, **exc.details}
            finished = store.get_run(args.user_id, run["run_id"])
    show({"result": result if args.details or result is None else compact_result(result),
          "run": finished, "error": error,
          "real_map_http_attempts": client.attempts_started if client else 0,
          "model": decider.metadata() if decider else None,
          "elapsed_seconds": round(time.perf_counter() - started, 3)})
    if run:
        show({"checkpoint_audit": inspect_checkpoint(store, args.user_id, run["run_id"])})
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description="MeetPoint M5：条件、显式偏好与安全检查点")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("offline", help="受控句式、条件恢复和真实SQLite检查点；零外部请求")
    audit = sub.add_parser("audit", help="检查数据库表及指定运行的持久化字段")
    audit.add_argument("--user-id", default="demo_user")
    audit.add_argument("--run-id")
    online = sub.add_parser("live", help="以最新条件重新查询路线与规划；需人工确认起点")
    online.add_argument("--session-id", required=True)
    online.add_argument("--user-id", default="demo_user")
    online.add_argument("--expected-version", required=True, type=int)
    online.add_argument("--date", default=datetime.now(CHINA_TZ).date().isoformat())
    online.add_argument("--time", default="10:00")
    online.add_argument("--max-map-http", type=int, choices=range(1, 41), default=40)
    online.add_argument("--max-llm-calls", type=int, choices=range(1, 13), default=12)
    online.add_argument("--details", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "offline":
            return offline()
        if args.mode == "live":
            return live(args)
        store = MemoryStore()
        show({"storage": store.inspect(), "memory_schema_version": 1})
        if args.run_id:
            audit = inspect_checkpoint(store, args.user_id, args.run_id)
            show(audit)
            return 0 if audit["persistent_field_boundary_passed"] else 1
        return 0
    except StateError as exc:
        show({"error": {"code": exc.code, "message": exc.message}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())