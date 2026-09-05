"""Small, flat extraction schema for diagnostic/first-pass observations.

Top-level arrays are REQUIRED. Empty {} is not a successful empty analysis.
No database identifiers, arbitrary JSON payloads or implicit type coercion.
"""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True,min_length=1,max_length=120)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True,min_length=1,max_length=600)]


class StrictOutput(BaseModel):
    model_config=ConfigDict(strict=True,extra="forbid",frozen=True,allow_inf_nan=False)


class Evidence(StrictOutput):
    block: Annotated[str,StringConstraints(pattern=r"^B[0-9]{3,6}$")]
    quote: Annotated[str,StringConstraints(min_length=1,max_length=512)]


class EntityFinding(StrictOutput):
    name: Name
    kind: Literal["character","location","organization","item","setting"]
    evidence: list[Evidence]=Field(min_length=1,max_length=8)


class FactFinding(StrictOutput):
    subject: Name
    field: Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=80)]
    value: ShortText
    mode: Literal["stable","state","uncertain"]
    evidence: list[Evidence]=Field(min_length=1,max_length=8)


class EventFinding(StrictOutput):
    summary: ShortText
    participants: list[Name]=Field(max_length=32)
    story_time: str | None=Field(max_length=160)
    evidence: list[Evidence]=Field(min_length=1,max_length=8)


class FactExtractionResult(StrictOutput):
    entities: list[EntityFinding]=Field(max_length=64)
    facts: list[FactFinding]=Field(max_length=128)
    events: list[EventFinding]=Field(max_length=64)
