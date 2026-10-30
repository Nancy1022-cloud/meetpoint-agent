"""M12 final evaluation orchestration. Adds files only; never patches the product."""
import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.m8_cases import digest, calibration_ids
from app.m8_trace import append_json, exclusive_run, read_jsonl, SpendLedger, valid_usage

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ("B0", "RULE", "V1", "V2")
GROUPS = ("planning", "refusal", "abnormal", "memory")
NOTE = ("作者自建、作者可见且有相关场景族；不是独立盲测。主评测使用受控地图工具，"
        "真实高德HTTP为0。V1=原M9搜索与范围规则；V2=M10搜索优化；"
        "两臂Memory均为原受控解析器，40条Memory不证明M11自然语言解析能力。")


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def stamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files(root=None):
    root = Path(ROOT if root is None else root)
    paths = sorted(p for p in (root/"app").rglob("*") if p.is_file()
                   and p.suffix in (".py", ".js", ".html") and "__pycache__" not in p.parts)
    paths += [root/p for p in ("requirements.txt", "pyproject.toml", "uv.lock") if (root/p).is_file()]
    return {p.relative_to(root).as_posix(): sha(p) for p in paths}


def verify_suite(root):
    data, meta = read(Path(root)/"suite.json"), read(Path(root)/"dataset_manifest.json")
    if digest(data) != meta["suite_sha256"]:
        raise ValueError("M8用例哈希不符；不要重新freeze覆盖旧数据。")
    for split in ("dev", "holdout"):
        ids = [c["id"] for c in data[split]]
        if len(ids) != len(set(ids)) or len(ids) != meta["counts"][split]:
            raise ValueError("用例数量或ID不符")
    if set(c["id"] for c in data["dev"]) & set(c["id"] for c in data["holdout"]):
        raise ValueError("开发集与预留集ID重叠")
    return data, meta


def select_cases(data, stage):
    if stage == "final":
        cases = data["holdout"]
        if len(cases) != 200 or Counter(c["group"] for c in cases) != Counter(
                planning=80, refusal=40, abnormal=40, memory=40):
            raise ValueError("预留集与预定200条分组不符，停止而不是补造用例。")
        return cases
    if stage == "pilot":
        ids = set(calibration_ids(data["dev"]))
        return [c for c in data["dev"] if c["id"] in ids]
    return data["dev"]


def plan_trials(cases, mode, repeats, policies=POLICIES):
    result = []
    for c in cases:
        for repeat in range(1, repeats+1):
            # Alternate arm order by repeat, fixed in the plan before execution.
            order = policies if repeat % 2 else tuple(reversed(policies))
            for policy in order:
                if policy in ("B0", "RULE") and c["group"] != "planning":
                    continue
                result.append({"trial_id": f"{c['id']}:{policy}:{mode}:{repeat}",
                               "case_id": c["id"], "policy": policy, "mode": mode, "repeat": repeat})
    return result


def doctor(args):
    from app.config import get_settings
    s = get_settings()
    data, meta = verify_suite(args.data_root)
    required = ("m8_eval.py", "m9_metrics.py", "m10_eval.py", "m10_metrics.py", "m11_parse.py", "m12_report.py")
    runtime = (ROOT/"app/m6_runtime.py").read_text(encoding="utf-8-sig")
    main = (ROOT/"app/main.py").read_text(encoding="utf-8-sig")
    ready = all((ROOT/"app"/p).is_file() for p in required)
    ready = ready and "M10_INTEGRATION_V1" in runtime and "m11_router" in main
    result = {"milestone": "M12", "integration_ready": ready,
        "configured_model": s.llm_model, "llm_key_present": bool(s.llm_api_key.get_secret_value().strip()),
        "model_matches_previous": s.llm_model == "qwen3.7-flash" or s.llm_model.startswith("qwen3.7-flash-2026-"),
        "beijing_endpoint": s.llm_base_url.rstrip("/") == "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "suite_sha256": meta["suite_sha256"], "counts": {k: len(data[k]) for k in ("dev", "holdout")},
        "holdout_executed_by_this_command": False, "real_map_http": 0,
        "known_M11_NL_development_result": "31/36, 86.1%; target90% not passed",
        "note": NOTE}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ready else 1


