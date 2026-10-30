"""M4入口：离线检查、真实模型反馈对照、真实地图+模型规划。"""

import argparse
import json
import time
from datetime import datetime
from importlib.metadata import version

from app.config import get_settings
from app.m1_models import BudgetLimits, StartRun
from app.m1_store import StateError, StateStore
from app.m2 import CHINA_TZ, run_meta
from app.m2_client import MapClient
from app.m2_models import MapToolError
from app.m2_tools import MapTools
from app.m3 import compact_result
from app.m3_baseline import error_reason, finish_planning
from app.m4_agent import B1Planner
from app.m4_decider import LLMDecider
from app.m4_models import DecisionError
from app.m4_synthetic import CASE_NAMES, run_case


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def choose_origin(role, query, result):
    print(f"\n{role.upper()}的起点：{query}。核对后选择，系统不会替你猜。", flush=True)
    for index, place in enumerate(result.places, 1):
        print(f"{index}. {place.name} | {place.address or '未提供地址'} | {place.poi_id}", flush=True)
    while True:
        try:
            answer = input("输入序号确认；不确定请输入q：").strip()
        except EOFError:
            return None
        if answer.lower() == "q":
            return None
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(result.places):
            return int(answer)
        print("序号无效，请重新输入。", flush=True)


def progress(event):
    if event["step"] == "decision":
        show({"progress": {"step": "decision", "decision": event["decision"], "reason": event["reason"]}})
    else:
        show({"progress": event})


def brief(report):
    result = report["result"]
    return {k: v for k, v in report.items() if k != "result"} | {"result": {
        "policy": result["policy"], "outcome": result["outcome"],
        "termination_reason": result["termination_reason"],
        "actions": [r["action"] for r in result["rounds"]],
        "feasible_count": result["feasible_count"], "pending_user": result["pending_user"],
    }}


def llm_check(args):
    settings, reports = get_settings(), []
    for case in ("a_time", "b_time"):
        report = run_case(case, decider_factory=lambda store, rid: LLMDecider(
            settings, store, "demo_user", rid,
        ))
        reports.append(report)
        show(report if args.details else brief(report))
    actual = [[r["action"] for r in report["result"]["rounds"]] for report in reports]
    expected = [["middle", "toward_a"], ["middle", "toward_b"]]
    passed = actual == expected and all(
        r["result"]["feasible_count"] == 3 and r["model"]["real_llm_attempts"] > 0
        and r["error"] is None for r in reports
    )
    show({"feedback_pair_passed": passed, "actual_actions": actual, "expected_actions": expected,
          "real_map_http_attempts": 0,
          "real_llm_attempts": sum(r["model"]["real_llm_attempts"] for r in reports),
          "note": "自建反馈对照，仅验证本模型本次是否据反馈改变动作，不是M7效果评测。"})
    return 0 if passed else 1


def live(args):
    store = run = client = decider = planner = None
    error, reason, started = None, "api_error", time.perf_counter()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        datetime.strptime(args.time, "%H:%M")
        settings = get_settings()
        store = StateStore()
        task = store.get_task(args.user_id, args.session_id)
        if task["constraints_version"] != args.expected_version:
            raise StateError("version_conflict", "请重新读取任务版本后运行。")
        run = store.start_run(args.session_id, StartRun(user_id=args.user_id,
                              expected_version=args.expected_version),
                              limits=BudgetLimits(max_map_http=args.max_map_http,
                                                  max_llm_calls=args.max_llm_calls),
                              execution_mode="m4_live")
        show({"meta": run_meta(), "langgraph_version": version("langgraph"),
              "run_id": run["run_id"], "execution_mode": "m4_live", "policy": "B1",
              "departure_date": args.date, "departure_time": args.time,
              "constraints_version": task["constraints_version"], "requirements": task["requirements"]})
        decider = LLMDecider(settings, store, args.user_id, run["run_id"])
        client = MapClient(settings, store, args.user_id, run["run_id"])
        planner = B1Planner(MapTools(client, args.date, args.time), task, decider,
                            choose_origin=choose_origin, on_event=progress)
        reason = planner.run()
        error = planner.error
    except (KeyboardInterrupt, EOFError):
        reason, error = "manual_stop", {"code": "manual_stop", "message": "用户结束本次运行。"}
    except StateError as exc:
        reason, error = error_reason(exc), {"code": exc.code, "message": exc.message, **exc.details}
    except MapToolError as exc:
        reason, error = error_reason(exc), exc.as_dict()
    except DecisionError as exc:
        reason, error = "llm_error", {"code": exc.code, "message": exc.message}
    except ValueError:
        reason, error = "api_error", {"code": "invalid_local_input", "message": "参数或配置格式不正确。"}
    finally:
        if client:
            client.close()
        if decider:
            decider.close()
    finished, result = None, None
    if store is not None and run is not None:
        try:
            finished = finish_planning(store, args.user_id, run["run_id"], reason)
            if planner is not None:
                result = planner.summary(finished["stop_reason"])
        except StateError as exc:
            error = {"code": exc.code, "message": exc.message, **exc.details}
            finished = store.get_run(args.user_id, run["run_id"])
    show({"result": result if args.details or result is None else compact_result(result),
          "error": error, "run": finished,
          "real_map_http_attempts": client.attempts_started if client else 0,
          "map_retries": client.retries_started if client else 0,
          "model": decider.metadata() if decider else None,
          "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description="MeetPoint M4 反馈决策Agent")
    sub = parser.add_subparsers(dest="mode", required=True)
    offline = sub.add_parser("offline", help="完全离线；脚本模型仅检查循环执行")
    offline.add_argument("--case", choices=["all", *CASE_NAMES], default="all")
    offline.add_argument("--details", action="store_true")
    check = sub.add_parser("llm-check", help="真实模型+合成地图反馈；不请求高德")
    check.add_argument("--details", action="store_true")
    online = sub.add_parser("live", help="真实地图与模型，起点需人工核对")
    online.add_argument("--session-id", required=True)
    online.add_argument("--user-id", default="demo_user")
    online.add_argument("--expected-version", type=int, required=True)
    online.add_argument("--date", default=datetime.now(CHINA_TZ).date().isoformat())
    online.add_argument("--time", default="10:00")
    online.add_argument("--max-map-http", type=int, choices=range(1, 41), default=40)
    online.add_argument("--max-llm-calls", type=int, choices=range(1, 13), default=12)
    online.add_argument("--details", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "live":
            return live(args)
        show({"meta": run_meta(), "langgraph_version": version("langgraph"),
              "execution_mode": "m4_llm_check" if args.mode == "llm-check" else "m4_offline"})
        if args.mode == "llm-check":
            return llm_check(args)
        selected = CASE_NAMES if args.case == "all" else [args.case]
        for case in selected:
            report = run_case(case)
            show(report if args.details else brief(report))
        show({"offline_cases_passed": len(selected), "offline_cases_total": len(selected),
              "real_map_http_attempts": 0, "real_llm_attempts": 0,
              "note": "脚本决策器通过不等于真实模型通过，请继续llm-check。"})
        return 0
    except (StateError, DecisionError) as exc:
        show({"error": {"code": exc.code, "message": exc.message}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
