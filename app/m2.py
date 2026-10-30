import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.m1_models import BudgetLimits, StartRun
from app.m1_store import StateError, StateStore
from app.m2_client import MapClient
from app.m2_models import MapToolError
from app.m2_tools import MapTools

CHINA_TZ = timezone(timedelta(hours=8))


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def run_meta():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {"tested_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
            "code_version": digest.hexdigest()[:16], "api_version": "v5"}


def select_origin(tools, role, query):
    result = tools.resolve_place(role, query)
    print(f"\n{role.upper()} 的起点：{query}；解析状态：{result.status}", flush=True)
    if result.status != "ok":
        show(result.model_dump())
        return None, result.status
    for index, place in enumerate(result.places, 1):
        print(f"{index}. {place.name} | {place.address or '未提供地址'} | "
              f"{place.location} | {place.poi_id}", flush=True)
    while True:
        try:
            answer = input("核对名称/地址后输入序号；不确定请输入 q：").strip()
        except EOFError:
            return None, "needs_clarification"
        if answer.lower() == "q":
            return None, "needs_clarification"
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(result.places):
            place = result.places[int(answer) - 1]
            return tools.confirm_origin(role, place.poi_id), "ok"
        print("序号不在列表内，请重新输入。", flush=True)


def probe(tools, task):
    for role in ("a", "b"):
        _, status = select_origin(tools, role, task["requirements"][f"person_{role}"]["origin_text"])
        if status != "ok":
            return status, {"stage_result": status}
    search = tools.search_meeting_places(tools.midpoint(), radius=5000)
    print(f"\n商场搜索状态：{search.status}；本轮新增 {len(search.places)} 个候选。", flush=True)
    if search.status != "ok":
        return search.status, {"stage_result": search.status, "search": search.model_dump()}
    pairs = []
    for place in search.places:
        print(f"查询双方到 {place.name} 的路线……", flush=True)
        a = tools.get_transit_routes("a", place.poi_id)
        b = tools.get_transit_routes("b", place.poi_id)
        pairs.append({"candidate": place.model_dump(),
                      "a": a.model_dump(), "b": b.model_dump()})
    complete_pairs = sum(item["a"]["status"] == item["b"]["status"] == "ok" for item in pairs)
    missing = any(item[role]["status"] == "data_missing" for item in pairs for role in ("a", "b"))
    stage = "tools_checked" if complete_pairs else "data_missing" if missing else "no_route"
    return stage, {
        "stage_result": stage, "complete_route_pairs": complete_pairs,
        "departure_date": tools.departure_date, "departure_time": tools.departure_time,
        "note": "M2仅验收工具与字段；是否满足双方限制和推荐顺序在M3计算。",
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser(description="MeetPoint M2 地图工具联调")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--user-id", default="demo_user")
    parser.add_argument("--expected-version", type=int, required=True)
    parser.add_argument("--date", default=datetime.now(CHINA_TZ).date().isoformat())
    parser.add_argument("--time", default="10:00")
    parser.add_argument("--max-map-http", type=int, default=40, choices=range(1, 41))
    args = parser.parse_args()
    store, run, client = None, None, None
    try:
        settings = get_settings()
        # 无效日期/时间在创建运行前就报错；不发起任何地图请求。
        datetime.strptime(args.date, "%Y-%m-%d")
        datetime.strptime(args.time, "%H:%M")
        if not settings.amap_web_key.get_secret_value().strip():
            raise MapToolError("missing_amap_key", "请在 .env 中填写高德 Web 服务 Key。")
        store = StateStore()
        task = store.get_task(args.user_id, args.session_id)
        run = store.start_run(args.session_id, StartRun(
            user_id=args.user_id, expected_version=args.expected_version,
        ), limits=BudgetLimits(max_map_http=args.max_map_http), execution_mode="m2_live")
        show({"meta": run_meta(), "run_id": run["run_id"], "execution_mode": "m2_live",
              "constraints_version": run["constraints_version"]})
        client = MapClient(settings, store, args.user_id, run["run_id"])
        tools = MapTools(client, args.date, args.time)
        reason, result = probe(tools, task)
        store.assert_run_current(args.user_id, run["run_id"])
        finished = store.finish_run(args.user_id, run["run_id"], reason)
        show({"result": result, "run": finished,
              "http_attempts_started_this_process": client.attempts_started,
              "retries_started_this_process": client.retries_started})
        return 0
    except (KeyboardInterrupt, EOFError):
        reason, error = "manual_stop", {"code": "manual_stop", "message": "用户结束本次联调。"}
    except StateError as exc:
        reason, error = "api_error", {"code": exc.code, "message": exc.message, **exc.details}
    except MapToolError as exc:
        reason = "data_missing" if exc.code == "data_missing" else "api_error"
        error = exc.as_dict()
    except ValueError:
        reason, error = "api_error", {"code": "invalid_local_input", "message": "参数格式或配置不正确。"}
    finally:
        if client is not None:
            client.close()
    # 仅输出安全错误摘要；预算耗尽/旧版本状态由M1保留，不覆盖。
    summary = None
    if store is not None and run is not None:
        try:
            store.assert_run_current(args.user_id, run["run_id"])
            summary = store.finish_run(args.user_id, run["run_id"], reason)
        except StateError:
            summary = store.get_run(args.user_id, run["run_id"])
    show({"error": error, "run": summary,
          "http_attempts_started_this_process": client.attempts_started if client else 0})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())