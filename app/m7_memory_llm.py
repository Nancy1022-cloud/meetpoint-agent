"""独立实模Memory对照：同模型、同提示、同工具、同上下文Token上限。

对话完全自建。唯一改变的是最近N条事件，或结构化条件加相关偏好。
输入Token由服务端usage事后核验；未知或超限保留为协议未通过，不能当成功。
"""

import json
import time

import openai
from openai import OpenAI
from pydantic import ValidationError, model_validator

from app.m1_models import Requirements, StrictModel
from app.m5_models import PreferenceValues
from app.m5_store import flatten
from app.m7_memory import default_new, replay


CONTEXT_TOKEN_CAP = 2048
MEMORY_SYSTEM_PROMPT = """你负责恢复双人会合任务的最终条件与长期偏好。只调用emit_memory一次。
我/A指person_a，朋友/B指person_b。你只依据给出的context，不调用地图。
new创建新会话，当前条件从默认值、长期偏好、new中明确字段依次合并。
长期个人限制仅适用于A；明确条件优先；本次修改只改当前任务；以后默认才改长期偏好。
save显式保存偏好。say使用项目的受控句式；含糊或否定句不得部分更新，pending为true。
明确有效的say更新或new可清除pending。删除全部长期偏好不会反改已有当前条件。
若context提供结构化state，读取其中已确认条件、preferences和pending。
若context只提供events，按顺序恢复可见事件；未明示的字段使用给出的默认值。
输出完整requirements、当前preferences字典及pending。不要编造窗口之外的事件。
"""


class MemoryAnswer(StrictModel):
    requirements: Requirements
    preferences: dict[str, int | str]
    pending: bool

    @model_validator(mode="after")
    def valid_preferences(self):
        if self.preferences:
            PreferenceValues.model_validate(self.preferences)
        return self


def expectation(case):
    base = flatten(Requirements.model_validate({"person_a": default_new()["a"],
                                               "person_b": default_new()["b"]}).model_dump())
    return {"requirements": {**base, **case["expected_changes_from_defaults"]},
            "preferences": case["expected_preferences"], "pending": case["expected_pending"]}


def checked_response(response, expected):
    usage = response.usage
    usage = usage.model_dump(exclude_none=True) if usage is not None else {}
    input_tokens = usage.get("prompt_tokens")
    verified = type(input_tokens) is int and 0 <= input_tokens <= CONTEXT_TOKEN_CAP
    actual, error = None, None
    try:
        choices = response.choices
        if not choices or choices[0].finish_reason == "length":
            raise ValueError("missing_or_truncated_output")
        calls = choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != "emit_memory":
            raise ValueError("invalid_tool_call")
        answer = MemoryAnswer.model_validate_json(calls[0].function.arguments)
        actual = {"requirements": flatten(answer.requirements.model_dump()),
                  "preferences": answer.preferences, "pending": answer.pending}
    except (ValueError, ValidationError, TypeError, AttributeError):
        error = "invalid_memory_output"
    return {"actual": actual, "matches_expected": actual == expected,
            "passed": verified and actual == expected, "input_token_cap_verified": verified,
            "prompt_tokens_reported": input_tokens,
            "completion_tokens_reported": usage.get("completion_tokens"),
            "returned_model": response.model, "error_code": error}