def prepare(args):
    from app.config import get_settings
    s = get_settings()
    data, meta = verify_suite(args.data_root)
    cases = select_cases(data, args.stage)
    mode = "scripted" if args.stage == "smoke" else "llm"
    policies = tuple(args.policies.split(","))
    if not policies or len(set(policies)) != len(policies) or not set(policies) <= set(POLICIES):
        raise ValueError("策略只支持B0,RULE,V1,V2，不能重复。")
    if not {"V1", "V2"} <= set(policies):
        raise ValueError("正式对照必须包含V1和V2")
    if not all(math.isfinite(v) for v in (args.batch_yuan, args.total_yuan)) or not 0 < args.batch_yuan <= args.total_yuan <= 100:
        raise ValueError("估值限额须满足0<单批<=累计<=100")
    batch = Path(args.root)/args.stage
    if batch.exists():
        m, _, _, _, _ = load_batch(batch, current=True)
        same = (m["suite_sha256"] == meta["suite_sha256"] and m["repeats"] == args.repeats
                and m["policies"] == list(policies) and m["configured_model"] == s.llm_model
                and m["money_limits"] == {"batch_yuan": args.batch_yuan, "project_yuan": args.total_yuan})
        if not same:
            raise ValueError("已有清单与参数不同；禁止覆盖。保留旧批次，另选明确的新版本目录。")
        print(json.dumps({"status": "already_prepared_verified", "batch": str(batch)}))
        return 0
    if args.stage == "final":
        from app.m12_report import forecast
        prediction = forecast(Path(args.root)/"pilot")
        if prediction["policies"] != list(policies):
            raise ValueError("正式策略与pilot不同；请先校准相同策略。")
        if prediction["three_times_buffer_yuan"] > args.batch_yuan:
            raise ValueError("pilot外推的三倍费用缓冲超过单批估值上限，请先核对预算。")
    plans = plan_trials(cases, mode, args.repeats, policies)
    files = source_files()
    packages = {"python": platform.python_version()}
    for p in ("openai", "pydantic", "langgraph", "langgraph-checkpoint-sqlite"):
        packages[p] = importlib.metadata.version(p)
    manifest = {"schema_version": 1, "milestone": "M12", "batch_id": f"m12_{args.stage}_{uuid4().hex[:12]}",
        "created_at": stamp(), "stage": args.stage, "split": "holdout" if args.stage == "final" else "dev",
        "mode": mode, "model": s.llm_model if mode == "llm" else "not_called", "configured_model": s.llm_model,
        "endpoint": s.llm_base_url.rstrip("/"), "repeats": args.repeats, "policies": list(policies),
        "suite_sha256": meta["suite_sha256"], "selected_cases_sha256": digest(cases),
        "source_files_sha256": files, "source_digest": digest(files), "packages": packages,
        "data_root": str(Path(args.data_root)), "primary_policy": "V2", "trials": plans,
        "money_limits": {"batch_yuan": args.batch_yuan, "project_yuan": args.total_yuan},
        "pricing": {"input": .2, "output": .8, "unit": "CNY/million_tokens", "region": "Beijing",
                    "basis": "M8 retained historical estimate; NOT current quote or invoice"},
        "parameters": {"temperature": 0, "max_tokens": 512, "enable_thinking": False, "sdk_max_retries": 0},
        "thresholds": {"acceptance_each_group": .9, "citation_precision": 1, "citation_coverage": 1,
                       "refusal_precision": .9, "refusal_recall": .9, "false_refusal_rate_max": .05},
        "scope": NOTE, "prior_holdout_exposure": "not_independently_verified_author_visible",
        "known_M11_NL": {"stage": "observed_development", "passed": 31, "planned": 36, "quality_gate_passed": False},
        "online_validation": "not_executed_by_this_entry; optional and separate"}
    batch.mkdir(parents=True, exist_ok=False)
    dump(batch/"manifest.json", manifest)
    (batch/"manifest.sha256").write_text(digest(manifest)+"\n", encoding="utf-8")
    dump(batch/"cases.json", cases)
    for name in files:
        dest = batch/"source_snapshot"/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    print(json.dumps({"batch": str(batch), "stage": args.stage, "unique_tasks": len(cases),
        "planned": len(plans), "primary_two_arms": sum(t["policy"] in ("V1", "V2") for t in plans),
        "source_digest": manifest["source_digest"], "model_calls_made": 0,
        "money_limits_are_historical_estimates": True}, ensure_ascii=False, indent=2))
    return 0


