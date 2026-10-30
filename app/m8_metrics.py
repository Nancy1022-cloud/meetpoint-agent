"""独立业务判分、缺失/重复核验、五项指标。未知值永远不用0冒充。"""

from collections import Counter
from statistics import mean

from app.m8_evidence import FIELDS, score_citations
from app.m8_trace import cost_yuan, valid_usage


def ratio(numerator, denominator):
    return {"numerator":numerator,"denominator":denominator,
            "value":numerator/denominator if denominator else None}


def grade(row, case):
    failed, hard = [], []
    if row.get("actual_outcome") not in case["expected_outcomes"]:
        failed.append("wrong_outcome")
    if case.get("expected_reason") and row.get("reason") != case["expected_reason"]:
        failed.append("wrong_reason")
    if row.get("error") and row.get("actual_outcome") in ("execution_error","measurement_stopped"):
        failed.append("execution_or_measurement_error")
    result = row.get("result") or {}
    fixtures = {c["id"]:c for c in case.get("candidates",[])}
    for rec in result.get("recommendations",[]):
        cid = rec.get("candidate",{}).get("poi_id")
        original = fixtures.get(cid)
        if original is None:
            hard.append("candidate_not_from_fixture")
            continue
        for role in ("a","b"):
            route = rec.get(role,{}).get("selected_route")
            if not route:
                hard.append("missing_route_"+role)
                continue
            limit = case["requirements"]["person_"+role]
            route_value = {k:route.get(f) for f,_,k in FIELDS}
            if route_value not in original[role]:
                hard.append("route_not_from_fixture_"+role)
            for field, cap in (("duration_seconds","max_duration_seconds"),
                               ("walk_meters","max_walk_meters"),("transfers","max_transfers")):
                value = route.get(field)
                if type(value) is not int or not 0 <= value <= limit[cap]:
                    hard.append("hard_constraint_"+role+"_"+field)
    if result and result.get("constraints_version") != 1:
        hard.append("stale_constraints_version")
    counters, limits = row.get("counters",{}), row.get("limits",{})
    for key, cap in (("map_http","max_map_http"),("llm_calls","max_llm_calls"),
                     ("search_rounds","max_search_rounds"),("candidates_in_round","max_candidates_per_round")):
        if key in counters and (type(counters[key]) is not int or counters[key] > limits[cap]):
            hard.append("budget_exceeded_"+key)
    if counters.get("candidates_total",0) > counters.get("search_rounds",0)*limits.get("max_candidates_per_round",5):
        hard.append("budget_exceeded_candidates_total")
    if row.get("checks",{}).get("checkpoint_boundary") is False:
        hard.append("checkpoint_persistent_boundary")
    if not row.get("checks",{}).get("counter_consistency",False):
        failed.append("counter_mismatch")
    telemetry = row.get("telemetry",{})
    if not telemetry.get("complete"):
        failed.append("incomplete_telemetry")
    if any(t.get("status")=="started" for t in telemetry.get("llm",[])+telemetry.get("tools",[])):
        failed.append("unfinished_attempt")
    citation = score_citations(row,case)
    if citation["failures"]:
        hard.append("evidence_integrity")
    if citation["covered"] != citation["required"]:
        failed.append("citation_missing_or_wrong")
    if case["runner"] == "memory":
        expected = case["expected_memory"]
        # 仅结构变换；期望状态由用例明确登记，未从生产replay推导。
        flat = {k:v for k,v in expected["requirements"].items() if not isinstance(v,dict)}
        for role in ("person_a","person_b"):
            flat.update({role+"."+k:v for k,v in expected["requirements"][role].items()})
        actual = row.get("memory_actual") or {}
        if actual != {"requirements":flat,"preferences":expected["preferences"],"pending":expected["pending"]}:
            failed.append("memory_final_state_mismatch")
        # 显式长期偏好隔离：预登记expected_preferences可以发现临时修改污染。
        if actual.get("preferences") != expected["preferences"]:
            hard.append("preference_scope_or_state_mismatch")
    return {"passed":not failed and not hard,"failed_checks":sorted(set(failed+hard)),
            "hard_violations":sorted(set(hard)),"citation":citation}


