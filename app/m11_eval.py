"""Independent state scoring. Always confirm any valid preview, even a wrong one."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4
from app.m1_models import Requirements
from app.m5_store import flatten
from app.m8_trace import Trace, valid_usage, BudgetStop
from app.m11_store import ProposalStore
from app.m11_parse import rule_proposal, LLMParser
from app.m11_usage import MemoryClient, usage_summary

USER="m11_evaluation"
ORIGINS={"person_a":{"origin_text":"自建起点A"},"person_b":{"origin_text":"自建起点B"}}
BASE=flatten(Requirements.model_validate(ORIGINS).model_dump())

def state(store,sid):
    m=store.get_memory(USER,sid)
    return {"current":flatten(m["task"]["requirements"]),"defaults":m["preferences"]["values"]}

def run_case(case,policy,trial_id,*,mode="scripted",settings=None,ledger=None,sink=None,client_factory=None,scripted_parser=None):
    start=time.perf_counter();trace=Trace(trial_id,uuid4().hex,sink)
    row={"trial_id":trial_id,"case_id":case["id"],"family":case["family"],"policy":policy,"mode":mode,
         "started":True,"turns":[],"error":None,"actual":None,"real_map_http":0}
    trace.emit("trial_start",case_id=case["id"],policy=policy)
    client=None
    try:
        with tempfile.TemporaryDirectory(prefix="m11_case_") as temp:
            store=ProposalStore(Path(temp)/"state.sqlite3")
            m=store.create_memory_task({"user_id":USER,**ORIGINS});sid=m["task"]["session_id"]
            # A separate session and a separate user are created BEFORE edits.
            other=store.create_memory_task({"user_id":USER,**ORIGINS})["task"]["session_id"]
            outsider=store.create_memory_task({"user_id":"m11_other",**ORIGINS})["task"]["session_id"]
            if policy=="RULE":parser=rule_proposal
            elif mode=="llm":
                if client_factory is None:
                    from openai import OpenAI
                    delegate=OpenAI(api_key=settings.llm_api_key.get_secret_value(),base_url=settings.llm_base_url,
                                    timeout=settings.llm_timeout_seconds,max_retries=0)
                else:delegate=client_factory()
                client=MemoryClient(delegate,trace,ledger);parser=LLMParser(settings,client)
            elif scripted_parser is not None:parser=scripted_parser
            else:
                # Deliberately the SAME controlled grammar. Scripted mode validates
                # infrastructure, never substitutes expected answers for a parser.
                parser=rule_proposal
            expected_current,expected_defaults=deepcopy(BASE),{}
            for index,turn in enumerate(case["turns"]):
                before=state(store,sid);m=store.get_memory(USER,sid)
                req={"user_id":USER,"expected_version":m["task"]["constraints_version"],
                     "expected_preferences_revision":m["preferences"]["revision"],
                     "text":turn["text"],"parser":"rule" if policy=="RULE" or mode=="scripted" else "llm"}
                preview=store.preview(sid,req,parser)
                preview_no_write=state(store,sid)==before
                status=preview["status"];diff=preview.get("diff",[])
                if status=="awaiting_confirmation":
                    confirmed=store.confirm(sid,preview["proposal_id"],{k:req[k] for k in
                        ("user_id","expected_version","expected_preferences_revision")})
                    status=confirmed["status"]
                expected_current.update(turn["current"]);expected_defaults.update(turn["defaults"])
                actual=state(store,sid)
                current_ok=actual["current"]==expected_current;defaults_ok=actual["defaults"]==expected_defaults
                row["turns"].append({"index":index,"status":status,"expected_status":turn["expected"],
                    "preview_no_write":preview_no_write,"diff":diff,"actual":actual,
                    "expected":{"current":deepcopy(expected_current),"defaults":deepcopy(expected_defaults)},
                    "status_correct":status==turn["expected"],"current_correct":current_ok,"defaults_correct":defaults_ok,
                    "unrequested_current_fields":sorted(k for k,v in actual["current"].items() if before["current"].get(k)!=v and k not in turn["current"]),
                    "unrequested_default_fields":sorted(k for k in set(before["defaults"])|set(actual["defaults"]) if before["defaults"].get(k)!=actual["defaults"].get(k) and k not in turn["defaults"])})
            row["actual"]=state(store,sid)
            row["old_session_isolated"]=state(store,other)["current"]==BASE
            row["old_session_current"]=state(store,other)["current"]
            row["other_user_isolated"]=flatten(store.get_memory("m11_other",outsider)["task"]["requirements"])==BASE and store.read_preferences("m11_other")["values"]=={}
            row["other_user_actual"]={"current":flatten(store.get_memory("m11_other",outsider)["task"]["requirements"]),"defaults":store.read_preferences("m11_other")["values"]}
            new=store.create_memory_task({"user_id":USER,**ORIGINS})
            expected_new=deepcopy(BASE)
            for key,value in expected_defaults.items():expected_new[key if key=="preference" else "person_a."+key]=value
            row["new_session_correct"]=flatten(new["task"]["requirements"])==expected_new
            row["new_session_current"]=flatten(new["task"]["requirements"])
            row["hard_checks"]={"preview_no_write":all(t["preview_no_write"] for t in row["turns"]),
                "old_session_isolated":row["old_session_isolated"],"other_user_isolated":row["other_user_isolated"],
                "defaults_scope_integrity":all(not t["unrequested_default_fields"] for t in row["turns"]),
                "unrequested_current_fields":all(not t["unrequested_current_fields"] for t in row["turns"])}
    except BudgetStop as exc:
        row["error"]=str(exc);trace.complete=False
    except Exception as exc:
        row["error"]=type(exc).__name__
    finally:
        if client:client.close()
        row["attempts"]=trace.llm;row["usage"]=usage_summary(trace.llm)
        row["measurement_complete"]=trace.complete and row["usage"]["usage_complete"]
        row["elapsed_seconds"]=round(time.perf_counter()-start,6)
        trace.emit("trial_end",case_id=case["id"],error=row["error"])
    return row

def grade(row,case):
    # The saved expected fields on rows are presentation only. Rebuild the oracle
    # from immutable cases for every report; never trust row['passed'].
    wrong=[];turns=row.get("turns",[]);current=deepcopy(BASE);defaults={}
    if row.get("error"):wrong.append("execution:"+row["error"])
    if len(turns)!=len(case["turns"]):wrong.append("missing_turns")
    for i,spec in enumerate(case["turns"]):
        current.update(spec["current"]);defaults.update(spec["defaults"])
        actual=turns[i] if i<len(turns) else {}
        if actual.get("status")!=spec["expected"]:wrong.append(f"turn_{i}:status")
        if actual.get("actual",{}).get("current")!=current:wrong.append(f"turn_{i}:current")
        if actual.get("actual",{}).get("defaults")!=defaults:wrong.append(f"turn_{i}:defaults")
        if actual.get("preview_no_write") is not True:wrong.append(f"turn_{i}:preview_write")
    expected_new=deepcopy(BASE)
    for key,value in defaults.items():expected_new[key if key=="preference" else "person_a."+key]=value
    if row.get("old_session_current")!=BASE:wrong.append("old_session_isolated")
    if row.get("other_user_actual")!={"current":BASE,"defaults":{}}:wrong.append("other_user_isolated")
    if row.get("new_session_current")!=expected_new:wrong.append("new_session_correct")
    return sorted(set(wrong))

def summarize(manifest,cases,rows,events=None):
    by_id={c["id"]:c for c in cases};planned=manifest["plan"]
    results={};protocol=[];valid=[]
    for row in rows:
        tid=row.get("trial_id")
        if tid in results:protocol.append("duplicate_trial:"+str(tid))
        results[tid]=row
    expected_ids={p["trial_id"] for p in planned}
    if set(results)-expected_ids:protocol.append("unexpected_trials")
    for item in planned:
        row=results.get(item["trial_id"])
        wrong=["missing_result"] if row is None else grade(row,by_id[item["case_id"]])
        if row and (row.get("case_id")!=item["case_id"] or row.get("policy")!=item["policy"]):wrong.append("plan_identity")
        valid.append({**item,"passed":not wrong,"failed_checks":wrong})
    summaries={}
    for policy in manifest["policies"]:
        subset=[r for r in valid if r["policy"]==policy];raw=[results[r["trial_id"]] for r in subset if r["trial_id"] in results]
        attempts=[a for r in raw for a in r.get("attempts",[])];u=usage_summary(attempts)
        complete=(len(raw)==len(subset) and u["usage_complete"] and all(r.get("measurement_complete") for r in raw))
        n=len(raw)
        turn_count=sum(len(r.get("turns",[])) for r in raw)
        exact=0;tp=fp=fn=tn=0
        for row in raw:
            current=deepcopy(BASE);defaults={}
            for spec,observed in zip(by_id[row["case_id"]]["turns"],row.get("turns",[])):
                current.update(spec["current"]);defaults.update(spec["defaults"])
                exact+=observed.get("actual")=={"current":current,"defaults":defaults}
                expected_refuse=spec["expected"]=="refused";actual_refuse=observed.get("status")=="refused"
                tp+=expected_refuse and actual_refuse;fp+=not expected_refuse and actual_refuse
                fn+=expected_refuse and not actual_refuse;tn+=not expected_refuse and not actual_refuse
        hard=all(all(v is True for v in r.get("hard_checks",{}).values()) and bool(r.get("hard_checks")) for r in raw) and n==len(subset)
        summaries[policy]={"planned":len(subset),"started":n,"passed":sum(r["passed"] for r in subset),
            "automatic_acceptance_rate":sum(r["passed"] for r in subset)/len(subset) if subset else None,
            "state_exact_turns":exact,"observed_turns":turn_count,"usage":u,
            "refusal":{"tp":tp,"fp":fp,"fn":fn,"tn":tn,"precision":tp/(tp+fp) if tp+fp else None,
                       "recall":tp/(tp+fn) if tp+fn else None},
            "average_task_tokens":u["tokens"]["total_tokens"]/n if complete and n else None,
            "average_model_attempts":len(attempts)/n if n else None,"measurement_complete":complete,
            "hard_checks_passed":hard,"quality_targets_passed":bool(subset) and sum(r["passed"] for r in subset)/len(subset)>=.9,
            "average_business_tool_calls":0,"real_map_http":0,"citation_accuracy":None}
    complete=all(s["measurement_complete"] for s in summaries.values()) and not protocol
    if events is not None:
        for item in planned:
            tid=item["trial_id"]
            if sum(e.get("trial_id")==tid and e.get("event")=="trial_start" for e in events)!=1:protocol.append("trial_start:"+tid)
            if sum(e.get("trial_id")==tid and e.get("event")=="trial_end" for e in events)!=1:protocol.append("trial_end:"+tid)
        starts=[e.get("attempt_id") for e in events if e.get("event")=="llm_start"]
        ends=[e.get("attempt_id") for e in events if e.get("event")=="llm_end"]
        actual=[a.get("attempt_id") for r in rows for a in r.get("attempts",[])]
        if sorted(starts)!=sorted(ends) or sorted(ends)!=sorted(actual) or len(set(actual))!=len(actual):protocol.append("llm_event_mismatch")
    paired=[]
    indexed={(r["case_id"],r["policy"],r.get("repeat",1)):r for r in valid}
    for (cid,policy,rep),item in indexed.items():
        if policy=="RULE" and (cid,"NL",rep) in indexed:
            other=indexed[(cid,"NL",rep)]
            if item["trial_id"] in results and other["trial_id"] in results:
                paired.append({"case_id":cid,"repeat":rep,"RULE":item["passed"],"NL":other["passed"]})
    pair={"complete_pairs":len(paired),"NL_only_pass":sum(p["NL"] and not p["RULE"] for p in paired),
          "RULE_only_pass":sum(p["RULE"] and not p["NL"] for p in paired)}
    hard_ok=all(s["hard_checks_passed"] for s in summaries.values()) and not protocol
    development_gate=(manifest["stage"]=="dev" and manifest["mode"]=="llm" and len(cases)==36
        and set(manifest["policies"])=={"RULE","NL"} and complete and not protocol and hard_ok
        and summaries.get("NL",{}).get("quality_targets_passed",False))
    return {"batch_id":manifest["batch_id"],"mode":manifest["mode"],"stage":manifest["stage"],
        "independent_conversations":len(cases),"planned":len(planned),"started":len(results),
        "measurement_complete":complete and not protocol,"hard_checks_passed":hard_ok,
        "development_gate_passed":bool(development_gate),"comparison":pair,
        "protocol_errors":protocol,"policies":summaries,"failures":[r for r in valid if not r["passed"]],
        "scope_note":"自建Memory开发对话；LLM输出均经预览确认；真实高德为0；引用指标不适用。"}

def markdown(summary):
    lines=["# M11 Memory 开发评测","",summary["scope_note"],"",
        f"批次：`{summary['batch_id']}`；模式：{summary['mode']}；独立对话：{summary['independent_conversations']}。",
        f"计划/启动：{summary['planned']}/{summary['started']}；测量完整：{summary['measurement_complete']}；硬检查：{summary['hard_checks_passed']}。",
        f"完整实模开发门禁：{summary['development_gate_passed']}；RULE/NL完整配对：{summary['comparison']}。","",
        "| 策略 | 全部要求通过/计划 | 通过率 | 平均真实任务Token | 模型尝试 | usage完整 |",
        "| --- | ---: | ---: | ---: | ---: | --- |"]
    for p,s in summary["policies"].items():
        rate=s["automatic_acceptance_rate"]
        lines.append(f"| {p} | {s['passed']}/{s['planned']} | {rate:.1%} | {s['average_task_tokens']} | {s['usage']['attempts']} | {s['usage']['usage_complete']} |")
    lines += ["","RULE保存完整结构化状态；差别在输入解析，不将窗口丢失作为唯一基线。",
        "scripted模式两臂使用同一受控解析器，结果可未达质量目标；它只验证基础设施，不证明自然语言能力。",
        "业务地图工具为0，模型解析单独计数；引用准确率N/A。Token缺失时全量平均值为未知。",
        "真实模型不自动重试；失败和已开始但未完成的试次保留。报告不代表M12冻结评测。","","## 失败","",
        *[f"- {r['trial_id']}：{', '.join(r['failed_checks'])}" for r in summary["failures"]]]
    return "\n".join(lines)+"\n"