def load_batch(batch, current=False):
    batch = Path(batch)
    m, cases = read(batch/"manifest.json"), read(batch/"cases.json")
    if m.get("milestone") != "M12" or (batch/"manifest.sha256").read_text().strip() != digest(m):
        raise ValueError("M12清单哈希不符")
    if digest(cases) != m["selected_cases_sha256"] or digest(m["source_files_sha256"]) != m["source_digest"]:
        raise ValueError("用例或源码清单哈希不符")
    for name, expected in m["source_files_sha256"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("不安全的快照路径")
        if not (batch/"source_snapshot"/name).is_file() or sha(batch/"source_snapshot"/name) != expected:
            raise ValueError("源码快照哈希不符："+name)
    if current and source_files() != m["source_files_sha256"]:
        raise ValueError("当前源码与冻结清单不同；不要混用结果。可从原source_snapshot离线重算报告。")
    rows, re = read_jsonl(batch/"results.jsonl")
    events, ee = read_jsonl(batch/"events.jsonl")
    return m, cases, rows, events, re+ee


def resumable(manifest, rows, events, errors):
    if errors:
        raise ValueError("JSONL有损坏行；保留原记录，不能自动删行继续。")
    planned = {t["trial_id"] for t in manifest["trials"]}
    ids = [r.get("trial_id") for r in rows]
    starts = [e.get("trial_id") for e in events if e.get("event") == "trial_start"]
    if len(set(ids)) != len(ids) or len(set(starts)) != len(starts) or set(ids) != set(starts) or not set(ids) <= planned:
        raise ValueError("存在重复、未计划或已启动但缺结果的试次；先生成部分报告，不自动重跑已启动试次。")
    for row in rows:
        t = row.get("telemetry", {})
        if not t.get("complete") or any(not valid_usage(a.get("usage")) for a in t.get("llm", [])):
            raise ValueError("已有缺测/未知usage；先保留部分报告并核对，禁止自动扩大批次。")
    if rows:
        # Check completed records, not pending trials, before making another call.
        from app.m10_metrics import summarize
        completed_manifest = {**manifest, "trials": [t for t in manifest["trials"] if t["trial_id"] in set(ids)]}
        # Loaded batches carry immutable cases; the caller validates their hash.
        completed_cases = manifest.get("_resume_cases")
        if completed_cases is not None:
            audit = summarize(completed_manifest, completed_cases, rows, events, [])
            if not audit["measurement_complete"] or not audit["hard_checks_passed"]:
                raise ValueError("已有结果的事件或硬检查不完整；禁止自动继续请求。")
    return [t for t in manifest["trials"] if t["trial_id"] not in set(ids)]


def run(args):
    from app.config import get_settings
    from app.m8 import no_network
    from app.m10_eval import run_case
    from app.m12_report import report, result_exit
    batch = Path(args.batch)
    m, cases, rows, events, errors = load_batch(batch, current=True)
    todo = resumable({**m, "_resume_cases": cases}, rows, events, errors)
    s = get_settings()
    if m["mode"] == "llm":
        if s.llm_model != m["model"] or s.llm_base_url.rstrip("/") != m["endpoint"]:
            raise ValueError("模型或端点与清单不同")
        if not (s.llm_model == "qwen3.7-flash" or s.llm_model.startswith("qwen3.7-flash-2026-")):
            raise ValueError("沿用已测qwen3.7-flash；换模型请另建实验并重新校准")
        if m["endpoint"] != "https://dashscope.aliyuncs.com/compatible-mode/v1" or not s.llm_api_key.get_secret_value().strip():
            raise ValueError("请核对北京端点与自己.env中的Key")
    data, meta = verify_suite(m["data_root"])
    if meta["suite_sha256"] != m["suite_sha256"]:
        raise ValueError("原M8数据发生变化")
    by_id = {c["id"]: c for c in cases}
    completed, reason, paused = len(rows), None, False
    with exclusive_run(m["data_root"]):
        ledger = SpendLedger(m["data_root"], m["batch_id"], m["money_limits"]["batch_yuan"], m["money_limits"]["project_yuan"])
        before = ledger.spent()  # Includes old and unknown reservations.
        if not todo:
            r = report(batch)
            print(json.dumps({"status": "no_pending_trials", "summary": str(batch/"summary.json")}, ensure_ascii=False))
            return result_exit(r)
        if m["stage"] == "final":
            append_json(Path(m["data_root"])/"m12_holdout_access.jsonl", {"event": "M12_run_started", "batch_id": m["batch_id"], "time": stamp()})
        try:
            with no_network() if m["mode"] == "scripted" else nullcontext():
                for index, trial in enumerate(todo):
                    if args.max_new_trials and index >= args.max_new_trials:
                        paused = True
                        break
                    row = run_case(by_id[trial["case_id"]], trial["policy"], m["mode"], trial["trial_id"],
                        settings=s, ledger=ledger if m["mode"] == "llm" else None,
                        sink=lambda e: append_json(batch/"events.jsonl", e))
                    row["repeat"] = trial["repeat"]
                    append_json(batch/"results.jsonl", row)
                    completed += 1
                    print(json.dumps({"done": completed, "planned": len(m["trials"]), "case_id": trial["case_id"],
                                      "policy": trial["policy"], "repeat": trial["repeat"], "outcome": row["actual_outcome"]}, ensure_ascii=False), flush=True)
                    telemetry = row["telemetry"]
                    if not telemetry["complete"] or any(not valid_usage(a.get("usage")) for a in telemetry["llm"]):
                        reason = "measurement_or_usage_incomplete"
                        break
                    if any(a.get("status") == "error" for a in telemetry["llm"]):
                        reason = "model_request_failed"
                        break
        except KeyboardInterrupt:
            reason = "KeyboardInterrupt"
        finally:
            dump(batch/"run_status.json", {"completed_results": completed, "planned": len(m["trials"]),
                "paused_at_trial_boundary": paused, "interrupted": reason, "time": stamp(),
                "project_estimated_spend_before": before, "project_estimated_spend_or_reserved_after": ledger.spent()})
            r = report(batch)
    print(json.dumps({"batch": str(batch), "measurement_complete": r["measurement_complete"],
        "hard_checks_passed": r["hard_checks_passed"], "quality_targets_passed": r["quality_targets_passed"],
        "M12_delivery_complete": r["M12_delivery_complete"], "paused_at_trial_boundary": paused}, ensure_ascii=False, indent=2))
    return 0 if paused and reason is None else result_exit(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor"); p.add_argument("--data-root", default="data/m8/v1")
    p = sub.add_parser("prepare")
    p.add_argument("--stage", choices=["smoke", "pilot", "final"], required=True)
    p.add_argument("--data-root", default="data/m8/v1"); p.add_argument("--root", default="data/m12/v1")
    p.add_argument("--repeats", type=int, choices=[1, 2, 3], default=3)
    p.add_argument("--policies", default=",".join(POLICIES))
    p.add_argument("--batch-yuan", type=float, default=2.); p.add_argument("--total-yuan", type=float, default=5.)
    p = sub.add_parser("run"); p.add_argument("--batch", required=True); p.add_argument("--max-new-trials", type=int, default=0)
    for name in ("report", "forecast", "package"):
        p = sub.add_parser(name); p.add_argument("--batch", required=True)
    args = ap.parse_args()
    try:
        if args.command == "doctor": return doctor(args)
        if args.command == "prepare": return prepare(args)
        if args.command == "run":
            if args.max_new_trials < 0: raise ValueError("max-new-trials不能为负")
            return run(args)
        from app.m12_report import report, forecast, package, result_exit
        if args.command == "forecast":
            print(json.dumps(forecast(args.batch), ensure_ascii=False, indent=2)); return 0
        if args.command == "package":
            print(json.dumps(package(args.batch), ensure_ascii=False, indent=2)); return 0
        r = report(args.batch)
        print(json.dumps({k: r[k] for k in ("planned", "started", "valid_results", "measurement_complete", "hard_checks_passed", "quality_targets_passed", "M12_delivery_complete")}, ensure_ascii=False, indent=2))
        return result_exit(r)
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