def cost_summary(rows, started_ids):
    selected = [r for r in rows if r["trial_id"] in started_ids]
    count = len(started_ids)
    complete_tools = [r for r in selected if r.get("telemetry",{}).get("complete")
        and all(t.get("status")!="started" for t in r["telemetry"].get("tools",[]))]
    complete_tokens = [r for r in selected if r.get("telemetry",{}).get("complete")
        and all(valid_usage(a.get("usage")) for a in r["telemetry"].get("llm",[]))]
    llm = [a for r in selected for a in r.get("telemetry",{}).get("llm",[])]
    known = [a for a in llm if valid_usage(a.get("usage"))]
    token_totals = {k:sum(a["usage"][k] for a in known)
                    for k in ("prompt_tokens","completion_tokens","total_tokens")}
    full_tools = len(complete_tools)==count
    full_tokens = len(complete_tokens)==count
    tools_total = sum(len(r["telemetry"].get("tools",[])) for r in complete_tools)
    simulated_total = sum(len(r["telemetry"].get("simulated_http",[])) for r in complete_tools)
    return {"started_tasks":count,
        "tool_task_coverage":ratio(len(complete_tools),count),
        "token_task_coverage":ratio(len(complete_tokens),count),
        "usage_coverage":ratio(len(known),len(llm)), "real_llm_attempts":len(llm),
        "returned_models":sorted(set(a["returned_model"] for a in llm if a.get("returned_model"))),
        "tool_calls_mean":tools_total/count if count and full_tools else None,
        "tool_calls_complete_subset_mean":tools_total/len(complete_tools) if complete_tools else None,
        "tool_calls_observed_total":sum(len(r.get("telemetry",{}).get("tools",[])) for r in selected),
        "tool_calls_by_name":dict(Counter(t["name"] for r in selected for t in r.get("telemetry",{}).get("tools",[]))),
        "simulated_http_mean":simulated_total/count if count and full_tools else None,
        "real_map_http_total":sum(r.get("telemetry",{}).get("real_map_http",0) for r in selected),
        "token_mean":{k:token_totals[k]/count if count and full_tokens else None for k in token_totals},
        "token_observed_totals":token_totals,
        "token_observed_lower_bound_mean":{k:token_totals[k]/count if count else None for k in token_totals},
        "token_complete_subset_mean":{k:sum(a["usage"][k] for r in complete_tokens
                   for a in r["telemetry"].get("llm",[]))/len(complete_tokens) if complete_tokens else None
                   for k in token_totals},
        "estimated_cost_yuan":sum(cost_yuan(a["usage"]) for a in known) if full_tokens else None,
        "observed_cost_yuan_lower_bound":sum(cost_yuan(a["usage"]) for a in known),
        "elapsed_mean_seconds":mean(r["elapsed_seconds"] for r in selected) if selected else None}


