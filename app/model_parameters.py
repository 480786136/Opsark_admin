"""Validated model parameters that Admin may inject into upstream requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Thinking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["enabled", "disabled"]


class ModelParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thinking: Thinking | None = None
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)

    @model_validator(mode="after")
    def compatible(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens 与 max_completion_tokens 不能同时设置")
        if self.thinking and self.thinking.type == "disabled" and self.reasoning_effort not in {None, "none"}:
            raise ValueError("关闭思考时 reasoning_effort 只能为空或 none")
        return self

    def compact(self):
        return self.model_dump(exclude_none=True)


PARAMETER_NAMES = set(ModelParameters.model_fields)


def validate_call_parameters(body):
    try:
        values = {key: body[key] for key in PARAMETER_NAMES if key in body}
        return ModelParameters.model_validate(values).compact()
    except ValueError as exc:
        raise ValueError("INVALID_MODEL_PARAMETERS") from exc


def merge_parameters(defaults, request_values, overrides):
    result = {}
    for layer in (defaults or {}, request_values or {}, overrides or {}):
        if "max_tokens" in layer:
            result.pop("max_completion_tokens", None)
        if "max_completion_tokens" in layer:
            result.pop("max_tokens", None)
        result.update(layer)
    try:
        return ModelParameters.model_validate(result).compact()
    except ValueError as exc:
        raise ValueError("INVALID_MODEL_PARAMETERS") from exc
