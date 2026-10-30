"""M10 的搜索提示词和无模型规则对照；沿用 M4 的预算、重试和动作协议。"""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

from app.m4_decider import LLMDecider, SYSTEM_PROMPT as ORIGINAL_PROMPT


PROTOCOL = "m10_search_v1"
SYSTEM_PROMPT = """你是双人会合搜索Agent。只调用choose_next_action一次，只选allowed_actions。
根据累计observed_history和最新反馈选择搜索动作；不得推测未查询地点或放宽硬约束。
无路线不等于某一方超时；仅有no_route、数据缺失或空搜索时优先扩大覆盖。
已确认某侧约束违规时可向该侧搜索，但方向不保证公交改善；查空后应利用历史重新判断。
已有可行结果时仍可搜索改善结果或finish；无结果且有可用搜索预算时优先探索。
action_options给出合法动作的半径；同名expand可以是不同半径。不要重复已查参数。
仅输出动作、短理由码和问题码，不输出思维过程；finish不代表全城无解或全局最优。
validation_feedback只表示上次输出不合法。"""


class PromptClient:
    """替换系统提示词后才进入现有审计客户端，不新增模型请求。"""
    def __init__(self, delegate):
        self.delegate = delegate
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        messages = deepcopy(kwargs["messages"])
        if not messages or messages[0] != {"role": "system", "content": ORIGINAL_PROMPT}:
            raise ValueError("M4提示词入口已变化，请先核对适配")
        messages[0]["content"] = SYSTEM_PROMPT
        return self.delegate.chat.completions.create(**{**kwargs, "messages": messages})

    def close(self):
        self.delegate.close()


class M10LLMDecider(LLMDecider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = PromptClient(self.client)

    def metadata(self):
        result = super().metadata()
        result.update(prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                      search_protocol=PROTOCOL)
        return result


class RuleDecider:
    """较强规则基线，只读取与模型相同的已观测摘要；不读case、坐标或真值。"""
    simulated = True
    deterministic = True

    def __init__(self):
        self.observations = []

    def __call__(self, observation):
        self.observations.append(deepcopy(observation))
        allowed = observation["allowed_actions"]
        history = observation.get("observed_history", {})
        sides = history.get("sides", observation["latest_tool_feedback"]["sides"])
        burden = {r: sum(sides[r]["constraint_failures"].values()) for r in ("a", "b")}
        search = [a for a in allowed if a not in ("finish", "ask_user")]
        if not search:
            action, reason = "finish", "enough_for_now"
        else:
            order = []
            if not any(burden.values()):
                order.append(("expand", "few_candidates"))
            for role in sorted(("a", "b"), key=lambda r: (-burden[r], r)):
                if burden[role]:
                    order.append(("toward_" + role, role + "_burden"))
            # 某侧方向查空后也允许尝试另一侧，方向与公交代价并非单调关系。
            order += [("toward_a", "explore_other_area"), ("toward_b", "explore_other_area"),
                      ("expand", "few_candidates"), ("middle", "explore_other_area")]
            action, reason = next((a, r) for a, r in order if a in search)
        return {"action": action, "reason_code": reason, "question_code": "none"}