def summarize(manifest, cases, rows, events, parse_errors=None):
    plans = manifest["trials"]
    plan_by_id = {t["trial_id"]:t for t in plans}
    case_by_id = {c["id"]:c for c in cases}
    counts = Counter(r.get("trial_id") for r in rows)
    integrity = list(parse_errors or [])
    for tid,n in counts.items():
        if tid not in plan_by_id or n != 1:
            integrity.append({"trial_id":tid,"error":"unplanned_or_duplicate_result","count":n})
    start_counts = Counter(e.get("trial_id") for e in events if e.get("event")=="trial_start")
    if any(n!=1 or tid not in plan_by_id for tid,n in start_counts.items()):
        integrity.append({"error":"duplicate_or_unplanned_start"})
    started = set(start_counts)&set(plan_by_id)
    valid, failures = [], []
    for trial in plans:
        tid = trial["trial_id"]
        matches = [r for r in rows if r.get("trial_id")==tid]
        if len(matches)!=1 or tid not in started:
            failures.append({**trial,"failed_checks":["missing_or_duplicate_result_or_start"]})
            continue
        row = matches[0]
        if any(row.get(k)!=trial.get(k) for k in ("case_id","policy","mode")):
            integrity.append({"trial_id":tid,"error":"result_identity_mismatch"})
            failures.append({**trial,"failed_checks":["result_identity_mismatch"]})
            continue
        try:
            decision=grade(row,case_by_id[trial["case_id"]])
        except (KeyError,TypeError,ValueError,AttributeError,IndexError):
            integrity.append({"trial_id":tid,"error":"malformed_result_for_grader"})
            decision={"passed":False,"failed_checks":["malformed_result_for_grader"],
                "hard_violations":["invalid_output_schema"],"citation":{k:0 for k in ("cited","supported","required","covered")}}
        scored = {**row,"grade":decision}
        valid.append(scored)
        if not scored["grade"]["passed"]:
            failures.append({**trial,**scored["grade"]})
    # 事件和结果交叉核对：删掉某个尝试，不能让均值自动变小。
    for row in valid:
        tid = row["trial_id"]
        relevant = [e for e in events if e.get("trial_id")==tid]
        for event, field in (("llm_start","llm"),("tool_start","tools"),("simulated_http","simulated_http")):
            logged = [e.get("attempt_id") for e in relevant if e.get("event")==event]
            observed = [a["attempt_id"] for a in row["telemetry"].get(field,[])]
            values_match=True
            if event=="llm_start":
                ends=[e for e in relevant if e.get("event")=="llm_end"]
                by_aid={e.get("attempt_id"):e for e in ends}
                values_match=len(ends)==len(observed) and all(a["attempt_id"] in by_aid and
                    all(a.get(k)==by_aid[a["attempt_id"]].get(k) for k in ("usage","status","returned_model"))
                    for a in row["telemetry"].get(field,[]))
            if Counter(logged)!=Counter(observed) or len(observed)!=len(set(observed)) or not values_match:
                integrity.append({"trial_id":tid,"error":"event_result_attempt_mismatch","field":field})
                row["telemetry"]["complete"] = False
                row["grade"]["passed"] = False
                failures.append({**plan_by_id[tid],"failed_checks":["event_result_attempt_mismatch"]})
    # 即使进程在结果写入前中断，已发出的模型请求也进入usage覆盖率分母。
    cost_rows=list(valid)
    valid_ids={r["trial_id"] for r in valid}
    for tid in started-valid_ids:
        relevant=[e for e in events if e.get("trial_id")==tid]
        def recovered(start_event,end_event):
            starts=[e for e in relevant if e.get("event")==start_event]
            ends={e.get("attempt_id"):e for e in relevant if e.get("event")==end_event}
            return [ends.get(e.get("attempt_id"),e) for e in starts]
        cost_rows.append({**plan_by_id[tid],"elapsed_seconds":0,"telemetry":{"complete":False,
            "llm":recovered("llm_start","llm_end"),"tools":recovered("tool_start","tool_end"),
            "simulated_http":[e for e in relevant if e.get("event")=="simulated_http"],"real_map_http":0}})
    groups = {}
    policies = sorted(set(t["policy"] for t in plans))
    for policy in policies:
        for group in ("main","planning","refusal","abnormal","memory"):
            eligible_plans = [t for t in plans if t["policy"]==policy and
                (case_by_id[t["case_id"]]["group"]!="memory" if group=="main" else
                 case_by_id[t["case_id"]]["group"]==group)]
            ids = {t["trial_id"] for t in eligible_plans}
            selected = [r for r in valid if r["trial_id"] in ids]
            tp=fp=fn=tn=0
            # 缺结果的应拒任务算未完成召回；正常任务缺失不填成TN。
            actual_by_id={r["trial_id"]:r for r in selected}
            for t in eligible_plans:
                expected=case_by_id[t["case_id"]].get("expected_refusal")
                r=actual_by_id.get(t["trial_id"])
                if expected is True:
                    if r and r["actual_outcome"]=="refused": tp+=1
                    else: fn+=1
                elif expected is False and r:
                    if r["actual_outcome"]=="refused": fp+=1
                    else: tn+=1
            citation={k:sum(r["grade"]["citation"][k] for r in selected)
                      for k in ("cited","supported","required","covered")}
            solvable=[t for t in eligible_plans if case_by_id[t["case_id"]].get("known_has_solution") is True
                      and case_by_id[t["case_id"]]["group"]=="planning"]
            delivered=sum(bool((actual_by_id.get(t["trial_id"],{}).get("result") or {}).get("recommendations"))
                          and not actual_by_id[t["trial_id"]]["grade"]["hard_violations"] for t in solvable)
            groups[policy+":"+group]={"planned_trials":len(ids),
                "unique_case_ids":len(set(t["case_id"] for t in eligible_plans)),
                "scenario_families":len(set(case_by_id[t["case_id"]]["family"] for t in eligible_plans)),
                "acceptance":ratio(sum(r["grade"]["passed"] for r in selected),len(ids)),
                "known_solvable_delivery":ratio(delivered,len(solvable)),
                "citation_precision":ratio(citation["supported"],citation["cited"]),
                "citation_coverage":ratio(citation["covered"],citation["required"]),
                "refusal_precision":ratio(tp,tp+fp),"refusal_recall":ratio(tp,tp+fn),
                "false_refusal_rate":ratio(fp,fp+tn),"refusal_confusion":{"TP":tp,"FP":fp,"FN":fn,"TN":tn},
                "outcomes":dict(Counter(r["actual_outcome"] for r in selected)),
                "hard_violations":sum(bool(r["grade"]["hard_violations"]) for r in selected),
                "cost":cost_summary([r for r in cost_rows if r["trial_id"] in ids],ids&started)}
    measurement_ok = not integrity and len(valid)==len(plans) and len(started)==len(plans)
    measurement_ok = measurement_ok and all(r["telemetry"]["complete"] for r in valid)
    measurement_ok = measurement_ok and all(valid_usage(a.get("usage")) for r in valid
                                           for a in r["telemetry"].get("llm",[]))
    hard_ok = not any(r["grade"]["hard_violations"] for r in valid)
    quality_ok = True
    for p in policies:
        for name in ("main","memory"):
            g=groups[p+":"+name]
            if g["planned_trials"] and (g["acceptance"]["value"] or 0)<.9:
                quality_ok=False
        g=groups[p+":main"]
        has_refusal_tasks=any(case_by_id[t["case_id"]].get("expected_refusal") is True
                             for t in plans if t["policy"]==p)
        for key,target in (("citation_precision",1), ("citation_coverage",1), ("refusal_precision",.9), ("refusal_recall",.9)):
            metric=g[key]
            applicable=has_refusal_tasks if key.startswith("refusal") else metric["denominator"]>0
            if applicable and (metric["value"] is None or metric["value"]<target):
                quality_ok=False
        if g["false_refusal_rate"]["value"] is not None and g["false_refusal_rate"]["value"]>.05:
            quality_ok=False
    return {"schema_version":1,"planned":len(plans),"started":len(started),"valid_results":len(valid),
        "integrity_errors":integrity,"measurement_complete":measurement_ok,"hard_checks_passed":hard_ok,
        "quality_targets_passed":quality_ok,"acceptance_gate_passed":measurement_ok and hard_ok and quality_ok,
        "by_policy_group":groups,"failures":failures,
        "note":"作者自建相关场景；main不含Memory。引用仅覆盖统一结构化展示适配层。受控解析器路径不强制调用模型。"}


