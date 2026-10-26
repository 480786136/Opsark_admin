"""The same JSON fixtures run in TypeScript, Rust, and Python; no local copies."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from app.tool_configuration import validate_value

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "Opsark_core/contracts/tool-contracts.json").read_text(encoding="utf-8"))
CASES = json.loads((ROOT / "Opsark_core/contracts/schema-cases.json").read_text(encoding="utf-8"))["cases"]


def test_exported_catalog_contracts_match_admin():
    baseline = json.loads((ROOT / "Opsark_admin/app/resources/core_catalog.json").read_text(encoding="utf-8"))
    assert CONTRACTS["dialect"] == "http://json-schema.org/draft-07/schema#"
    assert CONTRACTS["tools"] == [
        {key: tool[key] for key in ("id", "inputSchema", "outputSchema") if key in tool}
        for tool in baseline["tools"]
    ]
    for tool in CONTRACTS["tools"]:
        Draft7Validator.check_schema(tool["inputSchema"])
        if "outputSchema" in tool:
            Draft7Validator.check_schema(tool["outputSchema"])
    Draft7Validator.check_schema(CONTRACTS["resultSchema"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_validation_case(case):
    if "schema" in case:
        schema = case["schema"]
    elif case["contract"] == "resultSchema":
        schema = CONTRACTS["resultSchema"]
    else:
        tool = next(tool for tool in CONTRACTS["tools"] if tool["id"] == case["toolId"])
        schema = tool[case["contract"]]
    before = copy.deepcopy(case["value"])
    assert Draft7Validator(schema).is_valid(case["value"]) is case["valid"]
    if case["valid"]:
        validate_value(schema, case["value"], "shared")
    else:
        from app.security import ApiError
        with pytest.raises(ApiError):
            validate_value(schema, case["value"], "shared")
    assert case["value"] == before
