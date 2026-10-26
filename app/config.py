from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8-sig", extra="ignore"
    )
    amap_web_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.8-flash"
    m0_city: str = "北京市"
    m0_citycode: str = Field(default="010", pattern=r"^\d{3,4}$")
    amap_timeout_seconds: float = Field(default=20, ge=1, le=120)
    llm_timeout_seconds: float = Field(default=60, ge=1, le=180)
    amap_min_interval_seconds: float = Field(default=1.1, ge=0.1, le=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