def report_markdown(summary):
    def pct(m):
        return "N/A (0/0)" if m["value"] is None else f'{m["value"]:.1%} ({m["numerator"]}/{m["denominator"]})'
    def val(x):
        return "未知/N/A" if x is None else f"{x:.3f}"
    lines=["# M8 实测报告", "", summary["note"], "",
        f"计划 {summary['planned']}，启动 {summary['started']}，有效结果 {summary['valid_results']}。",
        f"测量完整：{summary['measurement_complete']}；硬检查：{summary['hard_checks_passed']}；效果目标：{summary['quality_targets_passed']}。", "",
        "| 策略:组 | 自动验收 | 引用准确率 | 引用覆盖率 | 拒答Precision | 拒答Recall | 平均工具调用 | 平均总Token |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: |"]
    for name,g in summary["by_policy_group"].items():
        if not g["planned_trials"]: continue
        lines.append(f"| {name} | {pct(g['acceptance'])} | {pct(g['citation_precision'])} | {pct(g['citation_coverage'])} | "
             f"{pct(g['refusal_precision'])} | {pct(g['refusal_recall'])} | {val(g['cost']['tool_calls_mean'])} | "
             f"{val(g['cost']['token_mean']['total_tokens'])} |")
    lines += ["", "完整原始分子/分母、输入/输出Token、工具/HTTP分层、usage覆盖率见 summary.json。",
              "模拟地图HTTP与真实地图HTTP分列；本入口真实地图HTTP恒为0。无模型调用路径Token为0；缺usage为未知。",
              "旧产品没有拒答门禁时，超范围请求可能被澄清：Precision为N/A、Recall为0是有效基线。",
              "引用指标来自M8输出适配层，不证明旧网页自由文本已经有引用能力。",
              "计费按北京地域原价估算，不等同账单。Memory为现有受控解析器的整个对话终态，不冒称实模Memory实验。", ""]
    return "\n".join(lines)
