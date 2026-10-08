"""Editable parameter contracts constrained by the shipped executor's capabilities."""

import math

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .security import ApiError

EDITABLE = {
    "description",
    "default",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "enum",
    "required",
}


def invalid(path, message):
    raise ApiError(422, "INVALID_TOOL_PARAMETERS", f"{path or '参数协议'}：{message}")


def validate_value(schema, value, path):
    try:
        Draft7Validator(schema).validate(value)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path)
        invalid(f"{path}.{location}" if location else path, "默认值不符合参数协议")


def validate_schema(schema, baseline, path="参数协议", depth=0):
    if depth > 12 or not isinstance(schema, dict):
        invalid(path, "必须为受支持的参数对象")
    for key in (set(schema) | set(baseline)) - EDITABLE - {"properties", "items"}:
        if key not in schema or key not in baseline or schema[key] != baseline[key]:
            invalid(path, f"不能修改执行协议字段 {key}")
    if depth == 0:
        try:
            Draft7Validator.check_schema(schema)
        except SchemaError:
            invalid(path, "参数 schema 不符合 Draft-07 协议")
    kind = baseline["type"]
    if "description" in schema and (not isinstance(schema["description"], str) or len(schema["description"]) > 2000):
        invalid(path, "参数说明最多 2000 字符")
    for lower, upper, types in (
        ("minimum", "maximum", {"integer", "number"}),
        ("minItems", "maxItems", {"array"}),
        ("minLength", "maxLength", {"string"}),
    ):
        for key in (lower, upper):
            if key in schema:
                val = schema[key]
                if kind not in types or type(val) not in (int, float) or not math.isfinite(val):
                    invalid(path, f"{key} 类型不正确")
                if key not in ("minimum", "maximum") and (type(val) is not int or not 0 <= val <= 1000000):
                    invalid(path, f"{key} 必须为非负整数")
        if schema.get(lower, -math.inf) < baseline.get(lower, -math.inf) or schema.get(upper, math.inf) > baseline.get(
            upper, math.inf
        ):
            invalid(path, "不能放宽 Core 内置执行范围")
        if schema.get(lower, -math.inf) > schema.get(upper, math.inf):
            invalid(path, "最小值不能超过最大值")
    if "enum" in baseline and "enum" not in schema:
        invalid(path, "不能移除内置枚举范围")
    if "enum" in schema:
        values = schema["enum"]
        if (
            kind not in {"string", "number", "integer", "boolean"}
            or not isinstance(values, list)
            or not 1 <= len(values) <= 200
        ):
            invalid(path, "枚举必须是非空的标量列表")
        for value in values:
            validate_value({k: v for k, v in schema.items() if k not in ("enum", "default")}, value, path)
            if "enum" in baseline and value not in baseline["enum"]:
                invalid(path, "不能增加 Core 不支持的枚举值")
    if kind == "object":
        props, old = schema.get("properties"), baseline.get("properties", {})
        if not isinstance(props, dict) or set(props) != set(old):
            invalid(path, "参数名称由 Core 实现提供，不能增删或重命名")
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(k, str) for k in required)
            or len(set(required)) != len(required)
            or not set(required).issubset(props)
        ):
            invalid(path, "必填项必须是已存在且不重复的参数名称")
        if not set(baseline.get("required", [])).issubset(required):
            invalid(path, "不能移除 Core 必需参数")
        for key, child in props.items():
            validate_schema(child, old[key], f"{path}.{key}", depth + 1)
    elif "properties" in schema or "required" in schema:
        invalid(path, "非对象不能声明字段或必填项")
    if kind == "array":
        validate_schema(schema.get("items"), baseline["items"], path + "[]", depth + 1)
    elif "items" in schema:
        invalid(path, "非数组不能声明 items")
    if "default" in schema:
        validate_value(schema, schema["default"], path)
