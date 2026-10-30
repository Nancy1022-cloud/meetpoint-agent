"""Pure local M12 scoring, paired comparisons, budget forecast and export."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
from statistics import median
from uuid import uuid4
import zipfile

from app.m8_cases import digest
from app.m8_metrics import grade
from app.m8_trace import INPUT_PRICE, OUTPUT_PRICE, SpendLedger
from app.m10_metrics import summarize, comparison, comparison_markdown, diagnostics
from app.m9_metrics import markdown as core_markdown
from app.m12 import ROOT, GROUPS, NOTE, dump, load_batch, plan_trials, select_cases, sha, stamp, source_files


def pct(metric):
    return "N/A" if metric["value"] is None else f"{metric['value']:.1%} ({metric['numerator']}/{metric['denominator']})"


def result_exit(result):
    if not result["measurement_complete"] or not result["hard_checks_passed"]: return 3
    return 0 if result["quality_targets_passed"] else 2


def stability(manifest, cases, rows):
    by_id = {c["id"]: c for c in cases}
    planned = {(t["case_id"], t["policy"]): [] for t in manifest["trials"]}
    results = {r["trial_id"]: r for r in rows}
    for t in manifest["trials"]:
        r = results.get(t["trial_id"])
        passed = bool(r and grade(r, by_id[t["case_id"]])["passed"])
        planned[(t["case_id"], t["policy"])].append(passed)
    output = {}
    for p in manifest["policies"]:
        for group in GROUPS:
            selected = [v for (cid, policy), v in planned.items() if policy == p and by_id[cid]["group"] == group]
            if selected:
                output[p+":"+group] = {"unique_tasks": len(selected), "all_repeats_pass": sum(all(v) for v in selected),
                    "at_least_one_pass": sum(any(v) for v in selected),
                    "pass_count_distribution": dict(Counter(str(sum(v)) for v in selected)),
                    "note": "按用例汇总；不是更多独立题目，也不是独立同分布置信区间。"}
    return output


def test_logs(checks):
    lines = []
    for name in ("m12_tests.txt", "regression.txt"):
        p = Path(checks)/name
        if not p.is_file():
            lines.append(f"- {name}：未提供。")
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
        counts = re.findall(r"Ran\s+(\d+)\s+tests?\b", text)
        okay = bool(re.search(r"(?m)^OK\s*$", text)) and not re.search(r"(?m)^(?:FAILED|ERROR:)\b", text)
        lines.append(f"- {name}：测试数{counts[-1] if counts else '未知'}；日志含OK且无FAILED/ERROR：{okay}。")
    return "\n".join(lines)


def candidate_targets(result, manifest):
    missing = []
    for group in ("main",)+GROUPS:
        g = result["by_policy_group"].get("V2:"+group)
        if g and g["planned_trials"] and (g["acceptance"]["value"] is None or g["acceptance"]["value"] < manifest["thresholds"]["acceptance_each_group"]):
            missing.append(group+":acceptance_below_90_percent")
    main = result["by_policy_group"]["V2:main"]
    for key, target in (("citation_precision", 1), ("citation_coverage", 1), ("refusal_precision", .9), ("refusal_recall", .9)):
        metric = main[key]
        applicable = metric["denominator"] > 0 or key.startswith("refusal")
        if applicable and (metric["value"] is None or metric["value"] < target): missing.append(key)
    if main["false_refusal_rate"]["value"] is not None and main["false_refusal_rate"]["value"] > .05:
        missing.append("false_refusal_rate_above_5_percent")
    return missing


def report(batch):
    batch = Path(batch)
    m, cases, rows, events, errors = load_batch(batch, current=True)
    result = summarize(m, cases, deepcopy(rows), deepcopy(events), errors)
    result["note"] = NOTE
    result.update(batch_id=m["batch_id"], stage=m["stage"], model=m["model"], source_digest=m["source_digest"],
        unique_task_count=len(cases), scenario_family_count=len({c["family"] for c in cases}), repeat_count=m["repeats"],
        primary_two_arm_trials=sum(t["policy"] in ("V1", "V2") for t in m["trials"]),
        extra_baseline_trials=sum(t["policy"] in ("B0", "RULE") for t in m["trials"]))
    # M10 checks first, then additional M12 identity and measurement checks.
    identity = {t["trial_id"]: t for t in m["trials"]}
    extra = []
    if len(identity) != len(m["trials"]): extra.append("duplicate_plan_trial")
    for r in rows:
        if r.get("trial_id") in identity and r.get("repeat") != identity[r["trial_id"]]["repeat"]:
            extra.append("repeat_identity:"+r["trial_id"])
        if r.get("telemetry", {}).get("real_map_http", 0) != 0:
            extra.append("unexpected_real_map_http:"+str(r.get("trial_id")))
    if extra:
        result["measurement_complete"] = result["hard_checks_passed"] = False
    result["m12_protocol_errors"] = extra
    misses = candidate_targets(result, m)
    result["quality_targets_passed"] = not misses
    result["unmet_candidate_targets"] = misses
    result["acceptance_gate_passed"] = result["measurement_complete"] and result["hard_checks_passed"] and not misses
    observed_model_attempts = sum(len(r.get("telemetry", {}).get("llm", [])) for r in rows)
    result["M12_delivery_complete"] = bool(m["stage"] == "final" and m["mode"] == "llm" and result["measurement_complete"] and observed_model_attempts > 0)
    result["known_M11_NL"] = m["known_M11_NL"]
    result["M11_NL_quality_passed"] = m["known_M11_NL"]["quality_gate_passed"]
    result["whole_product_all_quality_goals_passed"] = result["acceptance_gate_passed"] and result["M11_NL_quality_passed"]
    result["online_validation"] = m["online_validation"]
    result["repeat_stability"] = stability(m, cases, rows)
    result["latency"] = {}
    for p in m["policies"]:
        for group in GROUPS:
            observed = sorted(r["elapsed_seconds"] for r in rows if r["policy"] == p and r["group"] == group)
            if observed:
                import math
                result["latency"][p+":"+group] = {"observed_tasks": len(observed), "p50_seconds": median(observed),
                    "p95_seconds_nearest_rank": observed[math.ceil(.95*len(observed))-1]}
    pair = comparison(m, cases, rows, result)
    pair["record_type"] = "M12_same_batch_paired_"+m["split"]
    pair["note"] = "同一冻结用例、预算和模型；按预登记顺序执行，重复不是新增独立样本。质量比较仅在共同交付子集计算。"
    pair["valid_for_effect_claims"] = bool(m["mode"] == "llm" and result["measurement_complete"] and result["hard_checks_passed"])
    # Never market scripted trajectories as model performance.
    if m["mode"] == "scripted":
        result["real_model_performance_evidence"] = False
    else:
        result["real_model_performance_evidence"] = result["measurement_complete"] and observed_model_attempts > 0
    failures = result.pop("failures")
    dump(batch/"summary.json", result)
    dump(batch/"comparison.json", pair)
    (batch/"failures.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in failures), encoding="utf-8")
    table = core_markdown(result).replace("# M9 开发评测报告", "# M12 评测报告").replace("# M8 实测报告", "# M12 评测报告")
    table += f"\nM12交付测量完整：{result['M12_delivery_complete']}；候选质量门禁：{result['acceptance_gate_passed']}。\n"
    table += "\n本表的五项指标按main、planning、refusal、abnormal、memory分别报告。主验收不合并Memory。\n"
    table += "\nM11自然语言开发集31/36（86.1%）尚未达90%；原40条受控Memory的成绩不覆盖此结论。\n"
    table += "\n来源、失败、分母、稳定性分布和延迟见JSON。费用沿用M8历史估值，不是当前报价或账单。\n"
    (batch/"report.md").write_text(table, encoding="utf-8")
    (batch/"comparison.md").write_text(comparison_markdown(pair).replace("# M10 规划策略配对", "# M12 规划策略配对"), encoding="utf-8")
    (batch/"search_diagnostics.md").write_text(diagnostics(rows).replace("M10", "M12"), encoding="utf-8")
    acceptance = ("# MeetPoint M12 验收自动记录\n\n"
        f"批次：`{m['batch_id']}`；阶段：{m['stage']}；模型：`{m['model']}`。\n\n"
        f"作者自建用例{len(cases)}条，每条重复{m['repeats']}次；总计划{result['planned']}试次。\n\n"
        f"M12测量交付完成：{result['M12_delivery_complete']}；测量完整：{result['measurement_complete']}；"
        f"已定义硬检查：{result['hard_checks_passed']}；V2效果目标：{result['quality_targets_passed']}。\n\n"
        f"V2未达目标：{', '.join(misses) if misses else '无'}。\n\n"
        "## 测试日志\n\n"+test_logs(batch.parent/"checks")+"\n\n"
        "测试日志是执行者提供的记录；新增测试已包含在完整回归中，不能相加。\n\n"
        "## 范围与待办\n\n"
        "- M11自然语言开发验收31/36，未达90%；保留5条误澄清。此处没有修复或重跑它们。\n"
        "- M12的40条Memory继续验证原受控解析与结构化状态，不作为自然语言泛化结论。\n"
        "- 高德现场验证：本入口未执行；手工执行记录另见ONLINE_VALIDATION.md。\n"
        "- M11第9项卡片现场检查：此前未执行，不能自动补成通过。\n"
        "- 作者可见、相关场景族、非独立盲测；真实城市可用性不由本批证明。\n"
        "- 未执行、缺usage、失败与未知预留保留；指标不达标仍交付真实报告。\n\n"+table)
    (batch/"M12_ACCEPTANCE_AUTO.md").write_text(acceptance, encoding="utf-8")
    g = result["by_policy_group"]["V2:main"]
    plan = result["by_policy_group"]["V2:planning"]
    readme = ("# MeetPoint Agent\n\n"
        "两人从不同地点出发，用公共交通选择满足双方时间、换乘与步行限制的见面商场。\n\n"
        "FastAPI + LangGraph + SQLite + OpenAI兼容接口 + Docker Compose。代码负责约束、预算与排序，"
        "模型根据逐轮反馈选择搜索动作，结构化状态支持会话恢复与明确的长期偏好。\n\n"
        "## 本次评测\n\n"
        f"模型：`{m['model']}`；源码指纹：`{m['source_digest']}`。\n\n"
        f"阶段：{m['stage']}；用例{len(cases)}条，重复{m['repeats']}次；计划{result['planned']}试次。"
        f"测量完整：{result['measurement_complete']}；硬检查：{result['hard_checks_passed']}。\n\n"
        "| V2指标 | 实测 |\n| --- | --- |\n"
        f"| main自动验收 | {pct(g['acceptance'])} |\n"
        f"| 规划自动验收 | {pct(plan['acceptance'])} |\n"
        f"| 结构化引用准确率 / 覆盖率 | {pct(g['citation_precision'])} / {pct(g['citation_coverage'])} |\n"
        f"| 范围拒答Precision / Recall | {pct(g['refusal_precision'])} / {pct(g['refusal_recall'])} |\n"
        f"| main平均业务工具调用 | {g['cost']['tool_calls_mean']} |\n"
        f"| main平均总Token | {g['cost']['token_mean']['total_tokens']} |\n\n"
        "main含规划、范围消息及异常，单列规划避免零模型路径降低均值造成误读。"
        "Memory不并入main。全部失败、分子分母、输入/输出Token、usage覆盖率和基线结果见report.md/json与comparison.md/json。\n\n"
        "## 边界\n\n"+NOTE+"\n\n"
        "M11自然语言开发集31/36（86.1%）未达90%。本批未证明模型优于规则；只按comparison.json的实际胜负与成本描述收益。"
        "引用仅核验结构化展示字段，不代表任意自由文本。未执行的在线检查不计为通过。\n\n"
        "## 重算已有记录\n\n```powershell\n"
        f"python -m app.m12 report --batch {batch.as_posix()}\n```\n\n"
        "在已安装原项目依赖的Python或指南中的Docker环境执行；本命令不调用模型或高德。"
        "源码变化后需使用该批source_snapshot及相同依赖重算。\n")
    (batch/"README_M12.md").write_text(readme, encoding="utf-8")
    online = batch/"ONLINE_VALIDATION.md"
    if not online.exists():
        online.write_text("# 可选高德现场验证\n\n状态：未执行。\n\n"
            "如实际执行，请另记时间、源码版本、用例数、权限/配额、服务组请求计数与停止行为。"
            "不要把历史联调写成当前版本验证；仅保留控制元数据和核验摘要。\n\n"
            "M11第9项营业信息未知与通勤余量卡片检查：未执行。\n", encoding="utf-8")
    return result


def forecast(batch):
    batch = Path(batch)
    r = report(batch)
    m, cases, rows, events, errors = load_batch(batch, current=True)
    if m["stage"] != "pilot" or m["mode"] != "llm" or not r["measurement_complete"] or not r["hard_checks_passed"]:
        raise ValueError("先完成开发集真实模型pilot及硬检查，再预测。效果未达标会保留，不阻断测量。")
    totals = {"prompt_tokens": 0., "completion_tokens": 0., "total_tokens": 0.}
    by_group = {}
    for p in m["policies"]:
        for group in GROUPS:
            if p in ("B0", "RULE") and group != "planning": continue
            n = (80 if group == "planning" else 40)*3
            g = r["by_policy_group"][p+":"+group]
            means = g["cost"]["token_mean"]
            if not g["planned_trials"] or any(v is None for v in means.values()):
                raise ValueError("pilot缺少适用分组完整usage，不能外推")
            estimate = {k: means[k]*n for k in totals}
            for k in totals: totals[k] += estimate[k]
            by_group[p+":"+group] = {"pilot_tasks": g["unique_case_ids"], "formal_trials": n, "estimated_tokens": estimate}
    estimate = (totals["prompt_tokens"]*INPUT_PRICE + totals["completion_tokens"]*OUTPUT_PRICE)/1_000_000
    result = {"basis": "按开发集分组均值外推；不保证预留集具有相同难度。费用为M8历史估值，非当前报价/实际扣费。",
        "formal_repeats_assumed": 3, "policies": m["policies"], "estimated_tokens": totals,
        "linear_historical_cost_yuan": estimate, "three_times_buffer_yuan": estimate*3,
        "project_estimated_spend_or_reserved": SpendLedger(m["data_root"], m["batch_id"]).spent(), "by_group": by_group}
    dump(batch/"forecast.json", result)
    return result


def package(batch):
    batch = Path(batch).resolve()
    result = report(batch)
    m, _, _, _, _ = load_batch(batch, current=True)
    if m["stage"] != "final": raise ValueError("交付打包仅接受final批次；开发检查不能冒充最终评测。")
    output = ROOT/("M12_acceptance_materials_"+uuid4().hex[:8]+".zip")
    selected = [(p, "m12_final/"+p.relative_to(batch).as_posix()) for p in sorted(batch.rglob("*")) if p.is_file()
                and p.suffix in (".json", ".jsonl", ".md", ".sha256", ".py", ".js", ".html", ".txt", ".toml", ".lock")
                and "__pycache__" not in p.parts]
    checks = batch.parent/"checks"
    if checks.exists(): selected += [(p, "checks/"+p.name) for p in sorted(checks.iterdir()) if p.is_file() and p.suffix in (".txt", ".json")]
    for name in ("suite.json", "dataset_manifest.json", "spend.jsonl", "m12_holdout_access.jsonl"):
        p = Path(m["data_root"])/name
        if p.is_file(): selected.append((p, "m8_provenance/"+name))
    for name in ("M10_ACCEPTANCE.md", "M11_ACCEPTANCE.md"):
        p = ROOT/name
        if p.is_file(): selected.append((p, "prior_acceptance/"+name))
    files = {name: sha(path) for path, name in selected}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in selected: z.write(path, name)
        z.writestr("archive_sha256.json", json.dumps(files, ensure_ascii=False, indent=2))
    return {"archive": str(output), "sha256": sha(output), "files": len(files),
        "M12_delivery_complete": result["M12_delivery_complete"], "quality_gate_passed": result["acceptance_gate_passed"],
        "note": "打包不会把缺测或未达标改成通过。"}
