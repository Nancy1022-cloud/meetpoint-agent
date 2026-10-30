"""M11 CLI: frozen development conversations, measured runs, independent re-scoring."""
import argparse
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo
from app.config import get_settings
from app.m8_trace import SpendLedger, append_json, exclusive_run, read_jsonl
from app.m11_cases import development_cases
from app.m11_eval import run_case, summarize, markdown
from app.m8 import no_network

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")

def fingerprint():
    root=Path(__file__).resolve().parents[1]
    names=[p.relative_to(root).as_posix() for p in (root/"app").glob("m11*.py")]
    names += ["app/test_m11.py","app/m1_models.py","app/m1_store.py","app/m5_models.py","app/m5_store.py",
        "app/m5_parse.py","app/m8_trace.py","app/m9_scope.py","app/m5_api.py","app/main.py",
        "app/m6_runtime.py","app/static/meetpoint.html","app/static/m11_ui.js","apply_m11_changes.py","M11_patch_manifest.json"]
    hashes={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in sorted(names) if (root/n).is_file()}
    return {"source_files_sha256":hashes,"source_digest":digest(hashes)}

def doctor():
    root=Path(__file__).resolve().parents[1];settings=get_settings()
    read=lambda name:(root/name).read_text(encoding="utf-8-sig") if (root/name).is_file() else ""
    return {"milestone":"M11","configured_model":settings.llm_model,
        "llm_key_present":bool(settings.llm_api_key.get_secret_value().strip()),
        "model_matches_M10":settings.llm_model=="qwen3.7-flash" or settings.llm_model.startswith("qwen3.7-flash-2026-"),
        "integration_ready":all("M11_INTEGRATION_V1" in read(n) for n in ("app/main.py","app/m5_api.py","app/static/meetpoint.html")),
        "M10_planner_preserved":"M10_INTEGRATION_V1" in read("app/m6_runtime.py"),
        "new_development_conversations":len(development_cases()),"M8_holdout_executed":False,
        "model_proposals_require_confirmation":True,"map_http":0,"fingerprint":fingerprint()}

def freeze(root):
    path=Path(root)/"suite.json";cases=development_cases();data={"protocol":"m11_dev_v1","cases":cases}
    if path.exists():
        old=json.loads(path.read_text(encoding="utf-8-sig"))
        if old!=data:raise ValueError("M11开发集已有不同内容；保留旧版本，另建v2目录。")
    else:dump(path,data)
    sha=digest(data)
    manifest=Path(root)/"dataset_manifest.json"
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8-sig"))["sha256"]!=sha:
        raise ValueError("M11数据清单不匹配，不能覆盖。")
    if not manifest.exists():dump(manifest,{"sha256":sha,"independent_conversations":len(cases),"role_mapping":"我=A,朋友/他/她/对方=B","source":"author_written_development"})
    return {"suite":str(path),"sha256":sha,"conversations":len(cases),"model_calls":0,"map_calls":0}

def load_suite(root):
    p=Path(root);data=json.loads((p/"suite.json").read_text(encoding="utf-8-sig"))
    expected=json.loads((p/"dataset_manifest.json").read_text(encoding="utf-8-sig"))["sha256"]
    if digest(data)!=expected:raise ValueError("M11冻结开发数据哈希不一致。")
    return data,expected

