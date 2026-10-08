"""Validated model parameters that Admin may inject into upstream requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Thinking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["enabled", "disabled"]


class ModelParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thinking: Thinking | None = None
    enable_thinking: bool | None = Field(default=None, strict=True)
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2, strict=True)
    top_p: float | None = Field(default=None, ge=0, le=1, strict=True)
    max_tokens: int | None = Field(default=None, ge=1, le=131072, strict=True)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, le=131072, strict=True)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2, strict=True)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2, strict=True)

    @model_validator(mode="after")
    def compatible(self):
        if sum(value is not None for value in (self.max_tokens, self.max_completion_tokens, self.max_output_tokens)) > 1:
            raise ValueError("生成预算字段不能同时设置")
        if self.thinking and self.thinking.type == "disabled" and self.reasoning_effort not in {None, "none"}:
            raise ValueError("关闭思考时 reasoning_effort 只能为空或 none")
        if self.enable_thinking is not None:
            if self.thinking and self.enable_thinking != (self.thinking.type == "enabled"):
                raise ValueError("thinking 与 enable_thinking 冲突")
            if not self.enable_thinking and self.reasoning_effort not in {None, "none"}:
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
        for budget in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if budget in layer:
                for other in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                    if other != budget:
                        result.pop(other, None)
        result.update(layer)
    try:
        return ModelParameters.model_validate(result).compact()
    except ValueError as exc:
        raise ValueError("INVALID_MODEL_PARAMETERS") from exc
