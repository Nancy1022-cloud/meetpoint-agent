"""独立判分沿用M8/M9；新增短路证据核验、配对对照和逐轮诊断。"""

from collections import Counter
from copy import deepcopy
from statistics import mean

from app.m8_metrics import grade
from app.m8_trace import valid_usage
from app.m9_metrics import summarize as summarize_m9, markdown as markdown_m9


def control_events(row):
    return row.get("controller_events", (row.get("result") or {}).get("trajectory", []))


def short_circuit_errors(row, case):
    result = row.get("result") or {}
    records = result.get("short_circuits", [])
    items = {i["candidate"]["poi_id"]: i for i in result.get("candidate_checks", [])}
    skipped = {cid for cid, i in items.items() if i["b"].get("not_queried")}
    recorded = [r.get("candidate_id") for r in records]
    errors = []
    if len(set(recorded)) != len(recorded) or set(recorded) != skipped:
        errors.append("short_circuit_records_mismatch")
    fixtures = {i["id"]: i for i in case.get("candidates", [])}
    for record in records:
        cid, original = record.get("candidate_id"), fixtures.get(record.get("candidate_id"))
        if original is None:
            errors.append("short_circuit_unknown_candidate")
            continue
        limits = case["requirements"]["person_a"]
        routes = original["a"]
        full = lambda r: all(type(r.get(k)) is int and r[k] >= 0 for k in ("seconds", "walk", "transfers"))
        okay = lambda r: (r["seconds"] <= limits["max_duration_seconds"]
                           and r["walk"] <= limits["max_walk_meters"] and r["transfers"] <= limits["max_transfers"])
        proof = "no_route" if not routes else "constraints_unsatisfied" if (
            all(full(r) for r in routes) and not any(okay(r) for r in routes)) else None
        if (proof is None or record.get("proof_status") != proof or record.get("proof_role") != "a"
                or record.get("skipped_role") != "b" or items[cid]["status"] == "feasible"):
            errors.append("short_circuit_invalid_proof")
        attempts = [t for t in row["telemetry"]["tools"] if t["name"] == "get_transit_routes"
                    and t.get("candidate_id") == cid]
        if (not any(t.get("role") == "a" and t["status"] == "returned" for t in attempts)
                or any(t.get("role") == "b" for t in attempts)):
            errors.append("short_circuit_tool_trace_mismatch")
    return sorted(set(errors))


def summarize(manifest, cases, rows, events, parse_errors=None):
    # 原汇总器可能给行附加判分信息；使用副本保证重跑报告无副作用。
    result = summarize_m9(manifest, cases, deepcopy(rows), deepcopy(events), parse_errors)
    by_id, plans = {c["id"]: c for c in cases}, {t["trial_id"]: t for t in manifest["trials"]}
    errors = []
    for row in rows:
        plan = plans.get(row.get("trial_id"))
        if not plan or any(row.get(k) != plan.get(k) for k in ("case_id", "policy", "mode")):
            continue
        try:
            problems = short_circuit_errors(row, by_id[row["case_id"]])
        except (KeyError, TypeError, ValueError):
            problems = ["malformed_short_circuit_record"]
        if row["policy"] in ("RULE", "V2", "V2_OBS") and by_id[row["case_id"]]["runner"] == "planner":
            for tool in row.get("telemetry", {}).get("tools", []):
                ends = [e for e in events if e.get("trial_id") == row["trial_id"]
                        and e.get("event") == "tool_end" and e.get("attempt_id") == tool.get("attempt_id")]
                if len(ends) != 1 or any(ends[0].get(k) != tool.get(k) for k in ("name", "role", "candidate_id", "status")):
                    problems.append("tool_endpoint_event_mismatch")
        for attempt in row.get("telemetry", {}).get("llm", []):
            if attempt.get("status") == "returned" and attempt.get("returned_model") != manifest["model"]:
                problems.append("returned_model_differs_from_manifest")
        ended = [e for e in events if e.get("trial_id") == row["trial_id"] and e.get("event") == "trial_end"]
        if len(ended) != 1 or ended[0].get("actual_outcome") != row.get("actual_outcome"):
            problems.append("trial_end_mismatch")
        if problems:
            errors.append({"trial_id": row["trial_id"], "errors": sorted(set(problems))})
    result["m10_protocol_errors"] = errors
    primary = manifest["primary_policy"]
    chosen_manifest = {**manifest, "trials": [t for t in manifest["trials"] if t["policy"] == primary]}
    chosen_ids = {t["trial_id"] for t in chosen_manifest["trials"]}
    primary_result = summarize_m9(chosen_manifest, cases,
        deepcopy([r for r in rows if r.get("trial_id") in chosen_ids]),
        deepcopy([e for e in events if e.get("trial_id") in chosen_ids]), parse_errors)
    result["all_arms_quality_targets_passed"] = result["quality_targets_passed"]
    result["quality_target_policy"] = primary
    result["quality_targets_passed"] = primary_result["quality_targets_passed"]
    result["acceptance_gate_passed"] = (result["measurement_complete"] and result["hard_checks_passed"]
                                        and result["quality_targets_passed"])
    if errors:
        result["hard_checks_passed"] = result["acceptance_gate_passed"] = False
    result["optimization_counters"] = {}
    for policy in sorted({t["policy"] for t in manifest["trials"]}):
        selected = [r for r in rows if r.get("policy") == policy]
        result["optimization_counters"][policy] = {
            "route_queries_skipped": sum(len((r.get("result") or {}).get("short_circuits", [])) for r in selected),
            "fast_path_decisions": sum((r.get("result") or {}).get("fast_path_decisions", 0) for r in selected),
            "decision_sources": dict(Counter(e["decision_source"] for r in selected
                for e in control_events(r) if e.get("step") == "decision")),
            "note": "跳过查询数是执行器记录；HTTP/Token净变化需要看完整配对对照，不能直接等同。"}
    mode_note = "真实模型+自建工具环境" if manifest.get("mode") == "llm" else "脚本替身/确定性规则+自建工具环境"
    result["note"] = (f"M10开发实验，{mode_note}，真实高德HTTP为0。main不含Memory。"
        "B0/RULE仅参加规划组；V1为原M9搜索；V2含新提示词、累计反馈、短路与受控快路径。"
        "脚本结果不是模型成绩。预留集不运行；所有失败保留。")
    return result


