"""M10：开发集同预算策略对照；不触碰200条预留集，不创建高德客户端。"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.m4_decider import SYSTEM_PROMPT as OLD_PROMPT
from app.m8 import dump, no_network, verify
from app.m8_cases import digest
from app.m8_trace import SpendLedger, append_json, exclusive_run, valid_usage
from app.m9 import fingerprint as fingerprint_m9, load_batch
from app.m9_evidence import PROTOCOL as EVIDENCE_PROTOCOL
from app.m9_scope import PROTOCOL as SCOPE_PROTOCOL
from app.m10_decider import PROTOCOL, SYSTEM_PROMPT
from app.m10_eval import POLICIES, run_case
from app.m10_metrics import comparison, comparison_markdown, diagnostics, markdown, summarize


def fingerprint():
    data = fingerprint_m9()
    root = Path(__file__).resolve().parents[1]
    names = [p.relative_to(root).as_posix() for p in (root / "app").glob("m10*.py")]
    names += ["app/test_m10.py", "apply_m10_changes.py", "M10_patch_manifest.json"]
    data["source_files_sha256"].update({p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                                     for p in names if (root / p).is_file()})
    data["source_digest"] = digest(data["source_files_sha256"])
    return data


def doctor():
    root = Path(__file__).resolve().parents[1]
    runtime = (root / "app/m6_runtime.py").read_text(encoding="utf-8-sig")
    settings = get_settings()
    return {"milestone": "M10", "integration_ready": "M10_INTEGRATION_V1" in runtime,
        "M9_evidence_integration_present": "M9_INTEGRATION_V1" in runtime,
        "configured_model": settings.llm_model,
        "model_matches_M8_M9": settings.llm_model == "qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-"),
        "llm_key_present": bool(settings.llm_api_key.get_secret_value().strip()),
        "beijing_endpoint_for_retained_estimate": settings.llm_base_url.rstrip("/") == "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "map_network_used_by_M10_evaluation": False, "holdout_executed_by_M10": False,
        "search_protocol": PROTOCOL, "fingerprint": fingerprint()}


def select_cases(data, stage, groups):
    cases = [c for c in data["dev"] if c["group"] in groups]
    if stage == "pilot":
        seen, selected = set(), []
        for c in cases:
            if c["group"] == "planning" and c["family"] not in seen:
                selected.append(c)
                seen.add(c["family"])
        cases = selected
    if not cases:
        raise ValueError("没有选中开发用例")
    return cases


def make_report(batch):
    batch = Path(batch)
    manifest, cases, rows, events, errors = load_batch(batch)
    if manifest.get("milestone") != "M10":
        raise ValueError("M10 report只写M10批次，不修改M8/M9历史")
    result = summarize(manifest, cases, rows, events, errors)
    pair = comparison(manifest, cases, rows, result)
    pairs = pair["comparisons"].get("V1->V2")
    no_regression = None if pairs is None else (
        pair["valid_for_effect_claims"] and pairs["right_only_pass"] >= pairs["left_only_pass"])
    result.update(batch_id=manifest["batch_id"], model=manifest["model"], source_digest=manifest["source_digest"],
        stage=manifest["stage"], nonregression_vs_V1_planning=no_regression,
        promotion_gate_passed=(manifest["stage"] == "dev" and result["acceptance_gate_passed"]
                               and no_regression is True and manifest["mode"] == "llm"
                               and manifest["selected_all_dev"]
                               and pairs["common_top_ranking"].get("worse", 0) == 0))
    dump(batch / "summary.json", {k: v for k, v in result.items() if k != "failures"})
    (batch / "failures.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in result["failures"]), encoding="utf-8")
    (batch / "report.md").write_text(markdown(result), encoding="utf-8")
    dump(batch / "comparison.json", pair)
    (batch / "comparison.md").write_text(comparison_markdown(pair), encoding="utf-8")
    (batch / "search_diagnostics.md").write_text(diagnostics(rows), encoding="utf-8")
    return result


def exit_code(result):
    return 3 if not result["measurement_complete"] or not result["hard_checks_passed"] else 0 if result["quality_targets_passed"] else 2


def acceptance(args):
    """只重算已有记录并生成草稿；不会填写人工现场检查为已通过。"""
    batch = Path(args.batch)
    result = make_report(batch)
    manifest, _, _, _, _ = load_batch(batch)
    lines = ["# MeetPoint M10 验收自动草稿", "",
        "本文件由已有批次自动生成，不调用模型或地图。它不替代归档核验及人工现场检查。", "",
        f"批次：`{manifest['batch_id']}`；模式：`{manifest['mode']}`；阶段：`{manifest['stage']}`。",
        f"模型：`{manifest['model']}`；源码指纹：`{manifest['source_digest']}`。",
        f"冻结开发集全部选中：{manifest['selected_all_dev']}；200条预留集：本入口未运行。", "",
        f"测量完整：{result['measurement_complete']}；硬检查：{result['hard_checks_passed']}；"
        f"主策略{result['quality_target_policy']}效果门槛：{result['quality_targets_passed']}。",
        f"V1/V2规划验收不退化：{result['nonregression_vs_V1_planning']}；"
        f"完整实模开发复测门禁：{result['promotion_gate_passed']}。", "",
        "## 测试日志", ""]
    checks = Path(args.checks_dir)
    for name in ("m10_tests.txt", "regression.txt"):
        path = checks / name
        if not path.is_file():
            lines.append(f"- {name}：未提供，未核验。")
            continue
        raw = path.read_bytes()
        # Windows PowerShell 5 的Tee-Object常写UTF-16；PowerShell 7通常写UTF-8。
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
        counts = re.findall(r"Ran\s+(\d+)\s+tests?\b", text)
        ok = bool(re.search(r"(?m)^OK\s*$", text)) and not re.search(r"(?m)^FAILED\b", text)
        lines.append(f"- {name}：日志测试数{counts[-1] if counts else '未知'}，含最终OK且无FAILED：{ok}。")
    lines += ["", "## 自动评测与对照", "", (batch / "report.md").read_text(encoding="utf-8"),
        (batch / "comparison.md").read_text(encoding="utf-8"),
        "## 人工与范围边界", "",
        "- 网页人工现场检查：未由本程序核验；自动化网页检查与手工检查分开。",
        "- 真实高德现场验证：未由本程序核验；本批主评测真实高德HTTP为0。",
        "- Memory仍为受控解析器回归；规则拒答仍为系统规则效果。",
        "- 费用沿用M8/M9估值口径，不是当前报价或实际扣费；M8共用账本里的未知预留继续保留。",
        "- 失败以本批failures.jsonl为准，不删失败、不修改冻结预期，不把脚本usage算作实模成绩。", ""]
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"acceptance_draft": output.as_posix(), "promotion_gate_passed": result["promotion_gate_passed"]}, ensure_ascii=False))
    return exit_code(result)


def run(args):
    data, meta = verify(args.data_root)
    if Path(args.root).resolve() == Path(args.data_root).resolve():
        raise ValueError("M10输出目录须与原M8数据分开")
    policies = args.policies.split(",")
    if not policies or len(policies) != len(set(policies)) or not set(policies) <= set(POLICIES):
        raise ValueError("policies只支持B0,RULE,V1,V2,V2_OBS且不能重复")
    groups = args.groups.split(",")
    if not set(groups) <= {"planning", "refusal", "abnormal", "memory"}:
        raise ValueError("未知用例组")
    cases = select_cases(data, args.stage, groups)
    settings = get_settings()
    uses_llm = args.mode == "llm" and any(p in ("V1", "V2", "V2_OBS") for p in policies)
    if uses_llm:
        if not (settings.llm_model == "qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-")):
            raise ValueError("请沿用M8/M9 qwen3.7-flash，换模型须单独记录")
        if settings.llm_base_url.rstrip("/") != "https://dashscope.aliyuncs.com/compatible-mode/v1":
            raise ValueError("沿用的预算估值仅对应M8北京端点")
        if not settings.llm_api_key.get_secret_value().strip():
            raise ValueError("缺少模型Key，请只在你自己的.env填写")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    batch_id = f"{args.mode}_{args.stage}_{now}_{uuid4().hex[:6]}"
    batch = Path(args.root) / "runs" / batch_id
    trials = []
    for c in cases:
        for repeat in range(1, args.repeats + 1):
            for p in policies:
                if p in ("B0", "RULE") and c["group"] != "planning":
                    continue
                trials.append({"trial_id": f"{c['id']}:{p}:{args.mode}:{repeat}", "case_id": c["id"],
                               "policy": p, "mode": args.mode, "repeat": repeat})
    if not trials:
        raise ValueError("选中的策略没有适用任务")
    batch.mkdir(parents=True, exist_ok=False)
    primary = next(p for p in ("V2", "V2_OBS", "V1", "RULE", "B0") if p in policies)
    manifest = {"schema_version": 1, "milestone": "M10", "batch_id": batch_id, "split": "dev",
        "stage": args.stage, "mode": args.mode, "primary_policy": primary,
        "selected_all_dev": digest(cases) == digest(data["dev"]),
        "suite_sha256": meta["suite_sha256"], "selected_cases_sha256": digest(cases),
        "model": settings.llm_model if uses_llm else "not_called", "configured_model": settings.llm_model,
        "prompt_sha256_by_policy": {p: hashlib.sha256((OLD_PROMPT if p == "V1" else SYSTEM_PROMPT).encode()).hexdigest()
                                   for p in policies if p in ("V1", "V2", "V2_OBS")},
        "parameters": {"temperature": 0, "max_tokens": 512, "enable_thinking": False, "sdk_max_retries": 0},
        "search_protocol": PROTOCOL, "scope_protocol": SCOPE_PROTOCOL, "evidence_protocol": EVIDENCE_PROTOCOL,
        "policy_definitions": {"B0": "original_fixed_1500_3000_5000",
            "RULE": "cumulative_feedback_rule_with_short_circuit_and_fast_paths",
            "V1": "M9_original_MemoryPlanner_and_prompt",
            "V2": "new_prompt_history_short_circuit_fast_paths",
            "V2_OBS": "new_prompt_history_no_short_circuit_no_fast_paths"},
        "money_limits": {"batch_yuan": args.budget_yuan, "project_yuan": args.project_budget_yuan},
        "money_ledger_root": Path(args.data_root).as_posix(),
        "pricing": {"basis": "M8/M9 retained estimate, NOT current quotation or invoice",
                    "region": "Beijing", "input": .2, "output": .8, "unit": "CNY/million_tokens"},
        "thresholds": {"acceptance": .9, "citation_precision": 1, "citation_coverage": 1,
                       "refusal_precision": .9, "refusal_recall": .9, "false_refusal_rate_max": .05},
        "trials": trials, **fingerprint()}
    dump(batch / "manifest.json", manifest)
    (batch / "manifest.sha256").write_text(digest(manifest) + "\n", encoding="utf-8")
    dump(batch / "cases.json", cases)
    source = Path(__file__).resolve().parents[1]
    for name in manifest["source_files_sha256"]:
        dest = batch / "source_snapshot" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((source / name).read_bytes())
    ledger = SpendLedger(args.data_root, batch_id, args.budget_yuan, args.project_budget_yuan)
    by_id, interrupted = {c["id"]: c for c in cases}, None
    try:
        with exclusive_run(args.data_root):
            for index, trial in enumerate(trials, 1):
                kwargs = {"settings": settings, "ledger": ledger,
                          "sink": lambda e: append_json(batch / "events.jsonl", e)}
                if args.mode == "scripted" or trial["policy"] in ("B0", "RULE"):
                    with no_network():
                        row = run_case(by_id[trial["case_id"]], trial["policy"], args.mode, trial["trial_id"], **kwargs)
                else:
                    row = run_case(by_id[trial["case_id"]], trial["policy"], args.mode, trial["trial_id"], **kwargs)
                row["repeat"] = trial["repeat"]
                append_json(batch / "results.jsonl", row)
                print(json.dumps({"done": index, "planned": len(trials), "case_id": row["case_id"],
                    "policy": row["policy"], "outcome": row["actual_outcome"]}, ensure_ascii=False), flush=True)
                if not row["telemetry"]["complete"] or any(not valid_usage(a.get("usage")) for a in row["telemetry"]["llm"]):
                    interrupted = "测量或usage缺失，停止新增请求"
                    break
    except (KeyboardInterrupt, OSError) as exc:
        interrupted = type(exc).__name__
    finally:
        result = make_report(batch)
        dump(batch / "run_status.json", {"interrupted": interrupted, "planned": len(trials),
             "completed": result["valid_results"], "budget_spent_or_reserved_yuan": ledger.spent()})
    print(json.dumps({"batch": batch.as_posix(), "primary_policy": primary,
        **{k: result[k] for k in ("planned", "started", "valid_results", "measurement_complete",
            "hard_checks_passed", "quality_targets_passed", "nonregression_vs_V1_planning", "promotion_gate_passed")}},
        ensure_ascii=False, indent=2))
    return exit_code(result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    s = sub.add_parser("run")
    s.add_argument("--data-root", default="data/m8/v1")
    s.add_argument("--root", default="data/m10/v1")
    s.add_argument("--stage", choices=["pilot", "dev"], default="dev")
    s.add_argument("--mode", choices=["scripted", "llm"], default="scripted")
    s.add_argument("--policies", default="B0,RULE,V1,V2")
    s.add_argument("--groups", default="planning,refusal,abnormal,memory")
    s.add_argument("--repeats", type=int, choices=[1, 2, 3], default=1)
    s.add_argument("--budget-yuan", type=float, default=1)
    s.add_argument("--project-budget-yuan", type=float, default=5)
    s = sub.add_parser("report")
    s.add_argument("--batch", required=True)
    s = sub.add_parser("acceptance")
    s.add_argument("--batch", required=True)
    s.add_argument("--checks-dir", default="data/m10/v1/checks")
    s.add_argument("--out", default="data/m10/v1/M10_ACCEPTANCE_AUTO.md")
    args = p.parse_args()
    try:
        if args.command == "doctor":
            result = doctor()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["integration_ready"] and result["M9_evidence_integration_present"] else 1
        if args.command == "report":
            result = make_report(args.batch)
            print(json.dumps({k: result[k] for k in ("measurement_complete", "hard_checks_passed", "quality_targets_passed")}, ensure_ascii=False))
            return exit_code(result)
        if args.command == "acceptance":
            return acceptance(args)
        if not 0 < args.budget_yuan <= args.project_budget_yuan <= 100:
            raise ValueError("预算应满足0<单批上限<=累计上限<=100")
        return run(args)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
