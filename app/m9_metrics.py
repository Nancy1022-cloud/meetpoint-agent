"""保留M8独立判分口径，额外验证页面确实呈现同一批引用事实。"""

from collections import Counter

from app.m8_metrics import grade, ratio, report_markdown, summarize as summarize_m8
from app.m9_evidence import build_view


def summarize(manifest, cases, rows, events, parse_errors=None):
    result = summarize_m8(manifest, cases, rows, events, parse_errors)
    cases_by_id = {c["id"]: c for c in cases}
    plans = {t["trial_id"]: t for t in manifest["trials"]}
    counts = Counter(r.get("trial_id") for r in rows)
    additional = []
    for row in rows:
        if row.get("trial_id") not in plans or counts[row["trial_id"]] != 1:
            continue
        plan = plans[row["trial_id"]]
        if any(row.get(k) != plan.get(k) for k in ("case_id", "policy", "mode")):
            continue  # base已登记身份错误，不以不可信字段访问其他分组。
        case = cases_by_id[plan["case_id"]]
        errors = []
        try:
            expected = build_view(row, case.get("requirements"))
            if not row.get("result"):
                expected.update(outcome=row["actual_outcome"], stop_kind=row["reason"])
            if row.get("presentation") != expected:
                errors.append("presentation_does_not_match_evidence")
            if not expected["evidence_valid"]:
                errors.extend(expected["validation_errors"])
            if row.get("checks", {}).get("refusal_keeps_state") is False:
                errors.append("refusal_changed_state")
            original = grade(row, case)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            errors.append("malformed_m9_presentation")
            original = {"passed": False, "hard_violations": ["invalid_output_schema"]}
        if not errors:
            continue
        record = {**plans[row["trial_id"]], "failed_checks": sorted(set(errors)),
                  "hard_violations": ["m9_presentation_integrity"]}
        additional.append(record)
        # 同一失败不重复扣任务通过数，也不把base已违规任务再累计一次。
        for group in ("main", "planning", "refusal", "abnormal", "memory"):
            if not (case["group"] != "memory" if group == "main" else case["group"] == group):
                continue
            g = result["by_policy_group"][row["policy"] + ":" + group]
            g["acceptance"] = ratio(g["acceptance"]["numerator"] - int(original["passed"]),
                                    g["acceptance"]["denominator"])
            if not original["hard_violations"]:
                g["hard_violations"] += 1
    result["failures"].extend(additional)
    result["m9_protocol_errors"] = additional
    for name, group in result["by_policy_group"].items():
        policy, category = name.split(":")
        selected = [r for r in rows if counts[r.get("trial_id")] == 1
            and r.get("trial_id") in plans and r.get("policy") == policy
            and r.get("case_id") == plans[r["trial_id"]]["case_id"]
            and (cases_by_id[r["case_id"]]["group"] != "memory" if category == "main" else
                 cases_by_id[r["case_id"]]["group"] == category)]
        def presented(r, key):
            p = r.get("presentation")
            return p.get(key, "unknown") if isinstance(p, dict) else "invalid"
        group["presentation_outcomes"] = dict(Counter(presented(r, "outcome") for r in selected))
        group["search_stop_kinds"] = dict(Counter(presented(r, "stop_kind") for r in selected))
    if additional:
        result["hard_checks_passed"] = False
        result["quality_targets_passed"] = False
    result["acceptance_gate_passed"] = (result["measurement_complete"] and result["hard_checks_passed"]
                                         and result["quality_targets_passed"])
    result["note"] = ("沿用M8冻结开发数据与原判分口径；新增共享规则范围门禁、八字段证据页面协议。"
                      "V1搜索策略未优化；Memory仍为受控解析器；本入口真实高德HTTP为0。")
    return result


def markdown(summary):
    return report_markdown(summary).replace("# M8 实测报告", "# M9 开发评测报告").split("旧产品没有拒答门禁时")[0] + (
        "\n范围拒答使用确定性规则，不产生模型Token。页面协议另做反例校验，不能扩展为任意自由文本引用准确率。\n"
        "actual_outcome沿用M8判分契约以便对照；更细的页面结果和停止类型见summary.json各组presentation_outcomes、search_stop_kinds。\n")


def diagnostics(rows, cases):
    expected = {c["id"]: c for c in cases}
    lines = ["# 搜索失败复盘", "", "只读取已有轨迹；本命令不调用模型或地图。", ""]
    for row in rows:
        case = expected.get(row.get("case_id"), {})
        if case.get("group") != "planning":
            continue
        try:
            if grade(row, case)["passed"]:
                continue
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            lines += ["无法复盘格式不完整的结果：" + str(row.get("trial_id")), ""]
            continue
        result = row.get("result") or {}
        lines += ["## " + row["trial_id"], "", "预登记自建全集有解：" + str(case.get("known_has_solution")),
                  "停止原因：`" + str(row.get("reason")) + "`。", "",
                  "| 轮次 | 动作 | 半径 | 接纳 | 评估 | 可行 |", "| --- | --- | ---: | ---: | ---: | ---: |"]
        for r in result.get("rounds", []):
            lines.append(f"| {r['round']} | {r.get('action')} | {r['radius_meters']} | {r['accepted_count']} | {r['evaluated_count']} | {r['feasible_count']} |")
        lines += ["", "模型选择（动作、理由码、合法动作与剩余搜索轮数）：", ""]
        for event in result.get("trajectory", []):
            if event.get("step") == "decision":
                obs, dec = event["observation"], event["decision"]
                lines.append(f"- `{dec['action']}` / `{dec['reason_code']}`；合法动作 `{obs['allowed_actions']}`；余轮 `{obs['remaining']['search_rounds']}`。")
        lines += ["", "此记录可以定位搜索选择，尚不能仅凭结果认定某个提示词修改必然有效。", ""]
    return "\n".join(lines)
