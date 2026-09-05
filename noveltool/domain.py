"""Validated, immutable project data. No HTTP, SQLite, or background work here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4E56544C  # "NVTL"; reject databases belonging to another program.


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True,
        str_strip_whitespace=True, allow_inf_nan=False,
    )


class ProjectConfig(StrictModel):
    api_base_url: str = Field(default="http://127.0.0.1:8000/v1", max_length=2048)
    api_key_env: str = Field(
        default="NOVELTOOL_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128,
    )
    # Empty model names mean "not configured"; M1 never calls a model.
    writer_model: str = Field(default="", max_length=256)
    analysis_model: str = Field(default="", max_length=256)
    context_window: int = Field(default=20000, ge=2048, le=1_000_000)
    context_safety_ratio: float = Field(default=0.85, gt=0, le=1)
    min_chars: int = Field(default=600, ge=1, le=1_000_000)
    max_chars: int = Field(default=1000, ge=1, le=1_000_000)
    candidate_count: int = Field(default=4, ge=1, le=32)
    writer_temperature: float = Field(default=0.7, ge=0, le=10)
    analysis_temperature: float = Field(default=0.1, ge=0, le=10)
    api_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    autosave_seconds: int = Field(default=60, ge=10, le=3600)
    retain_llm_logs: bool = True

    @field_validator("api_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("API 地址或端口无效") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 地址必须是包含主机名的 http/https 地址")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("API 地址不能包含用户名或密码；请使用密钥环境变量")
        if parsed.query or parsed.fragment or any(c.isspace() for c in value):
            raise ValueError("API 地址不能包含查询参数、片段或空白")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("API 端口必须在 1～65535 之间")
        return value.rstrip("/")

    @model_validator(mode="after")
    def valid_length_range(self) -> ProjectConfig:
        if self.max_chars < self.min_chars:
            raise ValueError("max_chars（B）必须不小于 min_chars（A）")
        return self


class ProjectMeta(StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    # This is a configuration edit counter, NOT a future manuscript revision number.
    data_version: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    saved_at: str

    @field_validator("created_at", "updated_at", "saved_at")
    @classmethod
    def timezone_required(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("时间戳必须包含时区")
        return value


@dataclass(frozen=True, slots=True)
class ProjectData:
    meta: ProjectMeta
    config: ProjectConfig


class ConfigUpdate(StrictModel):
    expected_memory_version: int = Field(ge=0)
    config: ProjectConfig


class TitleUpdate(StrictModel):
    expected_memory_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
