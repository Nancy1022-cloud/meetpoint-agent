from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    # 拒绝未知字段，避免把字符串或布尔值悄悄转换成数字。
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


UserId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Origin = Annotated[str, Field(min_length=1, max_length=80)]
Duration = Annotated[int, Field(ge=60, le=10800)]
Transfers = Annotated[int, Field(ge=0, le=5)]
Walk = Annotated[int, Field(ge=0, le=10000)]
Mode = Literal["balanced", "total_time"]


class PersonRequirements(StrictModel):
    origin_text: Origin
    max_duration_seconds: Duration = 2700
    max_transfers: Transfers = 1
    max_walk_meters: Walk = 1500


class Requirements(StrictModel):
    # 第一版先限定为北京市、两个人、商场。
    city: Literal["北京市"] = "北京市"
    venue_type: Literal["mall"] = "mall"
    person_a: PersonRequirements
    person_b: PersonRequirements
    preference: Mode = "balanced"


class NonEmptyPatch(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def check_explicit_fields(cls, value):
        if isinstance(value, dict):
            if not value:
                raise ValueError("更新至少要包含一个字段。")

            if any(item is None for item in value.values()):
                raise ValueError(
                    "不能用 null 清空条件；不修改的字段请省略。"
                )

        return value


class PersonPatch(NonEmptyPatch):
    origin_text: Origin | None = None
    max_duration_seconds: Duration | None = None
    max_transfers: Transfers | None = None
    max_walk_meters: Walk | None = None


class RequirementsPatch(NonEmptyPatch):
    person_a: PersonPatch | None = None
    person_b: PersonPatch | None = None
    preference: Mode | None = None


def merge_requirements(
    current: Requirements,
    patch: RequirementsPatch,
):
    data = current.model_dump()

    # 只取本次明确提交的字段，未提交的字段保留旧值。
    for key, value in patch.model_dump(exclude_unset=True).items():
        if key in ("person_a", "person_b"):
            data[key].update(value)
        else:
            data[key] = value

    # 合并完成后，重新验证整份条件。
    return Requirements.model_validate(data)


class BudgetLimits(StrictModel):
    max_map_http: Annotated[int, Field(ge=1, le=40)] = 40
    max_llm_calls: Annotated[int, Field(ge=1, le=12)] = 12
    max_search_rounds: Annotated[int, Field(ge=1, le=3)] = 3
    max_candidates_per_round: Annotated[int, Field(ge=1, le=5)] = 5


class BudgetCounters(StrictModel):
    map_http: Annotated[int, Field(ge=0)] = 0
    llm_calls: Annotated[int, Field(ge=0)] = 0
    search_rounds: Annotated[int, Field(ge=0)] = 0
    candidates_in_round: Annotated[int, Field(ge=0)] = 0
    candidates_total: Annotated[int, Field(ge=0)] = 0


class Reservation(StrictModel):
    resource: Literal["map_http", "llm", "search", "candidates"]
    count: Annotated[int, Field(ge=1, le=5)] = 1

    @model_validator(mode="after")
    def check_count(self):
        if self.resource != "candidates" and self.count != 1:
            raise ValueError(
                "请求和搜索每次只允许预扣 1 次；重试要另扣。"
            )

        return self


class CreateTask(StrictModel):
    user_id: UserId
    requirements: Requirements


class UpdateTask(StrictModel):
    user_id: UserId
    expected_version: Annotated[int, Field(ge=1)]
    changes: RequirementsPatch


class StartRun(StrictModel):
    user_id: UserId
    expected_version: Annotated[int, Field(ge=1)]


class ReserveRequest(Reservation):
    user_id: UserId


class FinishRun(StrictModel):
    user_id: UserId
    reason: Literal["demo_finished", "manual_stop"] = "manual_stop"