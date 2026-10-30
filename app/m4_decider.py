"""OpenAI兼容Tool Calling；每次HTTP尝试预扣模型预算，SDK自动重试关闭。"""

import hashlib
import json
import logging
import time
from urllib.parse import urlsplit

import openai
from openai import OpenAI

from app.m1_models import Reservation
from app.m4_models import Decision, DecisionError


SYSTEM_PROMPT = """你是双人会合规划Agent的搜索决策模块。只调用choose_next_action一次。
输入是代码生成的本轮工具结果摘要、已尝试动作、可选动作与剩余预算。
首轮相同，后续请依据实际反馈选择最有希望的未尝试动作，不要背固定搜索顺序。
若主要是A侧超时/换乘/步行失败，优先考虑toward_a；B侧同理。
候选少或搜索为空时可考虑expand；双方都失败或无路线时综合判断其他区域。
方向偏移不保证公交改善，必须通过下一轮工具重新验证。
存在有希望的合法搜索动作时，优先补查；收益不足可以finish，需用户决定才ask_user。
不得编造地点、坐标、路线，不得自行放宽硬约束；finish不代表全城无解或全城最优。
严格只选allowed_actions中动作。只输出动作、理由码和问题码，不输出思维过程。
validation_feedback仅表示上一输出不合法，请重新判断，不要复述原始错误内容。"""


def decision_tool(allowed):
    schema = Decision.model_json_schema()
    schema["properties"]["action"]["enum"] = list(allowed)
    schema["required"] = ["action", "reason_code", "question_code"]
    return {"type": "function", "function": {
        "name": "choose_next_action", "description": "根据工具反馈选择一个合法的后续动作。",
        "parameters": schema,
    }}


class LLMDecider:
    simulated = False

    def __init__(self, settings, store, user_id, run_id, *, client=None, sleeper=time.sleep):
        self.settings, self.store, self.user_id, self.run_id = settings, store, user_id, run_id
        self.attempts_started, self.retries_started = 0, 0
        self.usages, self.returned_models = [], []
        self.sleeper = sleeper
        self.owns_client = client is None
        if client is None:
            key = settings.llm_api_key.get_secret_value().strip()
            url = urlsplit(settings.llm_base_url)
            if not key:
                raise DecisionError("missing_llm_key", "请在.env中填写LLM_API_KEY。")
            if (url.scheme != "https" or not url.netloc
                    or not url.path.rstrip("/").endswith("/compatible-mode/v1")
                    or any(c in settings.llm_base_url for c in "{}")):
                raise DecisionError("invalid_llm_endpoint", "请使用M0已验证的百炼兼容端点。")
            client = OpenAI(api_key=key, base_url=settings.llm_base_url,
                            timeout=settings.llm_timeout_seconds, max_retries=0)
        self.client = client
        for name in ("httpx", "httpcore", "openai"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def close(self):
        if self.owns_client:
            self.client.close()

    def __call__(self, observation):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(observation, ensure_ascii=False)},
        ]
        for attempt in range(2):
            self.store.assert_run_current(self.user_id, self.run_id)
            self.store.reserve(self.user_id, self.run_id, Reservation(resource="llm"))
            self.attempts_started += 1
            self.retries_started += int(attempt > 0)
            try:
                response = self.client.chat.completions.create(
                    model=self.settings.llm_model, messages=messages,
                    tools=[decision_tool(observation["allowed_actions"])],
                    tool_choice={"type": "function", "function": {"name": "choose_next_action"}},
                    parallel_tool_calls=False, temperature=0, max_tokens=512,
                    extra_body={"enable_thinking": False},
                )
            except (openai.APITimeoutError, openai.APIConnectionError):
                error = DecisionError("llm_connection_error", "模型连接失败或超时。")
                retryable = True
            except openai.APIStatusError as exc:
                status = exc.status_code
                retryable = status == 429 or 500 <= status <= 599
                error = DecisionError("llm_http_error", f"模型返回HTTP {status}；核对端点、权限与配额。")
            else:
                # 先检查版本，再接纳返回数据。计费统计只取usage，不保留完整响应。
                self.store.assert_run_current(self.user_id, self.run_id)
                self.returned_models.append(response.model)
                self.usages.append(response.usage.model_dump(exclude_none=True)
                                   if response.usage is not None else None)
                if not response.choices or response.choices[0].finish_reason == "length":
                    return {}  # 交给动作校验，最多允许一次格式修复。
                calls = response.choices[0].message.tool_calls or []
                if len(calls) != 1 or calls[0].function.name != "choose_next_action":
                    return {}
                try:
                    return json.loads(calls[0].function.arguments)
                except (ValueError, TypeError):
                    return {}
            self.store.assert_run_current(self.user_id, self.run_id)
            if not retryable or attempt == 1:
                raise error from None
            # 没有第二次预算时不sleep、不发出第二次HTTP。
            run = self.store.assert_run_current(self.user_id, self.run_id)
            if run["remaining"]["llm_calls"] == 0:
                self.store.reserve(self.user_id, self.run_id, Reservation(resource="llm"))
            self.sleeper(1)
        raise AssertionError("unreachable")

    def metadata(self):
        known = [u for u in self.usages if u is not None]
        return {
            "configured_model": self.settings.llm_model,
            "returned_models": sorted(set(self.returned_models)),
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "parameters": {"temperature": 0, "max_tokens": 512, "enable_thinking": False,
                           "sdk_max_retries": 0, "extra_retries_per_call": 1},
            "real_llm_attempts": self.attempts_started,
            "llm_retries": self.retries_started,
            "prompt_tokens_reported": sum(u.get("prompt_tokens", 0) for u in known),
            "completion_tokens_reported": sum(u.get("completion_tokens", 0) for u in known),
            "usage_coverage": {"attempts": self.attempts_started, "responses_with_usage": len(known)},
        }
