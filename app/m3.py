import argparse
import json
from datetime import datetime

from app.config import get_settings
from app.m1_models import BudgetLimits, StartRun
from app.m1_store import StateError, StateStore
from app.m2 import CHINA_TZ, run_meta, select_origin
from app.m2_client import MapClient
from app.m2_models import MapToolError
from app.m2_tools import MapTools
from app.m3_baseline import B0Planner, error_reason, finish_planning
from app.m3_synthetic import cases, run_case


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def compact_result(result):
    keep = {k: v for k, v in result.items() if k not in ("candidate_checks", "recommendations")}
    keep["recommendations"] = [{
        "candidate_id": item["candidate"]["poi_id"], "name": item["candidate"]["name"],
        "a": {k: item["a"]["selected_route"][k] for k in (
            "route_id", "duration_seconds", "walk_meters", "transfers",
        )},
        "b": {k: item["b"]["selected_route"][k] for k in (
            "route_id", "duration_seconds", "walk_meters", "transfers",
        )},
        "metrics": item["metrics"],
    } for item in result["recommendations"]]
    return keep


def live(args):
    store = run = client = planner = None
    error, reason = None, "api_error"
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        datetime.strptime(args.time, "%H:%M")
        settings = get_settings()
        if not settings.amap_web_key.get_secret_value().strip():
            raise MapToolError("missing_amap_key", "请在.env中填写高德Web服务Key。")
        store = StateStore()
        task = store.get_task(args.user_id, args.session_id)
        # 防止读取旧快照、随后恰好以新版本启动，却使用旧快照规划。
        if task["constraints_version"] != args.expected_version:
            raise StateError("version_conflict", "请重新读取任务版本后运行。")
        run = store.start_run(args.session_id, StartRun(
            user_id=args.user_id, expected_version=args.expected_version,
        ), limits=BudgetLimits(max_map_http=args.max_map_http), execution_mode="m3_live")
        show({"meta": run_meta(), "run_id": run["run_id"], "execution_mode": "m3_live",
              "constraints_version": run["constraints_version"],
              "departure_date": args.date, "departure_time": args.time,
              "policy": "B0", "radii_meters": [1500, 3000, 5000],
              "requirements": task["requirements"]})
        client = MapClient(settings, store, args.user_id, run["run_id"])
        tools = MapTools(client, args.date, args.time)
        planner = B0Planner(tools, task, on_event=lambda event: show({"progress": event}))
        for role in ("a", "b"):
            _, status = select_origin(tools, role, task["requirements"][f"person_{role}"]["origin_text"])
            if status != "ok":
                reason = status
                break
        else:
            reason = planner.run()
    except (KeyboardInterrupt, EOFError):
        reason, error = "manual_stop", {"code": "manual_stop", "message": "用户结束本次规划。"}
    except StateError as exc:
        reason, error = error_reason(exc), {"code": exc.code, "message": exc.message, **exc.details}
    except MapToolError as exc:
        reason, error = error_reason(exc), exc.as_dict()
    except ValueError:
        reason, error = "api_error", {"code": "invalid_local_input", "message": "参数格式或配置不正确。"}
    finally:
        if client is not None:
            client.close()

    finished, result = None, None
    if store is not None and run is not None:
        try:
            finished = finish_planning(store, args.user_id, run["run_id"], reason)
            if planner is not None:
                result = planner.summary(finished["stop_reason"])
                result["departure_date"], result["departure_time"] = args.date, args.time
        except StateError as exc:
            # 条件变化等情况下，不展示旧候选、旧路线或旧推荐。
            error = {"code": exc.code, "message": exc.message, **exc.details}
            finished = store.get_run(args.user_id, run["run_id"])
    show({"result": result if args.details or result is None else compact_result(result),
          "error": error, "run": finished,
          "http_attempts_started_this_process": client.attempts_started if client else 0,
          "retries_started_this_process": client.retries_started if client else 0})
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description="MeetPoint M3 确定性规划核心")
    sub = parser.add_subparsers(dest="mode", required=True)
    offline = sub.add_parser("offline", help="自建合成场景，无需Key，不联网")
    offline.add_argument("--case", default="all", choices=["all", *cases()])
    offline.add_argument("--preference", default="balanced", choices=["balanced", "total_time"])
    offline.add_argument("--details", action="store_true")
    online = sub.add_parser("live", help="用M1条件和M2地图工具真实规划")
    online.add_argument("--session-id", required=True)
    online.add_argument("--user-id", default="demo_user")
    online.add_argument("--expected-version", type=int, required=True)
    online.add_argument("--date", default=datetime.now(CHINA_TZ).date().isoformat())
    online.add_argument("--time", default="10:00")
    online.add_argument("--max-map-http", type=int, default=40, choices=range(1, 41))
    online.add_argument("--details", action="store_true")
    args = parser.parse_args()
    if args.mode == "live":
        return live(args)
    show({"meta": run_meta(), "execution_mode": "m3_offline",
          "data_source": "self_authored_synthetic", "real_http_attempts": 0})
    selected = list(cases()) if args.case == "all" else [args.case]
    for case_id in selected:
        report = run_case(case_id, args.preference)
        if not args.details:
            report["result"] = compact_result(report["result"])
        show(report)
    show({"offline_cases_passed": len(selected), "offline_cases_total": len(selected),
          "real_http_attempts": 0, "llm_calls": 0,
          "note": "开发用合成场景通过；不代表M7策略效果或本机真实接口已验收。"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())