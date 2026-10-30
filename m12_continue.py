"""Continue only unstarted M12 trials after a reviewed connection/usage gap.

Run from the project root, OUTSIDE app/. Original source, manifest, cases,
completed trials and unknown spend reservations are never rewritten.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

from app.m12 import ROOT, load_batch, verify_suite, stamp, dump, sha
from app.m8_trace import read_jsonl, append_json, exclusive_run, SpendLedger, valid_usage
from app.m10_metrics import summarize

REVIEWED_CONNECTION_ATTEMPT = "fe48ef7fc7b547509f067f463eaa7b3d"
SIDECAR = "connection_continuation"


def inspect_records(manifest, cases, rows, events, errors, reviewed):
    """Validate all existing evidence; missing usage remains missing."""
    if errors: raise ValueError("JSONL有损坏行，停止。")
    plan = manifest["trials"]
    plan_ids = [t["trial_id"] for t in plan]
    ids = [r.get("trial_id") for r in rows]
    if len(set(plan_ids)) != len(plan_ids) or len(set(ids)) != len(ids):
        raise ValueError("计划或结果有重复试次。")
    if ids != plan_ids[:len(ids)]:
        raise ValueError("已有结果不是原计划顺序前缀，不能自动续跑。")
    starts = [e.get("trial_id") for e in events if e.get("event") == "trial_start"]
    ends = [e.get("trial_id") for e in events if e.get("event") == "trial_end"]
    if Counter(starts) != Counter(ids) or Counter(ends) != Counter(ids):
        raise ValueError("存在重复/未计划/已启动缺结果的试次，不自动重跑。")
    by_id = {c["id"]: c for c in cases}
    found, gaps, attempts = set(), [], []
    for t, row in zip(plan, rows):
        if any(row.get(k) != t[k] for k in ("case_id", "policy", "mode", "repeat")) or row.get("group") != by_id[t["case_id"]]["group"]:
            raise ValueError("结果身份与原计划不一致。")
        tel = row.get("telemetry", {})
        if tel.get("complete") is not True or tel.get("real_map_http", 0) != 0:
            raise ValueError("除已核对usage缺口外还有测量问题，停止。")
        if any(a.get("status") == "started" for a in tel.get("tools", [])):
            raise ValueError("存在未结束工具尝试。")
        for a in tel.get("llm", []):
            aid = a.get("attempt_id")
            if not isinstance(aid, str) or not aid: raise ValueError("模型尝试缺少ID。")
            attempts.append(aid)
            if aid in reviewed:
                found.add(aid)
                if not (a.get("status") == "error" and a.get("error") == "APIConnectionError"
                        and a.get("usage") is None and a.get("http_status") is None and a.get("returned_model") is None):
                    raise ValueError("人工核对项与APIConnectionError/未知usage记录不一致。")
                gaps.append({"trial_id": row["trial_id"], "attempt_id": aid,
                             "error": "APIConnectionError", "usage": None, "billing": "unknown_reservation_retained"})
            elif a.get("status") != "returned" or not valid_usage(a.get("usage")):
                raise ValueError("发现未核对的模型失败或缺usage："+aid+"。停止，不自动豁免。")
    if len(attempts) != len(set(attempts)): raise ValueError("跨试次模型attempt_id重复。")
    if found != set(reviewed): raise ValueError("指定的已核对连接异常未在本批已有记录中找到。")
    completed = {**manifest, "trials": plan[:len(rows)]}
    audit = summarize(completed, cases, deepcopy(rows), deepcopy(events), [])
    if audit["valid_results"] != len(rows) or not audit["hard_checks_passed"] or any(audit.get(k) for k in (
            "integrity_errors", "m9_protocol_errors", "m10_protocol_errors")):
        raise ValueError("已有记录有硬检查或事件协议问题，不能续跑。")
    # No replacement usage and no artificial measurement_complete=true.
    llm = [a for r in rows for a in r["telemetry"].get("llm", [])]
    known = [a for a in llm if valid_usage(a.get("usage"))]
    totals = {k: sum(a["usage"][k] for a in known) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {"planned": len(plan), "recorded": len(rows), "remaining": len(plan)-len(rows),
        "execution_complete": len(rows) == len(plan), "hard_checks_passed": True,
        "known_gaps": gaps, "llm_attempts": len(llm), "attempts_with_usage": len(known),
        "usage_coverage": len(known)/len(llm) if llm else None, "observed_tokens_lower_bound": totals,
        "all_task_average_tokens": None if gaps else totals["total_tokens"]/len(rows) if rows else None,
        "strict_measurement_complete": audit["measurement_complete"] and len(rows) == len(plan),
        "note": "连接异常请求用量仍未知；成功重试不补回第一次usage，未知预留不清零。"}


def check_ledger(manifest, gaps):
    rows, errors = read_jsonl(Path(manifest["data_root"])/"spend.jsonl")
    if errors: raise ValueError("费用账本有损坏行。")
    for gap in gaps:
        aid = gap["attempt_id"]
        reserves = [r for r in rows if r.get("attempt_id") == aid and r.get("event") == "reserve"]
        settles = [r for r in rows if r.get("attempt_id") == aid and r.get("event") == "settle"]
        if len(reserves) != 1 or reserves[0].get("batch_id") != manifest["batch_id"] or settles:
            raise ValueError("已核对缺口的费用预留不完整/已改写，停止。")


def prefix_info(path):
    raw = Path(path).read_bytes()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def verify_prefix(path, info):
    with Path(path).open("rb") as f: raw = f.read(info["bytes"])
    if len(raw) != info["bytes"] or hashlib.sha256(raw).hexdigest() != info["sha256"]:
        raise ValueError("续跑前的原始记录前缀变化："+str(path))


def checkpoint(batch, manifest, reviewed, rows):
    directory = batch/SIDECAR
    path = directory/"continuation_manifest.json"
    ledger_path = Path(manifest["data_root"])/"spend.jsonl"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        expected = (directory/"continuation_manifest.sha256").read_text().strip()
        from app.m8_cases import digest
        if digest(saved) != expected or saved["reviewed_attempts"] != sorted(reviewed) or saved["runner_sha256"] != sha(__file__):
            raise ValueError("续跑清单或工具版本变化，停止。")
        for name, info in saved["original_prefixes"].items():
            verify_prefix(ledger_path if name == "spend.jsonl" else batch/name, info)
        return
    directory.mkdir(parents=True, exist_ok=False)
    files = {name: prefix_info(batch/name) for name in ("results.jsonl", "events.jsonl")}
    files["spend.jsonl"] = prefix_info(ledger_path)
    saved = {"created_at": stamp(), "original_batch_id": manifest["batch_id"],
        "original_manifest_sha256": sha(batch/"manifest.json"), "original_source_digest": manifest["source_digest"],
        "original_recorded_results": len(rows), "reviewed_attempts": sorted(reviewed),
        "runner_sha256": sha(__file__), "original_prefixes": files,
        "policy": "append_only_unstarted_original_trials; preserve_unknown_usage_and_reservation"}
    from app.m8_cases import digest
    dump(path, saved)
    (directory/"continuation_manifest.sha256").write_text(digest(saved)+"\n", encoding="utf-8")
    for name in ("run_status.json", "summary.json"):
        if (batch/name).is_file(): shutil.copy2(batch/name, directory/("before_"+name))
    shutil.copy2(__file__, directory/"m12_continue.py")


def new_trial_ok(manifest, case, trial, row, events):
    # Reuse the frozen graders, with exactly this new trial's events.
    if row.get("telemetry", {}).get("complete") is not True: return False
    if any(a.get("status") != "returned" or not valid_usage(a.get("usage")) for a in row["telemetry"].get("llm", [])): return False
    result = summarize({**manifest, "trials": [trial]}, [case], deepcopy([row]), deepcopy(events), [])
    return result["measurement_complete"] and result["hard_checks_passed"]


def write_report(batch, reviewed, interrupted=None):
    from app.m12_report import report
    strict = report(batch)  # Original grading: its false flags remain false.
    m, cases, rows, events, errors = load_batch(batch, current=True)
    try:
        result = inspect_records(m, cases, rows, events, errors, reviewed)
        result["continuation_audit_ok"] = True
    except ValueError as exc:
        result = {"planned": len(m["trials"]), "recorded": len(rows), "remaining": len(m["trials"])-len(rows),
            "execution_complete": len(rows) == len(m["trials"]), "continuation_audit_ok": False,
            "diagnostic": str(exc), "strict_measurement_complete": strict["measurement_complete"]}
    result.update(time=stamp(), interrupted=interrupted,
        original_measurement_complete=strict["measurement_complete"],
        original_hard_checks_passed=strict["hard_checks_passed"],
        original_quality_targets_passed=strict["quality_targets_passed"],
        original_M12_delivery_complete=strict["M12_delivery_complete"])
    directory = batch/SIDECAR; directory.mkdir(exist_ok=True)
    dump(directory/"continuation_status.json", result)
    note = ("# M12 连接异常后续跑记录\n\n"
        f"原计划{result['planned']}试次，已记录{result['recorded']}，剩余{result['remaining']}。\n\n"
        f"原计划执行完成：{result['execution_complete']}；续跑核验：{result['continuation_audit_ok']}。\n\n"
        f"原严格测量完整：{strict['measurement_complete']}；硬检查：{strict['hard_checks_passed']}；效果目标：{strict['quality_targets_passed']}。\n\n"
        "已核对的APIConnectionError没有返回usage。成功重试的用量不替代失败请求用量，未知费用预留继续保留。"
        "原始results/events前缀、清单、源码、用例和失败不改写，只追加原计划尚未启动的试次。\n\n"
        "本次仍有Token缺测，严格measurement_complete与M12_delivery_complete保持false；"
        "可以交付带局限的执行结果，不宣称完整Token测量或全量质量验收通过。各组已观测用量及覆盖率见原summary.json。\n\n"
        "原run_status.json保留第一次中断时状态，续跑进度以本目录continuation_status.json为准。\n")
    (directory/"CONTINUATION_REPORT.md").write_text(note, encoding="utf-8")
    return result


def execute(args):
    batch = Path(args.batch)
    reviewed = {REVIEWED_CONNECTION_ATTEMPT}
    m, cases, rows, events, errors = load_batch(batch, current=True)
    if m["stage"] != "final" or m["mode"] != "llm": raise ValueError("此工具只处理原M12 final实模批次。")
    with exclusive_run(m["data_root"]):
        state = inspect_records(m, cases, rows, events, errors, reviewed)
        check_ledger(m, state["known_gaps"])
        # Verify an existing append-only checkpoint even during inspection.
        if (batch/SIDECAR/"continuation_manifest.json").exists(): checkpoint(batch, m, reviewed, rows)
        ledger = SpendLedger(m["data_root"], m["batch_id"], m["money_limits"]["batch_yuan"], m["money_limits"]["project_yuan"])
        state["estimated_project_spend_or_reserved"] = ledger.spent()
        if not args.run and not args.package:
            print(json.dumps(state, ensure_ascii=False, indent=2)); return 0
        checkpoint(batch, m, reviewed, rows)
        if args.package:
            result = write_report(batch, reviewed)
            from app.m12_report import package
            archive = package(batch)
            print(json.dumps({"continuation": result, "package": archive}, ensure_ascii=False, indent=2)); return 0
        from app.config import get_settings
        from app.m10_eval import run_case
        s = get_settings()
        if s.llm_model != m["model"] or s.llm_base_url.rstrip("/") != m["endpoint"] or not s.llm_api_key.get_secret_value().strip():
            raise ValueError("模型、端点或Key与原批次要求不符。")
        _, meta = verify_suite(m["data_root"])
        if meta["suite_sha256"] != m["suite_sha256"]: raise ValueError("原用例集变化。")
        todo = m["trials"][len(rows):]
        lookup = {c["id"]: c for c in cases}
        reason, added = None, 0
        try:
            for trial in todo:
                if args.max_new_trials and added >= args.max_new_trials:
                    reason = "clean_trial_boundary_pause"; break
                observed = []
                def sink(event):
                    observed.append(deepcopy(event)); append_json(batch/"events.jsonl", event)
                row = run_case(lookup[trial["case_id"]], trial["policy"], m["mode"], trial["trial_id"], settings=s, ledger=ledger, sink=sink)
                row["repeat"] = trial["repeat"]
                append_json(batch/"results.jsonl", row)
                added += 1
                print(json.dumps({"done": len(rows)+added, "planned": len(m["trials"]), "trial_id": trial["trial_id"], "outcome": row["actual_outcome"]}, ensure_ascii=False), flush=True)
                if not new_trial_ok(m, lookup[trial["case_id"]], trial, row, observed):
                    reason = "new_measurement_or_hard_check_problem"; break
        except KeyboardInterrupt:
            reason = "KeyboardInterrupt"
        finally:
            result = write_report(batch, reviewed, reason)
            # Check that the original 1285-result prefix and ledger survived intact.
            checkpoint(batch, m, reviewed, rows)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["continuation_audit_ok"]: return 3
        # 4 means explicit partial measurement, never a pass override for code 3.
        return 4 if result["execution_complete"] else 0 if reason == "clean_trial_boundary_pause" else 3


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch", default="data/m12/v1/final")
    group = p.add_mutually_exclusive_group(); group.add_argument("--run", action="store_true"); group.add_argument("--package", action="store_true")
    p.add_argument("--max-new-trials", type=int, default=0)
    args = p.parse_args()
    try:
        if args.max_new_trials < 0: raise ValueError("max-new-trials不能为负")
        return execute(args)
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
