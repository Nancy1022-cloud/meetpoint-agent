import re
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.m1_models import StrictModel


class MapToolError(Exception):
    def __init__(self, code, message, retryable=False, infocode=None, http_status=None):
        self.code, self.message = code, message
        self.retryable = retryable
        self.infocode, self.http_status = infocode, http_status
        super().__init__(message)

    def as_dict(self):
        return {key: value for key, value in {
            "code": self.code, "message": self.message,
            "retryable": self.retryable, "infocode": self.infocode,
            "http_status": self.http_status,
        }.items() if value is not None}


def location_text(value):
    if not isinstance(value, str):
        raise ValueError("坐标必须是经度,纬度字符串。")
    value = value.strip()
    if not re.fullmatch(r"-?\d{1,3}(?:\.\d{1,6})?,-?\d{1,2}(?:\.\d{1,6})?", value):
        raise ValueError("坐标格式错误，小数最多6位。")
    lng, lat = map(float, value.split(","))
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("坐标范围错误。")
    return value


def nonnegative_int(value):
    # 只接受非负整数及其十进制字符串；缺失、布尔、浮点均保持未知。
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


class Place(StrictModel):
    poi_id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    location: str
    citycode: Literal["010"]
    typecode: str | None = None
    address: str | None = None

    @field_validator("location")
    @classmethod
    def validate_location(cls, value):
        return location_text(value)


class TransitRoute(StrictModel):
    route_id: str
    duration_seconds: Annotated[int, Field(gt=0)] | None = None
    walk_meters: Annotated[int, Field(ge=0)] | None = None
    boardings: Annotated[int, Field(ge=0)] | None = None
    transfers: Annotated[int, Field(ge=0)] | None = None
    line_options: list[list[str]] = Field(default_factory=list)
    walking_source: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @property
    def complete(self):
        return (all(v is not None for v in (
            self.duration_seconds, self.walk_meters, self.transfers,
        )) and not self.missing_fields and not self.issues)


class TransitResult(StrictModel):
    status: Literal["ok", "no_route", "data_missing"]
    routes: list[TransitRoute] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class PlaceResult(StrictModel):
    status: Literal["ok", "no_place", "no_candidates", "data_missing"]
    places: list[Place] = Field(default_factory=list)
    rejected_incomplete: int = 0
    rejected_category: int = 0
    rejected_city: int = 0