def report(batch):
    batch=Path(batch);manifest=json.loads((batch/"manifest.json").read_text(encoding="utf-8"))
    if digest(manifest)!=(batch/"manifest.sha256").read_text().strip():raise ValueError("批次清单哈希失败。")
    cases=json.loads((batch/"cases.json").read_text(encoding="utf-8"))
    if digest(cases)!=manifest["selected_cases_sha256"]:raise ValueError("选中用例哈希失败。")
    for name,sha in manifest["source_files_sha256"].items():
        p=batch/"source_snapshot"/name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError("源码快照哈希失败："+name)
    current_root=Path(__file__).resolve().parents[1]
    for name in ("app/m11_eval.py","app/m11_cases.py","app/m11_usage.py"):
        if hashlib.sha256((current_root/name).read_bytes()).hexdigest()!=manifest["source_files_sha256"].get(name):
            raise ValueError("当前判分依赖与批次快照不同，不覆盖历史报告；请使用该批次对应代码："+name)
    rows,errors=read_jsonl(batch/"results.jsonl");events,ee=read_jsonl(batch/"events.jsonl")
    result=summarize(manifest,cases,rows,events)
    if errors or ee:
        result["measurement_complete"]=False;result["protocol_errors"]+=errors+ee
    dump(batch/"summary.json",{k:v for k,v in result.items() if k!="failures"})
    (batch/"failures.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in result["failures"]),encoding="utf-8")
    (batch/"report.md").write_text(markdown(result),encoding="utf-8")
    return result

def run(args):
    data,sha=load_suite(args.root);cases=data["cases"]
    if args.stage=="pilot":
        families={"pronoun_duration","default_natural","mixed_scope","missing_role","drive_synonym","default_then_override"}
        cases=[c for c in cases if c["family"] in families]
    settings=get_settings()
    if args.mode=="llm" and (settings.llm_model!="qwen3.7-flash" and not settings.llm_model.startswith("qwen3.7-flash-2026-")):
        raise ValueError("本轮沿用qwen3.7-flash；先核对.env，换模型需独立记录。")
    if args.mode=="llm" and not settings.llm_api_key.get_secret_value().strip():raise ValueError("缺少LLM_API_KEY。")
    bid=f"{args.mode}_{args.stage}_"+datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")+"_"+uuid4().hex[:6]
    batch=Path(args.root)/"runs"/bid;batch.mkdir(parents=True)
    plan=[{"trial_id":f"{c['id']}:{p}:{rep}","case_id":c["id"],"policy":p,"repeat":rep}
          for c in cases for p in args.policies for rep in range(1,args.repeats+1)]
    manifest={"milestone":"M11","batch_id":bid,"mode":args.mode,"stage":args.stage,
        "policies":args.policies,"repeats":args.repeats,"plan":plan,"dataset_sha256":sha,
        "selected_cases_sha256":digest(cases),"model":settings.llm_model,"price_basis":"retained_M8_estimate_not_invoice",
        "batch_yuan_limit":args.batch_yuan,"cumulative_yuan_limit":args.total_yuan,**fingerprint()}
    dump(batch/"manifest.json",manifest);(batch/"manifest.sha256").write_text(digest(manifest)+"\n")
    dump(batch/"cases.json",cases)
    source=Path(__file__).resolve().parents[1]
    for name in manifest["source_files_sha256"]:
        dest=batch/"source_snapshot"/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((source/name).read_bytes())
    lookup={c["id"]:c for c in cases};completed=0
    sink=lambda row:append_json(batch/"events.jsonl",row)
    # Same lock and same retained M8 spend ledger as M8-M10 and the web parser.
    with exclusive_run(args.spend_root), (no_network() if args.mode=="scripted" else nullcontext()):
        ledger=SpendLedger(args.spend_root,bid,args.batch_yuan,args.total_yuan) if args.mode=="llm" else None
        for item in plan:
            row=run_case(lookup[item["case_id"]],item["policy"],item["trial_id"],mode=args.mode,
                         settings=settings,ledger=ledger,sink=sink)
            append_json(batch/"results.jsonl",row);completed+=1
            print(json.dumps({"completed":completed,"planned":len(plan),"trial_id":item["trial_id"],"error":row["error"]}),flush=True)
            if row["error"] or not row["measurement_complete"]:
                break
    dump(batch/"run_status.json",{"completed":completed,"planned":len(plan),"finished":completed==len(plan)})
    result=report(batch)
    print(json.dumps({"batch":str(batch),"measurement_complete":result["measurement_complete"],"hard_checks_passed":result["hard_checks_passed"]},ensure_ascii=False,indent=2))
    return result

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor")
    p=sub.add_parser("freeze");p.add_argument("--root",default="data/m11/v1")
    p=sub.add_parser("run");p.add_argument("--root",default="data/m11/v1");p.add_argument("--mode",choices=["scripted","llm"],default="scripted")
    p.add_argument("--stage",choices=["pilot","dev"],default="pilot");p.add_argument("--policies",nargs="+",choices=["RULE","NL"],default=["RULE","NL"])
    p.add_argument("--repeats",type=int,choices=[1,2,3],default=1);p.add_argument("--spend-root",default="data/m8/v1")
    p.add_argument("--batch-yuan",type=float,default=.2);p.add_argument("--total-yuan",type=float,default=5.)
    p=sub.add_parser("report");p.add_argument("--batch",required=True)
    p=sub.add_parser("acceptance");p.add_argument("--batch",required=True);p.add_argument("--output",default="data/m11/v1/M11_ACCEPTANCE_AUTO.md")
    args=ap.parse_args()
    if args.command=="doctor":print(json.dumps(doctor(),ensure_ascii=False,indent=2));return
    if args.command=="freeze":print(json.dumps(freeze(args.root),ensure_ascii=False,indent=2));return
    if args.command=="run":
        if not all(math.isfinite(v) and v>0 for v in (args.batch_yuan,args.total_yuan)) or len(set(args.policies))!=len(args.policies):raise ValueError("预算须为有限正数，策略不能重复。")
        r=run(args)
    else:
        r=report(args.batch)
        if args.command=="acceptance":
            path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("# M11 验收自动草稿\n\n只重算已有记录，不调用模型或地图；开发评测不是M12独立测试。\n\n"+markdown(r)+
                "\n网页手工确认、真实模型/完整回归日志、默认删除现场检查：待核验。\n",encoding="utf-8")
        print(json.dumps({k:v for k,v in r.items() if k!="failures"},ensure_ascii=False,indent=2))
    # Scripted mode is an infrastructure test, never a natural-language quality claim.
    if not r["measurement_complete"] or not r["hard_checks_passed"]:sys.exit(3)
    if r["mode"]=="llm" and "NL" in r["policies"] and not r["policies"]["NL"]["quality_targets_passed"]:sys.exit(2)

if __name__=="__main__":main()
