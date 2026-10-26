"""M7冻结、执行和报告。python -m app.m7 --help"""

import argparse
import hashlib
import json
import math
import platform
import socket
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.m4_decider import SYSTEM_PROMPT
from app.m7_cases import suite
from app.m7_eval import run_one, summarize
from app.m7_memory import evaluate_memory
from app.m7_memory_llm import MEMORY_SYSTEM_PROMPT, CONTEXT_TOKEN_CAP, evaluate_model_memory, memory_model_report


ROOT = Path(__file__).resolve().parents[1]
CHINA_TZ = timezone(timedelta(hours=8))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hashes():
    paths = sorted((ROOT / "app").rglob("*.py")) + sorted(ROOT.glob("requirements*.txt"))
    return {p.relative_to(ROOT).as_posix(): digest(p.read_text(encoding="utf-8-sig")) for p in paths}


def identity():
    from app.config import get_settings
    settings = get_settings()
    return {"configured_model": settings.llm_model,
            "prompt_sha256": digest(SYSTEM_PROMPT),
            "memory_prompt_sha256": digest(MEMORY_SYSTEM_PROMPT),
            "memory_context_token_cap": CONTEXT_TOKEN_CAP,
            "memory_parameters": {"temperature": 0, "max_tokens": 1024, "enable_thinking": False,
                                  "sdk_max_retries": 0, "extra_retries_per_call": 0},
            "parameters": {"temperature": 0, "max_tokens": 512, "enable_thinking": False,
                           "sdk_max_retries": 0, "extra_retries_per_call": 1}}


def environment():
    return {"python": platform.python_version(),
            "packages": {p: version(p) for p in ("pydantic", "langgraph", "openai", "langgraph-checkpoint-sqlite")}}


def freeze(root):
    root = Path(root)
    frozen = root / "frozen"
    # 重跑不覆盖原验收集。新协议应换目录并说明原因。
    if frozen.exists():
        raise ValueError("冻结目录已存在；请verify，修改实验协议时另用v2目录。")
    data, model, env, sources = suite(), identity(), environment(), source_hashes()
    frozen.mkdir(parents=True)
    write_json(frozen / "cases.json", data)
    write_json(frozen / "manifest.json", {
        "schema_version": 1, "frozen_at": datetime.now(CHINA_TZ).isoformat(),
        "cases_sha256": digest((frozen / "cases.json").read_text(encoding="utf-8-sig")),
        "source_hashes": sources, "model": model, "environment": env,
        "protocol": {"B0_radii": [1500, 3000, 5000], "feasible_target": 3,
                     "recent_event_window": 3, "b1_llm_repeats": [2, 3]},
        "scope_note": "作者可见的预先登记合成集，非独立盲测；改规则后另立版本，不能隐藏旧结果。"})
    return {"status": "frozen", "directory": str(frozen), "planning_tasks": len(data["planning"]),
            "memory_tasks": len(data["memory"]), "model": model["configured_model"]}


def verify(root):
    root = Path(root)
    manifest = read_json(root / "frozen/manifest.json")
    text = (root / "frozen/cases.json").read_text(encoding="utf-8-sig")
    if digest(text) != manifest["cases_sha256"]:
        raise ValueError("冻结数据哈希不一致；禁止悄悄修改验收题。")
    if source_hashes() != manifest["source_hashes"]:
        raise ValueError("代码或依赖清单已变化；保留旧结果，另建v2冻结目录。")
    if identity() != manifest["model"] or environment() != manifest["environment"]:
        raise ValueError("模型、Prompt或运行环境已变化；需要另立实验版本。")
    return manifest, json.loads(text)


def fee(model, input_rate=None, output_rate=None):
    attempts = model["real_llm_attempts"]
    if attempts == 0:
        return {"reported_usage_price_estimate": 0, "complete_usage": True,
                "note": "本条未发生真实模型请求"}
    coverage = model.get("usage_coverage", {})
    complete = coverage.get("responses_with_usage") == attempts
    estimate = None if input_rate is None or output_rate is None or not coverage.get("responses_with_usage") else (
        model["prompt_tokens_reported"]*input_rate + model["completion_tokens_reported"]*output_rate)/1000000
    return {"reported_usage_price_estimate": estimate, "complete_usage": complete,
            "note": "按用户给定每百万Token单价计算；不是账单。缺usage时仅覆盖已报告部分；未知不填0。"}


