export const emptyParameters = () => ({
  thinking_type: "",
  enable_thinking: "",
  reasoning_effort: "",
  temperature: null,
  top_p: null,
  max_tokens: null,
  max_completion_tokens: null,
  max_output_tokens: null,
  presence_penalty: null,
  frequency_penalty: null,
});

export const parametersFromApi = (value = {}) => ({
  ...emptyParameters(),
  ...value,
  thinking_type: value.thinking?.type || "",
});

export function parametersToApi(value) {
  const result = {};
  if (value.thinking_type) result.thinking = { type: value.thinking_type };
  if (value.reasoning_effort) result.reasoning_effort = value.reasoning_effort;
  if (typeof value.enable_thinking === "boolean") {
    result.enable_thinking = value.enable_thinking;
  }
  for (const name of [
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "presence_penalty",
    "frequency_penalty",
  ]) {
    if (
      value[name] !== null &&
      value[name] !== "" &&
      value[name] !== undefined
    ) {
      result[name] = value[name];
    }
  }
  return result;
}

export function selectPreset(route, presets) {
  const preset = presets.find((item) => item.id === route.preset_id);
  if (preset && !preset.models.includes(route.upstream_model)) {
    route.upstream_model = preset.models[0];
  }
  // Do not overwrite aliases, provider/key destinations or explicit overrides.
}

export function presetParameterError(parameters, preset) {
  const rules = preset?.parameter_rules;
  const values = parametersToApi(parameters);
  if (
    [values.max_tokens, values.max_completion_tokens, values.max_output_tokens].filter((value) => value !== undefined).length > 1
  )
    return "生成预算只能设置一个字段。";
  if (
    values.thinking &&
    values.enable_thinking !== undefined &&
    (values.thinking.type === "enabled") !== values.enable_thinking
  )
    return "两个思考开关的值冲突，请只设置一个或保持一致。";
  if (
    (values.thinking?.type === "enabled" || values.enable_thinking === true) &&
    values.reasoning_effort === "none"
  )
    return "开启思考与 reasoning_effort=none 冲突。";
  if (!rules) return "";
  const thinking =
    values.thinking?.type === "enabled" || values.enable_thinking === true;
  if (thinking && !rules.thinkingEnabled)
    return "当前结构化接入预设不支持开启思考，请关闭或清空。";
  const effort = values.reasoning_effort;
  if (
    effort &&
    effort !== "none" &&
    !rules.reasoningEfforts.includes(effort) &&
    !(effort === "max" && preset.id === "deepseek-chat-v1")
  )
    return "当前预设不支持所选推理强度，请重新选择。";
  if (
    effort &&
    effort !== "none" &&
    (values.thinking?.type === "disabled" || values.enable_thinking === false)
  )
    return "关闭思考与显式推理强度冲突。";
  if (values.frequency_penalty !== undefined && !rules.frequencyPenalty)
    return "当前预设不支持 Frequency penalty，请清空已有值。";
  if (
    values.temperature !== undefined &&
    rules.temperatureExclusiveMax !== undefined &&
    values.temperature >= rules.temperatureExclusiveMax
  )
    return `Temperature 必须小于 ${rules.temperatureExclusiveMax}。`;
  return "";
}
