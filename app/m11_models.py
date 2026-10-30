"""M11 proposals are data only; neither parser can write Memory directly."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.m1_models import StrictModel, UserId

PROTOCOL = "m11_memory_v1"
PathName = Literal[
    "person_a.max_duration_seconds", "person_b.max_duration_seconds",
    "person_a.max_transfers", "person_b.max_transfers",
    "person_a.max_walk_meters", "person_b.max_walk_meters",
    "person_a.origin_text", "person_b.origin_text", "preference",
]

class Operation(StrictModel):
    scope: Literal["current", "defaults"]
    path: PathName
    value: int | str
    evidence: Annotated[str, Field(min_length=1, max_length=400)]

class Proposal(StrictModel):
    intent: Literal["change", "clarify"]
    operations: list[Operation] = Field(max_length=6)
    question_code: Literal["none", "role", "field_value", "scope", "unsupported_expression"]

    @model_validator(mode="after")
    def consistent(self):
        if self.intent == "change" and (not self.operations or self.question_code != "none"):
            raise ValueError("change must contain operations and no question")
        if self.intent == "clarify" and (self.operations or self.question_code == "none"):
            raise ValueError("clarify cannot contain writes")
        return self

class PreviewRequest(StrictModel):
    user_id: UserId
    expected_version: Annotated[int, Field(ge=1)]
    expected_preferences_revision: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(min_length=1, max_length=400)]
    parser: Literal["rule", "llm"] = "rule"

class ConfirmRequest(StrictModel):
    user_id: UserId
    expected_version: Annotated[int, Field(ge=1)]
    expected_preferences_revision: Annotated[int, Field(ge=0)]

class Actor(StrictModel):
    user_id: UserId

QUESTIONS = {
    "role": "请明确修改 A（我）还是 B（朋友）。这里的‘我’固定指 A。",
    "field_value": "请给出要改的字段和绝对数值，例如 B 最多通勤60分钟。",
    "scope": "请分别说明本次条件和以后默认值；默认偏好只保存 A 本人的限制。",
    "unsupported_expression": "这句话尚未形成可靠修改。请明确人物、字段、新值和本次/默认范围。",
}

def clarification(code="unsupported_expression"):
    return Proposal(intent="clarify", operations=[], question_code=code)
