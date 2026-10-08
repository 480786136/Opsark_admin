<script setup>
defineProps({ parameters: { type: Object, required: true }, thinkingAllowed: { type: Boolean, default: true } });
</script>

<template>
  <label
    >max_completion_tokens（与最大输出 Token 二选一）
    <input
      v-model.number="parameters.max_completion_tokens"
      type="number"
      min="1"
      max="131072"
      placeholder="未设置"
    />
  </label>
  <label
    >enable_thinking（原生参数，可留空使用思考模式）
    <select v-model="parameters.enable_thinking">
      <option value="">未设置</option>
      <option :value="false">关闭</option>
      <option :value="true" :disabled="!thinkingAllowed">开启</option>
    </select>
  </label>
</template>
