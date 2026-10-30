"""M9：沿用M8开发集，验证范围拒答、页面证据和终止状态；不运行预留集。"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.m4_decider import SYSTEM_PROMPT
from app.m8 import dump, fingerprint as fingerprint_m8, no_network, verify
from app.m8_cases import digest
from app.m8_metrics import summarize as summarize_m8
from app.m8_trace import SpendLedger, append_json, exclusive_run, read_jsonl, valid_usage
from app.m9_eval import run_case
from app.m9_metrics import diagnostics, markdown, summarize
from app.m9_evidence import PROTOCOL as EVIDENCE_PROTOCOL
from app.m9_scope import PROTOCOL as SCOPE_PROTOCOL


def stamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")


def fingerprint():
    data = fingerprint_m8()
    root = Path(__file__).resolve().parents[1]
    paths = [p.relative_to(root).as_posix() for p in (root / "app").glob("m9*.py")]
    paths += ["app/m5_api.py", "app/m6_runtime.py", "app/static/meetpoint.html", "app/static/m9_ui.js"]
    data["source_files_sha256"].update({p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths})
    data["source_digest"] = digest(data["source_files_sha256"])
    return data


def doctor():
    root = Path(__file__).resolve().parents[1]
    markers = {p: "M9_INTEGRATION_V1" in (root / p).read_text(encoding="utf-8-sig")
               for p in ("app/m5_api.py", "app/m6_runtime.py", "app/static/meetpoint.html")}
    settings = get_settings()
    return {"milestone": "M9", "integration": markers, "integration_ready": all(markers.values()),
        "configured_model": settings.llm_model,
        "llm_key_present": bool(settings.llm_api_key.get_secret_value().strip()),
        "model_matches_M8": settings.llm_model == "qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-"),
        "beijing_endpoint_for_budget": settings.llm_base_url.rstrip("/") == "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "map_network_used_by_M9_evaluation": False, "search_strategy_changed": False,
        "evidence_protocol": EVIDENCE_PROTOCOL, "scope_protocol": SCOPE_PROTOCOL,
        "fingerprint": fingerprint()}


def load_batch(batch):
    batch = Path(batch)
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8-sig"))
    if (batch / "manifest.sha256").read_text().strip() != digest(manifest):
        raise ValueError("批次清单哈希不符，禁止继续判分")
    cases = json.loads((batch / "cases.json").read_text(encoding="utf-8-sig"))
    if digest(cases) != manifest["selected_cases_sha256"]:
        raise ValueError("批次用例哈希不符，禁止继续判分")
    rows, re = read_jsonl(batch / "results.jsonl")
    events, ee = read_jsonl(batch / "events.jsonl")
    return manifest, cases, rows, events, re + ee


def make_report(batch):
    batch = Path(batch)
    manifest, cases, rows, events, errors = load_batch(batch)
    if manifest.get("milestone") != "M9":
        raise ValueError("M9 report只能写M9批次；不会修改M8归档")
    result = summarize(manifest, cases, rows, events, errors)
    result.update(model=manifest["model"], batch_id=manifest["batch_id"], source_digest=manifest["source_digest"])
    dump(batch / "summary.json", {k: v for k, v in result.items() if k != "failures"})
    (batch / "failures.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in result["failures"]), encoding="utf-8")
    (batch / "report.md").write_text(markdown(result), encoding="utf-8")
    if not errors:
        (batch / "search_diagnostics.md").write_text(diagnostics(rows, cases), encoding="utf-8")
    return result


def exit_code(result):
    return 3 if not result["measurement_complete"] or not result["hard_checks_passed"] else 0 if result["quality_targets_passed"] else 2


def run(args):
    data, meta = verify(args.data_root)
    if Path(args.root).resolve() == Path(args.data_root).resolve():
        raise ValueError("M9结果目录必须与M8数据目录分开")
    policies = args.policies.split(",")
    if len(policies) != len(set(policies)) or not set(policies) <= {"V0", "V1"}:
        raise ValueError("policies只支持V0,V1且不得重复")
    settings = get_settings()
    if args.mode == "llm":
        if not (settings.llm_model == "qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-")):
            raise ValueError("请沿用M8的qwen3.7-flash；更换模型须分开记录")
        if settings.llm_base_url.rstrip("/") != "https://dashscope.aliyuncs.com/compatible-mode/v1":
            raise ValueError("当前保护估价只适用M8北京端点")
        if not settings.llm_api_key.get_secret_value().strip():
            raise ValueError("缺少LLM_API_KEY；只在自己的.env填写")
    cases = data["dev"]
    batch_id = f"{args.mode}_dev_{stamp()}_{uuid4().hex[:6]}"
    batch = Path(args.root) / "runs" / batch_id
    batch.mkdir(parents=True, exist_ok=False)
    trials = [{"trial_id": f"{c['id']}:{p}:{args.mode}:{rep}", "case_id": c["id"],
               "policy": p, "mode": args.mode, "repeat": rep}
              for c in cases for rep in range(1, args.repeats + 1) for p in policies]
    manifest = {"schema_version": 1, "milestone": "M9", "batch_id": batch_id, "split": "dev",
        "calibration": False, "suite_sha256": meta["suite_sha256"], "selected_cases_sha256": digest(cases),
        "model": settings.llm_model if args.mode == "llm" else "not_called",
        "configured_model": settings.llm_model, "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "parameters": {"temperature": 0, "max_tokens": 512, "enable_thinking": False, "sdk_max_retries": 0},
        "scope_protocol": SCOPE_PROTOCOL, "evidence_protocol": EVIDENCE_PROTOCOL,
        "search_strategy_changed": False,
        "money_limits": {"batch_yuan": args.budget_yuan, "project_yuan": args.project_budget_yuan},
        "money_ledger_root": Path(args.data_root).as_posix(),
        "pricing": {"region": "Beijing", "basis": "M8 retained estimate, not a new price quotation",
                    "input": .2, "output": .8, "unit": "CNY/million_tokens", "not_invoice": True},
        "citation_scope": "eight_structured_fields_and_derived_time_in_M9_web_view",
        "thresholds": {"acceptance": .9, "citation_precision": 1, "citation_coverage": 1,
                       "refusal_precision": .9, "refusal_recall": .9, "false_refusal_rate_max": .05},
        "trials": trials, **fingerprint()}
    dump(batch / "manifest.json", manifest)
    (batch / "manifest.sha256").write_text(digest(manifest) + "\n", encoding="utf-8")
    dump(batch / "cases.json", cases)
    ledger = SpendLedger(args.data_root, batch_id, args.budget_yuan, args.project_budget_yuan)
    by_id, interrupted = {c["id"]: c for c in cases}, None
    try:
        # 与M8共用排他锁和费用账本，避免另建目录绕过累计保护。
        with exclusive_run(args.data_root):
            for index, trial in enumerate(trials, 1):
                kwargs = {"settings": settings, "ledger": ledger,
                          "sink": lambda e: append_json(batch / "events.jsonl", e)}
                if args.mode == "scripted":
                    with no_network():
                        row = run_case(by_id[trial["case_id"]], trial["policy"], args.mode, trial["trial_id"], **kwargs)
                else:
                    row = run_case(by_id[trial["case_id"]], trial["policy"], args.mode, trial["trial_id"], **kwargs)
                row["repeat"] = trial["repeat"]
                append_json(batch / "results.jsonl", row)
                print(json.dumps({"done": index, "planned": len(trials), "case_id": row["case_id"],
                                  "policy": row["policy"], "outcome": row["actual_outcome"]}, ensure_ascii=False), flush=True)
                if not row["telemetry"]["complete"] or any(not valid_usage(a.get("usage")) for a in row["telemetry"]["llm"]):
                    interrupted = "测量或usage缺失，已停止新增请求"
                    break
    except (KeyboardInterrupt, OSError) as exc:
        interrupted = type(exc).__name__
    finally:
        result = make_report(batch)
        dump(batch / "run_status.json", {"interrupted": interrupted, "planned": len(trials),
             "completed": result["valid_results"], "budget_spent_or_reserved_yuan": ledger.spent()})
    print(json.dumps({"batch": batch.as_posix(), "report": (batch / "report.md").as_posix(),
        **{k: result[k] for k in ("planned", "started", "valid_results", "measurement_complete",
                                 "hard_checks_passed", "quality_targets_passed")}}, ensure_ascii=False, indent=2))
    return exit_code(result)


def compare(args):
    left, right = load_batch(args.baseline), load_batch(args.candidate)
    lm, lc, lr, le, lerrors = left
    rm, rc, rr, re, rerrors = right
    if rm.get("milestone") != "M9":
        raise ValueError("candidate必须是M9批次")
    if lm.get("split") != "dev" or rm.get("split") != "dev":
        raise ValueError("本命令只比较开发集")
    for field in ("suite_sha256", "selected_cases_sha256", "model", "prompt_sha256", "parameters"):
        if lm.get(field) != rm.get(field):
            raise ValueError("对照口径不一致：" + field)
    ls = (summarize if lm.get("milestone") == "M9" else summarize_m8)(lm, lc, lr, le, lerrors)
    rs = summarize(rm, rc, rr, re, rerrors)
    if not all(x["measurement_complete"] and x["hard_checks_passed"] for x in (ls, rs)):
        raise ValueError("先修复缺测/硬违规，不生成效果对照结论")
    lp, rp = args.baseline_policy, args.candidate_policy
    def keyset(m, policy):
        return {(t["case_id"], t["mode"], t["repeat"]) for t in m["trials"] if t["policy"] == policy}
    if not keyset(lm, lp) or keyset(lm, lp) != keyset(rm, rp):
        raise ValueError("两组任务、运行方式或重复次数不一致")
    output = {"record_type": "historical_dev_comparison_not_concurrent_randomized_trial",
        "baseline_batch": lm["batch_id"], "candidate_batch": rm["batch_id"],
        "baseline_source": lm["source_digest"], "candidate_source": rm["source_digest"],
        "scope": "same_frozen_dev_cases_model_prompt_parameters; no holdout claims",
        "groups": {g: {"baseline": ls["by_policy_group"][lp + ":" + g],
                       "candidate": rs["by_policy_group"][rp + ":" + g]}
                   for g in ("main", "planning", "refusal", "abnormal", "memory")}}
    out = Path(args.out or Path(args.candidate) / "comparison")
    out.mkdir(parents=True, exist_ok=False)
    dump(out / "comparison.json", output)
    lines = ["# M8 → M9 开发集历史对照", "", "同一冻结开发集、模型、搜索提示词和参数；分次执行，不代表盲测或同期随机实验。", "",
             "| 组别 | V0通过 | V1通过 | V0均Token | V1均Token |", "| --- | --- | --- | ---: | ---: |"]
    for group, pair in output["groups"].items():
        a, b = pair["baseline"], pair["candidate"]
        def fmt(g):
            q = g["acceptance"]
            return f"{q['numerator']}/{q['denominator']}"
        lines.append(f"| {group} | {fmt(a)} | {fmt(b)} | {a['cost']['token_mean']['total_tokens']} | {b['cost']['token_mean']['total_tokens']} |")
    lines += ["", "拒答规则不产生Token；搜索没有在M9优化，规划成本与通过率差异可能来自模型运行波动。",
              "拒答Precision/Recall、引用准确率/覆盖率和全部费用统计见comparison.json。", ""]
    (out / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"comparison": (out / "comparison.md").as_posix()}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    s = sub.add_parser("run")
    s.add_argument("--data-root", default="data/m8/v1")
    s.add_argument("--root", default="data/m9/v1")
    s.add_argument("--mode", choices=["scripted", "llm"], default="scripted")
    s.add_argument("--policies", default="V1")
    s.add_argument("--repeats", type=int, choices=[1, 2, 3], default=1)
    s.add_argument("--budget-yuan", type=float, default=1)
    s.add_argument("--project-budget-yuan", type=float, default=5)
    s = sub.add_parser("report"); s.add_argument("--batch", required=True)
    s = sub.add_parser("compare")
    s.add_argument("--baseline", required=True); s.add_argument("--candidate", required=True)
    s.add_argument("--baseline-policy", default="V0"); s.add_argument("--candidate-policy", default="V1")
    s.add_argument("--out")
    args = p.parse_args()
    try:
        if args.command == "doctor":
            d = doctor(); print(json.dumps(d, ensure_ascii=False, indent=2)); return 0 if d["integration_ready"] else 1
        if args.command == "report":
            r = make_report(args.batch)
            print(json.dumps({k: r[k] for k in ("measurement_complete", "hard_checks_passed", "quality_targets_passed")}, ensure_ascii=False))
            return exit_code(r)
        if args.command == "compare":
            compare(args); return 0
        if not 0 < args.budget_yuan <= args.project_budget_yuan <= 100:
            raise ValueError("预算需满足0<单批上限<=累计上限<=100")
        return run(args)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