def comparison(manifest, cases, rows, summary):
    by_id = {c["id"]: c for c in cases}
    policies = sorted({t["policy"] for t in manifest["trials"]})
    pairs = [(a, b) for a, b in (("V1", "V2"), ("B0", "V2"), ("RULE", "V2"), ("V2_OBS", "V2"))
             if a in policies and b in policies]
    result = {"record_type": "same_batch_paired_dev_comparison", "batch_id": manifest["batch_id"],
              "valid_for_effect_claims": summary["measurement_complete"] and summary["hard_checks_passed"],
              "note": "分次串行、固定模型与预算的开发配对；提示词及控制器允许不同，不是盲测或随机实验。",
              "comparisons": {}}
    for left, right in pairs:
        lookup = {(r["case_id"], r["repeat"], r["policy"]): r for r in rows}
        lp = {(t["case_id"], t["repeat"]) for t in manifest["trials"] if t["policy"] == left
              and by_id[t["case_id"]]["group"] == "planning"}
        rp = {(t["case_id"], t["repeat"]) for t in manifest["trials"] if t["policy"] == right
              and by_id[t["case_id"]]["group"] == "planning"}
        if lp != rp:
            raise ValueError("规划配对任务或重复次数不一致")
        details = []
        for cid, repeat in sorted(lp):
            a, b = lookup.get((cid, repeat, left)), lookup.get((cid, repeat, right))
            if not a or not b:
                details.append({"case_id": cid, "repeat": repeat, "missing": True})
                continue
            ag, bg = grade(a, by_id[cid]), grade(b, by_id[cid])
            at, bt = (a.get("result") or {}).get("recommendations", []), (b.get("result") or {}).get("recommendations", [])
            both = bool(at and bt) and not ag["hard_violations"] and not bg["hard_violations"]
            def tokens(r):
                attempts = r["telemetry"]["llm"]
                if not r["telemetry"]["complete"] or any(not valid_usage(u.get("usage")) for u in attempts):
                    return None
                return sum(u["usage"]["total_tokens"] for u in attempts)
            ta, tb = tokens(a), tokens(b)
            delta = {k: bt[0]["metrics"][k] - at[0]["metrics"][k] for k in (
                "max_duration_seconds", "total_duration_seconds", "gap_seconds", "total_walk_meters", "total_transfers")} if both else None
            rank_fields = (("max_duration_seconds", "gap_seconds", "total_walk_meters", "total_transfers")
                           if by_id[cid]["requirements"]["preference"] == "balanced" else
                           ("total_duration_seconds", "max_duration_seconds", "total_walk_meters", "total_transfers"))
            rank_a = tuple(at[0]["metrics"][k] for k in rank_fields) if both else None
            rank_b = tuple(bt[0]["metrics"][k] for k in rank_fields) if both else None
            relation = ("better" if rank_b < rank_a else "worse" if rank_b > rank_a else "equal") if both else None
            details.append({"case_id": cid, "repeat": repeat, "left_pass": ag["passed"], "right_pass": bg["passed"],
                "left_delivered": bool(at), "right_delivered": bool(bt), "both_delivered": both,
                "tool_delta_right_minus_left": len(b["telemetry"]["tools"]) - len(a["telemetry"]["tools"]),
                "http_delta_right_minus_left": len(b["telemetry"]["simulated_http"]) - len(a["telemetry"]["simulated_http"]),
                "token_delta_right_minus_left": tb - ta if ta is not None and tb is not None else None,
                "top_quality_delta": delta, "top_ranking_right_vs_left": relation})
        complete = [p for p in details if not p.get("missing")]
        both = [p for p in complete if p["both_delivered"]]
        result["comparisons"][left + "->" + right] = {
            "planned_pairs": len(lp), "complete_pairs": len(complete),
            "right_only_pass": sum(p["right_pass"] and not p["left_pass"] for p in complete),
            "left_only_pass": sum(p["left_pass"] and not p["right_pass"] for p in complete),
            "both_delivered_pairs": len(both),
            "common_top_ranking": dict(Counter(p["top_ranking_right_vs_left"] for p in both)),
            "all_complete_pairs_mean_delta": {k: mean(p[k] for p in complete) if complete and all(p[k] is not None for p in complete) else None for k in (
                "tool_delta_right_minus_left", "http_delta_right_minus_left", "token_delta_right_minus_left")},
            "both_delivered_mean_delta": {k: mean(p[k] for p in both) if both and all(p[k] is not None for p in both) else None for k in (
                "tool_delta_right_minus_left", "http_delta_right_minus_left", "token_delta_right_minus_left")},
            "pairs": details}
    return result


