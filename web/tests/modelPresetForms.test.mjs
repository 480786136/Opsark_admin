import assert from "node:assert/strict";
import test from "node:test";
import {
  emptyParameters,
  parametersFromApi,
  parametersToApi,
  selectPreset,
  presetParameterError,
} from "../src/modelPresetForms.js";

test("native fields round-trip, including false and zero", () => {
  const api = {
    max_completion_tokens: 1234,
    enable_thinking: false,
    temperature: 0,
  };
  assert.deepEqual(parametersToApi(parametersFromApi(api)), api);
  assert.deepEqual(parametersToApi(emptyParameters()), {});
});

test("preset rules reject unsupported values without silently clearing them", () => {
  const qwen = { id: "qwen-chat-v1", parameter_rules: {
    reasoningEfforts: [], thinkingEnabled: false, frequencyPenalty: false, temperatureExclusiveMax: 2,
  } };
  for (const values of [{ thinking: { type: "enabled" } }, { reasoning_effort: "high" },
    { temperature: 2 }, { frequency_penalty: 0 }]) {
    const form = parametersFromApi(values);
    const before = structuredClone(form);
    assert.notEqual(presetParameterError(form, qwen), "");
    assert.deepEqual(form, before);
  }
  assert.equal(presetParameterError(parametersFromApi({ enable_thinking: false, temperature: 1.9 }), qwen), "");
  assert.notEqual(presetParameterError(parametersFromApi({ max_tokens: 10, max_completion_tokens: 10 })), "");
  assert.notEqual(presetParameterError(parametersFromApi({ thinking: { type: "enabled" }, enable_thinking: false })), "");
  assert.notEqual(presetParameterError(parametersFromApi({ thinking: { type: "enabled" }, reasoning_effort: "none" })), "");
});

test("reset clears native fields and does not leak the old thinking object", () => {
  const form = parametersFromApi({
    thinking: { type: "enabled" },
    enable_thinking: true,
    max_completion_tokens: 100,
  });
  Object.assign(form, emptyParameters());
  assert.deepEqual(parametersToApi(form), {});
});

test("switching presets only changes an incompatible upstream model", () => {
  const route = {
    preset_id: "qwen",
    upstream_model: "old",
    alias: "public",
    provider_id: "p",
    parameters: { temperature: 0.5 },
  };
  const presets = [{ id: "qwen", models: ["qwen-plus", "qwen-flash"] }];
  selectPreset(route, presets);
  assert.deepEqual(route, {
    preset_id: "qwen",
    upstream_model: "qwen-plus",
    alias: "public",
    provider_id: "p",
    parameters: { temperature: 0.5 },
  });
  route.upstream_model = "qwen-flash";
  selectPreset(route, presets);
  assert.equal(route.upstream_model, "qwen-flash");
  route.preset_id = "custom";
  selectPreset(route, presets);
  assert.equal(route.upstream_model, "qwen-flash");
});