def evaluate_model_memory(cases, settings, *, repeats=2, recent_n=3, client=None, on_row=None):
    if repeats not in (2, 3):
        raise ValueError("实模Memory每任务每种表示重复2或3次")
    rows, owns = [], client is None
    if client is None:
        if not settings.llm_api_key.get_secret_value().strip():
            raise ValueError("实模Memory需要已有LLM_API_KEY")
        client = OpenAI(api_key=settings.llm_api_key.get_secret_value(), base_url=settings.llm_base_url,
                        timeout=settings.llm_timeout_seconds, max_retries=0)
    tool = {"type": "function", "function": {"name": "emit_memory",
            "description": "输出当前条件、长期偏好和待澄清标志", "parameters": MemoryAnswer.model_json_schema()}}
    try:
        for index, case in enumerate(cases):
            expected = expectation(case)
            contexts = {"structured": {"state": replay(case["events"])},
                        "recent_events": {"events": case["events"][-recent_n:]}}
            defaults = Requirements.model_validate({"person_a": default_new()["a"],
                                                   "person_b": default_new()["b"]}).model_dump()
            for rep in range(1, repeats+1):
                policies = ("structured", "recent_events") if (index+rep) % 2 else ("recent_events", "structured")
                for policy in policies:
                    started = time.perf_counter()
                    row = {"case_id": case["id"], "policy": policy, "repeat": rep,
                           "configured_model": settings.llm_model, "expected": expected,
                           "context_token_cap": CONTEXT_TOKEN_CAP, "real_llm_attempts": 1,
                           "parameters": {"temperature": 0, "max_tokens": 1024, "enable_thinking": False,
                                          "sdk_max_retries": 0, "extra_retries_per_call": 0}}
                    try:
                        response = client.chat.completions.create(model=settings.llm_model,
                            messages=[{"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                                      {"role": "user", "content": json.dumps({"defaults": defaults,
                                          "context": contexts[policy]}, ensure_ascii=False)}],
                            tools=[tool], tool_choice={"type": "function", "function": {"name": "emit_memory"}},
                            parallel_tool_calls=False, temperature=0, max_tokens=1024,
                            extra_body={"enable_thinking": False})
                        row.update(checked_response(response, expected))
                    except openai.OpenAIError as exc:
                        # 不保存原始异常/响应，保留失败样本及已发出的一次调用。
                        row.update(passed=False, matches_expected=False, input_token_cap_verified=False,
                                   actual=None, returned_model=None, prompt_tokens_reported=None,
                                   completion_tokens_reported=None, error_code=type(exc).__name__)
                    row["elapsed_seconds"] = round(time.perf_counter()-started, 6)
                    rows.append(row)
                    if on_row:
                        on_row(row)
    finally:
        if owns:
            client.close()
    return {"record_type": "m7_memory_llm_control", "data_source": "self_authored_synthetic_dialogues",
            "real_map_http_attempts": 0, "recent_n": recent_n, "repeats": repeats,
            "context_token_cap": CONTEXT_TOKEN_CAP,
            "scope_note": "同样2048输入Token上限，用服务usage事后核验；缺usage或超限的样本保留，passed=false。"
                          "同模型、提示、工具、输出上限；只改变上下文表示。无重试，逐重复保留。",
            "summary": {p: {"passed": sum(r["passed"] for r in rows if r["policy"] == p),
                            "total": sum(r["policy"] == p for r in rows),
                            "token_cap_verified": sum(r["input_token_cap_verified"] for r in rows if r["policy"] == p),
                            "api_or_output_errors": sum(r["error_code"] is not None for r in rows if r["policy"] == p),
                            "prompt_tokens_reported": sum(r["prompt_tokens_reported"] or 0 for r in rows if r["policy"] == p),
                            "completion_tokens_reported": sum(r["completion_tokens_reported"] or 0 for r in rows if r["policy"] == p),
                            "real_llm_attempts": sum(r["real_llm_attempts"] for r in rows if r["policy"] == p),
                            "elapsed_total_seconds": sum(r["elapsed_seconds"] for r in rows if r["policy"] == p)}
                        for p in ("structured", "recent_events")}, "rows": rows}


def memory_model_report(result):
    lines = ["# M7 实模Memory独立对照", "", result["scope_note"], "",
             "| 上下文表示 | 完整匹配且协议通过 | 输入Token上限已核验 | API或格式错误 |",
             "| --- | --- | --- | ---: |"]
    for p, s in result["summary"].items():
        lines.append(f"| {p} | {s['passed']}/{s['total']} | {s['token_cap_verified']}/{s['total']} | {s['api_or_output_errors']} |")
    lines += ["", "| 任务 | 重复 | 表示 | 匹配且协议通过 | 输入Token | 输出Token | 错误 |",
              "| --- | ---: | --- | --- | ---: | ---: | --- |"]
    for r in result["rows"]:
        lines.append(f"| {r['case_id']} | {r['repeat']} | {r['policy']} | {r['passed']} | "
                     f"{r['prompt_tokens_reported']} | {r['completion_tokens_reported']} | {r['error_code']} |")
    lines += ["", "这些任务是作者可见的小规模自建对话；不外推到任意对话、模型或Token窗口。",
              "两个系统均不进行地图搜索；只检查条件、偏好及澄清状态。完整输入表示受同一输入上限约束。",
              "有输入超限、缺usage或接口失败时，单独报告覆盖；不能把剩下的成功样本冒称全量结果。", ""]
    return "\n".join(lines)