def online_report(payload):
    rows = payload["observations"]
    if not rows:
        raise ValueError("在线观察记录为空")
    ids = [r["run_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("在线run_id重复")
    for r in rows:
        if r.get("data_source") != "amap_live":
            raise ValueError("在线报告只接纳标注amap_live的接入摘要")
        if r["counters"]["map_http"] != r["real_map_http_attempts"]:
            raise ValueError("在线地图计数不一致")
        if r["completed_plan"] != (r["recommendations_count"] > 0):
            raise ValueError("completed_plan与推荐数不一致")
    times = sorted(r["elapsed_seconds_including_confirmation"] for r in rows)
    n = len(times)
    med = times[n//2] if n % 2 else (times[n//2-1]+times[n//2])/2
    lines = ["# M7 在线接入观察报告", "", "来源：用户已完成的M6验收记录；本命令没有重新请求地图。", "",
        f"本次 {len(rows)} 次真实运行，{sum(r['completed_plan'] for r in rows)}/{len(rows)} 次交付至少一个可行方案。",
        f"地图HTTP合计 {sum(r['real_map_http_attempts'] for r in rows)}；模型调用合计 "
        f"{sum(r['model']['real_llm_attempts'] for r in rows)}。",
        f"总耗时均值 {sum(times)/n:.3f}s，中位数 {med:.3f}s，包含人工点选等待。", "",
        "| 编号 | 已评估 | 可行 | 推荐 | HTTP | 模型 | 含点选耗时s | 停止原因 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for i, r in enumerate(rows, 1):
        lines.append(f"| L{i:02} | {r['evaluated_count']} | {r['feasible_evaluated_count']} | "
            f"{r['recommendations_count']} | {r['real_map_http_attempts']} | "
            f"{r['model']['real_llm_attempts']} | {r['elapsed_seconds_including_confirmation']} | {r['stop_reason']} |")
    lines += ["", "五组条件沿用当次长期偏好或默认，未保存完整生效约束快照。没有同任务B0结果，不能做在线策略因果比较。",
              "L05按未交付保留；不能将其解释为全城无解。4/5是这五次接入观察，不是准确率或召回率。",
              "历史模型为qwen3.8-27b；没有实际单价和账单，货币费用未知。离线数字单独报告。", ""]
    return "\n".join(lines)


def planning_report(summary, rows, memory, manifest, mode):
    lines = ["# M7 离线受控评测报告", "",
        "数据：自建有限候选全集；真实地图请求为0。作者已见这些题，不称独立盲测集。",
        f"决策来源：{'真实LLM（仅地图工具为合成）' if mode == 'llm' else '脚本测试替身；不是实模Agent效果'}。",
        f"冻结时间：{manifest['frozen_at']}；配置模型：{manifest['model']['configured_model']}。", "",
        "| 策略 | 有解任务运行中找到方案 | 推荐违规项 | 预算越界 | 模拟HTTP总数 | 真实模型调用 |",
        "| --- | --- | ---: | ---: | ---: | ---: |"]
    for p, s in summary["by_policy"].items():
        lines.append(f"| {p} | {s['successful_eligible_runs']}/{s['eligible_runs_with_known_solution']} | "
            f"{s['recommendation_violations']} | {s['budget_failures']} | {s['simulated_map_http_total']} | "
            f"{s['real_llm_attempts_total']} |")
    lines += ["", "B0每任务一次；实模B1每任务2或3次。总调用数的运行次数不同，不直接据总数比较成本。",
        "逐任务、逐重复的调用差值和质量差值在metrics.json的paired_deltas中，完整运行记录在planning.jsonl中。",
        "成功率分母只包括自建全集中已知有解的任务运行；无解、未知及无路线任务仍保留在明细。", "",
        "| 任务 | 重复 | B0交付 | B1交付 | 模拟HTTP差B1-B0 | 最长耗时差/秒 | 时间差之差/秒 | 总耗时差/秒 |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |"]
    for pair in summary["paired_deltas"]:
        delta = pair["quality_delta_B1_minus_B0"]
        values = [delta[k] if delta else "不比较" for k in
                  ("max_duration_seconds", "gap_seconds", "total_duration_seconds")]
        lines.append(f"| {pair['case_id']} | {pair['repeat']} | {pair['B0_completed']} | {pair['B1_completed']} | "
            f"{pair['map_http_delta_B1_minus_B0']} | {values[0]} | {values[1]} | {values[2]} |")
    coverage = summary["paired_quality_coverage"]
    lines += ["", f"双方都交付的质量配对覆盖：{coverage['both_completed']}/{coverage['all_pairs']}。负值表示B1指标更小。",
        "不会将基线失败的任务塞入质量均值，也不会丢掉它们：所有成功/失败配对均在上表。", "",
        "## Memory 状态保留对照", "", "两个系统复用同一受控解析器和存储规则；窗口基线每次从默认任务重放最近3条用户事件。", ""]
    for p, s in memory["summary"].items():
        lines.append(f"- {p}：{s['passed']}/{s['total']} 个任务的完整条件、偏好和待澄清状态全部匹配。")
    lines += ["", memory["scope_note"], "", "## 成本及适用范围", "",
        "planning.jsonl逐条保留模型返回名、Token、usage覆盖及费用估算；未提供单价时费用为null。",
        "合成工具的执行时间不代表真实地图端到端延迟。LLM模式耗时包含模型网络请求，地图部分仍为合成。",
        "模拟HTTP只是冻结计费规则下的请求次数，不包含真实QPS、超时、重试分布或地图价格。",
        "全集最优值仅针对当前自建有限候选与确定性排序，不能外推为全城最优。",
        "实模每次重复单独保留；温度0也不能保证结果一致。小样本结果不外推。", ""]
    failures = [r for r in rows if not r["completed_plan"] or r["error_code"]]
    lines += ["## 未交付与错误明细", ""]
    for r in failures:
        lines.append(f"- {r['case_id']} {r['policy']} 第{r['repeat']}次：{r['outcome']}；"
                     f"{r['stop_reason']}；error_code={r['error_code']}。")
    return "\n".join(lines) + "\n"


def run(root, mode, repeats, input_rate=None, output_rate=None):
    root = Path(root)
    manifest, data = verify(root)
    if mode == "llm" and repeats not in (2, 3):
        raise ValueError("实模B1每任务运行2或3次；不挑最好的一次。")
    if mode == "scripted" and repeats != 1:
        raise ValueError("脚本决策器是确定性的，只运行1次。")
    if (input_rate is None) != (output_rate is None):
        raise ValueError("输入、输出单价一起给出或都省略。")
    if any(v is not None and (not math.isfinite(v) or v < 0) for v in (input_rate, output_rate)):
        raise ValueError("单价必须是有限非负数。")
    factory = None
    if mode == "llm":
        from app.config import get_settings
        from app.m4_decider import LLMDecider
        settings = get_settings()
        if not settings.llm_api_key.get_secret_value().strip():
            raise ValueError("LLM模式需要已有的LLM_API_KEY；不会请求地图。")
        factory = lambda store, rid: LLMDecider(settings, store, "demo_user", rid)
    stamp = datetime.now(CHINA_TZ).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    output = root / "results" / (mode + "_" + stamp)
    output.mkdir(parents=True)
    protocol = {"status": "running", "mode": mode, "B0_runs_per_task": 1,
                "B1_runs_per_task": repeats, "manifest": manifest,
                "user_supplied_rates_per_million": {"input": input_rate, "output": output_rate},
                "network_guard": "socket_connect_blocked" if mode == "scripted" else "synthetic_map_only_real_llm"}
    write_json(output / "run_status.json", protocol)
    rows = []
    try:
        with (output / "planning.jsonl").open("w", encoding="utf-8") as stream:
            with ExitStack() as guards:
                if mode == "scripted":
                    for method in ("connect", "connect_ex"):
                        guards.enter_context(patch.object(socket.socket, method,
                            side_effect=RuntimeError("offline_network_forbidden")))
                for case in data["planning"]:
                    for p, rep in [("B0", 1)] + [("B1", i+1) for i in range(repeats)]:
                        print(f"{case['id']} {p} repeat={rep}", flush=True)
                        row = run_one(case, p, repeat=rep, decider_factory=factory if p == "B1" else None)
                        row["fee"] = fee(row["model"], input_rate, output_rate)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stream.flush()
                        rows.append(row)
                        if row["error_code"] in ("llm_http_error", "llm_connection_error"):
                            raise ValueError("模型接入失败；已保留本批次记录并标记interrupted，排查后另开批次。")
        memory = evaluate_memory(data["memory"], manifest["protocol"]["recent_event_window"])
        summary = summarize(rows)
        write_json(output / "metrics.json", summary)
        write_json(output / "memory.json", memory)
        (output / "offline_report.md").write_text(
            planning_report(summary, rows, memory, manifest, mode), encoding="utf-8")
        online_path = ROOT / "app/m7_online_observations.json"
        if online_path.is_file():
            online = read_json(online_path)
            write_json(output / "online_observations.json", online)
            (output / "online_report.md").write_text(online_report(online), encoding="utf-8")
        integrity = (all(not r["recommendation_violations"] and r["budget_ok"] and r["counter_consistency"]
                         and (r["policy"] == "B0" or r["checkpoint_boundary_passed"] is True) for r in rows)
                     and memory["summary"]["structured"]["passed"] == len(data["memory"]))
        # 验收题、依赖或模型在运行中变更，也不能发布成完整验收。
        verify(root)
        protocol.update(status="completed", integrity_checks_passed=integrity,
                        completed_rows=len(rows))
        write_json(output / "run_status.json", protocol)
    except BaseException as exc:
        protocol.update(status="interrupted", completed_rows=len(rows), error_type=type(exc).__name__)
        write_json(output / "run_status.json", protocol)
        raise
    return {"status": "completed", "directory": str(output),
            "integrity_checks_passed": integrity, "summary": summary["by_policy"],
            "memory": memory["summary"], "real_map_http_attempts": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "verify", "run", "memory-llm"])
    parser.add_argument("--root", default="data/m7/v1")
    parser.add_argument("--mode", choices=["scripted", "llm"], default="scripted")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--input-per-million", type=float)
    parser.add_argument("--output-per-million", type=float)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze(args.root)
        elif args.command == "verify":
            manifest, data = verify(args.root)
            result = {"status": "verified", "planning_tasks": len(data["planning"]),
                      "cases_sha256": manifest["cases_sha256"]}
        elif args.command == "memory-llm":
            if (args.input_per_million is None) != (args.output_per_million is None):
                raise ValueError("输入、输出单价一起给出或都省略。")
            if any(v is not None and (not math.isfinite(v) or v < 0)
                   for v in (args.input_per_million, args.output_per_million)):
                raise ValueError("单价必须是有限非负数。")
            manifest, data = verify(args.root)
            from app.config import get_settings
            stamp = datetime.now(CHINA_TZ).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
            output = Path(args.root) / "results" / ("memory_llm_" + stamp)
            output.mkdir(parents=True)
            write_json(output / "run_status.json", {"status": "running", "manifest": manifest})
            try:
                with (output / "memory_llm.jsonl").open("w", encoding="utf-8") as stream:
                    def save_row(row):
                        row["fee"] = fee({"real_llm_attempts": 1,
                            "prompt_tokens_reported": row["prompt_tokens_reported"] or 0,
                            "completion_tokens_reported": row["completion_tokens_reported"] or 0,
                            "usage_coverage": {"responses_with_usage": int(row["prompt_tokens_reported"] is not None
                                and row["completion_tokens_reported"] is not None)}},
                            args.input_per_million, args.output_per_million)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stream.flush()
                        print(f"{row['case_id']} {row['policy']} repeat={row['repeat']} passed={row['passed']}", flush=True)
                        if row["error_code"] and row["error_code"] != "invalid_memory_output":
                            raise ValueError("实模Memory接口失败；已保留本条并标记interrupted，排查后另开批次。")
                    result = evaluate_model_memory(data["memory"], get_settings(),
                        repeats=args.repeats if args.repeats is not None else 2,
                        recent_n=manifest["protocol"]["recent_event_window"], on_row=save_row)
                verify(args.root)
                write_json(output / "memory_llm.json", result)
                (output / "memory_llm_report.md").write_text(memory_model_report(result), encoding="utf-8")
                write_json(output / "run_status.json", {"status": "completed", "manifest": manifest,
                    "protocol_coverage_verified": all(r["input_token_cap_verified"] for r in result["rows"])})
                result = {"status": "completed", "directory": str(output), "summary": result["summary"],
                          "real_map_http_attempts": 0}
            except BaseException as exc:
                write_json(output / "run_status.json", {"status": "interrupted", "error_type": type(exc).__name__,
                                                       "manifest": manifest})
                raise
        else:
            result = run(args.root, args.mode, args.repeats if args.repeats is not None else
                         (3 if args.mode == "llm" else 1), args.input_per_million, args.output_per_million)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("integrity_checks_passed") is False:
            raise SystemExit(1)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()