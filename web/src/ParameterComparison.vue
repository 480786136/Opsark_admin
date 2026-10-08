<script setup>
import { computed } from "vue";
const props = defineProps({
  requested: { type: Object, default: () => ({}) },
  effective: { type: Object, default: () => ({}) },
  outputMode: { type: String, default: "" },
  requestedLabel: { type: String, default: "请求值" },
  effectiveLabel: { type: String, default: "上游生效值" },
});
const actual = computed(() => ({
  ...props.effective,
  ...(props.outputMode ? { response_format: props.outputMode } : {}),
}));
const rows = computed(() =>
  [
    ...new Set([
      ...Object.keys(props.requested || {}),
      ...Object.keys(actual.value),
    ]),
  ].map((name) => {
    const before = props.requested?.[name],
      after = actual.value[name];
    const reason =
      before == null
        ? after == null
          ? "未设置"
          : "请求未提供，上游已设置"
        : after == null
          ? "未发送该字段（查看转换记录）"
          : JSON.stringify(before) === JSON.stringify(after)
            ? "保持原值"
            : "值已调整（查看转换记录）";
    return { name, before, after, reason };
  }),
);
const display = (value) =>
  value == null
    ? "—"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
</script>

<template>
  <div class="parameter-comparison">
    <div
      class="parameter-table-scroll"
      role="region"
      aria-label="参数生效对比"
      tabindex="0"
    >
      <table>
        <caption>
          参数生效对比
        </caption>
        <thead>
          <tr>
            <th>参数</th>
            <th>{{ requestedLabel }}</th>
            <th>{{ effectiveLabel }}</th>
            <th>变化说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.name">
            <th scope="row">{{ row.name }}</th>
            <td :data-label="requestedLabel">{{ display(row.before) }}</td>
            <td :data-label="effectiveLabel">{{ display(row.after) }}</td>
            <td data-label="变化说明">{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length">未设置额外参数。</p>
    </div>
    <details>
      <summary>查看 JSON 详情</summary>
      <pre>{{ JSON.stringify({ requested, effective: actual }, null, 2) }}</pre>
    </details>
  </div>
</template>

<style scoped>
.parameter-comparison {
  min-width: 0;
  width: 100%;
  margin: 12px 0;
}
.parameter-table-scroll {
  max-width: 100%;
  overflow: auto;
}
.parameter-table-scroll:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--accent, #187365);
  outline-offset: 2px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
caption {
  text-align: left;
  font-weight: 600;
  padding: 8px 0;
}
th,
td {
  text-align: left;
  vertical-align: top;
  padding: 10px 8px;
  border-bottom: 1px solid var(--border, #d8e1df);
  overflow-wrap: anywhere;
}
thead th {
  font-weight: 600;
}
tbody th {
  font-family: monospace;
  font-weight: 400;
}
summary {
  padding: 12px 0;
  cursor: pointer;
  font-size: 12px;
}
pre {
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  th,
  td {
    min-width: 96px;
  }
}
</style>

<style scoped>
@media (max-width: 600px) {
  table,
  tbody,
  tr,
  caption {
    display: block;
  }
  thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  tr {
    padding: 10px 0;
    border-bottom: 1px solid var(--border, #d8e1df);
  }
  th,
  td {
    min-width: 0;
    border: 0;
    padding: 5px 0;
  }
  tbody th {
    display: block;
    font-weight: 600;
  }
  td {
    display: grid;
    grid-template-columns: minmax(0, 100px) minmax(0, 1fr);
    gap: 8px;
  }
  td::before {
    content: attr(data-label);
    font-family: inherit;
    font-weight: 500;
  }
}
</style>
