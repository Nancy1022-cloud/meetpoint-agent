"""网页请求沿用M1的严格校验；前端不能提高预算上限。"""
from datetime import date, time
from typing import Annotated
from pydantic import Field, field_validator
from app.m1_models import BudgetLimits, StrictModel, UserId


class WebPlan(StrictModel):
    user_id: UserId
    expected_version: Annotated[int, Field(ge=1)]
    departure_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    departure_time: Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]
    limits: BudgetLimits = Field(default_factory=BudgetLimits)

    @field_validator("departure_date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value

    @field_validator("departure_time")
    @classmethod
    def valid_time(cls, value):
        time.fromisoformat(value)
        return value


class Actor(StrictModel):
    user_id: UserId


class OriginChoice(Actor):
    prompt_id: Annotated[str, Field(min_length=1, max_length=64)]
    index: Annotated[int, Field(ge=1, le=5)]