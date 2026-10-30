"""M4 的动作协议。模型只交回动作和短理由码，不提供坐标或路线数值。"""

from typing import Literal, TypedDict

from pydantic import model_validator

from app.m1_models import StrictModel


Action = Literal["middle", "toward_a", "toward_b", "expand", "ask_user", "finish"]
Reason = Literal[
    "a_burden", "b_burden", "few_candidates", "no_route", "data_missing",
    "enough_for_now", "need_user_input", "explore_other_area",
]
Question = Literal["none", "duration", "transfers", "walking", "direction"]

REASON_TEXT = {
    "a_burden": "尝试降低A侧通勤负担，效果由下一轮路线验证。",
    "b_burden": "尝试降低B侧通勤负担，效果由下一轮路线验证。",
    "few_candidates": "当前候选较少，尝试扩大搜索覆盖。",
    "no_route": "当前查询存在无路线结果，尝试其他搜索区域。",
    "data_missing": "当前存在必需数据缺失，不能据此认定无解。",
    "enough_for_now": "模型判断当前结果足以交付，提前结束探索。",
    "need_user_input": "后续推进需要用户明确选择。",
    "explore_other_area": "尝试尚未查询的区域，继续验证可行性。",
}
QUESTION_TEXT = {
    "duration": "本次已查候选存在耗时超限。是否调整某一方的时间上限？请明确角色和新值。",
    "transfers": "本次已查候选存在换乘超限。是否调整某一方的换乘上限？请明确角色和新值。",
    "walking": "本次已查候选存在步行超限。是否调整某一方的步行上限？请明确角色和新值。",
    "direction": "是否继续尝试其他搜索方向？如需调整条件，请明确指出修改字段。",
}


class Decision(StrictModel):
    action: Action
    reason_code: Reason
    question_code: Question = "none"

    @model_validator(mode="after")
    def check_question(self):
        if (self.action == "ask_user") != (self.question_code != "none"):
            raise ValueError("追问动作必须有问题码，其他动作必须使用none。")
        return self


class ControlState(TypedDict):
    # 地图、路线、消息、模型输入与完整轨迹均不进入图状态。
    run_id: str
    action: str
    completed_rounds: int
    stop_reason: str


class DecisionError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)