def comparison_markdown(data):
    lines = ["# M10 规划策略配对", "", data["note"], "",
             f"测量及硬检查支持效果判断：{data['valid_for_effect_claims']}。", "",
             "| 对照 | 完整/计划 | 右独过 | 左独过 | 共同交付 | 平均工具差 | 平均Token差 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, group in data["comparisons"].items():
        d = group["all_complete_pairs_mean_delta"]
        lines.append(f"| {name} | {group['complete_pairs']}/{group['planned_pairs']} | {group['right_only_pass']} | "
            f"{group['left_only_pass']} | {group['both_delivered_pairs']} | {d['tool_delta_right_minus_left']} | "
            f"{d['token_delta_right_minus_left']} |")
    lines += ["", "差值为右减左；负值表示右侧用量更小。质量差仅在共同交付任务上计算，逐项见comparison.json。",
              "失败任务仍计入全任务成本。缺测时本表仅作诊断，不据此发布节省或提升结论。",
              "RULE可能达到或超过V2；报告保留结果，不自动宣称模型有收益。", ""]
    return "\n".join(lines)


def markdown(summary):
    base = markdown_m9(summary).replace("# M9 开发评测报告", "# M10 开发评测报告")
    return base + "\n\n## M10附加核验\n\n" + (
        f"短路/模型/结束事件协议错误：{len(summary['m10_protocol_errors'])}。\n\n"
        "各策略跳过的路线查询、快路径决策次数见summary.json的optimization_counters。\n"
        "策略间配对见comparison.md/json；不保证V2优于规则，也不预写质量和成本成绩。\n")


def diagnostics(rows):
    lines = ["# M10逐轮轨迹", "", "只读取已有结果，不调用模型或地图。", ""]
    for row in rows:
        result = row.get("result")
        if not result:
            continue
        lines += ["## " + row["trial_id"], "", f"结果：{row['actual_outcome']}；停止：`{row['reason']}`。", "",
                  "| 轮 | 动作 | 半径 | 接纳 | 评估 | 可行 | 跳过B查询 | 模拟HTTP |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for r in result["rounds"]:
            lines.append(f"| {r['round']} | {r.get('action', 'fixed_expand')} | {r['radius_meters']} | "
                f"{r['accepted_count']} | {r['evaluated_count']} | {r['feasible_count']} | "
                f"{r.get('short_circuit_count', '未记录')} | {r.get('map_http_delta', '未记录')} |")
        for e in control_events(row):
            if e.get("step") == "decision":
                d = e["decision"]
                lines += ["", f"- {d['action']} / {d['reason_code']}；来源 `{e['decision_source']}`。"]
        lines.append("")
    return "\n".join(lines)
