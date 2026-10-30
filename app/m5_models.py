"""M5 只接纳可验证的显式字段；长期偏好仅代表用户本人A。"""

from typing import Annotated, Literal

from pydantic import Field

from app.m1_models import (
    Duration, Mode, NonEmptyPatch, Origin, RequirementsPatch,
    StrictModel, Transfers, UserId, Walk,
)

Version = Annotated[int, Field(ge=1)]
Revision = Annotated[int, Field(ge=0)]
PreferenceKey = Literal[
    "max_duration_seconds", "max_transfers", "max_walk_meters", "preference",
]


class DraftPerson(NonEmptyPatch):
    origin_text: Origin
    max_duration_seconds: Duration | None = None
    max_transfers: Transfers | None = None
    max_walk_meters: Walk | None = None


class NewMemoryTask(StrictModel):
    user_id: UserId
    person_a: DraftPerson
    person_b: DraftPerson
    preference: Mode | None = None


class PreferenceValues(NonEmptyPatch):
    max_duration_seconds: Duration | None = None
    max_transfers: Transfers | None = None
    max_walk_meters: Walk | None = None
    preference: Mode | None = None


class SetPreferences(StrictModel):
    user_id: UserId
    expected_revision: Revision
    values: PreferenceValues


class DeletePreferences(StrictModel):
    user_id: UserId
    expected_revision: Revision
    # 空列表表示删除全部。删除保留递增revision，防止旧请求重新生效。
    keys: list[PreferenceKey] = Field(default_factory=list, max_length=4)


class MemoryPatch(StrictModel):
    user_id: UserId
    expected_version: Version
    changes: RequirementsPatch


class MemoryMessage(StrictModel):
    user_id: UserId
    expected_version: Version
    text: Annotated[str, Field(min_length=1, max_length=400)]
    expected_preferences_revision: Revision | None = None


class RecoverRequest(StrictModel):
    user_id: UserId
    expected_version: Version