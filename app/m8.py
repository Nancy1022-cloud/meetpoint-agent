"""M8：数据冻结、零网络验证、真实模型校准、可恢复报告。"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import subprocess
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.m4_decider import SYSTEM_PROMPT
from app.m8_cases import calibration_ids, digest, suite
from app.m8_evidence import bind_claims
from app.m8_eval import run_case
from app.m8_metrics import report_markdown, summarize
from app.m8_trace import SpendLedger, append_json, exclusive_run, read_jsonl, valid_usage


SOURCE_FILES = ["app/m1_models.py","app/m1_store.py","app/m2_models.py","app/m3_baseline.py",
    "app/m3_core.py","app/m4_agent.py","app/m4_decider.py","app/m4_models.py",
    "app/m4_synthetic.py","app/m5_store.py","app/m5_parse.py","app/m5_checkpoint.py",
    "app/m7_eval.py","app/m7_memory.py","app/config.py"]


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(set(SOURCE_FILES+[p.relative_to(root).as_posix() for p in (root/"app").glob("m8*.py")]))
    files = {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths}
    try:
        commit = subprocess.run(["git","rev-parse","HEAD"],cwd=root,capture_output=True,
                                text=True,timeout=5,check=True).stdout.strip()
    except (OSError,subprocess.SubprocessError):
        commit = None  # Docker镜像通常没有.git；文件哈希仍可用。
    packages = {}
    for name in ("python","openai","pydantic","langgraph","langgraph-checkpoint-sqlite"):
        packages[name] = sys.version.split()[0] if name=="python" else importlib.metadata.version(name)
    return {"git_commit":commit,"source_files_sha256":files,"source_digest":digest(files),"packages":packages}


@contextmanager
def no_network():
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def deny(*args,**kwargs):
        raise RuntimeError("M8 scripted模式禁止网络连接")
    socket.socket.connect = socket.socket.connect_ex = deny
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = original, original_ex


def freeze(root):
    path = Path(root)/"suite.json"
    if path.exists():
        verify(root)
        print(json.dumps({"status":"already_frozen_verified","root":str(root)},ensure_ascii=False))
        return
    if Path(root).exists() and any(Path(root).iterdir()):
        raise ValueError("冻结目录不为空；请使用新的版本目录，禁止覆盖旧结果")
    data = suite()
    dump(path,data)
    manifest={"schema_version":1,"suite_sha256":digest(data),
        "created_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "counts":{s:len(data[s]) for s in ("dev","holdout")},
        "families":{s:len(set(c["family"] for c in data[s])) for s in ("dev","holdout")},
        "holdout_status":"reserved_author_visible_not_independent_blind",
        "generator_source_digest":fingerprint()["source_digest"]}
    dump(Path(root)/"dataset_manifest.json",manifest)
    print(json.dumps(manifest,ensure_ascii=False,indent=2))


def verify(root):
    data=json.loads((Path(root)/"suite.json").read_text(encoding="utf-8-sig"))
    meta=json.loads((Path(root)/"dataset_manifest.json").read_text(encoding="utf-8-sig"))
    if digest(data)!=meta["suite_sha256"]:
        raise ValueError("用例校验失败；不要重新freeze覆盖，请查清修改来源或另立数据版本")
    for split in ("dev","holdout"):
        ids=[c["id"] for c in data[split]]
        if len(ids)!=len(set(ids)) or len(ids)!=meta["counts"][split]:
            raise ValueError("用例ID重复或数量不一致")
    return data,meta


def doctor():
    settings=get_settings()
    required=[Path(p).is_file() for p in SOURCE_FILES]
    model_ok=settings.llm_model=="qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-")
    endpoint_ok=settings.llm_base_url.rstrip("/")=="https://dashscope.aliyuncs.com/compatible-mode/v1"
    result={"required_source_files_present":all(required),"configured_model":settings.llm_model,
        "model_matches_M8":model_ok,"beijing_endpoint_for_budget":endpoint_ok,
        "llm_key_present":bool(settings.llm_api_key.get_secret_value().strip()),
        "map_network_used_by_M8":False,"pricing_yuan_per_million":{"input":.2,"output":.8},
        "fingerprint":fingerprint()}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return all(required) and model_ok and endpoint_ok and result["llm_key_present"]


def make_report(batch):
    batch=Path(batch)
    manifest=json.loads((batch/"manifest.json").read_text(encoding="utf-8-sig"))
    if (batch/"manifest.sha256").read_text(encoding="utf-8").strip()!=digest(manifest):
        raise ValueError("批次清单已被修改，禁止删除失败任务或修改阈值")
    cases=json.loads((batch/"cases.json").read_text(encoding="utf-8-sig"))
    if digest(cases)!=manifest["selected_cases_sha256"]:
        raise ValueError("批次用例已被修改，禁止继续判分")
    rows, rerrors=read_jsonl(batch/"results.jsonl")
    events,eerrors=read_jsonl(batch/"events.jsonl")
    result=summarize(manifest,cases,rows,events,rerrors+eerrors)
    result["model"]=manifest["model"]
    result["batch_id"]=manifest["batch_id"]
    result["source_digest"]=manifest["source_digest"]
    dump(batch/"summary.json",{k:v for k,v in result.items() if k!="failures"})
    (batch/"failures.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in result["failures"]),encoding="utf-8")
    (batch/"report.md").write_text(report_markdown(result),encoding="utf-8")
    if manifest.get("calibration"):
        costs={}
        tokens={}
        for group in ("planning","refusal","abnormal","memory"):
            g=result["by_policy_group"]["V0:"+group]
            c=g["cost"]
            costs[group]=(c["estimated_cost_yuan"]/c["started_tasks"]
                          if c["estimated_cost_yuan"] is not None and c["started_tasks"] else None)
            tokens[group]=c["token_mean"]
        planned={"planning":80,"refusal":40,"abnormal":40,"memory":40}
        projected=(sum(costs[g]*n*3*2 for g,n in planned.items())
                   if result["measurement_complete"] and all(x is not None for x in costs.values()) else None)
        dump(batch/"calibration.json",{"completed_trials":result["valid_results"],
            "group_estimated_mean_yuan":costs,"group_token_means":tokens,
            "projected_200_cases_x3_x2_versions_yuan":projected,
            "assumptions":"20条开发用例分层外推；两版和各组消耗暂视为相同，且沿用北京原价；小样本估算不是保证或已扣费用。"})
    return result


def run(args):
    data,meta=verify(args.root)
    if args.split=="holdout" and not args.final:
        raise ValueError("M8先使用dev；预留集请到最终评测显式添加--final")
    if args.calibrate and (args.split!="dev" or args.mode!="llm" or args.policies!="V0" or args.repeats!=1):
        raise ValueError("校准固定为dev、llm、V0、每题1次，避免混入零模型基线")
    settings=get_settings()
    if args.mode=="llm":
        if not (settings.llm_model=="qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-")):
            raise ValueError("请先在.env设置LLM_MODEL=qwen3.7-flash并重建容器环境")
        if settings.llm_base_url.rstrip("/")!="https://dashscope.aliyuncs.com/compatible-mode/v1":
            raise ValueError("本版预算针对北京端点；其他地域需先核价，不沿用北京估价")
        if not settings.llm_api_key.get_secret_value().strip():
            raise ValueError("LLM_API_KEY为空；请在自己的.env填写，不要发给助手")
    cases=data[args.split]
    if args.calibrate:
        selected=set(calibration_ids(cases))
        cases=[c for c in cases if c["id"] in selected]
    policies=args.policies.split(",")
    if len(policies)!=len(set(policies)) or not set(policies)<={"B0","V0"}:
        raise ValueError("policies只支持B0,V0且不得重复")
    stamp=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    batch_id=f"{args.mode}_{'calibration' if args.calibrate else args.split}_{stamp}_{uuid4().hex[:6]}"
    batch=Path(args.root)/"runs"/batch_id
    batch.mkdir(parents=True,exist_ok=False)
    trials=[]
    for c in cases:
        for rep in range(1,args.repeats+1):
            for p in policies:
                trials.append({"trial_id":f"{c['id']}:{p}:{args.mode}:{rep}","case_id":c["id"],
                    "policy":p,"mode":args.mode,"repeat":rep})
    manifest={"schema_version":1,"batch_id":batch_id,"split":args.split,"calibration":args.calibrate,
        "suite_sha256":meta["suite_sha256"],"selected_cases_sha256":digest(cases),
        "model":settings.llm_model if args.mode=="llm" else "not_called",
        "configured_model":settings.llm_model,"prompt_sha256":hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "parameters":{"temperature":0,"max_tokens":512,"enable_thinking":False,"sdk_max_retries":0},
        "money_limits":{"batch_yuan":args.budget_yuan,"project_yuan":args.project_budget_yuan},
        "pricing":{"region":"Beijing","checked_at":"2026-10-29","input":.2,"output":.8,
            "unit":"CNY/million_tokens_for_input_up_to_32K","not_invoice":True},
        "citation_scope":"M8_shared_structured_output_adapter_not_original_UI",
        "thresholds":{"acceptance":.9,"citation_precision":1,"citation_coverage":1,
                      "refusal_precision":.9,"refusal_recall":.9,"false_refusal_rate_max":.05},
        "trials":trials,**fingerprint()}
    dump(batch/"manifest.json",manifest)
    (batch/"manifest.sha256").write_text(digest(manifest)+"\n",encoding="utf-8")
    dump(batch/"cases.json",cases)
    ledger=SpendLedger(args.root,batch_id,args.budget_yuan,args.project_budget_yuan)
    by_id={c["id"]:c for c in cases}
    interrupted=None
    try:
        with exclusive_run(args.root):
            for index,t in enumerate(trials,1):
                if args.mode=="scripted":
                    with no_network():
                        row=run_case(by_id[t["case_id"]],t["policy"],args.mode,t["trial_id"],
                            sink=lambda e:append_json(batch/"events.jsonl",e))
                else:
                    row=run_case(by_id[t["case_id"]],t["policy"],args.mode,t["trial_id"],settings,ledger,
                            sink=lambda e:append_json(batch/"events.jsonl",e))
                row["repeat"]=t["repeat"]
                bind_claims(row)
                append_json(batch/"results.jsonl",row)
                print(json.dumps({"done":index,"planned":len(trials),"case_id":row["case_id"],
                      "outcome":row["actual_outcome"]},ensure_ascii=False),flush=True)
                if row["actual_outcome"]=="measurement_stopped" or any(
                        not valid_usage(a.get("usage")) for a in row["telemetry"]["llm"]):
                    interrupted="测量或用量缺失，已暂停新增请求"
                    break
    except (KeyboardInterrupt,OSError) as exc:
        interrupted=type(exc).__name__
    finally:
        result=make_report(batch)
        dump(batch/"run_status.json",{"interrupted":interrupted,"planned":len(trials),
             "completed":result["valid_results"],"budget_spent_or_reserved_yuan":ledger.spent()})
    output={"batch":batch.as_posix(),"measurement_complete":result["measurement_complete"],
        "hard_checks_passed":result["hard_checks_passed"],"quality_targets_passed":result["quality_targets_passed"],
        "report":(batch/"report.md").as_posix()}
    print(json.dumps(output,ensure_ascii=False,indent=2))
    return 3 if not result["measurement_complete"] or not result["hard_checks_passed"] else 0 if result["acceptance_gate_passed"] else 2


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor")
    for cmd in ("freeze","verify"):
        s=sub.add_parser(cmd); s.add_argument("--root",default="data/m8/v1")
    s=sub.add_parser("run")
    s.add_argument("--root",default="data/m8/v1")
    s.add_argument("--split",choices=["dev","holdout"],default="dev")
    s.add_argument("--mode",choices=["scripted","llm"],default="scripted")
    s.add_argument("--policies",default="V0")
    s.add_argument("--repeats",type=int,choices=[1,2,3],default=1)
    s.add_argument("--calibrate",action="store_true")
    s.add_argument("--final",action="store_true")
    s.add_argument("--budget-yuan",type=float,default=1)
    s.add_argument("--project-budget-yuan",type=float,default=5)
    s=sub.add_parser("report");s.add_argument("--batch",required=True)
    args=p.parse_args()
    try:
        if args.command=="doctor": return 0 if doctor() else 1
        if args.command=="freeze": freeze(args.root);return 0
        if args.command=="verify":
            _,meta=verify(args.root);print(json.dumps({"verified":True,**meta},ensure_ascii=False,indent=2));return 0
        if args.command=="report":
            r=make_report(args.batch)
            print(json.dumps({"batch":args.batch,"measurement_complete":r["measurement_complete"],
                              "quality_targets_passed":r["quality_targets_passed"]},ensure_ascii=False))
            return 3 if not r["measurement_complete"] or not r["hard_checks_passed"] else 0 if r["acceptance_gate_passed"] else 2
        if not (0<args.budget_yuan<=args.project_budget_yuan<=100):
            raise ValueError("预算需满足0<单批上限<=项目上限<=100元")
        return run(args)
    except (ValueError,FileNotFoundError) as exc:
        print(str(exc),file=sys.stderr);return 1


if __name__=="__main__":
    raise SystemExit(main())
